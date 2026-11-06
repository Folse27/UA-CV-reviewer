"""PDF / DOCX / image handling, plus the temp-folder sweep."""
import base64
import logging
import os
import re
import time
import zipfile

import fitz

from config import ADMIN_PAGES, MAX_VISION_IMAGES, TMP_DIR, TMP_RETENTION_DAYS, VISION_PAGES

log = logging.getLogger(__name__)

# A PDF yielding less than this is almost certainly a scan, not a text layer.
SCAN_THRESHOLD = 200

# Both bound what a stranger's upload can make this process allocate.
MAX_EDGE = 1600            # px on a page's long side
MAX_DOCX_XML = 20_000_000  # uncompressed bytes of word/document.xml


def page_count(path: str) -> int:
    with fitz.open(path) as doc:
        return doc.page_count


def render_pages(path: str, limit: int = MAX_VISION_IMAGES) -> list[str]:
    """PDF pages -> PNG data URLs, capped at `limit` pages."""
    out = []
    with fitz.open(path) as doc:
        for page in doc.pages(0, min(limit, doc.page_count)):
            # Page geometry is attacker-controlled: a 200-inch page at a fixed dpi
            # would allocate gigabytes. Scale to a bounded long edge instead.
            longest = max(page.rect.width, page.rect.height, 1)
            scale = min(MAX_EDGE / longest, 140 / 72)
            png = page.get_pixmap(matrix=fitz.Matrix(scale, scale)).tobytes("png")
            out.append("data:image/png;base64," + base64.b64encode(png).decode())
    return out


def image_data_url(path: str) -> str:
    ext = "png" if path.lower().endswith(".png") else "jpeg"
    with open(path, "rb") as f:
        return f"data:image/{ext};base64," + base64.b64encode(f.read()).decode()


def extract_text(path: str) -> str:
    """sort=True reorders spans top-to-bottom, left-to-right, which is what
    rescues two-column CVs from interleaved garbage."""
    chunks = []
    with fitz.open(path) as doc:
        for page in doc:
            chunks.append(page.get_text("text", sort=True))
    return "\n".join(chunks).strip()


def docx_text(path: str) -> str:
    """Paragraph and table text straight out of the OOXML, no dependency."""
    with zipfile.ZipFile(path) as z:
        # Check the declared size before decompressing — a zip bomb is a few KB on disk.
        size = z.getinfo("word/document.xml").file_size
        if size > MAX_DOCX_XML:
            raise ValueError(f"word/document.xml claims {size} bytes, refusing to decompress")
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def route(kind: str, pages: int, png_requested: bool) -> str:
    """'vision' | 'text' | 'admin' — decided on page count alone, so it is testable."""
    if kind == "image":
        return "vision"
    if kind == "docx":
        return "text"
    if pages > ADMIN_PAGES:
        return "admin"
    if pages <= VISION_PAGES:
        return "vision"
    return "vision" if png_requested else "text"


def sweep_tmp(days: int = TMP_RETENTION_DAYS) -> int:
    os.makedirs(TMP_DIR, exist_ok=True)
    cutoff = time.time() - days * 86400
    removed = 0
    for name in os.listdir(TMP_DIR):
        path = os.path.join(TMP_DIR, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            log.warning("could not remove temp file %s", path, exc_info=True)
    if removed:
        log.info("swept %d temp files older than %d days", removed, days)
    return removed
