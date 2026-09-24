"""可选增强引擎：本机 ``tesseract`` 与管理员自配的远程 OCR 接口。

**全部都是可选的。** 核心的 PDF 文字层提取与图片元数据读取都在本应用内部用标准库实现，
这两个引擎只是「检测到 / 配置了才多出来的能力」：

* 检测不到 tesseract → 图片 OCR 显示「不可用」并说明原因，应用照常启动；
* 远程接口**默认关闭**，只有管理员显式启用才会发出请求（隐私政策 §3 已声明这一点）。

安全约定（对应指引 16.2 与设计文档 §52）：

* 调外部程序一律**参数列表**形式，**禁止 ``shell=True``**，并带超时；
* 远程接口的 API Key 单独存 ``data/config/secrets.json``（权限 600），
  **不出现在接口响应里**，也不进日志（日志已有兜底脱敏，这里再加一层）；
* 远程请求只在显式启用时发出，且上传体积有上限。
"""

import json
import os
import shutil
import subprocess

from tnasapp import logx

#: tesseract 单张图片的处理超时（秒）
TESSERACT_TIMEOUT = 180
#: 远程接口默认超时（秒）
REMOTE_TIMEOUT = 30
#: 远程接口上传的图片体积上限（避免把大文件推给第三方）
REMOTE_MAX_BYTES = 20 * 1024 * 1024
#: 引擎状态缓存秒数 —— 探测要起子进程，不能每次请求都探一遍
STATUS_TTL = 60


class OcrError(Exception):
    """OCR 引擎调用失败（可读原因，直接给用户看）。"""


_STATUS_CACHE = {"at": 0.0, "value": None}


# ---------------------------------------------------------------- tesseract


def tesseract_status(force=False):
    """探测本机 tesseract。返回 ``{name, available, detail, version, languages}``。"""
    import time

    now = time.time()
    if not force and _STATUS_CACHE["value"] and now - _STATUS_CACHE["at"] < STATUS_TTL:
        return _STATUS_CACHE["value"]

    result = {
        "name": "tesseract",
        "available": False,
        "version": "",
        "languages": [],
        "detail": "",
        "enables": "把图片（含 PDF 里的扫描页）识别成文字",
    }
    exe = shutil.which("tesseract")
    if not exe:
        result["detail"] = (
            "系统上没有 tesseract 命令，图片 OCR 不可用；"
            "PDF 文字层提取与图片元数据读取不受影响"
        )
        _STATUS_CACHE.update({"at": now, "value": result})
        return result

    result["path"] = exe
    try:
        proc = subprocess.run(
            [exe, "--version"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=10, check=False,
        )
        first = (proc.stdout or b"").decode("utf-8", "replace").splitlines()
        result["version"] = (first[0] if first else "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        result["detail"] = "tesseract 存在但无法执行：%s" % exc
        _STATUS_CACHE.update({"at": now, "value": result})
        return result

    try:
        proc = subprocess.run(
            [exe, "--list-langs"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=15, check=False,
        )
        lines = (proc.stdout or b"").decode("utf-8", "replace").splitlines()
        # 第一行是「List of available languages in ...」之类的说明
        result["languages"] = [line.strip() for line in lines[1:] if line.strip()]
    except (OSError, subprocess.SubprocessError):
        result["languages"] = []

    result["available"] = True
    result["detail"] = "已检测到 %s" % result["version"]
    _STATUS_CACHE.update({"at": now, "value": result})
    return result


def ocr_image(path, lang="", timeout=TESSERACT_TIMEOUT):
    """用本机 tesseract 识别一张图片，返回文本。

    ``tesseract <图片> stdout`` —— 输出到 stdout，**不落任何中间文件**，
    因此不需要写临时目录，也不会和别的进程抢文件名。
    """
    status = tesseract_status()
    if not status["available"]:
        raise OcrError(status["detail"] or "本机没有可用的 tesseract")
    exe = status.get("path") or "tesseract"

    command = [exe, path, "stdout"]
    if lang:
        command += ["-l", lang]
    try:
        # check=False + 自己判返回码：tesseract 在「识别不出文字」时会返回非 0，
        # 那不算错误，stdout 里可能仍有内容
        proc = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise OcrError("tesseract 处理超时（超过 %d 秒）——图片可能过大" % timeout)
    except OSError as exc:
        raise OcrError("无法执行 tesseract：%s" % exc)

    text = (proc.stdout or b"").decode("utf-8", "replace")
    if proc.returncode != 0 and not text.strip():
        message = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise OcrError("tesseract 返回错误：%s" % (message or "退出码 %d" % proc.returncode))
    return {"text": text.strip(), "engine": "tesseract", "lang": lang or "默认"}


# ---------------------------------------------------------------- 远程接口


class Secrets:
    """凭据文件（``data/config/secrets.json``，权限 600）。

    与 ``runtime.json`` 分开存是刻意的：运行时配置会被接口**原样返回**给前端，
    凭据一旦混在里面就必然会被回显。分开之后，回显路径上根本读不到它。
    """

    def __init__(self, path):
        self.path = path
        self._data = {}
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            self._data = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            self._data = {}
        return self._data

    def get(self, key, default=""):
        value = self._data.get(key)
        return default if value is None else value

    def set(self, key, value):
        if value:
            self._data[key] = value
        else:
            self._data.pop(key, None)
        self._save()

    def has(self, key):
        return bool(self._data.get(key))

    def masked(self, key):
        """回显用的遮罩形式：只说「设过没有」和末 2 位，**绝不返回原文**。"""
        value = str(self._data.get(key) or "")
        if not value:
            return ""
        if len(value) <= 2:
            return "**"
        return "**" + value[-2:]

    def _save(self):
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(self._data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        try:
            # 权限 600：只有服务用户自己可读（指引 16.2 凭据保护）
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)


def remote_status(settings, secrets):
    """远程接口的可用状态（**不含任何凭据内容**）。"""
    enabled = bool(settings.get("remote_enabled", False))
    endpoint = str(settings.get("remote_endpoint") or "").strip()
    has_key = secrets.has("remote_api_key")
    if not endpoint:
        detail = "未配置接口地址 —— 远程 OCR 不可用（不影响其它功能）"
        available = False
    elif not enabled:
        detail = "已配置地址但未启用；启用后图片会发送到该地址"
        available = False
    elif not endpoint.startswith(("http://", "https://")):
        detail = "接口地址必须以 http:// 或 https:// 开头"
        available = False
    else:
        available = True
        detail = "已启用：图片会发送到 %s" % endpoint
    return {
        "name": "remote-api",
        "available": available,
        "enabled": enabled,
        "endpoint": endpoint,
        "key_set": has_key,
        "key_hint": secrets.masked("remote_api_key"),
        "timeout": int(settings.get("remote_timeout", REMOTE_TIMEOUT) or REMOTE_TIMEOUT),
        "detail": detail,
        "enables": "把图片送到管理员配置的接口做识别（默认关闭）",
    }


def detect(settings, secrets):
    """两个引擎的合并状态（给 ``App(engines=...)`` 用）。"""
    return {
        "tesseract": tesseract_status(),
        "remote": remote_status(settings, secrets),
    }


def _multipart(fields, files):
    """手工拼 multipart/form-data（标准库没有现成的便捷封装）。"""
    boundary = "----shh14ocr%s" % os.urandom(12).hex()
    parts = []
    for name, value in fields.items():
        parts.append(b"--" + boundary.encode())
        parts.append(('Content-Disposition: form-data; name="%s"' % name).encode())
        parts.append(b"")
        parts.append(str(value).encode("utf-8"))
    for name, filename, payload, mime in files:
        parts.append(b"--" + boundary.encode())
        parts.append(
            ('Content-Disposition: form-data; name="%s"; filename="%s"'
             % (name, filename)).encode("utf-8")
        )
        parts.append(("Content-Type: %s" % mime).encode())
        parts.append(b"")
        parts.append(payload)
    parts.append(b"--" + boundary.encode() + b"--")
    parts.append(b"")
    body = b"\r\n".join(parts)
    return body, "multipart/form-data; boundary=%s" % boundary


_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp", ".tif": "image/tiff",
    ".tiff": "image/tiff", ".webp": "image/webp",
}


def remote_ocr(path, settings, secrets, logger=None):
    """把一张图片 POST 给管理员配置的接口，取回文本。

    **调用方必须先确认 ``remote_status(...)["available"]`` 为真** ——
    也就是说必须是管理员显式启用过的。这里再兜一道，防止被绕过。
    """
    import urllib.error
    import urllib.request

    status = remote_status(settings, secrets)
    if not status["available"]:
        raise OcrError(status["detail"])

    endpoint = status["endpoint"]
    size = os.path.getsize(path)
    if size > REMOTE_MAX_BYTES:
        raise OcrError(
            "图片 %.1f MB 超过远程接口的上限 %d MB"
            % (size / 1048576.0, REMOTE_MAX_BYTES // 1048576)
        )

    with open(path, "rb") as handle:
        payload = handle.read()
    ext = os.path.splitext(path)[1].lower()
    body, content_type = _multipart(
        {"language": str(settings.get("remote_language") or "")},
        [("file", os.path.basename(path), payload,
          _MIME_BY_EXT.get(ext, "application/octet-stream"))],
    )

    headers = {"Content-Type": content_type, "Accept": "application/json"}
    key = str(secrets.get("remote_api_key") or "")
    if key:
        headers["Authorization"] = "Bearer %s" % key

    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    timeout = int(status["timeout"] or REMOTE_TIMEOUT)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(2048).decode("utf-8", "replace")
        except Exception:
            detail = ""
        # 错误信息里可能有对方回显的内容，但**我们自己**绝不把 key 写进去
        raise OcrError("远程接口返回 HTTP %s：%s" % (exc.code, logx.redact(detail)[:300]))
    except urllib.error.URLError as exc:
        raise OcrError("无法连接远程接口：%s" % exc.reason)
    except OSError as exc:
        raise OcrError("远程接口请求失败：%s" % exc)

    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise OcrError("远程接口没有返回合法 JSON（预期 {\"text\": \"…\"}）")

    text = ""
    if isinstance(parsed, dict):
        for key_name in ("text", "result", "content", "data"):
            value = parsed.get(key_name)
            if isinstance(value, str) and value.strip():
                text = value
                break
            if isinstance(value, dict) and isinstance(value.get("text"), str):
                text = value["text"]
                break
    if not text:
        raise OcrError("远程接口的响应里没有 text 字段")

    if logger:
        # 只记「用了哪个地址、多大、多少字」，不记内容也不记凭据
        logger.info("远程 OCR 完成：%s（%d 字节 → %d 字符）", endpoint, size, len(text))
    return {"text": text.strip(), "engine": "remote-api", "endpoint": endpoint}
