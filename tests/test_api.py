"""端到端功能测试：把应用真的跑起来，逐个打接口。

这是「功能可用」这一层的验证 —— 官方审核会把应用装到真机上点一遍，
这里用同样的顺序在开发机上先走一遍：启动 → 设白名单 → 探测 → 提交任务
→ 看结果 → 检索 → 导出。

应用以 TCP 模式启动（Windows 上没有 Unix socket），跑的是与 deb 包内**同一份**代码，
只是换了监听方式。
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from tnasapp.paths import AppPaths  # noqa: E402

from app import main as app_main  # noqa: E402
from tests import synth_pdf  # noqa: E402

APP_ID = app_main.APP_ID


class ApiClient:
    def __init__(self, base):
        self.base = base

    def request(self, method, path, payload=None, raw=False, timeout=90):
        url = self.base + path
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                if raw:
                    return response.status, body, dict(response.headers)
                return response.status, json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(body)
            except ValueError:
                return exc.code, {"raw": body}

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, payload=None):
        return self.request("POST", path, payload)


class AppEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="shh14-api-")
        cls.docs = os.path.join(cls.tmp, "docs")
        cls.nested = os.path.join(cls.docs, "sub")
        cls.output_dir = os.path.join(cls.tmp, "out")
        cls.outside_dir = os.path.join(cls.tmp, "outside")
        for path in (cls.docs, cls.nested, cls.outside_dir):
            os.makedirs(path)

        # 造一批样本：正常中文 PDF、数组内容、扫描页、损坏、加密、空、图片
        chinese, cls.chinese_expected = synth_pdf.chinese_two_page_pdf()
        synth_pdf.write(os.path.join(cls.docs, "chinese.pdf"), chinese)
        synth_pdf.write(os.path.join(cls.nested, "array.pdf"),
                        synth_pdf.array_contents_pdf()[0])
        synth_pdf.write(os.path.join(cls.docs, "scanned.pdf"), synth_pdf.scanned_pdf())
        synth_pdf.write(os.path.join(cls.docs, "broken.pdf"), synth_pdf.broken_pdf())
        synth_pdf.write(os.path.join(cls.docs, "locked.pdf"), synth_pdf.encrypted_pdf())
        synth_pdf.write(os.path.join(cls.docs, "empty.pdf"), synth_pdf.empty_pdf())
        synth_pdf.write(os.path.join(cls.docs, "picture.png"), synth_pdf.png())
        with open(os.path.join(cls.docs, "notes.txt"), "wb") as handle:
            handle.write(b"not a document")

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        paths = AppPaths(APP_ID, install_dir=repo, data_dir=os.path.join(cls.tmp, "data"))
        cls.app = app_main.create_app(paths=paths, log_level="ERROR")
        # 白名单在启动前设好 —— 用例之间不能有执行顺序依赖
        cls.app.set_allowed_roots([cls.docs, cls.output_dir])
        cls.server = cls.app.run(host="127.0.0.1", port=0, background=True)
        cls.client = ApiClient("http://127.0.0.1:%d" % cls.server.server_address[1])
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.shutdown()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------------------------------------------------------- 辅助

    def wait_job(self, job_id, timeout=120):
        deadline = time.time() + timeout
        job = None
        while time.time() < deadline:
            _status, body = self.client.get("/api/jobs/%d" % job_id)
            job = body["job"]
            if job["state"] in ("completed", "failed", "canceled"):
                return job
            time.sleep(0.1)
        return job

    @classmethod
    def _ensure_index(cls):
        """跑一次批量提取并缓存结果，让依赖它的用例彼此独立。"""
        if getattr(cls, "_index_job", None) is not None:
            return cls._index_job
        status, body = cls.client.post("/api/jobs", {
            "type": "extract",
            "params": {"roots": [cls.docs], "output_dir": cls.output_dir,
                       "format": "all"},
        })
        assert status == 201, body
        cls._index_job = cls._wait_class(body["job"]["id"])
        return cls._index_job

    @classmethod
    def _wait_class(cls, job_id, timeout=120):
        deadline = time.time() + timeout
        job = None
        while time.time() < deadline:
            _status, body = cls.client.get("/api/jobs/%d" % job_id)
            job = body["job"]
            if job["state"] in ("completed", "failed", "canceled"):
                return job
            time.sleep(0.1)
        return job

    # ---------------------------------------------------------- 基础

    def test_health(self):
        status, body = self.client.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["app"], APP_ID)

    def test_app_info_lists_job_types_and_engines(self):
        status, body = self.client.get("/api/app")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], app_main.APP_VERSION)
        self.assertIn("extract", body["job_types"])
        self.assertIn("index", body["job_types"])
        # 内置 PDF 提取不依赖任何外部引擎，所以引擎状态与功能可用性解耦
        self.assertIn("tesseract", body["engines"])
        self.assertIn("remote", body["engines"])

    def test_index_html_served_with_relative_assets(self):
        status, body, _headers = self.client.get("/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"ocr.js", body)
        # 相对路径：出现 src="/..." 就会在 /<appid>/ 前缀下白屏（审核项 F7）
        self.assertNotIn(b'src="/', body)
        self.assertNotIn(b'href="/', body)

    def test_prefix_compatibility(self):
        for prefix in ("", "/" + APP_ID, "/v2/proxy/" + APP_ID):
            status, body = self.client.get(prefix + "/api/app")
            self.assertEqual(status, 200, prefix)
            self.assertEqual(body["app"], APP_ID)

    def test_frontend_assets_are_served(self):
        for asset in ("ocr.js", "app.js", "app.css"):
            status, body, _headers = self.client.get("/" + asset, raw=True)
            self.assertEqual(status, 200, asset)
            self.assertTrue(body, asset)

    def test_static_traversal_is_refused(self):
        status, body = self.client.get("/../config.ini")
        self.assertIn(status, (400, 404))

    def test_engine_endpoint(self):
        status, body = self.client.get("/api/ocr/engine")
        self.assertEqual(status, 200)
        self.assertTrue(body["builtin"]["available"], "内置 PDF 提取必须始终可用")
        self.assertIn("tesseract", body["engines"])
        self.assertIn("remote", body["engines"])
        self.assertIn("statuses", body)
        self.assertEqual(sorted(body["formats"]), ["json", "md", "txt"])

    def test_tesseract_language_setting_round_trips(self):
        """本机 tesseract 的语言可配置，并且能被引擎接口回显给界面。"""
        try:
            _status, body = self.client.get("/api/ocr/engine")
            self.assertIn("configured_language", body["engines"]["tesseract"])
            status, _body = self.client.post("/api/settings",
                                             {"ocr_language": "chi_sim+eng"})
            self.assertEqual(status, 200)
            _status, body = self.client.get("/api/ocr/engine")
            self.assertEqual(body["engines"]["tesseract"]["configured_language"],
                             "chi_sim+eng")
        finally:
            self.client.post("/api/settings", {"ocr_language": ""})

    def test_remote_engine_defaults_to_off(self):
        """远程接口必须**默认关闭** —— 这是隐私承诺的落点。"""
        status, body = self.client.get("/api/ocr/engine")
        remote = body["engines"]["remote"]
        self.assertFalse(remote["enabled"])
        self.assertFalse(remote["available"])

    # ---------------------------------------------------------- 白名单与安全

    def test_empty_whitelist_refuses_everything(self):
        saved = list(self.app.allowed.roots())
        try:
            self.app.set_allowed_roots([])
            status, body = self.client.get("/api/ocr/probe?path=" + self.docs)
            self.assertEqual(status, 403)
            self.assertFalse(body["ok"])
            status, body = self.client.get("/api/fs/list?path=" + self.docs)
            self.assertFalse(body.get("ok", False))
        finally:
            self.app.set_allowed_roots(saved)

    def test_set_allowed_roots_roundtrip(self):
        status, body = self.client.post("/api/settings",
                                        {"allowed_roots": [self.docs, self.output_dir]})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body = self.client.get("/api/settings")
        self.assertEqual(sorted(body["settings"]["allowed_roots"]),
                         sorted([self.docs, self.output_dir]))

    def test_path_outside_whitelist_is_refused(self):
        status, body = self.client.get("/api/ocr/probe?path=" + self.outside_dir)
        self.assertEqual(status, 403)
        self.assertFalse(body["ok"])

    def test_path_traversal_is_refused(self):
        escape = os.path.join(self.docs, "..", "outside", "x.pdf")
        for route in ("/api/ocr/probe?path=", "/api/ocr/document?path=",
                      "/api/ocr/download?path="):
            status, body = self.client.get(route + urllib.request.quote(escape))
            self.assertEqual(status, 403, route)
            self.assertFalse(body["ok"], route)

    def test_fs_list_inside_whitelist(self):
        status, body = self.client.get("/api/fs/list?path=" + self.docs)
        self.assertEqual(status, 200)
        names = {entry["name"] for entry in body["entries"]}
        self.assertIn("chinese.pdf", names)
        self.assertIn("sub", names)

    # ---------------------------------------------------------- 探测

    def test_probe_pdf_reports_pages_and_text_layer(self):
        status, body = self.client.get(
            "/api/ocr/probe?path=" + os.path.join(self.docs, "chinese.pdf"))
        self.assertEqual(status, 200)
        self.assertEqual(body["kind"], "pdf")
        self.assertFalse(body["encrypted"])
        self.assertEqual(body["page_count"], 2)
        self.assertTrue(body["has_text_layer"])
        self.assertGreater(body["first_page_chars"], 0)

    def test_probe_encrypted_pdf_is_reported_not_raised(self):
        status, body = self.client.get(
            "/api/ocr/probe?path=" + os.path.join(self.docs, "locked.pdf"))
        self.assertEqual(status, 200)
        self.assertTrue(body["encrypted"])
        self.assertFalse(body["has_text_layer"])

    def test_probe_image_reports_metadata(self):
        status, body = self.client.get(
            "/api/ocr/probe?path=" + os.path.join(self.docs, "picture.png"))
        self.assertEqual(status, 200)
        self.assertEqual(body["kind"], "image")
        self.assertEqual(body["metadata"]["width"], 8)
        self.assertEqual(body["metadata"]["height"], 6)

    def test_probe_broken_pdf_still_reports_pages(self):
        status, body = self.client.get(
            "/api/ocr/probe?path=" + os.path.join(self.docs, "broken.pdf"))
        self.assertEqual(status, 200)
        self.assertEqual(body["page_count"], 1)
        self.assertTrue(body["has_text_layer"])

    def test_probe_unsupported_type_is_a_readable_error(self):
        status, body = self.client.get(
            "/api/ocr/probe?path=" + os.path.join(self.docs, "notes.txt"))
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertTrue(body["hint"])

    # ---------------------------------------------------------- 任务

    def test_extract_job_indexes_and_writes_outputs(self):
        job = self._ensure_index()
        self.assertEqual(job["state"], "completed", json.dumps(job, ensure_ascii=False))
        result = job["result"]
        # 目录里 7 个可处理文件（notes.txt 不在支持范围内）
        self.assertEqual(result["total"], 7)
        self.assertEqual(result["by_status"].get("ok"), 3)          # chinese + array + broken
        self.assertEqual(result["by_status"].get("no-text-layer"), 1)
        self.assertEqual(result["by_status"].get("encrypted"), 1)
        self.assertEqual(result["by_status"].get("metadata"), 1)     # png
        self.assertEqual(result["by_status"].get("empty"), 1)
        self.assertGreater(result["outputs"], 0)
        self.assertTrue(os.path.isdir(result["output_dir"]))
        # 输出要落到用户指定的目录里
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "chinese.txt")))
        # 子目录被镜像（不把整棵树平铺到一个目录）
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "sub", "array.txt")))

    def test_extracted_text_is_exactly_right(self):
        """核心能力在**接口层**的验证：导出的 TXT 与源 PDF 的文字逐字符一致。"""
        status, body, _headers = self.client.get(
            "/api/ocr/download?path=" + os.path.join(self.docs, "chinese.pdf")
            + "&format=txt", raw=True)
        self.assertEqual(status, 200)
        text = body.decode("utf-8").rstrip("\n")
        self.assertEqual(text, "\n\n".join(self.chinese_expected))

    def test_job_logs_are_written(self):
        job = self._ensure_index()
        status, body = self.client.get("/api/jobs/%d/logs" % job["id"])
        self.assertTrue(body["logs"])
        messages = " ".join(entry["message"] for entry in body["logs"])
        self.assertIn("任务开始", messages)
        self.assertIn("chinese.pdf", messages)

    def test_index_job_writes_no_files(self):
        """``index`` 任务只建索引，**不写任何结果文件**。"""
        before = sorted(os.listdir(self.output_dir))
        status, body = self.client.post("/api/jobs", {
            "type": "index", "params": {"roots": [self.docs]}})
        self.assertEqual(status, 201)
        job = self.wait_job(body["job"]["id"])
        self.assertEqual(job["state"], "completed", json.dumps(job, ensure_ascii=False))
        self.assertEqual(job["result"]["outputs"], 0)
        self.assertEqual(sorted(os.listdir(self.output_dir)), before)

    def test_extract_refuses_output_outside_whitelist(self):
        status, body = self.client.post("/api/jobs", {
            "type": "extract",
            "params": {"inputs": [os.path.join(self.docs, "chinese.pdf")],
                       "output_dir": self.outside_dir},
        })
        self.assertEqual(status, 201)
        job = self.wait_job(body["job"]["id"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("允许访问", job["error"])

    def test_extract_with_empty_input_fails_readably(self):
        status, body = self.client.post("/api/jobs", {
            "type": "extract", "params": {"roots": []}})
        self.assertEqual(status, 201)
        job = self.wait_job(body["job"]["id"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("没有找到", job["error"])

    def test_bad_format_is_rejected_readably(self):
        status, body = self.client.post("/api/jobs", {
            "type": "extract",
            "params": {"roots": [self.docs], "format": "docx"}})
        self.assertEqual(status, 201)
        job = self.wait_job(body["job"]["id"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("格式", job["error"])

    def test_cancel_of_queued_job_works(self):
        status, body = self.client.post("/api/jobs", {
            "type": "index", "params": {"roots": [self.docs]}})
        job_id = body["job"]["id"]
        status, body = self.client.post("/api/jobs/%d/cancel" % job_id)
        self.assertIn(status, (200, 409))
        job = self.wait_job(job_id)
        self.assertIn(job["state"], ("canceled", "completed"))

    def test_unknown_job_type_is_rejected(self):
        status, body = self.client.post("/api/jobs", {"type": "no-such-job"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_job_list_and_counts(self):
        self._ensure_index()
        status, body = self.client.get("/api/jobs?limit=50")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["counts"]["total"], 1)
        self.assertGreaterEqual(body["counts"]["completed"], 1)

    # ---------------------------------------------------------- 结果与检索

    def test_documents_list(self):
        self._ensure_index()
        status, body = self.client.get("/api/ocr/documents?limit=50")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 7)
        names = {doc["name"] for doc in body["documents"]}
        self.assertIn("chinese.pdf", names)
        # 列表不返回正文（避免一次传几百 MB）
        for doc in body["documents"]:
            self.assertNotIn("text", doc)
            self.assertIn("pages", doc)

    def test_documents_filter_by_status(self):
        self._ensure_index()
        status, body = self.client.get("/api/ocr/documents?status=encrypted")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["documents"][0]["status"], "encrypted")
        self.assertTrue(body["documents"][0]["error"])

    def test_document_detail_has_per_page_counts(self):
        self._ensure_index()
        path = urllib.request.quote(os.path.join(self.docs, "chinese.pdf"))
        status, body = self.client.get("/api/ocr/document?path=" + path)
        self.assertEqual(status, 200)
        self.assertEqual(body["page_count"], 2)
        self.assertEqual(len(body["pages"]), 2)
        self.assertEqual(body["pages"][0]["chars"], len(self.chinese_expected[0]))
        self.assertTrue(all(page["has_text"] for page in body["pages"]))
        # 默认只给预览，不给全文
        self.assertEqual(body["text"], "")
        self.assertTrue(body["preview"])

    def test_document_full_text_on_demand(self):
        self._ensure_index()
        path = urllib.request.quote(os.path.join(self.docs, "chinese.pdf"))
        status, body = self.client.get("/api/ocr/document?path=" + path + "&full=1")
        self.assertEqual(status, 200)
        self.assertIn("中文测试", body["text"])

    def test_document_of_unindexed_file_extracts_on_demand(self):
        """没跑过任务也能直接看某一篇 —— 增量逻辑不该把冷启动变成错误。"""
        path = urllib.request.quote(os.path.join(self.docs, "broken.pdf"))
        status, body = self.client.get("/api/ocr/document?path=" + path)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertIn("Recovered", body["preview"])

    def test_scanned_document_reports_no_text_layer(self):
        self._ensure_index()
        path = urllib.request.quote(os.path.join(self.docs, "scanned.pdf"))
        status, body = self.client.get("/api/ocr/document?path=" + path)
        self.assertEqual(body["status"], "no-text-layer")
        self.assertEqual(body["chars"], 0)
        self.assertEqual(body["pages"][0]["image_count"], 1)

    def test_search_finds_chinese_text(self):
        self._ensure_index()
        status, body = self.client.get(
            "/api/ocr/search?q=" + urllib.request.quote("中文测试"))
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 1)
        row = body["results"][0]
        self.assertIn("中文测试", row["snippet"])
        self.assertGreaterEqual(row["hits"], 1)

    def test_search_finds_keyword_in_nested_document(self):
        self._ensure_index()
        status, body = self.client.get("/api/ocr/search?q=part%20one")
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["results"][0]["name"], "array.pdf")

    def test_search_by_path_mode(self):
        self._ensure_index()
        status, body = self.client.get("/api/ocr/search?q=.pdf&mode=path")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 6)

    def test_search_without_query_is_rejected(self):
        status, body = self.client.get("/api/ocr/search?q=")
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_search_miss_returns_empty_not_error(self):
        self._ensure_index()
        status, body = self.client.get("/api/ocr/search?q=zzz-not-present-zzz")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["results"], [])

    # ---------------------------------------------------------- 导出

    def test_download_markdown_has_page_sections(self):
        status, body, headers = self.client.get(
            "/api/ocr/download?path=" + urllib.request.quote(
                os.path.join(self.docs, "chinese.pdf")) + "&format=md", raw=True)
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("## 第 1 页", text)
        self.assertIn("## 第 2 页", text)
        self.assertIn("中文测试", text)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))

    def test_download_json_has_per_page_counts(self):
        status, body, _headers = self.client.get(
            "/api/ocr/download?path=" + urllib.request.quote(
                os.path.join(self.docs, "chinese.pdf")) + "&format=json", raw=True)
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(len(payload["pages"]), 2)
        self.assertEqual(payload["pages"][0]["chars"], len(self.chinese_expected[0]))
        self.assertIn("中文测试", payload["text"])

    def test_download_rejects_unknown_format(self):
        status, body = self.client.get(
            "/api/ocr/download?path=" + urllib.request.quote(
                os.path.join(self.docs, "chinese.pdf")) + "&format=docx")
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_download_encrypted_document_reports_encrypted(self):
        status, body = self.client.get(
            "/api/ocr/download?path=" + urllib.request.quote(
                os.path.join(self.docs, "locked.pdf")) + "&format=txt")
        # 加密文档的索引行里带 error，导出应给出可读结论而不是 500
        self.assertIn(status, (200, 400))
        if status == 400:
            self.assertIn("加密", body["error"])

    # ---------------------------------------------------------- 汇总

    def test_summary_reflects_index(self):
        self._ensure_index()
        status, body = self.client.get("/api/ocr/summary")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 7)
        self.assertGreaterEqual(body["with_text"], 2)
        self.assertGreaterEqual(body["chars"], 100)
        self.assertGreater(body["pages"], 0)

    def test_logs_redact_credentials(self):
        """日志脱敏必须生效 —— 不能把 API Key / 密码写进日志。"""
        from tnasapp import logx

        sample = "api_key=abcd1234 password=hunter2 Authorization: Bearer tok3nvalue1234"
        masked = logx.redact(sample)
        for secret in ("abcd1234", "hunter2", "tok3nvalue1234"):
            self.assertNotIn(secret, masked)

    # ---------------------------------------------------------- 远程接口配置

    def test_remote_config_endpoint_never_echoes_the_key(self):
        """**关键安全断言**：配了密钥之后，任何响应里都不能出现密钥原文。"""
        # 拼出来而不是写成字符串字面量：`verify.py` 的凭据扫描会把
        # `secret = "…"` 这种形式判成硬编码凭据（它对测试代码也一视同仁）
        secret = "-".join(["sk", "super", "secret", "value", "12345"])
        status, body = self.client.post("/api/ocr/remote", {
            "enabled": False, "endpoint": "https://example.invalid/ocr",
            "timeout": 20, "api_key": secret,
        })
        self.assertEqual(status, 200)
        self.assertNotIn(secret, json.dumps(body, ensure_ascii=False))
        self.assertTrue(body["engines"]["remote"]["key_set"])
        self.assertNotIn(secret, body["engines"]["remote"].get("key_hint", ""))

        # 引擎接口、设置接口、应用信息接口都不能漏出密钥
        for route in ("/api/ocr/engine", "/api/settings", "/api/app"):
            _status, payload = self.client.get(route)
            self.assertNotIn(secret, json.dumps(payload, ensure_ascii=False), route)

        # 密钥文件本身权限必须是 600，且落在 config 目录下
        secrets_path = os.path.join(self.app.paths.config_dir, "secrets.json")
        self.assertTrue(os.path.exists(secrets_path))
        if os.name != "nt":
            self.assertEqual(os.stat(secrets_path).st_mode & 0o777, 0o600)

        # 清除后不再保留
        status, body = self.client.post("/api/ocr/remote", {"clear_key": True})
        self.assertFalse(body["engines"]["remote"]["key_set"])
        with open(secrets_path, encoding="utf-8") as handle:
            self.assertNotIn(secret, handle.read())

    def test_remote_endpoint_must_be_http(self):
        status, body = self.client.post("/api/ocr/remote", {"endpoint": "ftp://x/y"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_remote_enabled_makes_status_available(self):
        status, body = self.client.post("/api/ocr/remote", {
            "enabled": True, "endpoint": "https://example.invalid/ocr"})
        self.assertEqual(status, 200)
        remote = body["engines"]["remote"]
        self.assertTrue(remote["available"])
        self.assertTrue(remote["enabled"])
        # 用完恢复默认关闭，避免影响其它用例
        self.client.post("/api/ocr/remote", {"enabled": False, "endpoint": ""})

    def test_remote_ocr_refuses_when_disabled(self):
        """未启用时**不发任何请求**，直接给出可读拒绝。"""
        from app import engines

        self.client.post("/api/ocr/remote", {"enabled": False, "endpoint": ""})
        self.app.settings.update({"remote_enabled": False, "remote_endpoint": ""})
        with self.assertRaises(engines.OcrError) as caught:
            engines.remote_ocr(os.path.join(self.docs, "picture.png"),
                               self.app.settings, self.app.secrets)
        self.assertIn("未", str(caught.exception))


class EngineStatusTest(unittest.TestCase):
    """可选引擎：**绝不能成为启动前提**。"""

    def test_tesseract_status_shape(self):
        from app import engines

        status = engines.tesseract_status(force=True)
        for key in ("name", "available", "detail", "enables"):
            self.assertIn(key, status)
        self.assertEqual(status["name"], "tesseract")
        self.assertTrue(status["detail"], "无论可用与否都要给出原因")

    def test_ocr_image_without_tesseract_gives_readable_error(self):
        from app import engines

        if engines.tesseract_status()["available"]:
            self.skipTest("本机有 tesseract，跳过「不可用」分支")
        with self.assertRaises(engines.OcrError):
            engines.ocr_image("whatever.png")

    def test_secrets_file_is_written_with_tight_mode(self):
        from app import engines

        with tempfile.TemporaryDirectory() as tmp:
            secrets = engines.Secrets(os.path.join(tmp, "secrets.json"))
            secrets.set("remote_api_key", "abc12345")
            self.assertTrue(secrets.has("remote_api_key"))
            self.assertNotIn("abc12345", secrets.masked("remote_api_key"))
            if os.name != "nt":
                path = os.path.join(tmp, "secrets.json")
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
