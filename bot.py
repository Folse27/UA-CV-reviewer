"""Telegram CV reviewer. One process: batching, admin gates, Groq, webhook."""
import asyncio
import logging
import os
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import ai
import db
import files
import state
from config import (
    ADMIN_CHAT_ID,
    ADMIN_PAGES,
    BATCH_WINDOW,
    CV_CHARS,
    MAX_PHOTOS,
    MAX_VISION_IMAGES,
    PORT,
    TELEGRAM_TOKEN,
    TMP_DIR,
    VISION_PAGES,
    WEBHOOK_URL,
)

log = logging.getLogger("bot")

MSG = {
    "start": {
        "uk": "Вітаю! Я перевіряю резюме. Надішліть ваше CV файлом (PDF, DOCX) або фото — і я розберу його по пунктах.",
        "ru": "Здравствуйте! Я проверяю резюме. Пришлите ваше CV файлом (PDF, DOCX) или фото — и я разберу его по пунктам.",
    },
    "processing": {"uk": "Опрацьовую ваш запит", "ru": "Обрабатываю ваш запрос"},
    "photos": {
        "uk": "Не можна відправити більше ніж 2 фото",
        "ru": "Нельзя отправить больше 2 фото",
    },
    "rate_msgs": {
        "uk": "Забагато повідомлень поспіль. Зачекайте хвилинку, будь ласка, і напишіть ще раз.",
        "ru": "Слишком много сообщений подряд. Подождите минутку, пожалуйста, и напишите снова.",
    },
    "rate_pdfs": {
        "uk": "Забагато файлів за хвилину. Можна до 5 документів на хвилину — спробуйте трохи пізніше.",
        "ru": "Слишком много файлов за минуту. Можно до 5 документов в минуту — попробуйте чуть позже.",
    },
    "png_cooldown": {
        "uk": "Розбір у вигляді зображень доступний раз на 10 хвилин. Цього разу аналізую текстом.",
        "ru": "Разбор в виде изображений доступен раз в 10 минут. В этот раз анализирую текстом.",
    },
    "docx_png": {
        "uk": "DOCX не можна показати моделі як зображення. Збережіть його в PDF — тоді розберу разом із версткою.",
        "ru": "DOCX нельзя показать модели как изображение. Сохраните его в PDF — тогда разберу вместе с вёрсткой.",
    },
    "denied": {
        "uk": "Дякуємо за звернення. На жаль, ми не беремо в роботу резюме такого обсягу — наш розбір розрахований на документи до 8 сторінок. Скоротіть CV до найважливішого досвіду й надішліть ще раз, будь ласка, я з радістю його розберу.",
        "ru": "Спасибо за обращение. К сожалению, мы не берём в работу резюме такого объёма — наш разбор рассчитан на документы до 8 страниц. Сократите CV до самого важного опыта и пришлите снова, пожалуйста, я с радостью его разберу.",
    },
    "blocked": {
        "uk": "Я призупиняю діалог: питання не стосуються резюме. Зверніться до адміністратора, щоб продовжити.",
        "ru": "Я приостанавливаю диалог: вопросы не касаются резюме. Обратитесь к администратору, чтобы продолжить.",
    },
    "resumed": {
        "uk": "Діалог відновлено. Надішліть, будь ласка, ваше резюме.",
        "ru": "Диалог возобновлён. Пришлите, пожалуйста, ваше резюме.",
    },
    "resend": {
        "uk": "Запит підтверджено, але файл уже не збережений. Надішліть його ще раз, будь ласка.",
        "ru": "Запрос подтверждён, но файл уже не сохранён. Пришлите его ещё раз, пожалуйста.",
    },
    "busy": {
        "uk": "Зараз завелике навантаження на сервіс. Спробуйте, будь ласка, за кілька хвилин.",
        "ru": "Сейчас слишком большая нагрузка на сервис. Попробуйте, пожалуйста, через несколько минут.",
    },
    "error": {
        "uk": "Щось пішло не так під час обробки. Спробуйте надіслати ще раз, будь ласка.",
        "ru": "Что-то пошло не так во время обработки. Попробуйте отправить ещё раз, пожалуйста.",
    },
    "unsupported": {
        "uk": "Такий формат я не читаю. Надішліть PDF, DOCX або фото резюме.",
        "ru": "Такой формат я не читаю. Пришлите PDF, DOCX или фото резюме.",
    },
    "novision": {
        "uk": "Не вдалося прочитати зображення. Надішліть, будь ласка, резюме файлом PDF.",
        "ru": "Не удалось прочитать изображение. Пришлите, пожалуйста, резюме файлом PDF.",
    },
}

PNG_RE = re.compile(
    r"png|jpe?g|картинк|зображен|изображен|скрін|скрин|візуальн|визуальн|верстк|"
    r"як виглядає|как выглядит|у вигляді фото|в виде фото",
    re.I,
)


def guess_lang(text: str) -> str:
    """Picks the language of the canned messages only — the model handles its own
    replies. shortcut: letter-frequency guess, many sentences carry no distinctive
    letter at all and fall back to Ukrainian; replace if ru-speaking users complain."""
    if re.search(r"[ыэъёЫЭЪЁ]", text or ""):
        return "ru"
    return "uk"


def kind_of(name: str, mime: str) -> str:
    n = (name or "").lower()
    if (mime or "").startswith("image/") or n.endswith((".png", ".jpg", ".jpeg", ".webp")):
        return "image"
    if n.endswith(".pdf") or mime == "application/pdf":
        return "pdf"
    if n.endswith(".docx"):
        return "docx"
    return "other"


def who(update: Update) -> str:
    u = update.effective_user
    tag = f"@{u.username}" if u.username else "без username"
    return f"{u.full_name} ({tag}, id {u.id})"


async def say(ctx, chat_id: int, key: str, lang: str) -> None:
    await ctx.bot.send_message(chat_id, MSG[key].get(lang, MSG[key]["uk"]))


async def admin(ctx, text: str, markup=None) -> None:
    try:
        await ctx.bot.send_message(ADMIN_CHAT_ID, text, reply_markup=markup)
    except Exception:
        log.exception("could not reach the admin chat %s", ADMIN_CHAT_ID)


async def _typing(bot, chat_id: int) -> None:
    """Telegram's typing status expires after ~5s, so it has to be refreshed."""
    while True:
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            return
        await asyncio.sleep(4)


async def set_status(chat_id: int, status: str, **extra) -> None:
    await db.save(chat_id, status=status, **extra)
    state.STATUS[chat_id] = status


async def get_status(chat_id: int) -> str:
    if chat_id not in state.STATUS:
        state.STATUS[chat_id] = (await db.load(chat_id))["status"]
    return state.STATUS[chat_id]


# ---------------------------------------------------------------- collecting


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if await get_status(chat_id) != "ok":
        return
    await say(ctx, chat_id, "start", guess_lang(update.effective_user.language_code))


async def collect(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat_id = update.effective_chat.id

    if await get_status(chat_id) != "ok":
        return  # frozen pending an admin decision, or blocked

    sess = await db.load(chat_id)
    lang = sess["lang"] or guess_lang(msg.text or msg.caption or "")

    if not state.allow_message(chat_id):
        if state.alert_once(chat_id, "msgs"):
            await say(ctx, chat_id, "rate_msgs", lang)
            await admin(ctx, "Ліміт повідомлень (10/хв) вичерпано: " + who(update))
        return

    doc = msg.document
    if doc and kind_of(doc.file_name, doc.mime_type) in ("pdf", "docx"):
        if not state.allow_pdf(chat_id):
            if state.alert_once(chat_id, "pdfs"):
                await say(ctx, chat_id, "rate_pdfs", lang)
                await admin(ctx, "Ліміт документів (5/хв) вичерпано: " + who(update))
            return

    b = state.BATCHES.setdefault(
        chat_id, {"texts": [], "docs": [], "photos": [], "who": who(update)}
    )
    for t in (msg.text, msg.caption):
        if t:
            b["texts"].append(t)
    if msg.photo:
        b["photos"].append(msg.photo[-1].file_id)
    if doc:
        k = kind_of(doc.file_name, doc.mime_type)
        if k == "image":
            b["photos"].append(doc.file_id)
        elif k == "other":
            await say(ctx, chat_id, "unsupported", lang)
        else:
            b["docs"].append((doc.file_id, doc.file_name or "cv.pdf", k))

    if "task" not in b:
        b["task"] = asyncio.create_task(_flush_after_window(chat_id, ctx))


async def _flush_after_window(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.sleep(BATCH_WINDOW)
    b = state.BATCHES.pop(chat_id, None)
    if not b:
        return
    lang = (await db.load(chat_id))["lang"] or "uk"
    try:
        await handle(chat_id, ctx, b, lang)
    except ai.Overloaded:
        await say(ctx, chat_id, "busy", lang)
    except Exception:
        log.exception("batch failed for chat %s", chat_id)
        await say(ctx, chat_id, "error", lang)


# ----------------------------------------------------------------- handling


async def download(ctx, file_id: str, chat_id: int, name: str) -> str:
    os.makedirs(TMP_DIR, exist_ok=True)
    safe = re.sub(r"[^\w.\-]", "_", name)[-60:]
    path = os.path.join(TMP_DIR, f"{chat_id}_{int(time.time() * 1000)}_{safe}")
    f = await ctx.bot.get_file(file_id)
    await f.download_to_drive(path)
    return path


async def handle(chat_id: int, ctx, b: dict, lang: str) -> None:
    if len(b["photos"]) > MAX_PHOTOS:
        await say(ctx, chat_id, "photos", lang)
        if state.alert_once(chat_id, "photos"):
            await admin(
                ctx,
                f"Надіслано {len(b['photos'])} фото за раз (ліміт {MAX_PHOTOS}): {b['who']}",
            )
        return

    text = "\n".join(b["texts"]).strip()
    lang = lang or guess_lang(text)

    docs = []
    for file_id, name, kind in b["docs"]:
        path = await download(ctx, file_id, chat_id, name)
        pages = files.page_count(path) if kind == "pdf" else 0
        docs.append({"path": path, "name": name, "kind": kind, "pages": pages})

    oversized = [d for d in docs if d["pages"] > ADMIN_PAGES]
    if oversized:
        state.AWAITING[chat_id] = {
            "text": text,
            "docs": docs,
            "photos": b["photos"],
            "who": b["who"],
            "lang": lang,
            "ids": True,
        }
        await set_status(chat_id, "pending")
        await admin(
            ctx,
            f"Завелике резюме: {oversized[0]['pages']} стор. (ліміт {ADMIN_PAGES})\n"
            f"Від: {b['who']}\nФайл: {oversized[0]['name']}\n\n"
            "Користувач ігнорується, доки не буде рішення.",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Опрацювати", callback_data=f"ok:{chat_id}"),
                        InlineKeyboardButton("Відмовити", callback_data=f"deny:{chat_id}"),
                    ]
                ]
            ),
        )
        return

    photos = [await download(ctx, fid, chat_id, "photo.jpg") for fid in b["photos"]]
    await process(
        chat_id,
        ctx,
        {"text": text, "docs": docs, "photos": photos, "who": b["who"], "lang": lang},
    )


async def read_documents(chat_id: int, ctx, payload: dict) -> str:
    """Turn every attachment into CV text, choosing vision or extraction per document."""
    png_requested = bool(PNG_RE.search(payload["text"]))
    lang = payload["lang"]
    parts = []

    for d in payload["docs"]:
        if d["kind"] == "docx":
            if png_requested:
                await say(ctx, chat_id, "docx_png", lang)
            parts.append(files.docx_text(d["path"]))
            continue

        mode = files.route(d["kind"], d["pages"], png_requested)
        # The 10-minute quota only applies when images were asked for; short CVs
        # go through vision by default and must not burn it.
        if mode == "vision" and d["pages"] > VISION_PAGES:
            if not state.allow_png(chat_id):
                await say(ctx, chat_id, "png_cooldown", lang)
                mode = "text"

        if mode == "text":
            txt = files.extract_text(d["path"])
            if len(txt) < files.SCAN_THRESHOLD and d["pages"] <= MAX_VISION_IMAGES:
                mode = "vision"  # a scan with no text layer
            else:
                parts.append(txt)

        if mode == "vision":
            try:
                parts.append(await ai.transcribe(files.render_pages(d["path"])))
            except ai.VisionUnavailable:
                parts.append(files.extract_text(d["path"]))

    if payload["photos"]:
        try:
            parts.append(await ai.transcribe([files.image_data_url(p) for p in payload["photos"]]))
        except ai.VisionUnavailable:
            await say(ctx, chat_id, "novision", lang)

    return "\n\n---\n\n".join(p for p in parts if p).strip()


async def process(chat_id: int, ctx, payload: dict) -> None:
    lang = payload["lang"]
    await say(ctx, chat_id, "processing", lang)
    typing = asyncio.create_task(_typing(ctx.bot, chat_id))
    try:
        sess = await db.load(chat_id)
        new_cv = await read_documents(chat_id, ctx, payload)
        cv = (new_cv or sess["current_cv"])[:CV_CHARS]

        user_text = payload["text"]
        if new_cv:
            prompt = "[Користувач щойно надіслав резюме, його текст у контексті. Зроби розбір.]"
            user_text = (user_text + "\n\n" if user_text else "") + prompt
        if not user_text:
            return

        res = await ai.chat(sess["chat_summary"], cv, user_text)
        streak = sess["offtopic"] + 1 if res["offtopic"] else 0
        blocked = streak >= 3

        await db.save(
            chat_id,
            chat_summary=res["chat_summary"],
            current_cv=res["current_cv"],
            lang=res["lang"] or lang,
            offtopic=streak,
            status="blocked" if blocked else "ok",
        )
        state.STATUS[chat_id] = "blocked" if blocked else "ok"

        if res["reply"]:
            await ctx.bot.send_message(chat_id, res["reply"])
        if blocked:
            await say(ctx, chat_id, "blocked", res["lang"] or lang)
            await admin(
                ctx,
                "Зловживання: 3 запити поспіль не по темі резюме.\n"
                f"Від: {payload['who']}\nДіалог призупинено.",
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Відновити", callback_data=f"resume:{chat_id}")]]
                ),
            )
    finally:
        typing.cancel()


# ------------------------------------------------------------ admin buttons


async def admin_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if q.message.chat.id != ADMIN_CHAT_ID:
        return

    action, raw = q.data.split(":", 1)
    chat_id = int(raw)
    payload = state.AWAITING.pop(chat_id, None)
    lang = (payload or {}).get("lang") or (await db.load(chat_id))["lang"] or "uk"
    by = q.from_user.full_name

    if action == "ok":
        await set_status(chat_id, "ok")
        await q.edit_message_text(f"{q.message.text}\n\nОпрацьовано — {by}")
        if payload:
            payload["photos"] = [
                await download(ctx, fid, chat_id, "photo.jpg") for fid in payload["photos"]
            ]
            await process(chat_id, ctx, payload)
        else:
            await say(ctx, chat_id, "resend", lang)
    elif action == "deny":
        await set_status(chat_id, "ok")
        await q.edit_message_text(f"{q.message.text}\n\nВідмовлено — {by}")
        await say(ctx, chat_id, "denied", lang)
    elif action == "resume":
        await set_status(chat_id, "ok", offtopic=0)
        await q.edit_message_text(f"{q.message.text}\n\nВідновлено — {by}")
        await say(ctx, chat_id, "resumed", lang)


# ------------------------------------------------------------------ startup


async def on_start(app: Application) -> None:
    await db.init()
    files.sweep_tmp()
    log.info("ready; mode=%s", "webhook" if WEBHOOK_URL else "polling")


def main() -> None:
    # PTB 21 calls asyncio.get_event_loop(), which stopped creating one implicitly
    # in 3.14. Making it here also keeps the selector loop psycopg needs on Windows.
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(on_start).build()
    app.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(admin_action))
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (filters.TEXT | filters.PHOTO | filters.Document.ALL),
            collect,
        )
    )
    if WEBHOOK_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TELEGRAM_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}",
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
