"""合成 PDF 构造器 —— 用于在没有真实样本的情况下验证 PDF 解析与文字提取。

为什么要合成而不是找样本：本仓库不能把第三方文档入库；而自造文件能让每一个字节都可预期，
从而把最容易错的地方**精确断言**出来 —— 例如「提取出的文本逐字符等于预期」，
这在真实样本上做不到（只能靠肉眼看个大概）。

本模块只用标准库拼字节，产出的是**真正合法**的 PDF：xref 表、trailer、startxref 齐全，
``/Length`` 与实际数据一致（FlateDecode 的流长度按压缩后的字节数写）。
另有几个「故意坏掉」的变体：缺 startxref、加密、扫描页；
以及**现代形态**的样本：对象流（ObjStm）+ xref 流、简单字体写错 codespace、映射到康熙部首。

参考 ``shh11-media-audio/tests/synth.py`` 的做法（那边合成了 MP4/MKV）。
"""

import zlib

#: 中文页用到的字符 → 码位，测试里两边共用这一份，避免断言里再抄一遍
CJK_CHARS = {
    0x0001: "中",
    0x0002: "文",
    0x0003: "测",
    0x0004: "试",
    0x0005: "。",
    0x0006: "\U0001F600",  # 代理对（D83D DE00）：验证多码元目标的解码
    0x0010: "A",
    0x0011: "B",
    0x0012: "C",
}


def stream(data, compress=True, extra=b""):
    """一个流对象的对象体（不含 ``N 0 obj`` / ``endobj``）。

    ``/Length`` 是**压缩后**的长度 —— 写成原文长度是 PDF 里最常见的损坏原因之一，
    合成器这里刻意写对，好让「按 /Length 读」这条路径被测到。
    """
    if compress:
        packed = zlib.compress(data)
        head = b"<< /Length %d /Filter /FlateDecode" % len(packed)
    else:
        packed = data
        head = b"<< /Length %d" % len(packed)
    if extra:
        head += b" " + extra
    return head + b" >>\nstream\n" + packed + b"\nendstream"


def raw_stream(data, dictionary, compress=False):
    """自定义字典的流（用于图像 XObject 之类）。"""
    if compress:
        data = zlib.compress(data)
        dictionary = dictionary + b" /Filter /FlateDecode"
    return b"<< /Length %d " % len(data) + dictionary + b" >>\nstream\n" + data + \
        b"\nendstream"


def assemble(objects, root=1, extra_trailer=b"", with_xref=True, with_startxref=True,
             header=b"%PDF-1.4", with_trailer=True):
    """把 ``{对象号: 对象体字节}`` 组装成一个完整 PDF 文件字节串。

    :param root: ``/Root`` 指向的对象号
    :param extra_trailer: 追加到 trailer 字典里的内容（例如 ``/Encrypt 9 0 R``）
    :param with_xref: False = 不写 xref 表（考察线性扫描兜底）
    :param with_startxref: False = 不写 startxref（考察「没有 startxref 的损坏文件」）
    """
    out = bytearray(header + b"\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num
        out += objects[num]
        out += b"\nendobj\n"

    total = (max(objects) + 1) if objects else 1
    xref_offset = len(out)
    if with_xref:
        out += b"xref\n0 %d\n" % total
        out += b"0000000000 65535 f \n"
        for num in range(1, total):
            if num in offsets:
                out += b"%010d 00000 n \n" % offsets[num]
            else:
                out += b"0000000000 65535 f \n"

    if with_trailer:
        trailer = b"<< /Size %d /Root %d 0 R" % (total, root)
        if extra_trailer:
            trailer += b" " + extra_trailer
        trailer += b" >>"
        out += b"trailer\n" + trailer + b"\n"
    if with_startxref:
        out += b"startxref\n%d\n%%%%EOF\n" % xref_offset
    else:
        # 故意不写 startxref：真实世界里被截断 / 被拼接过的文件就是这样
        out += b"%%EOF\n"
    return bytes(out)


# ---------------------------------------------------------------- 常用对象


def catalog(pages_num):
    return b"<< /Type /Catalog /Pages %d 0 R >>" % pages_num


def pages_node(kids, count):
    refs = b" ".join(b"%d 0 R" % num for num in kids)
    return b"<< /Type /Pages /Kids [%s] /Count %d >>" % (refs, count)


def page(parent_num, contents, resources=b"<< /Font << /F1 5 0 R >> >>",
         media_box=b"[0 0 612 792]"):
    """一个页对象。``contents`` 可以是引用、引用数组，或 ``None``（无内容）。"""
    if contents is None:
        body = b"<< /Type /Page /Parent %d 0 R /MediaBox %s /Resources %s >>"
        return body % (parent_num, media_box, resources)
    if isinstance(contents, (list, tuple)):
        items = b" ".join(b"%d 0 R" % num for num in contents)
        return (b"<< /Type /Page /Parent %d 0 R /MediaBox %s /Resources %s"
                b" /Contents [%s] >>") % (parent_num, media_box, resources, items)
    return (b"<< /Type /Page /Parent %d 0 R /MediaBox %s /Resources %s"
            b" /Contents %d 0 R >>") % (parent_num, media_box, resources, contents)


def simple_font():
    """标准 14 字体之一（Type1 + WinAnsiEncoding → cp1252，无需 ToUnicode）。"""
    return b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica" \
           b" /Encoding /WinAnsiEncoding >>"


def type0_font(descendant_num, cmap_num):
    return (b"<< /Type /Font /Subtype /Type0 /BaseFont /SYNTH+CJK /Encoding /Identity-H"
            b" /DescendantFonts [%d 0 R] /ToUnicode %d 0 R >>") % (descendant_num, cmap_num)


def cid_font(descriptor_num):
    return (b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /SYNTH+CJK"
            b" /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >>"
            b" /FontDescriptor %d 0 R /DW 1000 /CIDToGIDMap /Identity >>") % descriptor_num


def font_descriptor():
    return (b"<< /Type /FontDescriptor /FontName /SYNTH+CJK /Flags 4"
            b" /FontBBox [0 -200 1000 900] /ItalicAngle 0 /Ascent 800 /Descent -200"
            b" /CapHeight 700 /StemV 80 >>")


def tounicode_cmap(mapping):
    """按映射表生成一份覆盖三种写法（bfchar / bfrange 单目标 / bfrange 数组）的 CMap。

    刻意混用三种形式，好让 :func:`pdfparse.parse_tounicode` 的每条分支都被走过：

    * ``bfchar``：逐个映射（0x0001~0x0006，其中一个目标是代理对）
    * ``bfrange`` + 数组目标：``<0003> <0005> [<6D4B> <8BD5> <3002>]``
    * ``bfrange`` + 单目标：``<0010> <0012> <0041>``（逐码递增）
    """
    def hex_utf16(text):
        return text.encode("utf-16-be").hex().upper().encode("ascii")

    singles = sorted(code for code in mapping if code <= 0x0006)
    array_range = [0x0003, 0x0004, 0x0005]
    sequential = [0x0010, 0x0011, 0x0012]

    lines = [
        b"/CIDInit /ProcSet findresource begin",
        b"12 dict begin",
        b"begincmap",
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        b"/CMapName /Adobe-Identity-UCS def",
        b"/CMapType 2 def",
        b"1 begincodespacerange",
        b"<0000> <FFFF>",
        b"endcodespacerange",
        b"%d beginbfchar" % len(singles),
    ]
    for code in singles:
        lines.append(b"<%04X> <%s>" % (code, hex_utf16(mapping[code])))
    lines.append(b"endbfchar")

    if all(code in mapping for code in array_range):
        lines.append(b"1 beginbfrange")
        targets = b" ".join(b"<%s>" % hex_utf16(mapping[code]) for code in array_range)
        lines.append(b"<0003> <0005> [%s]" % targets)
        lines.append(b"endbfrange")

    if all(code in mapping for code in sequential):
        lines.append(b"1 beginbfrange")
        lines.append(b"<0010> <0012> <%s>" % hex_utf16(mapping[0x0010]))
        lines.append(b"endbfrange")

    lines.extend([
        b"endcmap",
        b"CMapName currentdict /CMap defineresource pop",
        b"end",
        b"end",
    ])
    return b"\n".join(lines) + b"\n"


# ---------------------------------------------------------------- 完整样本


def chinese_two_page_pdf():
    """两页、有文字层、含中文 ToUnicode 映射的 PDF。

    第 1 页：标准 14 字体的一行英文 + 半行普通换行 + 一行中文（2 字节码位）
    第 2 页：TJ 字距数组、T* 与 ' 换行、十六进制字符串
    """
    font_simple = 5
    font_type0 = 6
    descendant = 7
    descriptor = 8
    cmap = 9
    contents_one = 10
    contents_two = 11

    page_one = (
        b"BT /F1 12 Tf 72 720 Td (Hello World) Tj 0 -16 Td (Second line) Tj ET\n"
        b"BT /F2 12 Tf 72 688 Td <0001 0002 0003 0004 0005> Tj ET\n"
        b"BT /F2 12 Tf 72 670 Td <0010 0011 0012> Tj ET\n"
    )
    page_two = (
        # 开头就用 `'` 显示两行：`'` 自身即「换到下一行再显示」，所以第一行之前
        # 必然多出一个换行 —— 放在页首，这个多出来的换行会被清理掉，结果才确定
        b"BT /F1 12 Tf 14 TL 1 0 0 1 72 720 Tm (Line one) ' (Line two) ' ET\n"
        b"BT /F1 12 Tf 72 660 Td [(Alpha)-400(Beta)-50(Gamma)] TJ ET\n"
        b"BT /F1 12 Tf 72 640 Td <48656C6C6F> Tj ET\n"
    )

    objects = {
        1: catalog(2),
        2: pages_node([3, 4], 2),
        3: page(2, contents_one,
                resources=b"<< /Font << /F1 5 0 R /F2 6 0 R >> >>"),
        4: page(2, contents_two,
                resources=b"<< /Font << /F1 5 0 R >> >>"),
        font_simple: simple_font(),
        font_type0: type0_font(descendant, cmap),
        descendant: cid_font(descriptor),
        descriptor: font_descriptor(),
        cmap: stream(tounicode_cmap(CJK_CHARS), compress=True),
        contents_one: stream(page_one, compress=True),
        contents_two: stream(page_two, compress=True),
    }
    expected = [
        "Hello World\nSecond line\n中文测试。\nABC",
        "Line one\nLine two\nAlpha BetaGamma\nHello",
    ]
    return assemble(objects, root=1), expected


def array_contents_pdf():
    """``/Contents`` 是**数组**（两个流按顺序拼接）的 PDF。"""
    objects = {
        1: catalog(2),
        2: pages_node([3], 1),
        3: page(2, [7, 8]),
        5: simple_font(),
        7: stream(b"BT /F1 12 Tf 72 700 Td (Part one) Tj ET"),
        8: stream(b"BT /F1 12 Tf 72 680 Td (part two) Tj ET"),
    }
    return assemble(objects, root=1), "Part one\npart two"


def scanned_pdf():
    """只有一张 DCTDecode（JPEG）图像、**没有文字层**的一页。"""
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"  # 假 JPEG 负载
    objects = {
        1: catalog(2),
        2: pages_node([3], 1),
        3: page(2, 7, resources=b"<< /XObject << /Im0 6 0 R >> >>"),
        6: raw_stream(jpeg, b"/Type /XObject /Subtype /Image /Width 64 /Height 64"
                            b" /ColorSpace /DeviceRGB /BitsPerComponent 8"
                            b" /Filter /DCTDecode"),
        7: stream(b"q 200 0 0 200 72 500 cm /Im0 Do Q"),
    }
    return assemble(objects, root=1)


def image_only_contents_pdf():
    """``/Contents`` 本身就是 DCTDecode 流（极端但存在）的 PDF。"""
    objects = {
        1: catalog(2),
        2: pages_node([3], 1),
        3: page(2, 6),
        6: raw_stream(b"\xff\xd8\xff\xe0 fake", b"/Filter /DCTDecode /Length 99999"),
    }
    return assemble(objects, root=1)


def broken_pdf(no_startxref=True, no_xref=False):
    """损坏的 PDF：缺 startxref（或连 xref 表也缺），只有对象体 + trailer。

    线性扫描的意义正在于此 —— 这类文件 xref 已经不可信了，照样能提取。
    """
    objects = {
        1: catalog(2),
        2: pages_node([3], 1),
        3: page(2, 7),
        5: simple_font(),
        7: stream(b"BT /F1 12 Tf 72 700 Td (Recovered from a broken file) Tj ET"),
    }
    return assemble(objects, root=1, with_xref=not no_xref,
                    with_startxref=not no_startxref)


def encrypted_pdf():
    """``/Encrypt`` 存在的最小 PDF。"""
    objects = {
        1: catalog(2),
        2: pages_node([3], 1),
        3: page(2, 7),
        5: simple_font(),
        7: stream(b"BT /F1 12 Tf 72 700 Td (secret) Tj ET"),
        9: b"<< /Filter /Standard /V 1 /R 2 /O <"
           + b"AB" * 32 + b"> /U <" + b"CD" * 32 + b"> /P -44 >>",
    }
    return assemble(objects, root=1, extra_trailer=b"/Encrypt 9 0 R /ID [<01><02>]")


def empty_pdf():
    """零页 PDF（只有 catalog + 空 Pages 节点）。"""
    objects = {
        1: catalog(2),
        2: pages_node([], 0),
    }
    return assemble(objects, root=1)


def png(width=8, height=6, bit_depth=8, color_type=2):
    """一张最小合法 PNG（用于图片元数据那部分用例）。"""
    import struct

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    raw = bytearray()
    for _row in range(height):
        raw.append(0)  # filter type 0
        raw.extend(bytes([0x40] * (width * channels * (bit_depth // 8))))
    header = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


def write(path, data):
    with open(path, "wb") as handle:
        handle.write(data)
    return path


# ------------------------------------------------- 现代 PDF：对象流 + xref 流


def build_objstm(entries):
    """把 ``[(对象号, 对象体字节), …]`` 打包成一个 ``/Type /ObjStm`` 流对象体。

    结构：流的前 ``/First`` 字节是 ``对象号 相对偏移`` 的配对表，后面紧跟各对象体。
    真实生成器（Chrome / Word / WPS）就是这么写的，页树与字体全住在里面 ——
    这也是「只会线性扫 ``N 0 obj``」的解析器在这些文件上直接失败的原因。
    """
    header = bytearray()
    bodies = bytearray()
    for number, body in entries:
        header += b"%d %d " % (number, len(bodies))
        bodies += body + b" "
    first = len(header)
    payload = bytes(header) + bytes(bodies)
    return (b"<< /Type /ObjStm /N %d /First %d /Length %d /Filter /FlateDecode >>\nstream\n"
            % (len(entries), first, len(zlib.compress(payload)))
            + zlib.compress(payload) + b"\nendstream")


def modern_pdf_shell(xref_num, size, root, packed_num, packed, contents_num, contents):
    """组装一个「PDF 1.5+ 形态」的文件：对象流 + xref 流，**没有 trailer 关键字**。

    ``/Root`` 只出现在 xref 流的字典里，页树只在对象流里 —— 两种新形态各测到一次。
    """
    objects = {
        packed_num: packed,
        contents_num: contents,
    }
    xref_data = zlib.compress(b"\x00\x00\x00\x00")
    objects[xref_num] = (
        b"<< /Type /XRef /Size %d /W [1 2 1] /Index [0 1] /Root %d 0 R"
        b" /Length %d /Filter /FlateDecode >>\nstream\n"
        % (size, root, len(xref_data)) + xref_data + b"\nendstream"
    )
    return assemble(objects, root=root, with_xref=False, with_trailer=False,
                    header=b"%PDF-1.5")


def object_stream_pdf():
    """页树与字体全在**对象流**里、trailer 换成 **xref 流**的 PDF（1 页）。"""
    text = b"Packed away in an object stream"
    objects = {}
    packed_entries = [
        (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
        (3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
            b" /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>"),
        (5, simple_font()),
    ]
    objects[8] = build_objstm(packed_entries)
    body = b"BT /F1 12 Tf 72 700 Td (%s) Tj ET" % text
    data = modern_pdf_shell(xref_num=9, size=10, root=1, packed_num=8, packed=objects[8],
                            contents_num=7, contents=stream(body))
    return data, text.decode()


def simple_font_with_two_byte_codespace_pdf():
    """简单字体（非 Type0）的 ToUnicode 里写了**两字节** codespace 的 PDF。

    这是现实中真实存在的写法（WPS 生成的 Type3 字体就是这样），而简单字体的码位
    按规范**一定是单字节**。照 codespace 按 2 字节切会让整串文字错位 ——
    表现是「中文里混进 ç、有些字整段消失」。这一条把这个回归钉住。
    """
    cmap = (
        b"1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        b"5 beginbfchar\n"
        b"<0048> <0048>\n<0065> <0065>\n<006C> <006C>\n<006F> <006F>\n<0020> <0020>\n"
        b"endbfchar\n"
    )
    font = (b"<< /Type /Font /Subtype /Type1 /BaseFont /SYNTH+Latin"
            b" /Encoding /WinAnsiEncoding /ToUnicode 6 0 R >>")
    objects = {
        1: catalog(2),
        2: pages_node([3], 1),
        3: page(2, 7),
        5: font,
        6: stream(cmap, compress=False),
        7: stream(b"BT /F1 12 Tf 72 700 Td <48656C6C6F> Tj ET"),
    }
    return assemble(objects, root=1)


def radical_glyph_pdf():
    """ToUnicode 把字符映射到**康熙部首**码位（WPS / Word 的常见写法）。

    U+2F46 与 U+65E0（无）是同一个字形、不同码位。屏幕上看不出差别，
    但复制出去搜不到、贴到别处是乱码 —— 必须归一化成规范码位。
    """
    cmap = (
        b"1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
        b"1 beginbfchar\n<0001> <2F46>\nendbfchar\n"
        b"1 beginbfrange\n<0002> <0003> <65E0>\nendbfrange\n"
    )
    font = (b"<< /Type /Font /Subtype /Type1 /BaseFont /SYNTH+Radicals"
            b" /ToUnicode 6 0 R >>")
    objects = {
        1: catalog(2),
        2: pages_node([3], 1),
        3: page(2, 7),
        5: font,
        6: stream(cmap, compress=False),
        7: stream(b"BT /F1 12 Tf 72 700 Td <000100020003> Tj ET"),
    }
    return assemble(objects, root=1)


def per_glyph_cjk_pdf():
    """逐字定位的 PDF（每个字一次 ``Td``）：中日韩文本不该被塞进空格。

    逐字定位会让「同一行上有明显前移」这条规则在每个字之间都触发 ——
    若照它补空格，中文就变成「性 能 测 试」，谁也别想搜到。
    这里用一份单字节 ToUnicode 把码位映射到汉字（西文部分用标准 14 字体对照）。
    """
    cmap = (
        b"1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
        b"3 beginbfchar\n<01> <4E2D>\n<02> <6587>\n<03> <6D4B>\nendbfchar\n"
    )
    font = (b"<< /Type /Font /Subtype /Type1 /BaseFont /SYNTH+Han"
            b" /ToUnicode 6 0 R >>")
    objects = {
        1: catalog(2),
        2: pages_node([3], 1),
        3: page(2, 7, resources=b"<< /Font << /F1 5 0 R /F2 6 0 R >> >>"),
        5: simple_font(),
        6: font,
        7: stream(b"BT /F1 12 Tf 72 700 Td (A) Tj 14 0 Td (B) Tj ET\n"
                  b"BT /F2 12 Tf 72 680 Td <01> Tj 14 0 Td <02> Tj 14 0 Td <03> Tj ET"),
        8: stream(cmap, compress=False),
    }
    # 字体对象引用 CMap 流 8
    objects[6] = (b"<< /Type /Font /Subtype /Type1 /BaseFont /SYNTH+Han"
                  b" /ToUnicode 8 0 R >>")
    return assemble(objects, root=1)


def font_without_encoding_pdf():
    """既没有 /Encoding 也没有 /ToUnicode 的字体：编码无从得知，必须如实标注。"""
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Mystery-Symbolic >>"
    objects = {
        1: catalog(2),
        2: pages_node([3], 1),
        3: page(2, 7),
        5: font,
        7: stream(b"BT /F1 12 Tf 72 700 Td (abcdef) Tj ET"),
    }
    return assemble(objects, root=1)


def form_xobject_pdf(self_referencing=False):
    """页眉文字放在**表单**（Form XObject）里的 PDF —— 不展开表单就整段丢失。

    ``self_referencing=True`` 时那个表单会 ``Do`` 自己：畸形但真实存在的写法，
    必须由深度上限挡住，而不是把递归栈撑爆。
    """
    header = b"BT /F1 10 Tf 72 760 Td (CONFIDENTIAL HEADER) Tj ET"
    if self_referencing:
        header += b"\n/Fm1 Do"
    body = b"BT /F1 12 Tf 72 700 Td (Body text of the page) Tj ET\n/Fm1 Do"
    objects = {
        1: catalog(2),
        2: pages_node([3], 1),
        3: page(2, 7, resources=b"<< /Font << /F1 5 0 R >>"
                                 b" /XObject << /Fm1 6 0 R >> >>"),
        5: simple_font(),
        6: stream(header, extra=b"/Type /XObject /Subtype /Form"
                                b" /BBox [0 0 612 792]"
                                b" /Resources << /Font << /F1 5 0 R >>"
                                b" /XObject << /Fm1 6 0 R >> >>"),
        7: stream(body),
    }
    return assemble(objects, root=1)
