"""Pre-push checks for this repository.

    python check.py          safety + offline tests, no network
    python check.py --live   also calls Neon, Groq and Telegram

Exits non-zero if anything fails, so it works unchanged in CI.
Nothing here ever prints a secret — only the name of the variable that leaked.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# Shapes of credentials that must never reach a public repo.
PATTERNS = {
    "Telegram bot token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "Groq API key": re.compile(r"\bgsk_[A-Za-z0-9]{32,}\b"),
    "database URL with password": re.compile(r"\bpostgres(?:ql)?://[^\s:/@]+:[^\s@/]+@"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

# Obvious stand-ins in .env.example and docs, not real credentials.
PLACEHOLDER = re.compile(r"USER|PASSWORD|HOST|user:pass|example|your|xxx|<", re.I)

# Variables whose values are secret. Model names, PORT and WEBHOOK_URL are not.
SECRET_HINT = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|DATABASE_URL|CHAT_ID", re.I)

MUST_BE_IGNORED = [".env", "ENVIRONMENT.md", "PLAN.md", "STATE.md", "tmp", ".venv", "__pycache__"]

# Everything the live checks need; absent when run outside the project venv.
REQUIRED_MODULES = ("dotenv", "fitz", "groq", "telegram", "psycopg")

failures = []


def report(ok, name, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(name)
    return ok


def redact_url(url):
    """The webhook's path IS the bot token, so only ever show scheme and host."""
    if not url:
        return "none registered (polling mode)"
    m = re.match(r"(https?://[^/]+)(/.*)?$", url)
    if not m:
        return "<unrecognised url, withheld>"
    return m.group(1) + ("/<redacted>" if m.group(2) else "")


def missing_dependency():
    """Name of the first importable-by-the-app module this interpreter lacks."""
    for mod in REQUIRED_MODULES:
        try:
            __import__(mod)
        except ModuleNotFoundError:
            return mod
    return None


def venv_python():
    sub = "Scripts" if os.name == "nt" else "bin"
    exe = "python.exe" if os.name == "nt" else "python"
    path = os.path.join(ROOT, ".venv", sub, exe)
    return path if os.path.exists(path) else None


def git(*args, binary=False):
    r = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, check=False
    )
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def publishable_files():
    """Exactly what a push would publish: tracked plus untracked-but-not-ignored."""
    out = git("ls-files", "-co", "--exclude-standard")
    return [f for f in out.splitlines() if f.strip()]


def env_secrets():
    """{VAR: value} for the secret-looking variables in a local .env, if present."""
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return {}
    found = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if len(v) >= 8 and SECRET_HINT.search(k):
                found[k] = v
    return found


def read_text(rel):
    try:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()
    except (UnicodeDecodeError, OSError):
        return None  # binary or unreadable; nothing to scan


def scan_working_tree(secrets):
    files = publishable_files()
    hits = []
    for rel in files:
        text = read_text(rel)
        if text is None:
            continue
        for var, value in secrets.items():
            if value in text:
                line = text[: text.index(value)].count("\n") + 1
                hits.append(f"{rel}:{line} contains the live value of {var}")
        if rel == ".env.example":
            continue  # placeholders live here by design; the literal check above still ran
        for label, pat in PATTERNS.items():
            for m in pat.finditer(text):
                if not PLACEHOLDER.search(m.group(0)):
                    line = text[: m.start()].count("\n") + 1
                    hits.append(f"{rel}:{line} looks like a {label}")
    report(not hits, f"Working tree clean ({len(files)} files would be published)")
    for h in hits:
        print(f"      -> {h}")
    return not hits


def scan_history(secrets):
    if not git("rev-list", "-n", "1", "--all").strip():
        return report(True, "History clean", "no commits yet")
    if not secrets:
        return report(True, "History clean", "no local .env to compare against")
    # Stream every blob through Python so no secret is ever passed on a command line.
    blob = git("cat-file", "--batch-all-objects", "--batch", binary=True)
    hits = [var for var, value in secrets.items() if value.encode() in blob]
    report(not hits, "History clean")
    for var in hits:
        print(f"      -> {var} appears in a past commit; rotate it, rewriting history is not enough")
    return not hits


def check_gitignore():
    missing = [
        p
        for p in MUST_BE_IGNORED
        if subprocess.run(
            ["git", "check-ignore", "-q", p], cwd=ROOT, check=False
        ).returncode != 0
    ]
    report(not missing, "Private paths ignored")
    for p in missing:
        print(f"      -> {p} is NOT ignored")
    return not missing


def check_env_example():
    example, real = read_text(".env.example"), env_secrets()
    if example is None:
        return report(False, ".env.example present")
    keys = {line.split("=", 1)[0].strip() for line in example.splitlines() if "=" in line and not line.startswith("#")}
    missing = [k for k in real if k not in keys]
    report(not missing, ".env.example documents every variable")
    for k in missing:
        print(f"      -> {k} is set locally but absent from .env.example")
    return not missing


def run_tests():
    r = subprocess.run([sys.executable, "test_smoke.py"], cwd=ROOT, capture_output=True, text=True)
    ok = r.returncode == 0
    report(ok, "Offline tests", r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")
    if not ok:
        print((r.stdout + r.stderr).strip())
    return ok


async def live_checks():
    import tempfile

    import fitz
    import telegram

    import ai
    import config
    import db
    import files

    try:
        await db.init()
        await db.save(0, chat_summary="check.py")
        assert (await db.load(0))["chat_summary"] == "check.py"
        async with db._pool.connection() as c:
            await c.execute("DELETE FROM sessions WHERE chat_id=0")
        await db.close()
        report(True, "Database", "schema, round-trip and purge all work")
    except Exception as e:
        report(False, "Database", f"{type(e).__name__}: {e}")

    try:
        r = await ai.chat("", "", "Привіт, поглянь на моє резюме")
        assert r["reply"]
        report(True, "Groq text + JSON mode", config.GROQ_TEXT_MODEL)
    except Exception as e:
        report(False, "Groq text + JSON mode", f"{type(e).__name__}: {e}")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "probe.pdf")
        doc = fitz.open()
        doc.new_page().insert_text((72, 100), "Ivan Petrenko - Python Developer", fontsize=14)
        doc.save(path)
        doc.close()
        try:
            await ai.transcribe(files.render_pages(path))
            report(True, "Groq vision", config.GROQ_VISION_MODEL)
        except ai.VisionUnavailable as e:
            report(False, "Groq vision", f"model rejected: {e.__cause__}")
        except Exception as e:
            report(False, "Groq vision", f"{type(e).__name__}: {e}")

    bot = telegram.Bot(config.TELEGRAM_TOKEN)
    try:
        me = await bot.get_me()
        report(True, "Telegram token", f"@{me.username}")
        member = await bot.get_chat_member(config.ADMIN_CHAT_ID, me.id)
        is_admin = member.status in ("administrator", "creator")
        report(is_admin, "Bot is admin of the alert group", f"status: {member.status}")
        info = await bot.get_webhook_info()
        report(True, "Webhook", redact_url(info.url))
    except Exception as e:
        report(False, "Telegram", f"{type(e).__name__}: {e}")


def main():
    live = "--live" in sys.argv
    secrets = env_secrets()
    print(f"-- safety ({len(secrets)} secret variables found locally to check against)")
    scan_working_tree(secrets)
    scan_history(secrets)
    check_gitignore()
    check_env_example()
    # The safety checks above need no dependencies; everything below does.
    print("\n-- offline")
    missing = missing_dependency()
    if missing:
        report(False, "Dependencies available", f"this interpreter has no module '{missing}'")
        print(f"      -> running under {sys.executable}")
        venv = venv_python()
        if venv:
            print(f"      -> use the project venv: {venv} {' '.join(sys.argv)}")
        else:
            print("      -> create a venv, then: pip install -r requirements.txt")
    else:
        run_tests()
        if live:
            print("\n-- live services")
            import asyncio

            import config  # noqa: F401  (sets the Windows event loop policy on import)

            asyncio.run(live_checks())
        else:
            print("\n(skipping live service checks; pass --live to include them)")

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: " + ", ".join(failures))
        return 1
    print("ALL CHECKS PASSED" + ("" if live else " (offline only)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
