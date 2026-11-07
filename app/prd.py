"""PRD 文件提取：pdf / docx / md / txt，≤10MB，截断 20000 字。"""

from __future__ import annotations

import io
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024
MAX_CHARS = 20_000
MAX_PDF_PAGES = 50


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _pdf_text(raw: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    parts = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        parts.append(page.extract_text() or "")
    return "\n".join(parts)


def _docx_text(raw: bytes) -> str:
    from docx import Document

    document = Document(io.BytesIO(raw))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


def extract(filename: str | None, raw: bytes) -> str:
    if not raw:
        raise ValueError("文件为空")
    if len(raw) > MAX_BYTES:
        raise ValueError("文件超过 10MB，请压缩后再上传")

    suffix = Path(filename or "").suffix.lower()
    try:
        if suffix == ".pdf":
            text = _pdf_text(raw)
        elif suffix == ".docx":
            text = _docx_text(raw)
        elif suffix in {".md", ".markdown", ".txt"}:
            text = _decode(raw)
        else:
            raise ValueError("只支持 pdf / docx / md / txt 文件")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"文件解析失败：{exc}") from exc

    text = text.strip()
    if not text:
        raise ValueError("没有提取到文本，请尝试粘贴内容")
    return text[:MAX_CHARS]
