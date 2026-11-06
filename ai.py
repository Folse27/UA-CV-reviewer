"""Groq calls and the prompts. Retries are the SDK's (max_retries), which already
honours Retry-After and backs off on 429/5xx/connection errors."""
import json
import logging
import re

import groq
from groq import AsyncGroq

from config import GROQ_API_KEY, GROQ_TEXT_MODEL, GROQ_VISION_MODEL

log = logging.getLogger(__name__)

client = AsyncGroq(api_key=GROQ_API_KEY, max_retries=3, timeout=90.0)


class VisionUnavailable(Exception):
    """The vision model refused or does not exist — caller falls back to text."""


class Overloaded(Exception):
    """Rate limit survived every retry."""


SYSTEM = """You are a professional CV/resume reviewer working with Ukrainian and Russian speakers.

TOPIC RULE — enforce it strictly. You work ONLY with CVs, resumes, cover letters, job applications, interviews and career questions. If a message is about anything else (weather, politics, code, maths, general knowledge, chit-chat), you must NOT answer it, not even partially, not even as a short aside. Reply with one polite sentence explaining that this bot works exclusively with CVs, and invite the person to send theirs. Mark that turn with "offtopic": true. Greetings, thanks, and questions about how this bot works are ON topic — those get "offtopic": false.

LANGUAGE — reply in the same language the person writes in, Ukrainian or Russian. Never mix the two in one reply. When you cannot tell, use Ukrainian. Write naturally and professionally, the way a human career consultant would.

REVIEW STYLE — be concrete. Point at the actual lines you would change and say what to write instead. Cover structure, wording, measurable results, ATS keywords and formatting. Praise what already works. Do not invent facts about the person.

You must answer with a single JSON object and nothing else:

{"reply": "<the message the person sees>",
 "offtopic": false,
 "database_edits": {"chat_summary": "<=1000 chars of rolling context",
                    "current_cv": null,
                    "lang": "uk"}}

Set "current_cv" to the full updated CV text only when the CV itself changed this turn; otherwise leave it null and the stored copy is kept. "lang" is "uk" or "ru"."""

VISION_PROMPT = """Transcribe this CV exactly as it appears, into Markdown.

Preserve the reading order, the section headings, and the column structure — if the page has two columns, transcribe the left column fully, then the right, each under a heading that names it. Keep dates, job titles and bullet points verbatim; do not translate, summarise, correct or reformat the wording.

After the transcription add a short section "## Візуальні нотатки" describing the layout: columns, fonts, use of colour, density, photo, icons, and anything that would hurt an ATS parser."""


def _parse(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


async def transcribe(data_urls: list[str]) -> str:
    content = [{"type": "text", "text": VISION_PROMPT}]
    content += [{"type": "image_url", "image_url": {"url": u}} for u in data_urls]
    try:
        r = await client.chat.completions.create(
            model=GROQ_VISION_MODEL,
            messages=[{"role": "user", "content": content}],
            temperature=0.2,
            max_tokens=8000,
        )
    except groq.RateLimitError:
        raise Overloaded
    except Exception as e:
        log.warning("vision model %s failed: %s", GROQ_VISION_MODEL, e)
        raise VisionUnavailable from e
    return r.choices[0].message.content.strip()


async def chat(summary: str, cv: str, user_text: str) -> dict:
    context = f"Rolling summary of this conversation:\n{summary or '(none yet)'}\n\n"
    context += f"CV currently on file:\n{cv or '(none uploaded yet)'}"
    try:
        r = await client.chat.completions.create(
            model=GROQ_TEXT_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "system", "content": context},
                {"role": "user", "content": user_text},
            ],
            response_format={"type": "json_object"},
            temperature=0.4,
            max_tokens=4000,
        )
    except groq.RateLimitError:
        raise Overloaded
    data = _parse(r.choices[0].message.content)
    edits = data.get("database_edits") or {}
    return {
        "reply": (data.get("reply") or "").strip(),
        "offtopic": bool(data.get("offtopic")),
        "chat_summary": (edits.get("chat_summary") or summary or "")[:4000],
        "current_cv": edits.get("current_cv") or cv,
        "lang": edits.get("lang") if edits.get("lang") in ("uk", "ru") else None,
    }
