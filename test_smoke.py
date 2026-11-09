"""Offline self-check: routing, parsing, limits. No network, no database.

Run: python test_smoke.py
"""
import base64
import os
import struct
import zipfile

os.environ.setdefault("TELEGRAM_TOKEN", "x")
os.environ.setdefault("GROQ_API_KEY", "x")
os.environ.setdefault("DATABASE_URL", "postgresql://x/x")
os.environ.setdefault("ADMIN_CHAT_ID", "-1")

import fitz

import ai
import bot
import files
import state


# Base-14 PDF fonts cannot encode Cyrillic, so the fixture text is Latin.
def make_pdf(path: str, pages: int, text: str = "Oleksandr Koval, Python developer") -> None:
    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 100), f"{text} {i}", fontsize=12)
    doc.save(path)
    doc.close()


def make_docx(path: str) -> None:
    xml = (
        '<?xml version="1.0"?><w:document xmlns:w="urn:x"><w:body>'
        "<w:p><w:r><w:t>Марія Шевченко</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Досвід: 5 років</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)


def test_routing():
    assert files.route("image", 0, False) == "vision"
    assert files.route("docx", 0, True) == "text"
    assert files.route("pdf", 1, False) == "vision"
    assert files.route("pdf", 2, False) == "vision"
    assert files.route("pdf", 3, False) == "text"
    assert files.route("pdf", 3, True) == "vision", "explicit PNG request overrides text mode"
    assert files.route("pdf", 8, True) == "vision"
    assert files.route("pdf", 9, True) == "admin", "over the limit goes to an admin regardless"
    assert files.route("pdf", 50, False) == "admin"


def test_pdf(tmp):
    one, nine = os.path.join(tmp, "one.pdf"), os.path.join(tmp, "nine.pdf")
    make_pdf(one, 1)
    make_pdf(nine, 9)
    assert files.page_count(one) == 1
    assert files.page_count(nine) == 9
    assert "Koval" in files.extract_text(one)
    urls = files.render_pages(one)
    assert len(urls) == 1 and urls[0].startswith("data:image/png;base64,")
    assert len(files.render_pages(nine)) == files.MAX_VISION_IMAGES, "vision images stay capped"


def png_size(data_url: str):
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return struct.unpack(">II", raw[16:24])


def test_render_is_bounded(tmp):
    normal, huge = os.path.join(tmp, "a4.pdf"), os.path.join(tmp, "huge.pdf")
    make_pdf(normal, 1)
    doc = fitz.open()
    doc.new_page(width=14400, height=14400)  # 200in, the largest a PDF may declare
    doc.save(huge)
    doc.close()

    w, h = png_size(files.render_pages(normal)[0])
    assert h > 1000, f"a normal page must still render legibly, got {w}x{h}"
    w, h = png_size(files.render_pages(huge)[0])
    assert max(w, h) <= files.MAX_EDGE, f"oversized page not clamped: {w}x{h}"


def test_docx_bomb(tmp):
    p = os.path.join(tmp, "bomb.docx")
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "a" * (files.MAX_DOCX_XML + 1))
    try:
        files.docx_text(p)
    except ValueError:
        pass
    else:
        raise AssertionError("an oversized document.xml must be refused before decompression")


def test_docx(tmp):
    p = os.path.join(tmp, "cv.docx")
    make_docx(p)
    out = files.docx_text(p)
    assert "Марія Шевченко" in out and "5 років" in out
    assert "<w:" not in out


def test_json_parsing():
    clean = '{"reply": "Привіт", "offtopic": false}'
    assert ai._parse(clean)["reply"] == "Привіт"
    fenced = '```json\n{"reply": "ok", "offtopic": true}\n```'
    assert ai._parse(fenced)["offtopic"] is True
    try:
        ai._parse("completely not json")
    except Exception:
        pass
    else:
        raise AssertionError("garbage must raise, so the caller can send the fallback")


def test_limits():
    chat = 999
    assert all(state.allow_message(chat) for _ in range(10)), "10 per minute must pass"
    assert not state.allow_message(chat), "the 11th must be rejected"
    assert all(state.allow_pdf(chat) for _ in range(5))
    assert not state.allow_pdf(chat), "the 6th document must be rejected"
    assert state.allow_png(chat)
    assert not state.allow_png(chat), "PNG review is once per 10 minutes"
    assert state.alert_once(chat, "msgs")
    assert not state.alert_once(chat, "msgs"), "admin alerts must not flood"


def test_helpers():
    assert bot.guess_lang("Можно ли исправить это резюме?") == "ru", "э marks Russian"
    assert bot.guess_lang("Чи можна виправити моє резюме?") == "uk"
    assert bot.guess_lang("Hello") == "uk", "no distinctive letter defaults to Ukrainian"
    assert bot.kind_of("cv.PDF", None) == "pdf"
    assert bot.kind_of("cv.docx", None) == "docx"
    assert bot.kind_of("scan.jpeg", None) == "image"
    assert bot.kind_of("photo", "image/png") == "image"
    assert bot.kind_of("notes.txt", "text/plain") == "other"
    assert bot.PNG_RE.search("подивись на зображення резюме")
    assert bot.PNG_RE.search("посмотри как выглядит мое резюме")
    assert not bot.PNG_RE.search("перевір орфографію")


def test_webhook_url_redaction():
    """The webhook path is the bot token, so check.py must never print it whole."""
    import check

    # Assembled at runtime so no token-shaped literal ever sits in a scanned file.
    token = "1234567890:" + "A" * 35
    out = check.redact_url(f"https://ua-cv-reviewer.onrender.com/{token}")
    assert token not in out, f"token survived redaction: {out}"
    assert out == "https://ua-cv-reviewer.onrender.com/<redacted>"
    assert check.redact_url("") == "none registered (polling mode)"
    assert check.redact_url("https://host.example") == "https://host.example", "no path, nothing to hide"
    assert token not in check.redact_url(f"ftp://odd/{token}"), "unparsed input must fail closed"


def test_webhook_url_resolution():
    """config resolves this at import, so each case needs its own process.
    Getting it wrong means Render binds no port and the deploy fails opaquely."""
    import subprocess
    import sys

    def resolved(**overrides):
        env = dict(os.environ, TELEGRAM_TOKEN="x", GROQ_API_KEY="x", RENDER_EXTERNAL_URL="")
        env.update(DATABASE_URL="postgresql://x/x", ADMIN_CHAT_ID="-1", WEBHOOK_URL="")
        env.update(overrides)
        out = subprocess.run(
            [sys.executable, "-c", "import config; print(config.WEBHOOK_URL)"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            env=env,
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    assert resolved() == "", "nothing set must stay empty, so local runs long-poll"
    assert resolved(RENDER_EXTERNAL_URL="https://a.onrender.com") == "https://a.onrender.com"
    assert resolved(WEBHOOK_URL="https://b.com", RENDER_EXTERNAL_URL="https://a.onrender.com") == (
        "https://b.com"
    ), "an explicit WEBHOOK_URL must win"
    assert resolved(WEBHOOK_URL="bare.example.com") == "https://bare.example.com", "scheme added"
    assert resolved(WEBHOOK_URL="https://c.com/") == "https://c.com", "trailing slash stripped"


def test_sweep(tmp):
    import time

    old = os.path.join(files.TMP_DIR, "old_test_artifact.bin")
    os.makedirs(files.TMP_DIR, exist_ok=True)
    with open(old, "wb") as f:
        f.write(b"x")
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    files.sweep_tmp()
    assert not os.path.exists(old), "files older than 7 days must be swept"


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        test_routing()
        test_pdf(tmp)
        test_render_is_bounded(tmp)
        test_docx_bomb(tmp)
        test_docx(tmp)
        test_json_parsing()
        test_limits()
        test_helpers()
        test_webhook_url_redaction()
        test_webhook_url_resolution()
        test_sweep(tmp)
    print("all smoke checks passed")
