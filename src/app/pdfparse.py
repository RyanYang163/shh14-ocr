"""PDF 文件结构解析与**文字层**提取（纯 Python 标准库）。

为什么自己写解析器：Deb 包内不得含任何预编译二进制（指引 16.4 一票否决），
生命周期脚本也不许联网装依赖，所以 PDF 的核心能力必须用标准库实现。
这里**不渲染**页面、不解码图像，只做「把内嵌的文字层取出来」这一件事。

------------------------------------------------------------------ 文件结构

一个 PDF 文件从字节层面看是这样的::

    %PDF-1.4                     ← 头
    1 0 obj                      ← 间接对象：编号 世代 obj … endobj
      << /Type /Catalog /Pages 2 0 R >>
    endobj
    4 0 obj
      << /Length 55 /Filter /FlateDecode >>
      stream
      …二进制（这里是 zlib 压缩过的内容流）…
      endstream
    endobj
    xref                         ← 交叉引用表：对象号 → 文件偏移
    trailer
      << /Size 6 /Root 1 0 R >>
    startxref
    1234                         ← xref 表的偏移

**本实现不依赖 xref。** 原因有三条：

1. PDF 1.5 起 xref 可以是**流对象**（``/Type /XRef``，自己还得先解压才能读，
   而解压参数又写在它自己的字典里 —— 要读它得先知道怎么读它）；
   而损坏的、增量更新过的、被工具截断过的文件里，xref 常常是错的那一份；
2. 交叉引用表把「对象在哪」当成权威，一旦它对不上，按它读就会读出垃圾；
3. 而 ``N 0 obj`` 这个模式在文件里是**自描述的**，直接线性扫描一遍全文件的
   对象头，用「同一个对象号取**最后一个**定义」建表（增量更新正是把新版本追加在文件尾部），
   既简单又容错。

代价是要扫一遍文件，但那是纯内存查找，实测比解析 xref + 处理各种边界更快也更稳。

具体做法：先用 ``N 0 obj`` 的正则扫一遍全文件，得到「对象号 → 所有定义偏移」的表；
真正用到某个对象时**才**解析它（并缓存），同一号从**后往前**试，第一个能解析成功的就是当前版本。
惰性 + 容错，两者缺一不可：只做容错而不惰性，一个几百 MB 的 PDF 会白白解析上万个用不到的对象。

------------------------------------------------------------------ 对象与流

对象类型（本文件全部支持）::

    字典   << /Key value >>          键是 /Name，值是任意对象
    数组   [ 1 2 /Three (four) ]
    名字   /Name                     #xx 转义
    字符串 (literal) 与 <hex>         转义见 _read_literal_string
    数字   12  -3.5  .5  +7
    布尔   true / false     空对象 null
    引用   12 0 R                    指向另一个间接对象
    流     << 字典 >> stream … endstream

流按 ``/Filter`` 解码。``/Filter`` 可以是名字或数组，``/DecodeParms`` 一一对应。
已实现：FlateDecode（含 PNG / TIFF 预测器）、LZWDecode、ASCIIHexDecode、
ASCII85Decode、RunLengthDecode。**DCTDecode / JPXDecode / CCITTFaxDecode 是图像编码**，
遇到就抛 :class:`ImageOnly` —— 那说明这一页是扫描页，没有文字层，这是**结论**而不是错误。

------------------------------------------------------------------ 文字与编码

内容流里显示文字的是一小组操作符：``BT``/``ET`` 文本块，``Tf`` 选字体，
``Td``/``TD``/``Tm``/``T*`` 定位，``TJ``/``Tj``/``'``/``"`` 显示字符串。

字符编码分两条路走：

* **简单字体**（Type1 / TrueType 非复合）：单字节码。有 ``/ToUnicode`` 就查表；
  没有就按 ``/Encoding``（WinAnsiEncoding ≈ cp1252 / MacRomanEncoding ≈ mac_roman /
  StandardEncoding）解，``/Differences`` 里的字形名查内置的 AGL 子集表。
* **复合字体**（``/Type0``，中日韩 PDF 基本都用它）：字符串是**多字节**的，码位是 CID，
  CID **不是** Unicode —— 直接按字节输出就是乱码。这类字体必须走 ``/ToUnicode`` CMap：
  解析 ``beginbfchar…endbfchar`` 与 ``beginbfrange…endbfrange``，把码位映射回 Unicode。
  码位的字节宽度由 CMap 的 ``begincodespacerange`` 决定（Identity-H 是 2 字节，
  也有 1 字节的 codespace），拿不到 codespace 时按 2 字节切。

**找不到 ToUnicode 时按字节直出，并把该字体记进 ``undecoded_fonts``** ——
诚实报告优于输出一段看着像字其实是乱的文本。
"""

import re
import zlib
from collections import namedtuple

#: 单个内容流解码后的字节上限（防内存被打爆；正常内容流远小于此）
MAX_CONTENT_BYTES = 64 * 1024 * 1024
#: 单个 CMap 的 bfrange 展开上限（防止畸形文件用一个超大区间把 CPU 占满）
MAX_CMAP_RANGE = 65536
#: 页树递归深度上限（防止环形 /Kids）
MAX_PAGE_DEPTH = 64
#: 兜底找 Catalog 时最多试解析多少个对象
MAX_CATALOG_SCAN = 5000
#: 对象流（ObjStm）里最多解出多少个对象（防止畸形文件把内存占满）
MAX_PACKED_OBJECTS = 200000
#: 表单（Form XObject）递归深度上限与单页最多展开个数（防止自引用撑爆栈）
MAX_FORM_DEPTH = 4
MAX_FORM_COUNT = 64


class PdfError(Exception):
    """PDF 解析失败（结构损坏、数据非法等）。"""


class UnsupportedPdf(PdfError):
    """文件不是 PDF，或用了本实现不支持的特性。"""


class PdfEncrypted(PdfError):
    """文件有 /Encrypt —— 标准库没有 RC4/AES 解密实现，明确报告而不是崩。"""


class ImageOnly(PdfError):
    """流是图像编码（DCTDecode 等），即「扫描页，没有文字层」。"""


# ---------------------------------------------------------------- 对象模型


class Name(str):
    """``/Foo``。用独立类型是为了把名字与字符串区分开（两者都长成 str）。"""

    __slots__ = ()

    def __repr__(self):
        return "/" + str.__str__(self)


class Ref(namedtuple("Ref", "num gen")):
    """``12 0 R`` —— 指向另一个间接对象。"""

    __slots__ = ()


class Stream:
    """间接流对象：一个字典 + 一段（可能被过滤的）字节。"""

    __slots__ = ("dict", "raw", "_doc")

    def __init__(self, dictionary, raw, doc=None):
        self.dict = dictionary
        self.raw = raw
        self._doc = doc

    def get(self, key, default=None):
        return self.dict.get(key, default)

    def __repr__(self):
        return "<Stream %s %d bytes>" % (self.dict.get("Filter"), len(self.raw))

    # ---- 解码 ----

    def decoded(self, limit=MAX_CONTENT_BYTES):
        """按 ``/Filter`` 链解码。放大超过 ``limit`` 时截断（并说明）。"""
        data = self.raw
        filters = self.dict.get("Filter")
        if filters is None:
            return data[:limit]
        if not isinstance(filters, list):
            filters = [filters]
        parms = self.dict.get("DecodeParms") or self.dict.get("DP")
        if parms is None:
            parms = [None] * len(filters)
        if not isinstance(parms, list):
            parms = [parms]

        for index, name in enumerate(filters):
            param = parms[index] if index < len(parms) else None
            data = _apply_filter(str(name), data, param)
            if len(data) > limit:
                # 截断而不是拒绝：前 limit 字节里的文字通常已经够用，
                # 但要让调用方知道截断过（见 extract.py 的 warnings）
                return data[:limit]
        return data

    def is_image(self):
        """这个流是不是图像编码（即没有文字层可言）。"""
        filters = self.dict.get("Filter")
        if filters is None:
            return False
        if not isinstance(filters, list):
            filters = [filters]
        return any(str(item) in IMAGE_FILTERS for item in filters)


#: 图像编码 —— 遇到即说明是扫描页
IMAGE_FILTERS = frozenset(
    ("DCTDecode", "DCT", "JPXDecode", "CCITTFaxDecode", "CCF", "JBIG2Decode")
)


# ---------------------------------------------------------------- 词法


_WS = b"\x00\t\n\x0c\r "
_DELIM = b"()<>[]{}/%"
_DELIM_WS = frozenset(_WS + _DELIM)
_NUM_RE = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")


class _ParseError(PdfError):
    """词法/语法层面的局部失败（调用方常可跳过继续）。"""


def _read_literal_string(data, pos):
    """读 ``(…)`` 字面字符串，返回 ``(bytes, 新位置)``。

    转义按规范处理：``\\n \\r \\t \\b \\f \\( \\) \\\\``、八进制 ``\\ddd``（1~3 位）、
    以及「反斜杠 + 换行」= 续行（不产生任何字符）。括号可嵌套，需要计深度。
    """
    out = bytearray()
    depth = 1
    end = len(data)
    while pos < end:
        ch = data[pos]
        if ch == 0x5C:  # \
            pos += 1
            if pos >= end:
                break
            nxt = data[pos]
            if nxt == 0x6E:
                out.append(0x0A)
            elif nxt == 0x72:
                out.append(0x0D)
            elif nxt == 0x74:
                out.append(0x09)
            elif nxt == 0x62:
                out.append(0x08)
            elif nxt == 0x66:
                out.append(0x0C)
            elif nxt in b"()\\":
                out.append(nxt)
            elif 0x30 <= nxt <= 0x37:  # 八进制，最多 3 位
                digits = bytearray()
                while pos < end and len(digits) < 3 and 0x30 <= data[pos] <= 0x37:
                    digits.append(data[pos])
                    pos += 1
                out.append(int(digits, 8) & 0xFF)
                continue
            elif nxt == 0x0D:  # 续行：\<CR> 或 \<CRLF>
                if pos + 1 < end and data[pos + 1] == 0x0A:
                    pos += 1
            elif nxt == 0x0A:
                pass
            else:
                out.append(nxt)
            pos += 1
            continue
        if ch == 0x28:  # (
            depth += 1
        elif ch == 0x29:  # )
            depth -= 1
            if depth == 0:
                return bytes(out), pos + 1
        out.append(ch)
        pos += 1
    return bytes(out), pos


def _read_hex_string(data, pos):
    """读 ``<hex>`` 字符串。奇数位补 0；非法字符（含空白）一律忽略。"""
    end = data.find(b">", pos)
    if end < 0:
        end = len(data)
    raw = re.sub(rb"[^0-9A-Fa-f]", b"", data[pos:end])
    if len(raw) % 2:
        raw += b"0"
    try:
        value = bytes.fromhex(raw.decode("ascii"))
    except ValueError:
        value = b""
    return value, min(end + 1, len(data))


def _decode_name(raw):
    """名字里的 ``#xx`` 是转义字节。"""
    if b"#" not in raw:
        return raw.decode("latin-1")
    out = bytearray()
    index = 0
    while index < len(raw):
        if raw[index] == 0x23 and index + 2 < len(raw):
            try:
                out.append(int(raw[index + 1:index + 3], 16))
                index += 3
                continue
            except ValueError:
                pass
        out.append(raw[index])
        index += 1
    return bytes(out).decode("latin-1")


class _Lexer:
    """PDF 字节流的词法分析器。

    :meth:`next_token` 一次吐出**已经成型**的一个 token（数组与字典也整块解好），
    这样对象解析器与内容流解析器可以共用同一套代码 —— 内容流的操作数就是同一批 token。
    """

    __slots__ = ("data", "pos", "resolver")

    def __init__(self, data, pos=0, resolver=None):
        self.data = data
        self.pos = pos
        #: 解析 /Length 这类「本来就是引用」的值时用（Document 会传进来）
        self.resolver = resolver

    # ---- 位置 ----

    def save(self):
        return self.pos

    def restore(self, pos):
        self.pos = pos

    def skip_space(self):
        data = self.data
        end = len(data)
        while self.pos < end:
            ch = data[self.pos]
            if ch in _WS:
                self.pos += 1
            elif ch == 0x25:  # % 注释到行尾
                stop = data.find(b"\n", self.pos)
                self.pos = end if stop < 0 else stop + 1
            else:
                return

    # ---- token ----

    def next_token(self):
        """返回 ``(kind, value)``。kind ∈ num/name/str/arr/dict/bool/null/ref/kw/eof。"""
        self.skip_space()
        data = self.data
        end = len(data)
        if self.pos >= end:
            return ("eof", None)
        ch = data[self.pos]

        if ch == 0x2F:  # /
            start = self.pos + 1
            pos = start
            while pos < end and data[pos] not in _DELIM_WS:
                pos += 1
            self.pos = pos
            return ("name", _decode_name(data[start:pos]))

        if ch == 0x28:  # (
            value, self.pos = _read_literal_string(data, self.pos + 1)
            return ("str", value)

        if ch == 0x3C:  # <
            if data.startswith(b"<<", self.pos):
                self.pos += 2
                return ("dict", self._read_dict())
            value, self.pos = _read_hex_string(data, self.pos + 1)
            return ("str", value)

        if ch == 0x5B:  # [
            self.pos += 1
            return ("arr", self._read_array())

        if ch == 0x3E:  # >>
            if data.startswith(b">>", self.pos):
                self.pos += 2
                return ("delim", ">>")
            self.pos += 1
            return ("kw", ">")

        if ch in b"]{}":  # 顶层遇到 ] 之类：交给调用方判断（内容流里不该出现）
            self.pos += 1
            return ("delim", chr(ch))

        if ch in b"+-.0123456789":
            found = _NUM_RE.match(data, self.pos)
            if found:
                text = found.group(0)
                self.pos = found.end()
                number = float(text) if b"." in text else int(text)
                # 引用 `N G R` 的前瞻：一个数字后面再跟「整数 + R」才是引用。
                # 注意内容流里的 `1 0 RG`（设置描边色）也符合「数字 数字 大写字母」，
                # 但第三个字母是 `RG` 而不是 `R`，所以不会误判。
                #
                # 这里**刻意手写扫描而不递归调用 next_token**：递归前瞻会让连续数字串
                # （`0 0 0 RG` 之类）的开销按 2^n 爆炸——每个数字都触发两个新的前瞻。
                if isinstance(number, int):
                    mark = self.save()
                    self.skip_space()
                    second = _NUM_RE.match(data, self.pos)
                    if second is not None and b"." not in second.group(0):
                        after = second.end()
                        self.pos = after
                        self.skip_space()
                        nxt = data[self.pos:self.pos + 1]
                        if nxt == b"R" and data[self.pos + 1:self.pos + 2] in (
                            b"", b" ", b"\t", b"\r", b"\n", b"/", b"[", b"]", b"<", b">"
                        ):
                            self.pos += 1
                            return ("ref", Ref(number, int(second.group(0))))
                    self.restore(mark)
                return ("num", number)

        # 裸词：操作符（BT / Tj / TJ / re …）或 true/false/null
        start = self.pos
        pos = start
        while pos < end and data[pos] not in _DELIM_WS:
            pos += 1
        if pos == start:
            # 没有进展：吃掉一个字节，绝不能让调用方陷入死循环
            self.pos = start + 1
            return ("kw", chr(ch))
        word = data[start:pos]
        self.pos = pos
        if word == b"true":
            return ("bool", True)
        if word == b"false":
            return ("bool", False)
        if word == b"null":
            return ("null", None)
        return ("kw", word.decode("latin-1"))

    # ---- 复合 token ----

    def _read_array(self):
        items = []
        while True:
            kind, value = self.next_token()
            if kind == "eof":
                break
            if kind == "delim" and value == "]":
                break
            # 数组里的关键字不是合法值（内容流里的 TJ 数组只有数字与字符串），
            # 遇到就跳过，避免 bad PDF 把整个数组吃掉
            if kind == "kw":
                continue
            items.append(_token_value(kind, value))
        return items

    def _read_dict(self):
        result = {}
        while True:
            kind, key = self.next_token()
            if kind == "eof":
                break
            if kind == "delim" and key == ">>":
                break
            if kind != "name":
                if kind == "kw":
                    continue
                break
            kind2, value = self.next_token()
            if kind2 == "eof":
                break
            if kind2 in ("kw", "delim"):
                continue
            result[str(key)] = _token_value(kind2, value)
        return result

    def skip_inline_image(self):
        """跳过 ``BI … ID <二进制> EI`` 的内联图像。

        二进制数据会被词法器当成乱码 token，整段文本就毁了，所以必须整块跳掉。
        ``EI`` 前面必须是空白，否则字符串里出现的 "EI" 会提前截断。
        """
        data = self.data
        index = self.pos
        while True:
            index = data.find(b"EI", index)
            if index < 0:
                self.pos = len(data)
                return
            before = data[index - 1:index]
            after = data[index + 2:index + 3]
            if before and before in _WS and (not after or after in _WS):
                self.pos = index + 2
                return
            index += 2

    def parse_indirect_object(self):
        """解析 ``N G obj <对象> [stream … endstream] endobj``。"""
        kind, num = self.next_token()
        if kind != "num" or not isinstance(num, int):
            raise _ParseError("对象头缺少编号")
        kind, gen = self.next_token()
        if kind != "num" or not isinstance(gen, int):
            raise _ParseError("对象头缺少世代号")
        kind, word = self.next_token()
        if kind != "kw" or word != "obj":
            raise _ParseError("对象头缺少 obj 关键字")

        kind, value = self.next_token()
        if kind == "dict":
            mark = self.save()
            kind2, word2 = self.next_token()
            if kind2 == "kw" and word2 == "stream":
                return Stream(value, self._read_stream_data(value))
            self.restore(mark)
            return value
        if kind in ("kw", "eof", "delim"):
            raise _ParseError("对象体不是合法对象")
        return _token_value(kind, value)

    def _read_stream_data(self, dictionary):
        """读 ``stream … endstream`` 之间的原始字节。

        长度优先信 ``/Length``（它可能是个引用，所以经 resolver 解析），但读完要**验一下**
        后面确实是 ``endstream`` —— 元数据撒谎的 PDF 太多了，验证不过就退化为搜索 ``endstream``。
        """
        data = self.data
        start = self.pos
        # `stream` 关键字后面跟 CRLF 或 LF（规范），现实的实现里也可能只跟一个 CR
        if data.startswith(b"\r\n", start):
            start += 2
        elif data[start:start + 1] in (b"\n", b"\r"):
            start += 1

        length = dictionary.get("Length")
        if isinstance(length, Ref) and self.resolver is not None:
            length = self.resolver(length)
        if isinstance(length, (int, float)) and length >= 0:
            length = int(length)
            if start + length <= len(data):
                tail = data[start + length:start + length + 24].lstrip(b"\r\n \t")
                if tail.startswith(b"endstream"):
                    self.pos = start + length
                    self._consume_endstream()
                    return data[start:start + length]

        stop = data.find(b"endstream", start)
        if stop < 0:
            raw = data[start:]
            self.pos = len(data)
            return raw
        raw = data[start:stop]
        # 紧邻 endstream 的那个 EOL 是分隔符，不属于数据
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith(b"\n") or raw.endswith(b"\r"):
            raw = raw[:-1]
        self.pos = stop
        self._consume_endstream()
        return raw

    def _consume_endstream(self):
        data = self.data
        if data.startswith(b"endstream", self.pos):
            self.pos += len(b"endstream")


def _token_value(kind, value):
    """把 token 值还原成对象（数组/字典的成员用）。"""
    if kind in ("num", "name", "str", "arr", "dict", "bool", "null", "ref"):
        return value
    return None


def _as_int(value, default=0):
    """把 ``/N`` ``/First`` 这类值取成整数（可能是引用、浮点或缺失）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 过滤器


def _apply_filter(name, data, parms):
    if name in ("FlateDecode", "Fl"):
        out = _inflate(data)
        return _apply_predictor(out, parms)
    if name in ("LZWDecode", "LZW"):
        early = 1
        if isinstance(parms, dict):
            early = int(parms.get("EarlyChange", 1) or 0)
        out = _lzw_decode(data, early)
        return _apply_predictor(out, parms)
    if name in ("ASCIIHexDecode", "AHx"):
        return _asciihex_decode(data)
    if name in ("ASCII85Decode", "A85"):
        return _ascii85_decode(data)
    if name in ("RunLengthDecode", "RL"):
        return _runlength_decode(data)
    if name in IMAGE_FILTERS:
        raise ImageOnly(
            "该流是图像编码（%s）：这是扫描页，没有可用于提取的文字层" % name
        )
    if name in ("Crypt",):
        raise UnsupportedPdf("流使用了 /Crypt 过滤器（加密内容）")
    raise UnsupportedPdf("不支持的流过滤器：%s" % name)


def _inflate(data):
    """zlib 解压，容忍「前面有垃圾」与「裸 deflate」两种情况。"""
    try:
        return zlib.decompress(data)
    except zlib.error:
        pass
    # 有些生成器会在 zlib 流前面多塞几个空行/字节，从每个可能的 zlib 头（0x78）试起
    for offset in range(1, min(len(data), 64)):
        if data[offset] == 0x78:
            try:
                return zlib.decompress(data[offset:])
            except zlib.error:
                continue
    # 裸 deflate（无 zlib 头）：个别 PDF 会这么写
    try:
        engine = zlib.decompressobj(-15)
        return engine.decompress(data)
    except zlib.error:
        pass
    # 最后一步：**边解边收，能解出多少算多少**。
    # 被截断 / 尾部损坏的内容流（实测真实文件里就有：zlib 头正常，
    # 收尾报 "invalid distance too far back"）在报错前其实已经吐出了大部分正文 ——
    # 丢掉整页文字不如留下已经解出来的那部分。页面上的文字本来就在流的前面。
    engine = zlib.decompressobj()
    out = bytearray()
    # 步长小 = 坏点之前能抢回更多正文（实测 803 字节的坏流：整块喂进去颗粒无收，
    # 按 64 字节喂能抢回 4.2 KB 正文）。大流用大步长，免得为抢救把 CPU 烧光。
    step = 64 if len(data) <= 256 * 1024 else 8192
    for start in range(0, len(data), step):
        try:
            out.extend(engine.decompress(data[start:start + step]))
        except zlib.error:
            break
    if out:
        return bytes(out)
    raise PdfError("FlateDecode 解压失败——流数据已损坏或不是 zlib 数据")


def _apply_predictor(data, parms):
    """PNG / TIFF 预测器还原（xref 流与少数内容流会用到）。"""
    if not isinstance(parms, dict):
        return data
    try:
        predictor = int(parms.get("Predictor") or 1)
    except (TypeError, ValueError):
        return data
    if predictor <= 1:
        return data
    try:
        colors = int(parms.get("Colors") or 1)
        bits = int(parms.get("BitsPerComponent") or 8)
        columns = int(parms.get("Columns") or 1)
    except (TypeError, ValueError):
        return data

    row_len = (colors * bits * columns + 7) // 8
    if row_len <= 0:
        return data
    sample_bytes = max(1, colors * bits // 8)

    if predictor == 2:
        # TIFF 预测器 2：同一行内，每个分量加上它**左边**同分量的值。
        # 只对 8 位分量实现（其它位宽极罕见，原样返回比猜错好）。
        if bits != 8:
            return data
        out = bytearray(data)
        for row_start in range(0, len(out) - row_len + 1, row_len):
            for index in range(row_start + sample_bytes, row_start + row_len):
                out[index] = (out[index] + out[index - sample_bytes]) & 0xFF
        return bytes(out)

    if 10 <= predictor <= 15:
        return _png_unpredict(data, row_len, sample_bytes)
    return data


def _png_unpredict(data, row_len, sample_bytes):
    """PNG 逐行过滤器还原（五种过滤器类型，见 PNG 规范的 Filter type 表）。"""
    out = bytearray()
    previous = bytearray(row_len)
    pos = 0
    total = len(data)
    while pos < total:
        filter_type = data[pos]
        pos += 1
        row = bytearray(data[pos:pos + row_len])
        pos += row_len
        if len(row) < row_len:  # 末行被截断：补 0 后按已到数据还原
            row.extend(bytes(row_len - len(row)))
        if filter_type == 1:  # Sub
            for index in range(sample_bytes, row_len):
                row[index] = (row[index] + row[index - sample_bytes]) & 0xFF
        elif filter_type == 2:  # Up
            for index in range(row_len):
                row[index] = (row[index] + previous[index]) & 0xFF
        elif filter_type == 3:  # Average
            for index in range(row_len):
                left = row[index - sample_bytes] if index >= sample_bytes else 0
                row[index] = (row[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for index in range(row_len):
                left = row[index - sample_bytes] if index >= sample_bytes else 0
                up = previous[index]
                up_left = previous[index - sample_bytes] if index >= sample_bytes else 0
                row[index] = (row[index] + _paeth(left, up, up_left)) & 0xFF
        out.extend(row)
        previous = row
    return bytes(out)


def _paeth(left, up, up_left):
    estimate = left + up - up_left
    dl = abs(estimate - left)
    du = abs(estimate - up)
    dul = abs(estimate - up_left)
    if dl <= du and dl <= dul:
        return left
    if du <= dul:
        return up
    return up_left


def _lzw_decode(data, early_change=1):
    """PDF 变体的 LZW（TIFF 的 LZW，9~12 位码长，256=Clear、257=EOD）。

    ``EarlyChange`` 只影响「码长何时从 n 位升到 n+1 位」这一处，默认 1。
    """
    out = bytearray()

    def fresh_table():
        return [bytes([value]) for value in range(256)] + [b"", b""]

    table = fresh_table()
    code_len = 9
    bit_pos = 0
    total_bits = len(data) * 8
    previous = None

    while bit_pos + code_len <= total_bits:
        byte_index = bit_pos >> 3
        shift = bit_pos & 7
        window = data[byte_index:byte_index + 3]
        if len(window) < 3:
            window = window + bytes(3 - len(window))
        chunk = int.from_bytes(window, "big")
        code = (chunk >> (24 - code_len - shift)) & ((1 << code_len) - 1)
        bit_pos += code_len

        if code == 256:
            table = fresh_table()
            code_len = 9
            previous = None
            continue
        if code == 257:
            break
        if previous is None:
            entry = table[code] if code < len(table) else b""
        elif code < len(table):
            entry = table[code]
        elif code == len(table):
            entry = previous + previous[:1]
        else:
            entry = b""

        out.extend(entry)
        if previous is not None and len(table) < 4096:
            table.append(previous + entry[:1])
            if len(table) + early_change >= (1 << code_len) and code_len < 12:
                code_len += 1
        previous = entry
    return bytes(out)


def _asciihex_decode(data):
    stop = data.find(b">")
    if stop >= 0:
        data = data[:stop]
    raw = re.sub(rb"[^0-9A-Fa-f]", b"", data)
    if len(raw) % 2:
        raw += b"0"
    try:
        return bytes.fromhex(raw.decode("ascii"))
    except ValueError:
        raise PdfError("ASCIIHexDecode 数据非法")


def _ascii85_decode(data):
    data = data.strip()
    if data.startswith(b"<~"):
        data = data[2:]
    stop = data.find(b"~>")
    if stop >= 0:
        data = data[:stop]

    out = bytearray()
    group = []
    for byte in data:
        if byte in _WS:
            continue
        if byte == 0x7A and not group:  # 'z' = 四个 0 字节
            out.extend(b"\x00\x00\x00\x00")
            continue
        if byte < 0x21 or byte > 0x75:
            continue
        group.append(byte - 0x21)
        if len(group) == 5:
            value = 0
            for digit in group:
                value = value * 85 + digit
            if value > 0xFFFFFFFF:
                raise PdfError("ASCII85Decode 数据超出 32 位")
            out.extend(value.to_bytes(4, "big"))
            group = []
    if group:
        padding = 5 - len(group)
        for _ in range(padding):
            group.append(84)  # 'u'
        value = 0
        for digit in group:
            value = value * 85 + digit
        out.extend(value.to_bytes(4, "big")[:4 - padding])
    return bytes(out)


def _runlength_decode(data):
    out = bytearray()
    index = 0
    end = len(data)
    while index < end:
        length = data[index]
        index += 1
        if length == 128:
            break
        if length < 128:
            out.extend(data[index:index + length + 1])
            index += length + 1
        else:
            if index < end:
                out.extend(bytes([data[index]]) * (257 - length))
                index += 1
    return bytes(out)


# ---------------------------------------------------------------- 字符映射


def parse_tounicode(data):
    """解析 CMap 流，返回 ``(mapping, code_lengths)``。

    * ``mapping``：码位（int）→ Unicode 字符串
    * ``code_lengths``：``begincodespacerange`` 声明的码位字节宽度（去重升序）

    ``beginbfrange`` 的目标有两种写法，都要支持::

        <4E00> <4E10> <0020>            # 起始、结束、目标起始（逐码递增）
        <4E00> <4E02> [<0041> <0042>]   # 目标是数组（逐码对应）
    """
    text = data.decode("latin-1", "replace")
    mapping = {}
    lengths = []

    for block in re.findall(r"begincodespacerange(.*?)endcodespacerange", text, re.S):
        for low, _high in re.findall(r"<([0-9A-Fa-f\s]*)>\s*<([0-9A-Fa-f\s]*)>", block):
            width = len(re.sub(r"\s", "", low)) // 2
            if width:
                lengths.append(width)

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for source, target in re.findall(
            r"<([0-9A-Fa-f\s]+)>\s*<([0-9A-Fa-f\s]*)>", block
        ):
            code = _hex_int(source)
            if code is None:
                continue
            mapping[code] = _utf16_string(target)

    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for found in re.finditer(
            r"<([0-9A-Fa-f\s]+)>\s*<([0-9A-Fa-f\s]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f\s]*>)",
            block,
        ):
            low = _hex_int(found.group(1))
            high = _hex_int(found.group(2))
            if low is None or high is None or high < low:
                continue
            target = found.group(3)
            limit = min(high, low + MAX_CMAP_RANGE - 1)
            if target.startswith("["):
                items = re.findall(r"<([0-9A-Fa-f\s]*)>", target)
                for code in range(low, limit + 1):
                    index = code - low
                    if index < len(items):
                        mapping[code] = _utf16_string(items[index])
            else:
                digits = re.sub(r"\s", "", target[1:-1])
                base = int(digits, 16) if digits else 0
                width = max(2, len(digits))
                for code in range(low, limit + 1):
                    value = base + (code - low)
                    mapping[code] = _utf16_string("%0*X" % (width, value))

    return mapping, sorted(set(lengths))


def _hex_int(text):
    digits = re.sub(r"\s", "", text or "")
    if not digits:
        return None
    try:
        return int(digits, 16)
    except ValueError:
        return None


def _utf16_string(hex_text):
    """把 CMap 目标里的十六进制串解成 Unicode。

    目标是 UTF-16BE。奇数长度或解不出来时退化为 latin-1 —— 有的生成器会写单字节目标。
    """
    digits = re.sub(r"\s", "", hex_text or "")
    if not digits:
        return ""
    if len(digits) % 2:
        digits += "0"
    try:
        raw = bytes.fromhex(digits)
    except ValueError:
        return ""
    if len(raw) % 2 == 0:
        try:
            return raw.decode("utf-16-be").replace("\x00", "")
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


#: ``/Differences`` 里字形名 → 字符的内置表（Adobe Glyph List 的常用子集）。
#: 只列真正会碰到的：ASCII 可打印字符名、常见排版符号、拉丁扩展的常用名字。
_GLYPH_NAMES = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#",
    "dollar": "$", "percent": "%", "ampersand": "&", "quotesingle": "'",
    "parenleft": "(", "parenright": ")", "asterisk": "*", "plus": "+",
    "comma": ",", "hyphen": "-", "period": ".", "slash": "/",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "colon": ":", "semicolon": ";", "less": "<", "equal": "=",
    "greater": ">", "question": "?", "at": "@",
    "bracketleft": "[", "backslash": "\\", "bracketright": "]",
    "asciicircum": "^", "underscore": "_", "grave": "`",
    "braceleft": "{", "bar": "|", "braceright": "}", "asciitilde": "~",
    "quoteright": "’", "quoteleft": "‘", "quotedblleft": "“",
    "quotedblright": "”", "quotesinglbase": "‚", "quotedblbase": "„",
    "endash": "–", "emdash": "—", "bullet": "•",
    "ellipsis": "…", "dagger": "†", "daggerdbl": "‡",
    "perthousand": "‰", "guillemotleft": "«", "guillemotright": "»",
    "guilsinglleft": "‹", "guilsinglright": "›", "florin": "ƒ",
    "fraction": "⁄", "fi": "ﬁ", "fl": "ﬂ", "ff": "ﬀ",
    "ffi": "ﬃ", "ffl": "ﬄ", "AE": "Æ", "ae": "æ",
    "OE": "Œ", "oe": "œ", "Oslash": "Ø", "oslash": "ø",
    "Lslash": "Ł", "lslash": "ł", "germandbls": "ß",
    "dotlessi": "ı", "circumflex": "ˆ", "tilde": "˜",
    "degree": "°", "sterling": "£", "yen": "¥", "Euro": "€",
    "cent": "¢", "section": "§", "paragraph": "¶",
    "copyright": "©", "registered": "®", "trademark": "™",
    "nobreakspace": " ", "minus": "−", "mu": "µ",
    "logicalnot": "¬", "plusminus": "±", "twosuperior": "²",
    "threesuperior": "³", "onesuperior": "¹", "onehalf": "½",
    "onequarter": "¼", "threequarters": "¾", "acute": "´",
    "cedilla": "¸", "macron": "¯", "breve": "˘",
    "dotaccent": "˙", "ring": "˚", "ogonek": "˛",
    "Agrave": "À", "Aacute": "Á", "Acircumflex": "Â",
    "Atilde": "Ã", "Adieresis": "Ä", "Aring": "Å",
    "Ccedilla": "Ç", "Egrave": "È", "Eacute": "É",
    "Ecircumflex": "Ê", "Edieresis": "Ë", "Igrave": "Ì",
    "Iacute": "Í", "Icircumflex": "Î", "Idieresis": "Ï",
    "Ntilde": "Ñ", "Ograve": "Ò", "Oacute": "Ó",
    "Ocircumflex": "Ô", "Otilde": "Õ", "Odieresis": "Ö",
    "Ugrave": "Ù", "Uacute": "Ú", "Ucircumflex": "Û",
    "Udieresis": "Ü", "Yacute": "Ý", "agrave": "à",
    "aacute": "á", "acircumflex": "â", "atilde": "ã",
    "adieresis": "ä", "aring": "å", "ccedilla": "ç",
    "egrave": "è", "eacute": "é", "ecircumflex": "ê",
    "edieresis": "ë", "igrave": "ì", "iacute": "í",
    "icircumflex": "î", "idieresis": "ï", "ntilde": "ñ",
    "ograve": "ò", "oacute": "ó", "ocircumflex": "ô",
    "otilde": "õ", "odieresis": "ö", "ugrave": "ù",
    "uacute": "ú", "ucircumflex": "û", "udieresis": "ü",
    "yacute": "ý", "ydieresis": "ÿ", "Thorn": "Þ",
    "thorn": "þ", "Eth": "Ð", "eth": "ð",
}

#: 单字母字形名就是它自己（A→A、a→a）
for _letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz":
    _GLYPH_NAMES.setdefault(_letter, _letter)

#: 各基础编码对应的 Python 编解码器。
#: WinAnsiEncoding 与 cp1252 是**逐字节等价**的；StandardEncoding 与 PDFDocEncoding
#: 在 ASCII 区间之外有差异，但用 latin-1 兜住绝大多数情况已足够（差异字符会落到
#: 不可解码的地方，此时按 U+FFFD 之外的原样字节输出，至少不丢位置）。
_BASE_ENCODING_CODEC = {
    "WinAnsiEncoding": "cp1252",
    "MacRomanEncoding": "mac_roman",
    "PDFDocEncoding": "cp1252",
    "StandardEncoding": "latin-1",
    "MacExpertEncoding": "mac_roman",
}


# ---------------------------------------------------------------- 字体


class FontInfo:
    """一个字体资源：怎么把字符串里的码位变成 Unicode。"""

    __slots__ = ("name", "composite", "mapping", "lengths", "table", "bytewise",
                 "reliable", "unmapped", "reason")

    def __init__(self, name):
        self.name = name
        self.composite = False
        self.mapping = None       # dict[int, str]（来自 /ToUnicode）
        self.lengths = []         # 码位字节宽度
        self.table = None         # 简单字体的 256 项单字节解码表
        self.bytewise = False     # 无任何映射可用：按字节直出
        self.reliable = True      # False = 结果可能不准，要如实告诉用户
        self.unmapped = 0         # 查不到映射的码位个数
        self.reason = ""

    @classmethod
    def build(cls, doc, font, name):
        info = cls(name)
        if not isinstance(font, dict):
            info.bytewise = True
            info.reliable = False
            info.reason = "字体对象缺失"
            return info

        subtype = str(font.get("Subtype") or "")
        info.composite = subtype == "Type0"

        to_unicode = doc.resolve(font.get("ToUnicode"))
        if isinstance(to_unicode, Stream):
            try:
                mapping, lengths = parse_tounicode(to_unicode.decoded(4 * 1024 * 1024))
            except (PdfError, OSError):
                mapping, lengths = {}, []
            # 空的 /ToUnicode（个别生成器写一个空流占位）当没有处理 ——
            # 拿它当准会让整页变空白，还不如退回按编码解。
            # 但**只有一条映射也是真的**：有的字体整页只用来渲染一个符号，
            # 按「至少两条」筛会把它们误判成「没有映射」，进而把好好的字解成乱码。
            if mapping:
                info.mapping = mapping
                # **码长由字体类型决定，不能听 CMap 的 codespace。**
                # 简单字体（Type1 / TrueType / Type3）的字符串按规范就是**单字节**，
                # 但现实中大量 WPS / Word 生成的 Type3 字体在 ToUnicode 里写了
                # `<0000> <FFFF>` 这种「两字节」codespace，而实际码位只有 0x03、0x10 这样
                # 的单字节值。照 codespace 按 2 字节切，整个字符串会错位 ——
                # 表现就是「中文里混着 ç、有些字整段消失」。判别依据只能是字体类型。
                info.lengths = [1] if not info.composite else (lengths or [2])
                return info

        if info.composite:
            # 复合字体没有 ToUnicode：码位是 CID 不是 Unicode，无法可靠还原。
            info.bytewise = True
            info.reliable = False
            info.reason = "复合字体（Type0）缺少 /ToUnicode 映射"
            return info

        info.table, unknown, missing_encoding = _build_simple_table(doc, font)
        base_font = str(font.get("BaseFont") or "") or "（未声明 BaseFont）"
        if missing_encoding:
            # 既没有 /Encoding 也没有 /ToUnicode：码位到字符的对应关系**没有任何依据**，
            # 只能按 StandardEncoding 猜。这种必须如实标注。
            info.reliable = False
            info.reason = "%s 既没有 /Encoding 也没有 /ToUnicode，编码只能猜" % base_font
        elif unknown:
            info.reliable = False
            info.reason = (
                "%s 的 /Differences 里有 %d 个字形名不在内置对照表里，"
                "这些字符按字节输出（可能是错的）" % (base_font, unknown)
            )
        return info

    # ---- 解码 ----

    def decode(self, raw):
        """``bytes`` → ``(text, unmapped_count)``。"""
        if not raw:
            return "", 0
        if self.bytewise:
            return raw.decode("latin-1"), 0
        if self.mapping is not None:
            parts = []
            missing = 0
            for code in self._split_codes(raw):
                text = self.mapping.get(code)
                if text is None:
                    # 映射表里没有这个码位：**丢弃而不是猜**。
                    # 常见于 .notdef / 空白码位，输出一串 U+FFFD 只会让正文更难读。
                    missing += 1
                    continue
                parts.append(text)
            self.unmapped += missing
            return "".join(parts), missing
        if self.table is not None:
            parts = []
            missing = 0
            for byte in raw:
                char = self.table[byte]
                if char is None:
                    char = chr(byte)
                    missing += 1
                parts.append(char)
            self.unmapped += missing
            return "".join(parts), missing
        self.reliable = False
        self.bytewise = True
        self.reason = self.reason or "字体没有任何可用的编码信息"
        return raw.decode("latin-1"), 0

    def _split_codes(self, raw):
        lengths = self.lengths or [1]
        if len(set(lengths)) == 1:
            width = lengths[0]
            return [
                int.from_bytes(raw[index:index + width], "big")
                for index in range(0, len(raw) - width + 1, width)
            ]
        # 码长不唯一：按「长的先试」贪心切（codespacerange 允许多种宽度）
        ordered = sorted(set(lengths), reverse=True)
        codes = []
        index = 0
        total = len(raw)
        while index < total:
            for width in ordered:
                if index + width <= total:
                    codes.append(int.from_bytes(raw[index:index + width], "big"))
                    index += width
                    break
            else:
                break
        return codes


def _build_simple_table(doc, font):
    """简单字体的单字节解码表。

    返回 ``(256 项解码表, 不认识的字形名个数, 是否完全没有编码信息)`` ——
    后两项用来判断「这份文本可不可信」，交给调用方如实标注。
    """
    encoding = doc.resolve(font.get("Encoding"))
    base = None
    differences = None
    if isinstance(encoding, str):
        base = str(encoding)
    elif isinstance(encoding, dict):
        base = str(encoding.get("BaseEncoding") or "") or None
        differences = doc.resolve(encoding.get("Differences"))

    codec = _BASE_ENCODING_CODEC.get(base or "StandardEncoding", "latin-1")
    table = []
    for value in range(256):
        try:
            table.append(bytes([value]).decode(codec))
        except (UnicodeDecodeError, LookupError):
            table.append(None)

    unknown = 0
    if isinstance(differences, list):
        code = 0
        for item in differences:
            if isinstance(item, (int, float)):
                code = int(item)
                continue
            if isinstance(item, str):  # Name 也是 str 的子类
                char = _GLYPH_NAMES.get(str(item))
                if 0 <= code < 256:
                    table[code] = char
                if char is None:
                    # 名字不认识（例如某某厂商的自造字形名）—— 统计出来，别默默输出错字
                    unknown += 1
                code += 1
    missing_encoding = base is None and differences is None
    return table, unknown, missing_encoding


# ---------------------------------------------------------------- 文档


_OBJ_RE = re.compile(rb"(?<![0-9])(\d{1,10})[ \t\r\n]+(\d{1,5})[ \t\r\n]+obj\b")
_TRAILER_KEYS = ("Root", "Size", "Info", "Encrypt", "Prev", "ID", "XRefStm")

#: 按 ``/Type`` 定位流对象。**允许 ``/Type/ObjStm`` 这种没空格的写法** ——
#: 有的生成器就是不写空格，只认带空格的那一种会漏掉整份文档。
_TYPE_STREAM_RE = {
    "ObjStm": re.compile(rb"/Type\s*/ObjStm\b"),
    "XRef": re.compile(rb"/Type\s*/XRef\b"),
}


class Document:
    """一个已加载的 PDF。**只有 :meth:`load` 会读盘，其余全在内存里做。**

    对象是**惰性解析**的：构造时只线性扫一遍对象头（外加解包对象流）建索引，
    真正用到某个对象时才解析它（并缓存）。这样即使文件有几百 MB、几万个对象，
    提取几页文字也只解析用到的那几十个。
    """

    def __init__(self, data, name=""):
        self.data = data
        self.name = name
        self.version = self._read_version()
        #: 直接定义（``N 0 obj``）的对象号 → 文件偏移列表
        self._offsets = self._scan_offsets()
        #: 对象流（ObjStm）里的对象号 → ``[(对象流偏移, 对象体字节), …]``
        self._packed = {}
        self._cache = {}
        self._fonts = {}
        self._root_scan_done = False
        self.trailer = self._read_trailer()
        # 现代 PDF（1.5+）的 trailer 就是 **xref 流**的字典，且页树常常整个放在
        # **对象流**里 —— 这两步是「能读真实世界的 PDF」的分水岭：
        # Chrome / Word / WPS 导出的文件基本都是这种形态，不做这两步就只能报告
        # 「找不到文档目录」，而它的 /Type /Catalog 明明就在文件里。
        self._load_cross_reference_streams()
        self._load_object_streams()
        self.encrypted = self._detect_encrypt()

    # ---- 载入 ----

    @classmethod
    def load(cls, path):
        with open(path, "rb") as handle:
            data = handle.read()
        return cls(data, name=path)

    @classmethod
    def from_bytes(cls, data, name=""):
        return cls(data, name=name)

    def _read_version(self):
        found = re.match(rb"%PDF-(\d+\.\d+)", self.data[:1024])
        if not found:
            raise UnsupportedPdf(
                "不是 PDF 文件：开头没有 %PDF- 标记（可能是改了扩展名的其它文件）"
            )
        return found.group(1).decode("ascii")

    def _scan_offsets(self):
        table = {}
        for found in _OBJ_RE.finditer(self.data):
            table.setdefault(int(found.group(1)), []).append(found.start())
        return table

    def _read_trailer(self):
        """收集全部 ``trailer`` 字典。

        不做「找 startxref 再跳过去」那套 —— 损坏文件里最不可信的就是它们。
        逐个 ``trailer`` 试着解析，只接受含已知键的字典（避免把内容流里出现的
        "trailer" 字样当成真的），后者覆盖前者（增量更新在文件尾部）。
        """
        merged = {}
        for found in re.finditer(rb"\btrailer\b", self.data):
            lexer = _Lexer(self.data, found.end(), resolver=self.resolve)
            try:
                kind, value = lexer.next_token()
            except (PdfError, RecursionError):
                continue
            if kind != "dict" or not isinstance(value, dict):
                continue
            if not any(key in value for key in _TRAILER_KEYS):
                continue
            merged.update(value)
        return merged

    def _detect_encrypt(self):
        """``/Encrypt`` 的判定。

        必须在**对象上下文**里找：``/Encrypt`` 后面跟一个引用或字典才是真的加密字典，
        内容流里恰好印着 "/Encrypt" 这几个字不算。
        """
        if "Encrypt" in self.trailer:
            return True
        for found in re.finditer(rb"/Encrypt\b", self.data):
            tail = self.data[found.end():found.end() + 64]
            if re.match(rb"\s*\d+\s+\d+\s+R\b", tail) or tail.lstrip().startswith(b"<<"):
                return True
        return False

    # ---- 现代 PDF：xref 流与对象流 ----

    def _iter_type_streams(self, type_name):
        """按文件顺序产出 ``/Type /<type_name>`` 的流对象：``(偏移, Stream)``。

        做法是「先按 ``/Type /Xxx`` 定位，再往前找最近的 ``N 0 obj`` 头」——
        不去解析 xref 流本身（那要按它的 ``/W`` 数组解二进制字段，属于另一套解析，
        而我们需要的信息（``/Root`` 等）本来就在它的字典里，动态字典已经解析好了）。
        """
        pattern = _TYPE_STREAM_RE.get(type_name)
        if pattern is None:
            return
        data = self.data
        seen = set()
        for found in pattern.finditer(data):
            head = None
            for match in _OBJ_RE.finditer(data, max(0, found.start() - 4096), found.start()):
                head = match
            if head is None or head.start() in seen:
                continue
            seen.add(head.start())
            try:
                obj = _Lexer(data, head.start(), resolver=self.resolve).parse_indirect_object()
            except (PdfError, RecursionError, ValueError, IndexError):
                continue
            if isinstance(obj, Stream) and str(obj.dict.get("Type") or "") == type_name:
                yield head.start(), obj

    def _load_cross_reference_streams(self):
        """把 xref 流的字典当成 trailer 用（PDF 1.5+ 没有 ``trailer`` 关键字）。"""
        for _offset, stream in self._iter_type_streams("XRef"):
            if any(key in stream.dict for key in _TRAILER_KEYS):
                self.trailer.update(stream.dict)

    def _load_object_streams(self):
        """解包 ``/Type /ObjStm`` —— 页树、字体、内容流引用常常整个住在里面。"""
        total = 0
        for offset, stream in self._iter_type_streams("ObjStm"):
            total += self._unpack_object_stream(stream, offset)
            if total >= MAX_PACKED_OBJECTS:
                break

    def _unpack_object_stream(self, stream, offset):
        """一个对象流的格式：``<< /Type /ObjStm /N 5 /First 40 /Filter … >>``。

        流的前 ``/First`` 字节是一张表：``对象号 相对偏移`` 一对一对排着（共 ``/N`` 对），
        表后面紧跟各个对象的**对象体**（不含 ``N 0 obj`` / ``endobj``，只有对象本身）。
        相对偏移是相对 ``/First`` 的。
        """
        try:
            payload = stream.decoded(MAX_CONTENT_BYTES)
        except (PdfError, OSError):
            return 0
        count = _as_int(self.resolve(stream.dict.get("N")))
        first = _as_int(self.resolve(stream.dict.get("First")))
        if not count or count <= 0 or not first or first <= 0 or first > len(payload):
            return 0

        pairs = re.findall(rb"(\d{1,10})[ \t\r\n]+(\d{1,10})", payload[:first])
        if not pairs:
            return 0
        pairs = pairs[:min(count, len(pairs))]

        stored = 0
        for index, (num_text, offset_text) in enumerate(pairs):
            number = int(num_text)
            # 直接定义优先：同一个对象号**同时**有两种定义只可能出现在增量更新里，
            # 而现实中出现过的顺序都是「先打包、后改写成直接对象」，所以直接定义更新。
            if number <= 0 or number in self._offsets:
                continue
            body_start = first + int(offset_text)
            if body_start >= len(payload):
                continue
            if index + 1 < len(pairs):
                body_end = first + int(pairs[index + 1][1])
                if body_end <= body_start:
                    body_end = len(payload)
            else:
                body_end = len(payload)
            self._packed.setdefault(number, []).append(
                (offset, payload[body_start:body_end])
            )
            stored += 1
        return stored

    # ---- 对象访问 ----

    def resolve(self, value, depth=0):
        """跟着引用一路解析到底。"""
        while isinstance(value, Ref) and depth < 32:
            value = self.get_obj(value.num)
            depth += 1
        return value

    def get_obj(self, num):
        """取间接对象。**同一个对象号取最后一个定义**（增量更新在文件尾部）。"""
        if num in self._cache:
            return self._cache[num]
        # (偏移, 对象体字节)；对象体为 None 表示「文件里直接定义的 N 0 obj」
        candidates = [(offset, None) for offset in self._offsets.get(num, ())]
        candidates.extend(self._packed.get(num, ()))
        if not candidates:
            return None
        # 从后往前试：最后一个定义才是当前版本；若那里解析失败（例如扫描到的其实是
        # 流数据里的假对象头），继续往前试更早的定义
        candidates.sort(key=lambda item: item[0], reverse=True)
        for offset, payload in candidates:
            try:
                if payload is None:
                    lexer = _Lexer(self.data, offset, resolver=self.resolve)
                    obj = lexer.parse_indirect_object()
                else:
                    # 对象流里的对象没有 obj/endobj 外壳，直接就是对象本身
                    lexer = _Lexer(payload, 0, resolver=self.resolve)
                    kind, value = lexer.next_token()
                    if kind in ("kw", "delim", "eof"):
                        raise _ParseError("对象流里的对象不是合法对象")
                    obj = _token_value(kind, value)
            except (PdfError, RecursionError, ValueError, IndexError):
                continue
            self._cache[num] = obj
            return obj
        self._cache[num] = None
        return None

    def object_count(self):
        return len(set(self._offsets) | set(self._packed))

    # ---- 结构 ----

    def catalog(self):
        """文档目录（``/Root``）。trailer 不可信时线性扫 ``/Type /Catalog`` 兜底。"""
        root = self.resolve(self.trailer.get("Root"))
        if isinstance(root, dict) and (
            str(root.get("Type") or "") == "Catalog" or "Pages" in root
        ):
            return root
        if self._root_scan_done:
            raise PdfError("找不到文档目录（/Root）——文件结构损坏")
        self._root_scan_done = True
        for num in sorted(set(self._offsets) | set(self._packed))[:MAX_CATALOG_SCAN]:
            obj = self.get_obj(num)
            if isinstance(obj, dict) and str(obj.get("Type") or "") == "Catalog":
                return obj
        raise PdfError(
            "找不到文档目录（/Root）——可能是加密、损坏或非标准 PDF"
        )

    def pages(self):
        """按阅读顺序返回 ``[(page_dict, 继承资源字典), …]``。

        ``/Resources`` 等可继承属性在**页树**上继承（父节点给了子节点就继承），
        所以递归时把继承链带上。
        """
        catalog = self.catalog()
        top = self.resolve(catalog.get("Pages"))
        result = []
        self._walk_pages(top, {"Resources": catalog.get("Resources")}, result, set(), 0)
        return result

    def _walk_pages(self, node, inherited, result, seen, depth):
        if not isinstance(node, dict) or depth > MAX_PAGE_DEPTH:
            return
        if id(node) in seen:  # 环形 /Kids：畸形文件里真的存在
            return
        seen.add(id(node))

        merged = dict(inherited)
        for key in ("Resources", "MediaBox", "CropBox", "Rotate"):
            if key in node:
                merged[key] = node[key]

        node_type = str(node.get("Type") or "")
        if node_type == "Page" or ("Kids" not in node and "Contents" in node):
            result.append((node, merged))
            return
        if node_type == "Pages" or "Kids" in node:
            kids = self.resolve(node.get("Kids"))
            if isinstance(kids, list):
                for kid in kids:
                    self._walk_pages(self.resolve(kid), merged, result, seen, depth + 1)
            return
        # 既没 Contents 也没 Kids：空页，也算一页
        result.append((node, merged))

    def info_dict(self):
        info = self.resolve(self.trailer.get("Info"))
        return info if isinstance(info, dict) else {}

    def metadata(self):
        info = self.info_dict()
        return {
            "title": _text_value(info.get("Title")),
            "author": _text_value(info.get("Author")),
            "creator": _text_value(info.get("Creator")),
            "producer": _text_value(info.get("Producer")),
            "creation_date": _text_value(info.get("CreationDate")),
        }

    # ---- 字体 ----

    def font(self, resources, name):
        """按资源名取字体（带缓存）。"""
        key = (id(resources), name)
        if key in self._fonts:
            return self._fonts[key]
        info = None
        if isinstance(resources, dict):
            fonts = self.resolve(resources.get("Font"))
            if isinstance(fonts, dict):
                entry = fonts.get(name)
                if entry is not None:
                    info = FontInfo.build(self, self.resolve(entry), name)
        self._fonts[key] = info
        return info

    # ---- 页面内容 ----

    def page_content(self, page, limit=MAX_CONTENT_BYTES):
        """把页的 ``/Contents`` 拼成一段内容流字节。

        ``/Contents`` 可能是**单个流**、**流的数组**，也可能是数组的引用 ——
        三种都要处理。多个流按顺序拼接（它们共享同一个图形状态）。
        """
        contents = self.resolve(page.get("Contents"))
        chunks = []
        if isinstance(contents, Stream):
            chunks.append(contents.decoded(limit))
        elif isinstance(contents, list):
            for item in contents:
                stream = self.resolve(item)
                if isinstance(stream, Stream) and not stream.is_image():
                    chunks.append(stream.decoded(limit))
        # 用换行拼接：内容流是一个操作符序列，缺了分隔符会把两个 token 粘成一个
        return b"\n".join(chunks)

    def page_images(self, resources):
        """页上引用的图像 XObject 数量（用于判断「扫描页」）。"""
        if not isinstance(resources, dict):
            return 0
        xobjects = self.resolve(resources.get("XObject"))
        if not isinstance(xobjects, dict):
            return 0
        count = 0
        for entry in xobjects.values():
            obj = self.resolve(entry)
            if isinstance(obj, Stream) and str(obj.get("Subtype") or "") == "Image":
                count += 1
        return count


def _text_value(value):
    if isinstance(value, bytes):
        return value.decode("utf-16-be", "replace") if value[:2] == b"\xfe\xff" \
            else value.decode("latin-1", "replace")
    if isinstance(value, str):
        return str(value)
    return ""


# ---------------------------------------------------------------- 内容流 → 文本


#: 单位矩阵 [a b c d e f]
_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

#: 控制字符里只保留换行与制表，其余（NUL、退格…）一律丢弃
_KEEP_CONTROL = frozenset(("\n", "\t"))


class _ContentText:
    """把一个内容流跑一遍，产出这一页的文本。

    排版还原只做三件**可靠**的事（不做字距表推算，那需要 /Widths 且极易出错）：

    1. ``Td`` / ``TD`` / ``Tm`` / ``T*`` 引起的 **Y 位移** → 换行；
    2. ``TJ`` 数组里的**大负位移** → 空格（字距 + 词距，PDF 里空格的典型实现）；
    3. **同一行上明显前移**的第二次定位 → 补一个空格（很多生成器逐词定位）。
    """

    __slots__ = ("doc", "resources", "warnings", "_out", "_stack", "_size", "_leading",
                 "_font_name", "_font", "_tm", "_tlm", "_last_y", "_last_x",
                 "_char_spacing", "_word_spacing", "_lexer", "undecoded_fonts",
                 "unmapped_codes", "text_shown", "images", "_pending_space",
                 "_depth", "_visited")

    def __init__(self, doc, resources, warnings, depth=0, visited=None):
        self.doc = doc
        self.resources = resources
        self.warnings = warnings
        self._out = []
        self._stack = []
        self._size = 0.0
        self._leading = 0.0
        self._char_spacing = 0.0
        self._word_spacing = 0.0
        self._font_name = None
        self._font = None
        self._tm = _IDENTITY
        self._tlm = _IDENTITY
        self._last_y = None
        self._last_x = None
        self._lexer = None
        #: 无法准确解码的字体 → 原因（用 dict 是为了把原因也带出去给用户看）
        self.undecoded_fonts = {}
        self.unmapped_codes = 0
        self.text_shown = 0
        self.images = 0
        #: 上一个「看起来该有空格」的位置 —— 见 _append 的说明
        self._pending_space = False
        self._depth = depth
        #: 已经展开过的表单对象 id（跨递归共享，防自引用）
        self._visited = visited if visited is not None else set()

    # ---- 主流程 ----

    def run(self, content):
        if not content:
            return ""
        self._lexer = _Lexer(content)
        lexer = self._lexer
        while True:
            try:
                kind, value = lexer.next_token()
            except _ParseError as exc:
                # 内容流损坏：**保留已经提取到的文本**，并把原因记下来（诚实且不崩）
                self.warnings.append("内容流解析中断：%s" % exc)
                break
            if kind == "eof":
                break
            if kind == "kw":
                try:
                    self._operator(value)
                except (TypeError, ValueError, IndexError):
                    # 操作数个数不对（畸形内容流）：忽略这一个操作符，继续往下读
                    pass
                finally:
                    self._stack = []
                continue
            if kind == "dict":
                # BDC 的属性字典等：压在栈上，不参与文本
                self._stack.append(value)
                continue
            if kind == "delim":
                continue
            self._stack.append(value)
        text = "".join(self._out)
        return _clean_text(text)

    # ---- 操作符 ----

    def _operator(self, op):
        args = self._stack

        if op == "BT":
            self._tm = _IDENTITY
            self._tlm = _IDENTITY
            # **不重置 _last_y**：大量 PDF 给每一行都包一个独立的 BT/ET
            # （`BT 1 0 0 1 72 700 Tm (x) Tj ET` 逐行重复），重置掉就等于把整页
            # 挤成一行。保留上一次的基线，让新块里的第一个定位操作符照常判断换行。
            return
        if op == "Tf":
            if len(args) >= 2 and isinstance(args[-2], str) and _is_number(args[-1]):
                self._font_name = str(args[-2])
                self._size = float(args[-1])
                self._font = None
            return
        if op == "Td":
            if len(args) >= 2:
                self._translate(_num(args[-2]), _num(args[-1]))
            return
        if op == "TD":
            if len(args) >= 2:
                self._leading = -_num(args[-1])   # TD 顺带设置行距（= -ty）
                self._translate(_num(args[-2]), _num(args[-1]))
            return
        if op == "Tm":
            if len(args) >= 6:
                self._tm = tuple(_num(item) for item in args[-6:])
                self._tlm = self._tm
                self._check_newline()
            return
        if op == "T*":
            self._translate(0.0, -self._leading)
            return
        if op == "TL":
            if args:
                self._leading = _num(args[-1])
            return
        if op == "Tc":
            if args:
                self._char_spacing = _num(args[-1])
            return
        if op == "Tw":
            if args:
                self._word_spacing = _num(args[-1])
            return
        if op == "Tj":
            if args:
                self._show(args[-1])
            return
        if op == "TJ":
            if args and isinstance(args[-1], list):
                self._show_array(args[-1])
            return
        if op == "'":
            self._translate(0.0, -self._leading)
            if args:
                self._show(args[-1])
            return
        if op == '"':
            if len(args) >= 3:
                self._word_spacing = _num(args[-3])
                self._char_spacing = _num(args[-2])
                self._translate(0.0, -self._leading)
                self._show(args[-1])
            return
        if op == "Do":
            self._do_xobject(args)
            return
        if op == "BI":
            # 内联图像：后面是**二进制**，必须整块跳过，否则词法器会把图像数据当 token 解
            self._lexer.skip_inline_image()
            return

    def _do_xobject(self, args):
        """``/Name Do``：若指向**表单**（Form XObject），把它的内容流也跑一遍。

        表单里是可以有文字的 —— 页眉页脚、水印、以及一部分排版工具（InDesign /
        LaTeX 的某些宏包）会把整页内容放进表单。不进去就等于整段丢失。
        表单的文字接在当前文本之后（表单自己的文本矩阵是相对于调用者的，
        这里没有做 CTM 还原，所以只能保证「内容不丢」，不保证位置精确）。
        """
        if not args or not isinstance(args[-1], str):
            return
        if self._depth >= MAX_FORM_DEPTH or len(self._visited) >= MAX_FORM_COUNT:
            return
        if not isinstance(self.resources, dict):
            return
        xobjects = self.doc.resolve(self.resources.get("XObject"))
        if not isinstance(xobjects, dict):
            return
        form = self.doc.resolve(xobjects.get(str(args[-1])))
        if not isinstance(form, Stream) or str(form.dict.get("Subtype") or "") != "Form":
            return
        key = id(form)
        if key in self._visited:
            return
        self._visited.add(key)
        try:
            content = form.decoded(MAX_CONTENT_BYTES)
        except (PdfError, OSError):
            return
        if not content.strip():
            return
        inner = self.doc.resolve(form.dict.get("Resources"))
        child = _ContentText(
            self.doc, inner if isinstance(inner, dict) else self.resources,
            self.warnings, self._depth + 1, self._visited,
        )
        text = child.run(content)
        self.undecoded_fonts.update(child.undecoded_fonts)
        self.unmapped_codes += child.unmapped_codes
        if text.strip():
            self.text_shown += 1
            self._newline()
            self._out.append(text)

    # ---- 定位 ----

    def _translate(self, tx, ty):
        a, b, c, d, e, f = self._tlm
        self._tlm = (a, b, c, d, e + tx * a + ty * c, f + tx * b + ty * d)
        self._tm = self._tlm
        self._check_newline()

    def _check_newline(self):
        y = self._tm[5]
        x = self._tm[4]
        if self._last_y is None:
            self._last_y = y
            self._last_x = x
            return
        threshold = max(1.5, self._size * 0.4) if self._size else 1.5
        if abs(y - self._last_y) > threshold:
            self._newline()
        elif self._last_x is not None:
            shift = x - self._last_x
            # 同一行上明显往前跳 = 中间原本有空白（逐词定位的 PDF 很常见）。
            # 阈值放到 1 个字宽：更小会把「同一词被切成两段」的情况也塞进空格。
            if shift > max(6.0, self._size):
                self._pending_space = True
        self._last_y = y
        self._last_x = x

    def _newline(self):
        # 换行会吃掉尚未落地的空格：本来就要换行了，前面那个空格没有意义
        self._pending_space = False
        if self._out and not self._out[-1].endswith("\n"):
            self._out.append("\n")

    # ---- 显示文本 ----

    def _show(self, value):
        if isinstance(value, bytes):
            self._append(self._decode(value))
        elif isinstance(value, str):
            self._append(value)

    def _show_array(self, items):
        # TJ 里的数字是**字距微调**（千分之一 em 的负值=拉大字间距）。
        # 大负值实际上就是空格；量级随字号缩放：10pt 时 -100 是经验阈值。
        limit = -10.0 * self._size if self._size else -100.0
        for item in items:
            if isinstance(item, bytes):
                self._append(self._decode(item))
            elif isinstance(item, str):
                self._append(item)
            elif _is_number(item) and float(item) < limit:
                self._pending_space = True

    def _decode(self, raw):
        info = self._font_info()
        if info is None:
            # 没选字体就显示文字（畸形但存在）：按 latin-1 直出
            return raw.decode("latin-1")
        text, _missing = info.decode(raw)
        if not info.reliable:
            self.undecoded_fonts[info.name or "?"] = info.reason or "字体缺少字符映射信息"
        return text

    def _font_info(self):
        if self._font is not None or self._font_name is None:
            return self._font
        self._font = self.doc.font(self.resources, self._font_name)
        return self._font

    def _append(self, text):
        """追加一段文字，并决定「待定的空格」要不要真的落地。

        空格**延迟到看得见下一个字符时才决定**，因为中日韩文本里不加空格：
        逐词定位的 PDF 会在每个词（对中文来说是每个字）之间产生一次明显位移，
        若照位移补空格，就会得到「性 能 测 试」这种谁都搜不到的结果。
        判断依据是两侧字符的类别 —— 只要有一侧是 CJK，就丢掉这个空格。
        """
        if not text:
            return
        if self._pending_space:
            self._pending_space = False
            tail = self._out[-1] if self._out else ""
            head = text[0]
            if not tail.endswith((" ", "\n")) and not (
                _is_cjk(tail[-1:]) or _is_cjk(head)
            ):
                self._out.append(" ")
        if text.strip():
            self.text_shown += 1
        self._out.append(text)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


#: 需要归一化的兼容字符区间：Unicode 自己声明了「与某个字符等价」的那些。
#: 现实里 WPS / Word 会把汉字的字形写成**康熙部首**（U+2F00–U+2FDF）或
#: **CJK 兼容汉字**（U+F900–U+FAFF）：屏幕上看着一模一样，码位却不是同一个 ——
#: 不去归一化就会得到「⽆法获取」这种看着对、搜不到、复制出去是乱码的文本。
_COMPAT_RANGES = ((0x2F00, 0x2FDF), (0xF900, 0xFAFF), (0x2E80, 0x2EFF))


def _normalize_compat(text):
    """把康熙部首 / CJK 兼容汉字换成规范码位（NFKC 只对这些区间用）。

    刻意**不**对全文做 NFKC：那会把 ``ﬁ`` 拆成 ``fi``、``①`` 变成 ``1``、``㎡`` 变成
    ``m2`` —— 提取结果应当尽量是原文，只有「Unicode 声明等价」的替换才是无损的。
    """
    import unicodedata

    if not text:
        return text
    out = []
    changed = False
    for char in text:
        code = ord(char)
        if any(low <= code <= high for low, high in _COMPAT_RANGES):
            fixed = unicodedata.normalize("NFKC", char)
            if fixed != char:
                out.append(fixed)
                changed = True
                continue
        out.append(char)
    return "".join(out) if changed else text


def _is_cjk(char):
    """这个字符是不是中日韩文字（决定「要不要补空格」）。"""
    if not char:
        return False
    code = ord(char)
    return (
        0x3040 <= code <= 0x30FF      # 日文假名
        or 0x3400 <= code <= 0x4DBF   # 扩展 A
        or 0x4E00 <= code <= 0x9FFF   # 基本区
        or 0xF900 <= code <= 0xFAFF   # 兼容汉字
        or 0xFF00 <= code <= 0xFFEF   # 全角
        or 0x3000 <= code <= 0x303F   # CJK 标点（、。「」…）
        or 0x20000 <= code <= 0x3FFFF # 扩展 B 及以后
    )


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clean_text(text):
    """去掉控制字符与全空白行，避免输出一堆看不见的东西。"""
    if not text:
        return ""
    kept = []
    for char in text:
        if char == "\n" or char == "\t" or ord(char) >= 0x20:
            kept.append(char)
    cleaned = _normalize_compat("".join(kept))
    lines = [line.rstrip() for line in cleaned.split("\n")]
    result = []
    for line in lines:
        if not line and result and not result[-1]:
            continue  # 连续空行压成一个
        result.append(line)
    while result and not result[0]:
        result.pop(0)
    while result and not result[-1]:
        result.pop()
    return "\n".join(result)


# ---------------------------------------------------------------- 对外入口


def extract_page_text(doc, page, inherited, warnings):
    """提取一页的文本，返回 ``(text, info)``。"""
    collector = _ContentText(doc, doc.resolve(inherited.get("Resources")), warnings)
    images = doc.page_images(doc.resolve(inherited.get("Resources")))
    collector.images = images
    try:
        content = doc.page_content(page)
    except ImageOnly:
        return "", {
            "images": images,
            "image_only": True,
            "undecoded_fonts": [],
            "unmapped_codes": 0,
            "warnings": ["本页内容流是图像编码（扫描页）"],
        }
    except (PdfError, OSError) as exc:
        warnings.append("内容流无法解码：%s" % exc)
        return "", {
            "images": images,
            "image_only": False,
            "undecoded_fonts": [],
            "unmapped_codes": 0,
            "warnings": ["内容流无法解码：%s" % exc],
        }

    text = collector.run(content)
    info = {
        "images": images,
        "image_only": False,
        "undecoded_fonts": sorted(collector.undecoded_fonts),
        "unmapped_codes": collector.unmapped_codes,
        "warnings": [],
    }
    if collector.undecoded_fonts:
        # 真实文件里一页可能有几十个无法解码的字体（WPS 一页一个子集字体），
        # 全列出来会把告警变成一屏乱码 —— 只列前几个，其余计数
        names = sorted(collector.undecoded_fonts)
        shown = "、".join(
            "%s（%s）" % (name, collector.undecoded_fonts[name]) for name in names[:3]
        )
        if len(names) > 3:
            shown += " 等 %d 个字体" % len(names)
        info["warnings"].append(
            "本页有 %d 个字体没有可用的字符映射，对应文字按字节直出（可能不是原字）：%s"
            % (len(names), shown)
        )
    if collector.unmapped_codes:
        info["warnings"].append(
            "有 %d 个码位不在映射表内，已跳过（通常是空白或 .notdef 字形）"
            % collector.unmapped_codes
        )
    return text, info


def extract(path, max_chars=5_000_000, max_pages=0, progress=None):
    """把一个 PDF 提取成结构化结果。

    :param max_chars: 全文累计字符上限（超出后停止追加，`truncated` 置真）
    :param max_pages: 只处理前 N 页（0 = 全部）
    :param progress: 可选回调 ``progress(done, total)``
    :raises PdfEncrypted: 文件有 /Encrypt
    :raises UnsupportedPdf: 不是 PDF
    :raises PdfError: 结构损坏到无法定位页面
    """
    import os

    doc = Document.load(path)
    size = os.path.getsize(path) if os.path.exists(path) else len(doc.data)

    if doc.encrypted:
        raise PdfEncrypted(
            "该 PDF 已加密（/Encrypt）。标准库不含 RC4/AES 解密实现，"
            "无法提取文字——请先用支持解密的工具去掉口令保护"
        )

    pages = doc.pages()
    total = len(pages)
    selected = pages[:max_pages] if max_pages else pages

    page_results = []
    all_text = []
    truncated = False
    total_chars = 0
    doc_warnings = []
    undecoded = set()
    unmapped_total = 0

    for index, (page, inherited) in enumerate(selected, 1):
        if progress:
            progress(index, total)
        warnings = []
        text, info = extract_page_text(doc, page, inherited, warnings)
        undecoded.update(info["undecoded_fonts"])
        unmapped_total += info["unmapped_codes"]
        chars = len(text)
        total_chars += chars
        if not truncated:
            all_text.append(text)
            if total_chars > max_chars:
                truncated = True
                doc_warnings.append(
                    "全文超过 %d 字符，已截断（索引与导出只含前 %d 字符）"
                    % (max_chars, max_chars)
                )
        page_results.append({
            "index": index,
            "chars": chars,
            "has_text": chars > 0,
            "image_count": info["images"],
            "image_only": info["image_only"],
            "undecoded_fonts": info["undecoded_fonts"],
            "warnings": warnings + info["warnings"],
            # 逐页正文：Markdown 输出要按页分节，靠「按空行切分全文」是不可靠的
            # （一页内部本身就可能有空行）。这里与 all_text 共用同一个字符串对象，
            # 不额外占内存；写库时会剔除这个字段。
            "text": text,
        })

    full_text = "\n\n".join(all_text)
    if truncated:
        full_text = full_text[:max_chars]

    stats = {
        "page_count": total,
        "pages_with_text": sum(1 for item in page_results if item["has_text"]),
        "pages_without_text": sum(1 for item in page_results if not item["has_text"]),
        "image_only_pages": [item["index"] for item in page_results if item["image_only"]],
        "chars": total_chars,
        "unmapped_codes": unmapped_total,
        "undecoded_fonts": sorted(undecoded),
        "truncated": truncated,
    }

    meta = doc.metadata()
    result = {
        "ok": True,
        "kind": "pdf",
        "path": path,
        "name": os.path.basename(path),
        "size": size,
        "format": "PDF %s" % doc.version,
        "object_count": doc.object_count(),
        "page_count": total,
        "pages": page_results,
        "text": full_text,
        "stats": stats,
        "metadata": meta,
        "warnings": doc_warnings,
        "status": _status_for(page_results, total),
    }
    return result


def _status_for(pages, total):
    """给这份文档一句话结论 —— 界面上一眼就能看出「要不要真 OCR」。"""
    if total == 0:
        return "empty"
    with_text = sum(1 for item in pages if item["has_text"])
    if with_text == 0:
        if any(item["image_only"] or item["image_count"] for item in pages):
            return "no-text-layer"
        return "empty"
    if with_text < len(pages):
        return "partial"
    return "ok"


def probe(path):
    """快速探测（不提取全文）：类型、是否加密、页数、首页是否有文字层。"""
    import os

    doc = Document.load(path)
    info = {
        "ok": True,
        "path": path,
        "name": os.path.basename(path),
        "size": os.path.getsize(path) if os.path.exists(path) else len(doc.data),
        "kind": "pdf",
        "format": "PDF %s" % doc.version,
        "encrypted": bool(doc.encrypted),
        "object_count": doc.object_count(),
        "metadata": doc.metadata(),
    }
    if doc.encrypted:
        info.update({"page_count": 0, "first_page_chars": 0,
                     "has_text_layer": False,
                     "note": "文件已加密，无法读取页结构"})
        return info

    pages = doc.pages()
    info["page_count"] = len(pages)
    if not pages:
        info.update({"first_page_chars": 0, "has_text_layer": False,
                     "note": "文档里没有任何页"})
        return info

    page, inherited = pages[0]
    warnings = []
    try:
        text, page_info = extract_page_text(doc, page, inherited, warnings)
    except (PdfError, OSError) as exc:
        info.update({"first_page_chars": 0, "has_text_layer": False, "note": str(exc)})
        return info
    info["first_page_chars"] = len(text)
    info["has_text_layer"] = bool(text.strip())
    info["first_page_images"] = page_info["images"]
    if page_info["image_only"]:
        info["note"] = "首页是扫描图像，没有文字层"
    elif not text.strip():
        info["note"] = "首页没有可提取的文字"
    if page_info["undecoded_fonts"]:
        info["undecoded_fonts"] = page_info["undecoded_fonts"]
    return info
