"""文档文字识别 —— 应用装配与业务逻辑。

职责划分：

* ``pdfparse.py`` —— PDF 结构解析与文字层提取（自研解析器，纯标准库）
* ``extract.py``  —— 按类型分派 + TXT / Markdown / JSON 渲染
* ``engines.py``  —— 可选引擎（本机 tesseract / 管理员自配远程接口）
* ``main.py``（本文件） —— 路由、任务处理器、SQLite 索引与检索

设计要点：

* **默认只读。** 只有用户显式指定输出目录时才会写文件，且永远不覆盖已有文件。
* **索引按 mtime + size 增量更新**，没变过的文件不重新解析（设计文档 §36）。
* **检索用 LIKE 而不是 FTS5** —— FTS5 在部分发行版上没编译进 Python 的 sqlite3，
  依赖它会让功能在真机上直接失效；LIKE 慢一点，但一定可用。
* **远程接口默认关闭**，密钥单独存 ``config/secrets.json``（600），且**不回显**。
"""

import json
import os
import time

from tnasapp import fsapi, imagedec, server as srv

from . import engines, extract, pdfparse

APP_ID = "shh14-ocr"
APP_VERSION = "1.0.025"
TITLE = "Document OCR"

#: 递归扫描的最大深度（防止误选根目录后无限下钻）
MAX_DEPTH = 24
#: 单次任务最多收录的文件数
MAX_FILES = 100000
#: 检索结果单条摘要的字符数
SNIPPET = 160

MIGRATIONS = [
    # 前 5 个版本是任务队列的 SCHEMA，从第 6 个起是本应用的表
    """
    CREATE TABLE IF NOT EXISTS ocr_documents (
        path          TEXT PRIMARY KEY,
        name          TEXT,
        size          INTEGER NOT NULL DEFAULT 0,
        mtime         REAL    NOT NULL DEFAULT 0,
        kind          TEXT,
        format        TEXT,
        status        TEXT,
        error         TEXT,
        hint          TEXT,
        page_count    INTEGER NOT NULL DEFAULT 0,
        chars         INTEGER NOT NULL DEFAULT 0,
        pages_json    TEXT,
        text          TEXT,
        text_truncated INTEGER NOT NULL DEFAULT 0,
        metadata_json TEXT,
        stats_json    TEXT,
        extracted_at  REAL NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ocr_status ON ocr_documents(status)",
    "CREATE INDEX IF NOT EXISTS idx_ocr_time ON ocr_documents(extracted_at)",
    """
    CREATE TABLE IF NOT EXISTS ocr_runs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id     INTEGER,
        path       TEXT NOT NULL,
        status     TEXT,
        ok         INTEGER NOT NULL DEFAULT 1,
        chars      INTEGER NOT NULL DEFAULT 0,
        page_count INTEGER NOT NULL DEFAULT 0,
        outputs    TEXT,
        error      TEXT,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ocr_runs_job ON ocr_runs(job_id)",
]


def create_app(paths=None, log_level="INFO"):
    resolved = paths or _fallback_paths()
    secrets = engines.Secrets(os.path.join(resolved.config_dir, "secrets.json"))
    app = App(
        APP_ID, TITLE, version=APP_VERSION, workers=2, log_level=log_level,
        extra_migrations=MIGRATIONS, paths=resolved,
        description="离线提取 PDF 的文字层与图片元数据，输出 TXT / Markdown / JSON 并建立本地检索索引。",
        engines={},
    )
    # 凭据对象挂在 app 上，路由与任务处理器共用（但它**不出现在任何响应里**）
    app.secrets = secrets
    app.engines = engines.detect(app.settings, secrets)

    fsapi.register(app)
    _register_routes(app)
    _register_jobs(app)
    return app


def _fallback_paths():
    """只为在 create_app 里先拿到 config 目录（App 内部还会再算一次，结果一致）。"""
    from tnasapp import paths as paths_mod

    return paths_mod.AppPaths(APP_ID)


# ---------------------------------------------------------------- 设置


def default_settings():
    """本应用的默认配置（合并进框架的默认配置）。"""
    return {
        "auth_mode": "lenient",
        "debug": False,
        # 远程 OCR 接口：**默认关闭**，必须管理员显式启用
        "remote_enabled": False,
        "remote_endpoint": "",
        "remote_timeout": engines.REMOTE_TIMEOUT,
        "remote_language": "",
        # tesseract 的语言包（空 = 用 tesseract 自己的默认）
        "ocr_language": "",
    }


class App(srv.App):
    """本应用的 App —— 只做一件事：把上面的默认配置合并进框架默认值。"""

    def default_settings(self):
        merged = dict(super().default_settings())
        merged.update(default_settings())
        return merged


# ---------------------------------------------------------------- 工具


def _require_allowed(app, path, must_exist=True):
    return app.allowed.check(path, must_exist=must_exist)


def _readable_file(app, path):
    real = _require_allowed(app, path)
    if not os.path.isfile(real):
        raise ValueError("不是文件：%s" % path)
    if not os.access(real, os.R_OK):
        raise PermissionError("没有读取权限：%s" % path)
    return real


def _walk_documents(app, root, ctx=None):
    """递归收集可处理的文档，返回 ``[(绝对路径, 相对根的子目录), …]``。

    相对子目录用于输出时**镜像目录结构**（批量处理一个目录树时，
    结果不该全平铺在一个目录里）。
    """
    root = _require_allowed(app, root)
    if os.path.isfile(root):
        return [(root, "")] if extract.is_supported(root) else []

    found = []
    stack = [(root, 0, "")]
    while stack:
        directory, depth, rel = stack.pop()
        if depth > MAX_DEPTH:
            continue
        if ctx is not None:
            ctx.checkpoint()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            name = entry.name
                            if name.startswith((".", "@", "$")):
                                continue
                            stack.append((entry.path, depth + 1,
                                          os.path.join(rel, name) if rel else name))
                        elif entry.is_file(follow_symlinks=False):
                            if extract.is_supported(entry.name):
                                found.append((entry.path, rel))
                                if len(found) >= MAX_FILES:
                                    return sorted(found)
                    except OSError:
                        continue
        except (PermissionError, OSError):
            continue
    return sorted(found)


def _collect_inputs(app, ctx, params):
    """把 ``roots``（递归）与 ``inputs``（单个文件）合并成待处理清单。"""
    items = []
    for root in params.get("roots") or []:
        items.extend(_walk_documents(app, root, ctx))
    for path in params.get("inputs") or []:
        real = _require_allowed(app, path)
        if os.path.isfile(real) and extract.is_supported(real):
            items.append((real, ""))
        elif os.path.isfile(real):
            ctx.log("跳过不支持的文件：%s" % real, "WARN")
    # 去重（同一文件可能既在 roots 里又被单独指定），保持稳定顺序
    seen = set()
    unique = []
    for path, rel in items:
        if path in seen:
            continue
        seen.add(path)
        unique.append((path, rel))
    return unique


def _row_pages(row):
    try:
        pages = json.loads(row.get("pages_json") or "[]")
        return pages if isinstance(pages, list) else []
    except ValueError:
        return []


def _row_metadata(row, key="metadata_json"):
    try:
        value = json.loads(row.get(key) or "{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _row_summary(row):
    """列表用的精简视图（**不含正文**，避免列表接口传几百 MB）。"""
    return {
        "path": row.get("path"),
        "name": row.get("name"),
        "size": row.get("size"),
        "kind": row.get("kind"),
        "format": row.get("format"),
        "status": row.get("status"),
        "status_label": extract.label(row.get("status")),
        "error": row.get("error"),
        "page_count": row.get("page_count"),
        "chars": row.get("chars"),
        "text_truncated": bool(row.get("text_truncated")),
        "extracted_at": row.get("extracted_at"),
        "pages": _row_pages(row),
        "metadata": _row_metadata(row),
        "stats": _row_metadata(row, "stats_json"),
    }


def _store_result(app, result, job_id=None, outputs=None):
    """把一次提取结果写进索引（正文按上限截断）。"""
    pages = [
        {
            "index": page.get("index"),
            "chars": page.get("chars"),
            "has_text": bool(page.get("has_text")),
            "image_count": page.get("image_count"),
            "warnings": page.get("warnings") or [],
        }
        for page in (result.get("pages") or [])
    ]
    text = result.get("text") or ""
    truncated = len(text) > extract.MAX_INDEX_CHARS
    stored_text = text[:extract.MAX_INDEX_CHARS] if truncated else text
    try:
        mtime = os.path.getmtime(result.get("path"))
    except OSError:
        mtime = 0.0

    app.store.execute(
        "INSERT INTO ocr_documents (path,name,size,mtime,kind,format,status,error,hint,"
        "page_count,chars,pages_json,text,text_truncated,metadata_json,stats_json,"
        "extracted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(path) DO UPDATE SET name=excluded.name, size=excluded.size,"
        " mtime=excluded.mtime, kind=excluded.kind, format=excluded.format,"
        " status=excluded.status, error=excluded.error, hint=excluded.hint,"
        " page_count=excluded.page_count, chars=excluded.chars,"
        " pages_json=excluded.pages_json, text=excluded.text,"
        " text_truncated=excluded.text_truncated, metadata_json=excluded.metadata_json,"
        " stats_json=excluded.stats_json, extracted_at=excluded.extracted_at",
        (
            result.get("path"), result.get("name"), int(result.get("size") or 0),
            float(mtime or 0), result.get("kind"), result.get("format"),
            result.get("status"), result.get("error"), result.get("hint"),
            int(result.get("page_count") or 0), int(result.get("chars") or 0),
            json.dumps(pages, ensure_ascii=False),
            stored_text, 1 if truncated else 0,
            json.dumps(result.get("metadata") or {}, ensure_ascii=False, default=str),
            json.dumps(result.get("stats") or {}, ensure_ascii=False, default=str),
            float(result.get("extracted_at") or time.time()),
        ),
    )
    app.store.execute(
        "INSERT INTO ocr_runs (job_id,path,status,ok,chars,page_count,outputs,error,"
        "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            job_id, result.get("path"), result.get("status"),
            1 if result.get("ok") else 0, int(result.get("chars") or 0),
            int(result.get("page_count") or 0),
            json.dumps(outputs or [], ensure_ascii=False),
            result.get("error"), time.time(),
        ),
    )


def _cached_row(app, path):
    """取索引里**仍然新鲜**的那一行（size + mtime 都对得上）。"""
    row = app.store.query_one("SELECT * FROM ocr_documents WHERE path=?", (path,))
    if not row:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    if abs((row.get("size") or 0) - stat.st_size) > 1:
        return None
    if abs((row.get("mtime") or 0) - stat.st_mtime) > 1:
        return None
    return row


def _result_from_row(row):
    """把库里的行还原成与实时提取**同构**的结果。"""
    text = row.get("text") or ""
    return {
        "ok": not row.get("error"),
        "kind": row.get("kind"),
        "path": row.get("path"),
        "name": row.get("name"),
        "size": row.get("size"),
        "format": row.get("format"),
        "status": row.get("status"),
        "error": row.get("error"),
        "hint": row.get("hint"),
        "page_count": row.get("page_count") or 0,
        "pages": _row_pages(row),
        "text": text,
        "text_truncated": bool(row.get("text_truncated")),
        "chars": row.get("chars") or 0,
        "stats": _row_metadata(row, "stats_json"),
        "metadata": _row_metadata(row),
        "warnings": [],
        "extracted_at": row.get("extracted_at"),
        "from_cache": True,
    }


def _load_document(app, path, fresh=False):
    """单份文档的结果。

    ``fresh=False``（默认）优先用索引里的新鲜行 —— 列表点开时不必重新解析一遍。
    ``fresh=True`` 一律从源文件重新提取：**导出与「查看全文」必须这么做**，
    因为索引里的逐页结构是精简过的（每页只留字符数与告警），够看不够用。
    """
    real = _readable_file(app, path)
    if not fresh:
        row = _cached_row(app, real)
        if row and not row.get("text_truncated"):
            result = _result_from_row(row)
            result["ok"] = not result.get("error")
            return result
    result = extract.extract_document(real)
    _store_result(app, result)
    return result


def _path_error(exc):
    """路径类错误一律 403（越权），其它一律 400（参数/数据问题）。"""
    from tnasapp import paths as paths_mod

    if isinstance(exc, (paths_mod.PathDenied, PermissionError)):
        return srv.Response.error(
            str(exc), 403, "请先在「设置」里把该目录加入可访问目录"
        )
    return srv.Response.error(str(exc), 400)


def _ocr_runner(app, allow_ocr):
    """构造一个「按需 OCR」的回调；未启用时返回 None。"""
    if not allow_ocr:
        return None

    def run(path):
        # 优先级：管理员显式启用的远程接口 > 本机 tesseract。
        # 远程失败不直接放弃，而是退回本机引擎 —— 用户已经表达了「想要 OCR」的意图，
        # 网络/对方服务的问题不该让整个动作失败；两边都不行才如实报告原因。
        notes = []
        status = engines.remote_status(app.settings, app.secrets)
        if status["available"]:
            try:
                found = engines.remote_ocr(path, app.settings, app.secrets, app.log)
                return found["text"], "remote-api", notes
            except engines.OcrError as exc:
                notes.append("远程接口失败：%s" % exc)
        else:
            notes.append("远程接口未启用（%s）" % status["detail"])

        local = engines.tesseract_status()
        if local["available"]:
            try:
                found = engines.ocr_image(path, app.settings.get("ocr_language") or "")
                return found["text"], "tesseract", notes
            except engines.OcrError as exc:
                notes.append("本机 tesseract 失败：%s" % exc)
        else:
            notes.append("本机没有 tesseract，图片文字未能识别")
        return "", None, notes

    return run


# ---------------------------------------------------------------- 路由


def _register_routes(app):
    @app.get("/api/ocr/engine")
    def _engine(req):
        tesseract = dict(engines.tesseract_status(force=req.bool_arg("refresh")))
        # 已配置的语言不属于「探测结果」（它来自设置），但界面要显示，所以并进来
        tesseract["configured_language"] = app.settings.get("ocr_language") or ""
        remote = engines.remote_status(app.settings, app.secrets)
        return srv.Response.json({
            "ok": True,
            "engines": {"tesseract": tesseract, "remote": remote},
            "builtin": {
                "name": "内置 PDF 解析器",
                "available": True,
                "detail": "自研解析：xref / 内容流 / FlateDecode / ToUnicode，"
                          "不依赖任何外部程序",
                "enables": "PDF 文字层提取、逐页字符数、TXT / Markdown / JSON 输出",
            },
            "statuses": extract.STATUS_LABELS,
            "formats": list(extract.FORMATS),
        })

    @app.get("/api/ocr/probe")
    def _probe(req):
        """探测一个文件：类型、是否加密、页数、有没有文字层。"""
        path = req.arg("path", "")
        try:
            real = _readable_file(app, path)
        except Exception as exc:
            return srv.Response.error(str(exc), 403,
                                      "请先在「设置」里把该目录加入可访问目录")
        kind = extract.kind_of(real)
        if kind == "image":
            try:
                meta = imagedec.image_metadata(real)
            except Exception as exc:
                return srv.Response.json({"ok": False, "kind": "image",
                                          "path": real, "error": str(exc)})
            return srv.Response.json({
                "ok": True, "kind": "image", "path": real,
                "size": meta.get("size"), "format": meta.get("format"),
                "page_count": 1, "has_text_layer": False,
                "note": "图片文件：可读尺寸与 EXIF；文字需要 OCR 引擎（可选）",
                "metadata": {key: value for key, value in meta.items() if key != "exif"},
                "exif_count": len(meta.get("exif") or {}),
            })
        if kind != "pdf":
            return srv.Response.error(
                "不支持的文件类型：%s" % (os.path.splitext(real)[1] or "无扩展名"),
                400, "本应用支持 PDF 与常见图片格式（jpg / png / gif / bmp / tiff）"
            )
        try:
            info = pdfparse.probe(real)
        except Exception as exc:
            return srv.Response.json({"ok": False, "kind": "pdf", "path": real,
                                      "error": str(exc)})
        row = _cached_row(app, real)
        if row:
            info["indexed"] = {
                "status": row.get("status"),
                "chars": row.get("chars"),
                "extracted_at": row.get("extracted_at"),
            }
        return srv.Response.json(info)

    @app.get("/api/ocr/documents")
    def _documents(req):
        limit = max(1, min(500, req.int_arg("limit", 100)))
        offset = max(0, req.int_arg("offset", 0))
        clauses, params = [], []
        status = req.arg("status")
        keyword = (req.arg("q") or "").strip()
        kind = req.arg("kind")
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if keyword:
            clauses.append("(path LIKE ? OR name LIKE ?)")
            params.extend(["%" + keyword + "%"] * 2)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = app.store.query(
            "SELECT * FROM ocr_documents%s ORDER BY extracted_at DESC LIMIT ? OFFSET ?"
            % where, tuple(params + [limit, offset]),
        )
        total = app.store.scalar(
            "SELECT COUNT(*) FROM ocr_documents%s" % where, tuple(params), default=0
        )
        return srv.Response.json({
            "ok": True, "total": total,
            "documents": [_row_summary(row) for row in rows],
        })

    @app.get("/api/ocr/document")
    def _document(req):
        path = req.arg("path", "")
        if not path:
            return srv.Response.error("缺少 path 参数", 400)
        try:
            result = _load_document(app, path, fresh=req.bool_arg("full"))
        except Exception as exc:
            return _path_error(exc)
        payload = dict(result)
        if not req.bool_arg("full"):
            # 默认只给预览：一份几百页的 PDF 正文可能很大，列表点开不该拖垮前端
            text = payload.get("text") or ""
            payload["preview"] = text[:4000]
            payload["text"] = ""
        # 逐页正文不进 JSON 响应（只有导出时才需要）
        payload["pages"] = [
            {key: value for key, value in page.items() if key != "text"}
            for page in (payload.get("pages") or [])
        ]
        payload["ok"] = True
        return srv.Response.json(payload)

    @app.get("/api/ocr/search")
    def _search(req):
        keyword = (req.arg("q") or "").strip()
        if not keyword:
            return srv.Response.error("请输入要检索的关键词", 400)
        limit = max(1, min(200, req.int_arg("limit", 50)))
        offset = max(0, req.int_arg("offset", 0))
        mode = req.arg("mode", "text")
        if mode == "path":
            where = "(path LIKE ? OR name LIKE ?)"
            params = ["%" + keyword + "%"] * 2
        else:
            where = "text LIKE ?"
            params = ["%" + keyword + "%"]
        rows = app.store.query(
            "SELECT path,name,kind,status,page_count,chars,text,extracted_at"
            " FROM ocr_documents WHERE %s ORDER BY extracted_at DESC LIMIT ? OFFSET ?"
            % where, tuple(params + [limit, offset]),
        )
        total = app.store.scalar(
            "SELECT COUNT(*) FROM ocr_documents WHERE %s" % where, tuple(params),
            default=0,
        )
        results = []
        lowered = keyword.lower()
        for row in rows:
            text = row.get("text") or ""
            haystack = text.lower()
            # 命中次数：PDF 里同一个词往往出现很多次，直接告诉用户
            hits = haystack.count(lowered) if mode == "text" else 0
            index = haystack.find(lowered) if mode == "text" else -1
            snippet = ""
            if index >= 0:
                start = max(0, index - SNIPPET // 2)
                snippet = text[start:start + SNIPPET].replace("\n", " ")
                if start:
                    snippet = "…" + snippet
                if start + SNIPPET < len(text):
                    snippet += "…"
            results.append({
                "path": row.get("path"), "name": row.get("name"),
                "kind": row.get("kind"), "status": row.get("status"),
                "status_label": extract.label(row.get("status")),
                "page_count": row.get("page_count"), "chars": row.get("chars"),
                "extracted_at": row.get("extracted_at"),
                "hits": hits, "snippet": snippet,
                "indexed": bool(text),
            })
        return srv.Response.json({
            "ok": True, "query": keyword, "mode": mode,
            "total": total, "results": results,
        })

    @app.get("/api/ocr/summary")
    def _summary(req):
        total = app.store.scalar("SELECT COUNT(*) FROM ocr_documents", default=0)
        with_text = app.store.scalar(
            "SELECT COUNT(*) FROM ocr_documents WHERE chars > 0", default=0)
        scanned = app.store.scalar(
            "SELECT COUNT(*) FROM ocr_documents WHERE status='no-text-layer'", default=0)
        encrypted = app.store.scalar(
            "SELECT COUNT(*) FROM ocr_documents WHERE status='encrypted'", default=0)
        failed = app.store.scalar(
            "SELECT COUNT(*) FROM ocr_documents WHERE status='error'", default=0)
        chars = app.store.scalar(
            "SELECT COALESCE(SUM(chars),0) FROM ocr_documents WHERE chars IS NOT NULL",
            default=0)
        bytes_ = app.store.scalar(
            "SELECT COALESCE(SUM(size),0) FROM ocr_documents", default=0)
        pages = app.store.scalar(
            "SELECT COALESCE(SUM(page_count),0) FROM ocr_documents", default=0)
        statuses = app.store.query(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(chars),0) AS chars"
            " FROM ocr_documents GROUP BY status ORDER BY n DESC")
        kinds = app.store.query(
            "SELECT kind, COUNT(*) AS n FROM ocr_documents GROUP BY kind")
        recent_jobs = app.jobs.list(limit=5)
        return srv.Response.json({
            "ok": True, "total": total, "with_text": with_text, "scanned": scanned,
            "encrypted": encrypted, "failed": failed, "chars": chars,
            "bytes": bytes_, "pages": pages,
            "statuses": statuses, "kinds": kinds, "recent_jobs": recent_jobs,
        })

    @app.get("/api/ocr/download")
    def _download(req):
        """导出提取结果。

        **每次都从源文件重新提取**，不用索引里的正文：索引为了控制体积对正文做了截断，
        而且逐页正文没有入库。导出是用户显式的一次动作，正确性优先于速度。
        """
        path = req.arg("path", "")
        fmt = (req.arg("format", "txt") or "txt").lower()
        if fmt not in extract.FORMATS:
            return srv.Response.error(
                "不支持的格式：%s" % fmt, 400, "可选：txt / md / json")
        if not path:
            return srv.Response.error("缺少 path 参数", 400)
        try:
            result = _load_document(app, path, fresh=True)
        except Exception as exc:
            return _path_error(exc) if not isinstance(exc, extract.ExtractError) \
                else srv.Response.error(str(exc), 400, "该文件无法提取文字")
        if not result.get("ok"):
            # 加密 / 解析失败：给出可读原因，而不是导出一个空文件让用户以为成功
            return srv.Response.error(
                result.get("error") or "该文件无法提取文字", 400,
                "文件本身没有可提取的文字层时，可以改用 JSON 格式查看逐页字符数",
            )
        stem = os.path.splitext(os.path.basename(result.get("path") or "document"))[0]
        body = extract.render(result, fmt)
        return srv.Response(
            200, body.encode("utf-8"),
            "text/plain; charset=utf-8" if fmt == "txt" else
            ("text/markdown; charset=utf-8" if fmt == "md"
             else "application/json; charset=utf-8"),
            {"Content-Disposition": "attachment; filename*=UTF-8''%s"
             % _quote("%s.%s" % (stem, fmt))},
        )

    @app.get("/api/ocr/statuses")
    def _statuses(req):
        return srv.Response.json({"ok": True, "statuses": extract.STATUS_LABELS})

    # ---- 远程接口配置（**密钥永远不回显**） ----

    @app.post("/api/ocr/remote")
    def _remote_save(req):
        body = req.json_body()
        if not isinstance(body, dict):
            return srv.Response.error("请求体必须是 JSON 对象", 400)
        updates = {}
        if "enabled" in body:
            updates["remote_enabled"] = bool(body.get("enabled"))
        if "endpoint" in body:
            endpoint = str(body.get("endpoint") or "").strip()
            if endpoint and not endpoint.startswith(("http://", "https://")):
                return srv.Response.error(
                    "接口地址必须以 http:// 或 https:// 开头", 400)
            updates["remote_endpoint"] = endpoint
        if "timeout" in body:
            try:
                updates["remote_timeout"] = max(5, min(600, int(body.get("timeout"))))
            except (TypeError, ValueError):
                return srv.Response.error("超时时间必须是数字（秒）", 400)
        if "language" in body:
            updates["remote_language"] = str(body.get("language") or "").strip()
        if updates:
            app.settings.update(updates)

        # 密钥走单独的凭据文件；空字符串 = 清除
        if "api_key" in body:
            app.secrets.set("remote_api_key", str(body.get("api_key") or "").strip())
        if body.get("clear_key"):
            app.secrets.set("remote_api_key", "")

        status = engines.remote_status(app.settings, app.secrets)
        app.engines = engines.detect(app.settings, app.secrets)
        app.log.info("远程 OCR 配置已更新：启用=%s 地址=%s 密钥=%s",
                     status["enabled"], status["endpoint"] or "（未设置）",
                     "已设置" if status["key_set"] else "未设置")
        # 注意：这里返回的是 remote_status()，**里面只有 key_hint（末 2 位遮罩）**
        return srv.Response.json({"ok": True, "engines": {
            "tesseract": engines.tesseract_status(),
            "remote": status,
        }})


def _quote(text):
    import urllib.parse

    return urllib.parse.quote(text)


# ---------------------------------------------------------------- 任务


def _register_jobs(app):
    def _run_extract(ctx, write_files):
        params = ctx.params or {}
        ocr_runner = _ocr_runner(app, bool(params.get("ocr")))
        fmt_list = _formats_of(params)

        out_dir = None
        if write_files:
            output_dir = params.get("output_dir")
            if not output_dir:
                # 不指定就落在应用自己的 output/ 目录（README 里声明过的落点）
                out_dir = app.paths.output_dir
            else:
                out_dir = _require_allowed(app, output_dir, must_exist=False)
            os.makedirs(out_dir, exist_ok=True)
            if not os.access(out_dir, os.W_OK):
                raise PermissionError("输出目录不可写：%s" % out_dir)

        items = _collect_inputs(app, ctx, params)
        if not items:
            raise ValueError(
                "没有找到可处理的文件（支持 PDF 与常见图片；"
                "请确认目录已加入可访问目录）"
            )

        ctx.log("待处理 %d 个文件%s"
                % (len(items), "，输出到 %s" % out_dir if out_dir else "（只建索引）"))
        counts = {}
        with_text = 0
        written = 0

        for index, (path, rel) in enumerate(items, 1):
            ctx.checkpoint()
            ctx.message("处理 %s" % os.path.basename(path))
            outputs = []
            try:
                # 长 PDF 的逐页进度 → 任务进度（同时把取消/暂停的检查点带进页循环）
                def page_progress(done, total, _index=index, _total=len(items)):
                    if done % 5 == 0 or done == total:
                        ctx.checkpoint()
                        ctx.progress(_index - 1 + (done / float(total or 1)),
                                     _total, "%d/%d 页" % (done, total))

                result = extract.extract_document(
                    path, progress=page_progress, ocr_runner=ocr_runner)
            except extract.ExtractError as exc:
                result = extract.failed_result(path, exc)
            except (OSError, MemoryError, RecursionError) as exc:
                result = extract.failed_result(path, "读取失败：%s" % exc)
            except Exception as exc:  # 单文件异常不该让整个任务失败
                result = extract.failed_result(path, "内部错误：%s" % exc)

            status = result.get("status") or "error"
            counts[status] = counts.get(status, 0) + 1
            if result.get("chars"):
                with_text += 1

            if out_dir and result.get("ok"):
                for fmt in fmt_list:
                    try:
                        outputs.append(extract.write_output(result, out_dir, rel, fmt))
                        written += 1
                    except OSError as exc:
                        ctx.log("写出 %s 失败：%s" % (fmt, exc), "WARN")

            _store_result(app, result, job_id=ctx.job_id, outputs=outputs)

            if result.get("ok"):
                ctx.log("%s → %s（%d 字符%s）"
                        % (result.get("name"), extract.label(status),
                           result.get("chars") or 0,
                           "，已导出 %d 个文件" % len(outputs) if outputs else ""))
            else:
                ctx.log("失败：%s —— %s" % (path, result.get("error")), "WARN")

            ctx.progress(index, len(items),
                         "%d/%d，有文字 %d" % (index, len(items), with_text))

        ctx.set_result({
            "total": len(items),
            "with_text": with_text,
            "by_status": counts,
            "outputs": written,
            "output_dir": out_dir,
        })

    @app.jobs.register("extract")
    def _job_extract(ctx):
        _run_extract(ctx, write_files=True)

    @app.jobs.register("index")
    def _job_index(ctx):
        """只建/更新检索索引，不写任何结果文件。"""
        _run_extract(ctx, write_files=False)


def _formats_of(params):
    """把 ``format`` 参数解析成格式列表。``all`` = 三种都写。"""
    raw = (params.get("format") or "txt").strip().lower()
    if raw in ("", "none"):
        return []
    if raw == "all":
        return list(extract.FORMATS)
    chosen = [item for item in raw.split(",") if item in extract.FORMATS]
    if not chosen:
        raise ValueError(
            "不支持的输出格式：%s（可选 txt / md / json / all / none）" % raw)
    return chosen


def main(argv=None):
    from tnasapp import cli

    return cli.main(APP_ID, APP_VERSION, create_app, argv=argv,
                    description="文档文字识别 —— 离线提取 PDF 文字层与图片元数据")
