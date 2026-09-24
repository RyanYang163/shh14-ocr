"""按文件类型分派提取、以及输出格式（TXT / Markdown / JSON）的渲染。

职责划分：

* ``pdfparse.py`` —— PDF 结构解析与文字层提取（自研，纯标准库）
* ``engines.py``  —— 可选的 tesseract / 远程接口
* ``extract.py``（本文件） —— 把上面两者拼成**一个统一的结果结构**，并渲染成三种格式

统一结果结构（也是写进 SQLite 的结构）::

    {
      "ok": bool, "kind": "pdf" | "image", "path", "name", "size", "format",
      "status": ok | partial | no-text-layer | empty | encrypted | error | metadata | ocr,
      "error": str | None, "hint": str | None,
      "page_count": int,
      "pages": [{index, chars, has_text, image_count, image_only, warnings}],
      "chars": int, "text": str, "text_truncated": bool,
      "stats": {...}, "metadata": {...}, "warnings": [str], "extracted_at": float
    }

``status`` 是给用户看的**一句话结论** —— 设计文档 §12.3 的要点：不要把所有 PDF 都
当成扫描件，也不要让用户对着一堆空结果猜「为什么没文字」。
"""

import json
import os
import time

from tnasapp import imagedec

from . import engines, pdfparse

PDF_EXTS = frozenset((".pdf",))
IMAGE_EXTS = frozenset(
    (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp")
)
SUPPORTED_EXTS = PDF_EXTS | IMAGE_EXTS

#: 结果里最多保留的文本字符数（再长就截断，避免一份超长 PDF 把内存和库撑爆）
MAX_TEXT_CHARS = 2_000_000
#: 写进 SQLite 索引的文本上限（单份文档）；超出部分不进索引，下载时会重新提取
MAX_INDEX_CHARS = 500_000

STATUS_LABELS = {
    "ok": "已提取文字层",
    "partial": "部分页有文字层",
    "no-text-layer": "无文字层（扫描页）",
    "empty": "没有文字",
    "encrypted": "已加密",
    "error": "提取失败",
    "metadata": "仅元数据",
    "ocr": "已 OCR 识别",
}

FORMATS = ("txt", "md", "json")


class ExtractError(Exception):
    """提取失败（带可读原因）。"""


class EncryptedDocument(ExtractError):
    """文档已加密。"""


def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXTS:
        return "pdf"
    if ext in IMAGE_EXTS:
        return "image"
    return ""


def is_supported(path):
    return bool(kind_of(path))


def label(status):
    return STATUS_LABELS.get(status, status or "未知")


# ---------------------------------------------------------------- 提取


def extract_pdf(path, progress=None, max_chars=MAX_TEXT_CHARS):
    """提取 PDF。异常统一转成 :class:`ExtractError`。"""
    try:
        result = pdfparse.extract(path, max_chars=max_chars, progress=progress)
    except pdfparse.PdfEncrypted as exc:
        raise EncryptedDocument(str(exc))
    except pdfparse.UnsupportedPdf as exc:
        raise ExtractError(str(exc))
    except pdfparse.PdfError as exc:
        raise ExtractError("PDF 解析失败：%s" % exc)
    except (OSError, MemoryError) as exc:
        raise ExtractError("无法读取文件：%s" % exc)
    return result


def extract_image(path, ocr=None):
    """图片：读尺寸与 EXIF；``ocr`` 给了就额外识别文字。"""
    try:
        meta = imagedec.image_metadata(path)
    except imagedec.UnsupportedImage as exc:
        raise ExtractError(str(exc))
    except OSError as exc:
        raise ExtractError("无法读取图片：%s" % exc)

    warnings = []
    text = ""
    status = "metadata"
    engine_name = None

    if ocr is not None:
        text, engine_name, notes = ocr
        warnings.extend(notes)
        if text.strip():
            status = "ocr"
        else:
            warnings.append("OCR 没有识别出文字（图片可能是纯图、空白或语言不匹配）")

    exif = meta.get("exif") or {}
    result = {
        "ok": True,
        "kind": "image",
        "path": path,
        "name": os.path.basename(path),
        "size": meta.get("size") or 0,
        "format": meta.get("format") or "IMAGE",
        "status": status,
        "error": None,
        "hint": None,
        "page_count": 1,
        "pages": [{
            "index": 1,
            "chars": len(text),
            "has_text": bool(text.strip()),
            "image_count": 1,
            "image_only": not text.strip(),
            "undecoded_fonts": [],
            "warnings": list(warnings),
        }],
        "text": text,
        "text_truncated": False,
        "chars": len(text),
        "stats": {
            "width": meta.get("width"),
            "height": meta.get("height"),
            "pixels": meta.get("pixels"),
            "bit_depth": meta.get("bit_depth"),
            "color_type": meta.get("color_type"),
            "engine": engine_name,
        },
        "metadata": {
            "kind": "image",
            "format": meta.get("format"),
            "width": meta.get("width"),
            "height": meta.get("height"),
            "pixels": meta.get("pixels"),
            "exif": exif,
            "exif_count": len(exif) if isinstance(exif, dict) else 0,
        },
        "warnings": warnings,
        "extracted_at": time.time(),
    }
    return result


def extract_document(path, progress=None, ocr_runner=None):
    """按扩展名分派。``ocr_runner(path)`` 返回 ``(text, engine, notes)``。"""
    kind = kind_of(path)
    if kind == "pdf":
        result = extract_pdf(path, progress=progress)
    elif kind == "image":
        ocr = None
        if ocr_runner is not None:
            ocr = ocr_runner(path)
        result = extract_image(path, ocr=ocr)
    else:
        raise ExtractError(
            "不支持的文件类型：%s（本应用支持 PDF 与常见图片格式）"
            % (os.path.splitext(path)[1] or "无扩展名")
        )
    result.setdefault("extracted_at", time.time())
    # 字符数统一用「逐页字符数之和」—— 全文里的页间分隔符不该算进「文档有多少字」
    pages = result.get("pages") or []
    result["chars"] = sum(int(page.get("chars") or 0) for page in pages)
    return result


def failed_result(path, error, hint=None, kind=""):
    """把失败也表达成**同一种结构** —— 界面与数据库不必为失败单独写一条路径。"""
    return {
        "ok": False,
        "kind": kind or kind_of(path) or "unknown",
        "path": path,
        "name": os.path.basename(path),
        "size": _safe_size(path),
        "format": "",
        "status": "encrypted" if isinstance(error, EncryptedDocument) else "error",
        "error": str(error),
        "hint": hint,
        "page_count": 0,
        "pages": [],
        "text": "",
        "text_truncated": False,
        "chars": 0,
        "stats": {},
        "metadata": {},
        "warnings": [],
        "extracted_at": time.time(),
    }


def _safe_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# ---------------------------------------------------------------- 渲染


def render_txt(result):
    """纯文本：**只有正文**，方便直接 grep / 复制。"""
    if result.get("kind") == "image" and not (result.get("text") or "").strip():
        return _render_image_metadata(result, plain=True)
    return (result.get("text") or "").rstrip() + "\n"


def render_markdown(result):
    """Markdown：每页包成一节（``## 第 N 页``），并把逐页字符数写在标题里。"""
    lines = ["# %s" % (result.get("name") or "文档"), ""]
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append("| 来源 | `%s` |" % (result.get("path") or ""))
    lines.append("| 类型 | %s |" % (result.get("format") or result.get("kind") or ""))
    lines.append("| 状态 | %s |" % label(result.get("status")))
    lines.append("| 页数 | %d |" % int(result.get("page_count") or 0))
    lines.append("| 字符数 | %d |" % int(result.get("chars") or 0))
    if result.get("error"):
        lines.append("| 错误 | %s |" % result["error"])
    if result.get("hint"):
        lines.append("| 建议 | %s |" % result["hint"])
    lines.append("")

    if result.get("kind") == "image" and not (result.get("text") or "").strip():
        lines.append("## 图片元数据")
        lines.append("")
        lines.append(_render_image_metadata(result, plain=False))
        lines.append("")

    pages = result.get("pages") or []
    if pages and result.get("kind") == "pdf":
        for index, page in enumerate(pages):
            lines.append("## 第 %d 页" % (index + 1))
            lines.append("")
            lines.append("> 字符数 %d%s"
                         % (page.get("chars") or 0,
                            "，本页无文字层" if not page.get("has_text") else ""))
            lines.append("")
            body = page.get("text") or ""
            lines.append(body if body.strip() else "_（本页没有可提取的文字）_")
            for warning in (page.get("warnings") or []):
                lines.append("")
                lines.append("> 注意：%s" % warning)
            lines.append("")
    elif (result.get("text") or "").strip():
        lines.append("## 正文")
        lines.append("")
        lines.append(result["text"])
        lines.append("")

    warnings = result.get("warnings") or []
    if warnings:
        lines.append("## 提取提示")
        lines.append("")
        for warning in warnings:
            lines.append("- %s" % warning)
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("由「文档文字识别」（shh14-ocr）在本地离线提取，未上传任何内容。")
    lines.append("")
    return "\n".join(lines)


def _render_image_metadata(result, plain=True):
    meta = result.get("metadata") or {}
    stats = result.get("stats") or {}
    rows = [
        ("文件", result.get("name") or ""),
        ("路径", result.get("path") or ""),
        ("格式", meta.get("format") or result.get("format") or ""),
        ("尺寸", "%s x %s" % (meta.get("width") or "?", meta.get("height") or "?")),
        ("像素", meta.get("pixels") or 0),
        ("位深", stats.get("bit_depth") or ""),
        ("大小", result.get("size") or 0),
    ]
    exif = meta.get("exif") or {}
    if not plain:
        header = "| 项 | 值 |\n|---|---|"
        body = "\n".join("| %s | %s |" % (key, value) for key, value in rows)
        parts = [header, body]
        if exif:
            parts.append("")
            parts.append("**EXIF**")
            parts.append("")
            parts.append("| 标签 | 值 |\n|---|---|")
            parts.append("\n".join("| %s | %s |" % (key, value)
                                   for key, value in sorted(exif.items())))
        return "\n".join(parts)
    lines = ["%s: %s" % (key, value) for key, value in rows]
    if exif:
        lines.append("")
        lines.append("EXIF:")
        lines.extend("  %s: %s" % (key, value) for key, value in sorted(exif.items()))
    return "\n".join(lines)


def render_json(result, pretty=True):
    """JSON：**带逐页字符数**，一眼看出哪几页没有文字层（设计文档 §12.3）。"""
    payload = {
        "ok": result.get("ok"),
        "kind": result.get("kind"),
        "path": result.get("path"),
        "name": result.get("name"),
        "size": result.get("size"),
        "format": result.get("format"),
        "status": result.get("status"),
        "status_label": label(result.get("status")),
        "error": result.get("error"),
        "hint": result.get("hint"),
        "page_count": result.get("page_count"),
        "chars": result.get("chars"),
        "text_truncated": bool(result.get("text_truncated")),
        "pages": [
            {
                "index": page.get("index"),
                "chars": page.get("chars"),
                "has_text": page.get("has_text"),
                "image_count": page.get("image_count"),
                "warnings": page.get("warnings") or [],
            }
            for page in (result.get("pages") or [])
        ],
        "metadata": result.get("metadata") or {},
        "stats": result.get("stats") or {},
        "warnings": result.get("warnings") or [],
        "extracted_at": result.get("extracted_at"),
        "text": result.get("text") or "",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None,
                      default=str)


RENDERERS = {"txt": render_txt, "md": render_markdown, "json": render_json}


def render(result, fmt):
    renderer = RENDERERS.get(fmt)
    if renderer is None:
        raise ExtractError("不支持的输出格式：%s（可选 %s）" % (fmt, "/".join(FORMATS)))
    return renderer(result)


EXT_BY_FORMAT = {"txt": ".txt", "md": ".md", "json": ".json"}


def safe_name(name, fallback="document"):
    """把文件名里不能当路径用的字符换掉（输出文件名来自用户文件，必须消毒）。"""
    cleaned = "".join(
        char if (char.isalnum() or char in "._- ()[]中文") else "_"
        for char in str(name or "")
    ).strip(" .")
    return cleaned or fallback


def output_path(out_dir, rel_dir, name, fmt, max_len=110):
    """算出输出文件路径：重名**不覆盖**，而是追加 -1、-2。"""
    stem = os.path.splitext(safe_name(name))[0][:max_len] or "document"
    target_dir = os.path.join(out_dir, rel_dir) if rel_dir else out_dir
    os.makedirs(target_dir, exist_ok=True)
    candidate = os.path.join(target_dir, stem + EXT_BY_FORMAT[fmt])
    index = 1
    while os.path.exists(candidate):
        candidate = os.path.join(target_dir, "%s-%d%s" % (stem, index, EXT_BY_FORMAT[fmt]))
        index += 1
        if index > 999:
            break
    return candidate


def write_output(result, out_dir, rel_dir, fmt):
    """写出一种格式，返回文件路径。"""
    path = output_path(out_dir, rel_dir, result.get("name") or "document", fmt)
    content = render(result, fmt)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    return path
