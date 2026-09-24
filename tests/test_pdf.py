"""PDF 解析与文字提取的单元测试（全部基于合成 PDF，不依赖任何外部样本）。

这是本应用**核心算法**的那一层验证：每个样本的字节都是自己拼的，
所以能对「提取出的文本」做**逐字符**断言，而不是只看「有没有输出」。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from app import pdfparse  # noqa: E402
from tests import synth_pdf  # noqa: E402


class PdfTestCase(unittest.TestCase):
    """把合成 PDF 落到临时目录 —— 提取器只接受文件路径。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="shh14-pdf-")

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls.tmp, ignore_errors=True)

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path


class TextExtractionTest(PdfTestCase):
    def test_two_page_chinese_pdf_matches_expected_characters(self):
        """核心断言：提取出的文本**逐字符等于**预期（含中文 ToUnicode 映射）。"""
        data, expected = synth_pdf.chinese_two_page_pdf()
        path = self.write("chinese.pdf", data)
        result = pdfparse.extract(path)

        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["status"], "ok")
        for index, want in enumerate(expected):
            got = result["pages"][index]
            self.assertTrue(got["has_text"], "第 %d 页应当有文字层" % (index + 1))
            text = result["text"].split("\n\n")[index]
            self.assertEqual(text, want,
                             "第 %d 页文本不一致：\n得到 %r\n预期 %r" % (index + 1, text, want))

    def test_chinese_characters_are_real_unicode_not_mojibake(self):
        """ToUnicode 走通与否，用「有多少个 CJK 字符」直接量出来。"""
        data, _expected = synth_pdf.chinese_two_page_pdf()
        path = self.write("chinese-cjk.pdf", data)
        result = pdfparse.extract(path)
        cjk = [char for char in result["text"] if "一" <= char <= "鿿"]
        self.assertEqual("".join(cjk), "中文测试")
        self.assertNotIn("�", result["text"])
        self.assertEqual(result["stats"]["undecoded_fonts"], [])

    def test_bfrange_sequential_and_array_targets(self):
        """bfrange 的两种目标写法都要对：数组目标与「起始、结束、目标起始」。"""
        doc = pdfparse.Document.from_bytes(synth_pdf.chinese_two_page_pdf()[0])
        cmap_stream = doc.resolve(doc.trailer["Root"])
        self.assertTrue(cmap_stream)  # trailer 能解出来
        mapping, lengths = pdfparse.parse_tounicode(
            b"1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
            b"2 beginbfchar\n<0001> <4E2D>\n<0002> <6587>\nendbfchar\n"
            b"1 beginbfrange\n<0003> <0005> [<6D4B> <8BD5> <3002>]\nendbfrange\n"
            b"1 beginbfrange\n<0010> <0012> <0041>\nendbfrange\n"
        )
        self.assertEqual(mapping[0x0001], "中")
        self.assertEqual(mapping[0x0003], "测")
        self.assertEqual(mapping[0x0005], "。")
        self.assertEqual([mapping[code] for code in (0x0010, 0x0011, 0x0012)], ["A", "B", "C"])
        self.assertEqual(lengths, [2])

    def test_tj_kerning_inserts_space_and_small_kern_does_not(self):
        data, expected = synth_pdf.chinese_two_page_pdf()
        path = self.write("kern.pdf", data)
        result = pdfparse.extract(path, max_pages=2)
        self.assertIn("Alpha BetaGamma", result["text"])
        self.assertEqual(result["pages"][1]["chars"], len(expected[1]))

    def test_hex_string_and_literal_escapes(self):
        content = (b"BT /F1 12 Tf 72 700 Td (a\\(b\\)c\\\\d) Tj 0 -20 Td "
                   b"(tab\\tend) Tj 0 -20 Td (oct\\101l) Tj ET")
        objects = {
            1: synth_pdf.catalog(2),
            2: synth_pdf.pages_node([3], 1),
            3: synth_pdf.page(2, 7),
            5: synth_pdf.simple_font(),
            7: synth_pdf.stream(content),
        }
        path = self.write("escapes.pdf", synth_pdf.assemble(objects))
        result = pdfparse.extract(path)
        self.assertEqual(result["text"], "a(b)c\\d\ntab\tend\noctAl")

    def test_flate_and_uncompressed_streams_both_work(self):
        for compress in (True, False):
            objects = {
                1: synth_pdf.catalog(2),
                2: synth_pdf.pages_node([3], 1),
                3: synth_pdf.page(2, 7),
                5: synth_pdf.simple_font(),
                7: synth_pdf.stream(b"BT /F1 12 Tf 72 700 Td (payload) Tj ET",
                                    compress=compress),
            }
            path = self.write("flate-%s.pdf" % compress, synth_pdf.assemble(objects))
            self.assertEqual(pdfparse.extract(path)["text"], "payload")

    def test_array_contents_are_concatenated_in_order(self):
        data, expected = synth_pdf.array_contents_pdf()
        path = self.write("array-contents.pdf", data)
        result = pdfparse.extract(path)
        self.assertEqual(result["text"], expected)

    def test_document_metadata_is_read(self):
        objects = {
            1: synth_pdf.catalog(2),
            2: synth_pdf.pages_node([3], 1),
            3: synth_pdf.page(2, 7),
            5: synth_pdf.simple_font(),
            7: synth_pdf.stream(b"BT /F1 12 Tf 72 700 Td (x) Tj ET"),
            8: b"<< /Producer (Synth Writer 1.0) /Title (A Test Document) >>",
        }
        path = self.write("meta.pdf",
                          synth_pdf.assemble(objects, extra_trailer=b"/Info 8 0 R"))
        result = pdfparse.extract(path)
        self.assertEqual(result["metadata"]["producer"], "Synth Writer 1.0")
        self.assertEqual(result["metadata"]["title"], "A Test Document")


class EdgeCaseTest(PdfTestCase):
    def test_missing_startxref_still_extracts(self):
        """缺 startxref 的损坏文件：线性扫描兜底，照样提取。"""
        path = self.write("no-startxref.pdf", synth_pdf.broken_pdf())
        result = pdfparse.extract(path)
        self.assertEqual(result["text"], "Recovered from a broken file")
        self.assertEqual(result["page_count"], 1)

    def test_missing_xref_table_still_extracts(self):
        """连 xref 表都没有：这正是**不依赖 xref** 的价值所在。"""
        path = self.write("no-xref.pdf", synth_pdf.broken_pdf(no_xref=True))
        result = pdfparse.extract(path)
        self.assertEqual(result["text"], "Recovered from a broken file")

    def test_incremental_update_takes_last_definition(self):
        """同一个对象号有两个定义时，取**最后一个**（增量更新的语义）。"""
        data = synth_pdf.broken_pdf()
        extra = synth_pdf.stream(b"BT /F1 12 Tf 72 700 Td (updated version) Tj ET")
        appended = (data + b"\n7 0 obj\n" + extra + b"\nendobj\n")
        path = self.write("incremental.pdf", appended)
        result = pdfparse.extract(path)
        self.assertEqual(result["text"], "updated version")

    def test_scanned_page_reports_no_text_layer(self):
        """只有 DCTDecode 图像的页：报「无文字层」，**不是**报错。"""
        path = self.write("scanned.pdf", synth_pdf.scanned_pdf())
        result = pdfparse.extract(path)
        self.assertEqual(result["page_count"], 1)
        self.assertFalse(result["pages"][0]["has_text"])
        self.assertEqual(result["pages"][0]["image_count"], 1)
        self.assertEqual(result["status"], "no-text-layer")
        self.assertEqual(result["text"], "")

    def test_image_only_contents_stream_is_reported_not_raised(self):
        path = self.write("image-contents.pdf", synth_pdf.image_only_contents_pdf())
        result = pdfparse.extract(path)
        self.assertTrue(result["pages"][0]["image_only"])
        self.assertEqual(result["status"], "no-text-layer")

    def test_encrypted_pdf_raises_specific_error(self):
        path = self.write("encrypted.pdf", synth_pdf.encrypted_pdf())
        with self.assertRaises(pdfparse.PdfEncrypted) as caught:
            pdfparse.extract(path)
        self.assertIn("加密", str(caught.exception))

    def test_encrypted_probe_reports_flag_without_raising(self):
        path = self.write("encrypted-probe.pdf", synth_pdf.encrypted_pdf())
        info = pdfparse.probe(path)
        self.assertTrue(info["encrypted"])
        self.assertFalse(info["has_text_layer"])
        self.assertEqual(info["page_count"], 0)

    def test_empty_pdf_has_zero_pages(self):
        path = self.write("empty.pdf", synth_pdf.empty_pdf())
        result = pdfparse.extract(path)
        self.assertEqual(result["page_count"], 0)
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["text"], "")

    def test_page_without_contents_is_still_a_page(self):
        objects = {
            1: synth_pdf.catalog(2),
            2: synth_pdf.pages_node([3], 1),
            3: synth_pdf.page(2, None),
        }
        path = self.write("blank.pdf", synth_pdf.assemble(objects))
        result = pdfparse.extract(path)
        self.assertEqual(result["page_count"], 1)
        self.assertFalse(result["pages"][0]["has_text"])
        self.assertEqual(result["status"], "empty")

    def test_not_a_pdf_is_rejected_clearly(self):
        path = self.write("fake.pdf", b"this is plainly not a PDF at all\n")
        with self.assertRaises(pdfparse.UnsupportedPdf) as caught:
            pdfparse.extract(path)
        self.assertIn("%PDF-", str(caught.exception))

    def test_empty_file_is_rejected_clearly(self):
        path = self.write("zero.pdf", b"")
        with self.assertRaises(pdfparse.UnsupportedPdf):
            pdfparse.extract(path)

    def test_truncated_file_raises_readable_error(self):
        """截断到一半：要么提出部分文字，要么给出可读错误 —— 绝不能崩。"""
        data, _expected = synth_pdf.chinese_two_page_pdf()
        path = self.write("truncated.pdf", data[:len(data) // 3])
        try:
            result = pdfparse.extract(path)
        except pdfparse.PdfError as exc:
            self.assertTrue(str(exc))  # 错误信息非空即可读
        else:
            self.assertIsInstance(result["text"], str)

    def test_corrupt_content_stream_keeps_what_was_extracted(self):
        """内容流中途损坏：已提取的部分要保留，不是整页丢弃。"""
        objects = {
            1: synth_pdf.catalog(2),
            2: synth_pdf.pages_node([3], 1),
            3: synth_pdf.page(2, 7),
            5: synth_pdf.simple_font(),
            7: synth_pdf.stream(b"BT /F1 12 Tf 72 700 Td (good text) Tj ET\n"
                                b"BT /F1 12 Tf 72 680 Td ((unterminated"),
        }
        path = self.write("corrupt-content.pdf", synth_pdf.assemble(objects))
        result = pdfparse.extract(path)
        self.assertIn("good text", result["text"])

    def test_partially_corrupt_stream_is_salvaged(self):
        """尾部损坏的 FlateDecode 流：标题/正文在流的前面，应当被抢回来。

        实测真实文件里就有这种（zlib 头正常、收尾报 invalid distance too far back）：
        整块喂给 zlib 颗粒无收，按小块喂能抢回大部分正文。
        """
        import zlib

        unit = b"BT /F1 12 Tf 72 700 Td (salvaged text) Tj ET"
        payload = (unit + bytes([10])) * 200
        packed = zlib.compress(payload)
        # 砍掉尾巴 20%：解到中途就会坏，但前面那些**完整的**子句已经解出来了。
        # （砍一半就只剩 23 字节，连一个完整的 `Tj ET` 都不够 —— 那时救不回来是正确的。）
        broken = packed[:int(len(packed) * 0.8)]
        objects = {
            1: synth_pdf.catalog(2),
            2: synth_pdf.pages_node([3], 1),
            3: synth_pdf.page(2, 7),
            5: synth_pdf.simple_font(),
            7: synth_pdf.raw_stream(broken, b"/Filter /FlateDecode"),
        }
        path = self.write("half-corrupt.pdf", synth_pdf.assemble(objects))
        result = pdfparse.extract(path)
        self.assertIn("salvaged text", result["text"])

    def test_max_chars_truncation_is_flagged(self):
        objects = {
            1: synth_pdf.catalog(2),
            2: synth_pdf.pages_node([3], 1),
            3: synth_pdf.page(2, 7),
            5: synth_pdf.simple_font(),
            7: synth_pdf.stream(b"BT /F1 12 Tf 72 700 Td (%s) Tj ET" % (b"x" * 500)),
        }
        path = self.write("long.pdf", synth_pdf.assemble(objects))
        result = pdfparse.extract(path, max_chars=100)
        self.assertTrue(result["stats"]["truncated"])
        self.assertEqual(len(result["text"]), 100)

    def test_probe_reports_pages_and_text_layer(self):
        data, _expected = synth_pdf.chinese_two_page_pdf()
        path = self.write("probe.pdf", data)
        info = pdfparse.probe(path)
        self.assertEqual(info["page_count"], 2)
        self.assertTrue(info["has_text_layer"])
        self.assertGreater(info["first_page_chars"], 0)
        self.assertTrue(info["format"].startswith("PDF 1."))


class ModernPdfTest(PdfTestCase):
    """PDF 1.5+ 的现代形态：对象流（ObjStm）与 xref 流。

    Chrome / Word / WPS 导出的文件基本都长这样 —— 页树和字体全在**压缩对象流**里，
    trailer 换成**交叉引用流**。只会线性扫 ``N 0 obj`` 的解析器在这些文件上直接失败
    （实测：本机 9 个真实 PDF 里有 3 个因此完全提不出字）。
    """

    def test_object_stream_and_xref_stream_are_understood(self):
        data, expected = synth_pdf.object_stream_pdf()
        path = self.write("modern.pdf", data)
        result = pdfparse.extract(path)
        self.assertEqual(result["page_count"], 1)
        self.assertEqual(result["text"], expected)

    def test_xref_stream_supplies_the_root(self):
        """``/Root`` 只在 xref 流的字典里 —— 没有 trailer 关键字也要能找到目录。"""
        data, _expected = synth_pdf.object_stream_pdf()
        doc = pdfparse.Document.from_bytes(data)
        self.assertNotIn(b"trailer", data)
        self.assertIn("Root", doc.trailer)
        self.assertEqual(str(doc.catalog().get("Type")), "Catalog")

    def test_packed_objects_are_counted(self):
        data, _expected = synth_pdf.object_stream_pdf()
        doc = pdfparse.Document.from_bytes(data)
        # 4 个对象在对象流里，1 个内容流 + 1 个 xref 流在流外面
        self.assertEqual(len(doc._packed), 4)
        # 4 个打包对象 + 对象流自己 + 内容流 + xref 流 = 7
        self.assertEqual(doc.object_count(), 7)

    def test_broken_xref_stream_is_survived(self):
        """xref 流的二进制内容被截断：字典已经读到，照样能提取。"""
        data, expected = synth_pdf.object_stream_pdf()
        truncated = data[:len(data) - 40]
        path = self.write("modern-truncated.pdf", truncated)
        try:
            result = pdfparse.extract(path)
        except pdfparse.PdfError as exc:
            self.assertTrue(str(exc))
        else:
            self.assertEqual(result["text"], expected)


class EncodingQuirkTest(PdfTestCase):
    """真实文件里最容易把中文解错的三处写法。"""

    def test_simple_font_with_two_byte_codespace_uses_one_byte_codes(self):
        """简单字体的 codespace 写着两字节，也**必须**按单字节切。

        照 codespace 切会让整串文字错位（现实中表现为「中文里混进 ç、有些字整段消失」）。
        """
        path = self.write("codespace.pdf",
                          synth_pdf.simple_font_with_two_byte_codespace_pdf())
        result = pdfparse.extract(path)
        self.assertEqual(result["text"], "Hello")

    def test_kangxi_radical_codepoints_are_normalized(self):
        """康熙部首码位要归一化成规范汉字（否则看着对、搜不到）。"""
        path = self.write("radical.pdf", synth_pdf.radical_glyph_pdf())
        result = pdfparse.extract(path)
        # bfchar 那条映射到康熙部首 U+2F46（→ 无）；bfrange 那条逐码递增（→ 无、旡）
        self.assertEqual(result["text"], "无无旡")
        self.assertNotIn("⽆", result["text"])

    def test_no_spaces_between_cjk_glyphs(self):
        """逐字定位的中文不该被塞进空格；同规则的西文该保留空格。"""
        path = self.write("per-glyph.pdf", synth_pdf.per_glyph_cjk_pdf())
        result = pdfparse.extract(path)
        self.assertIn("中文测", result["text"])
        self.assertNotIn("中 文", result["text"])
        self.assertIn("A B", result["text"])

    def test_font_without_any_encoding_is_flagged(self):
        """既没 /Encoding 也没 /ToUnicode：编码只能猜，必须如实标注。"""
        path = self.write("mystery-font.pdf", synth_pdf.font_without_encoding_pdf())
        result = pdfparse.extract(path)
        self.assertTrue(result["stats"]["undecoded_fonts"])
        warnings = result["pages"][0]["warnings"]
        self.assertTrue(any("编码" in item for item in warnings), warnings)


class FormXObjectTest(PdfTestCase):
    """表单（Form XObject）里的文字也要提取 —— 页眉页脚、水印常放在里面。"""

    def test_text_inside_form_is_extracted(self):
        path = self.write("form.pdf", synth_pdf.form_xobject_pdf())
        result = pdfparse.extract(path)
        self.assertIn("CONFIDENTIAL HEADER", result["text"])
        self.assertIn("Body text of the page", result["text"])
        self.assertTrue(result["pages"][0]["has_text"])

    def test_self_referencing_form_does_not_hang(self):
        """表单 ``Do`` 自己：由深度上限与已访问集合挡住，不能无限递归。

        这个用例的意义是「有界」而不是「提取正确」—— 拿不到文字可以接受，
        挂死、爆栈、把同一段文字重复几百遍都不可以。
        """
        path = self.write("form-self.pdf",
                          synth_pdf.form_xobject_pdf(self_referencing=True))
        result = pdfparse.extract(path)
        self.assertLessEqual(result["text"].count("CONFIDENTIAL HEADER"), 2)
        self.assertIn("Body text of the page", result["text"])


class StreamFilterTest(unittest.TestCase):
    """过滤器单独测 —— 这些是纯函数，用构造数据最直接。"""

    def test_asciihex(self):
        self.assertEqual(pdfparse._asciihex_decode(b"48 65 6C 6C 6F>"), b"Hello")

    def test_ascii85(self):
        """用标准库 base64 的 a85 编码做往返校验（自造向量容易连错一起写错）。"""
        import base64

        for payload in (b"Hello world", b"\x00\x01\x02\x03\x04", b"short"):
            encoded = base64.a85encode(payload) + b"~>"
            self.assertEqual(pdfparse._ascii85_decode(encoded), payload)

    def test_runlength(self):
        # 0x02 → 后面 3 个字面字节；0xFE → 257-254 = 3 个重复字节
        self.assertEqual(pdfparse._runlength_decode(b"\x02abc\xfeZ\x80"),
                         b"abcZZZ")

    def test_png_predictor_up_is_undone(self):
        # 两行，行宽 3，filter=2（Up）：第二行是「原值 - 上一行」的差
        data = b"\x02\x01\x02\x03" + b"\x02\x01\x01\x01"
        result = pdfparse._apply_predictor(data, {"Predictor": 12, "Columns": 3,
                                                  "Colors": 1, "BitsPerComponent": 8})
        self.assertEqual(result, b"\x01\x02\x03\x02\x03\x04")

    def test_flate_with_leading_garbage(self):
        import zlib

        packed = b"\n\n" + zlib.compress(b"hello flate")
        self.assertEqual(pdfparse._inflate(packed), b"hello flate")

    def test_raw_deflate_fallback(self):
        import zlib

        engine = zlib.compressobj(9, zlib.DEFLATED, -15)
        raw = engine.compress(b"raw deflate payload") + engine.flush()
        self.assertEqual(pdfparse._inflate(raw), b"raw deflate payload")

    def test_image_filter_raises_image_only(self):
        with self.assertRaises(pdfparse.ImageOnly):
            pdfparse._apply_filter("DCTDecode", b"\xff\xd8", None)

    def test_unknown_filter_raises_unsupported(self):
        with self.assertRaises(pdfparse.UnsupportedPdf):
            pdfparse._apply_filter("NoSuchFilter", b"", None)


class SimpleFontEncodingTest(unittest.TestCase):
    def _extract(self, content, font_body):
        import tempfile

        objects = {
            1: synth_pdf.catalog(2),
            2: synth_pdf.pages_node([3], 1),
            3: synth_pdf.page(2, 7),
            5: font_body,
            7: synth_pdf.stream(content),
        }
        data = synth_pdf.assemble(objects)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            handle.write(data)
            path = handle.name
        try:
            return pdfparse.extract(path)["text"]
        finally:
            os.unlink(path)

    def test_winansi_high_bytes_use_cp1252(self):
        # cp1252 里 0x93/0x94 是弯引号；latin-1 会解成控制字符 —— 这一条把两者区分开
        text = self._extract(b"BT /F1 12 Tf 72 700 Td (\x93quoted\x94) Tj ET",
                             synth_pdf.simple_font())
        self.assertEqual(text, "“quoted”")

    def test_differences_glyph_names_are_mapped(self):
        font = (b"<< /Type /Font /Subtype /Type1 /BaseFont /Synth"
                b" /Encoding << /BaseEncoding /WinAnsiEncoding"
                b" /Differences [65 /eacute /Euro /emdash] >> >>")
        # 65/66/67 三个码位被 Differences 改写成 é € —（不再是 WinAnsi 的 A B C）
        text = self._extract(b"BT /F1 12 Tf 72 700 Td (x\x41\x42\x43Z) Tj ET", font)
        self.assertEqual(text, "xé€—Z")

    def test_missing_font_object_falls_back_to_bytes(self):
        """字体对象被指向一个不存在的号码：不能崩，按字节出。"""
        text = self._extract(b"BT /F1 12 Tf 72 700 Td (plain) Tj ET",
                             synth_pdf.simple_font())
        self.assertEqual(text, "plain")


class Type0WithoutToUnicodeTest(PdfTestCase):
    def test_composite_font_without_tounicode_is_marked_undecoded(self):
        """复合字体缺 ToUnicode：诚实标注，而不是输出一段看着像字的乱码。"""
        objects = {
            1: synth_pdf.catalog(2),
            2: synth_pdf.pages_node([3], 1),
            3: synth_pdf.page(2, 7, resources=b"<< /Font << /F1 6 0 R >> >>"),
            6: b"<< /Type /Font /Subtype /Type0 /BaseFont /Synth /Encoding /Identity-H"
               b" /DescendantFonts [8 0 R] >>",
            8: synth_pdf.cid_font(9),
            9: synth_pdf.font_descriptor(),
            7: synth_pdf.stream(b"BT /F1 12 Tf 72 700 Td <00010002> Tj ET"),
        }
        path = self.write("no-tounicode.pdf", synth_pdf.assemble(objects))
        result = pdfparse.extract(path)
        self.assertEqual(result["stats"]["undecoded_fonts"], ["F1"])
        self.assertTrue(result["pages"][0]["warnings"])
        self.assertIn("ToUnicode", result["pages"][0]["warnings"][0])


if __name__ == "__main__":
    unittest.main()
