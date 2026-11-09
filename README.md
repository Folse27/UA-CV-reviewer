# UA CV Reviewer

A Telegram bot that reviews CVs in Ukrainian and Russian, built to run entirely on free tiers.

Send it a PDF, a DOCX or a photo of your CV and it replies with a concrete critique — structure, wording, measurable results, ATS keywords, formatting — in whichever of the two languages you wrote in. It keeps a rolling summary of the conversation, so follow-up questions work.

## How it works

A short CV is read **as an image**, so the model sees columns, spacing and visual hierarchy rather than a flattened text dump. A long one is read as text, because transcribing fifteen pages of images is slow and expensive. The choice is made on page count:

| Upload | Handling |
|---|---|
| Photo or image file | vision model |
| PDF, 1–2 pages | vision model |
| PDF, 3–8 pages | text extraction — unless you explicitly ask for a visual review |
| PDF, over 8 pages | held for a human decision, see below |
| DOCX | text extraction |
| PDF with no text layer | detected as a scan, falls back to vision |

Vision runs **once**, when the CV arrives, and its transcription is stored. Every later message is a text-only call against that stored copy. That keeps a conversation affordable on a free API tier, and it means the two jobs can use different models — which turns out to be necessary, since no single free model reliably does both strict JSON output and image input.

### Message batching

The bot waits **20 seconds** from your first message before doing anything, then processes everything that arrived in that window as one request. You can send a file, a sentence of context and two photos as four separate messages and get one coherent answer. The trade-off is deliberate: every reply is at least 20 seconds away.

### Limits and gates

- **10 messages and 5 documents per minute** per user
- **2 photos** per batch
- **Visual review on request: once per 10 minutes** per user
- **CVs over 8 pages** are not processed automatically. The operators' group gets a notification with approve/decline buttons, and the user is ignored until someone decides.
- **Off-topic questions are refused.** The topic rule lives in the system prompt, not in a keyword filter, so it holds up in both languages. Three off-topic messages in a row suspends the conversation and alerts the operators, who can resume it with a button.

Sessions expire after 3 days. Downloaded files are swept after 7.

## Stack

Every component has a permanent free tier.

| | |
|---|---|
| Bot | [python-telegram-bot](https://python-telegram-bot.org/) — its built-in webhook server means no separate web framework |
| AI | [Groq](https://groq.com/) — one text model with JSON mode, one vision model |
| Documents | [PyMuPDF](https://pymupdf.readthedocs.io/) for PDFs; DOCX via the standard library |
| Database | PostgreSQL via `psycopg3` — developed against [Neon](https://neon.tech/) |
| Hosting | [Render](https://render.com/) web service |

Six modules, no framework, no ORM, no migrations tool.

```
bot.py      handlers, batching, admin gates, orchestration
ai.py       Groq calls and prompts
db.py       connection pool, schema, session load/save/purge
files.py    PDF and DOCX handling, routing, temp sweep
state.py    in-memory batching and rate limits
config.py   environment and logging
```

## Setup

Requires Python 3.12 or newer. Tested on 3.12 and 3.14.

```bash
git clone https://github.com/Folse27/UA-CV-reviewer.git
cd UA-CV-reviewer
python -m venv .venv
source .venv/Scripts/activate     # Windows Git Bash
# .\.venv\Scripts\Activate.ps1    # PowerShell
# source .venv/bin/activate       # Linux / macOS
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`, then:

```bash
python check.py --live    # verifies database, both models, and Telegram
python bot.py             # long-polls while WEBHOOK_URL is empty
```

### Environment variables

| Variable | What it is |
|---|---|
| `TELEGRAM_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `GROQ_API_KEY` | From [console.groq.com](https://console.groq.com/) |
| `DATABASE_URL` | PostgreSQL connection string |
| `ADMIN_CHAT_ID` | Telegram group for alerts and approval buttons. Group ids are negative. The bot must be a member — make it an administrator so its messages are never restricted. |
| `GROQ_TEXT_MODEL` | Must support JSON mode |
| `GROQ_VISION_MODEL` | Must accept image input |
| `WEBHOOK_URL` | Public URL in production. **Leave empty to long-poll locally.** |
| `PORT` | Injected by the host; defaults to 8080 |

**Check which models your key can actually use** before trusting the defaults — availability differs sharply between Groq accounts, and models are retired regularly:

```python
from groq import Groq; print([m.id for m in Groq().models.list().data])
```

`check.py --live` calls both configured models and tells you which one failed and why. If the vision model is unavailable the bot still works — the call is logged and the document falls back to text extraction.

## Checks

```bash
python check.py           # secret scan, gitignore audit, offline tests
python check.py --live    # the above plus database, Groq and Telegram
python test_smoke.py      # offline tests alone
```

`check.py` is meant to be run before every push. It compares the real values in your local `.env` against everything git would publish — working tree and full history — and reports the variable name and line number without ever printing the value. It also scans for credential-shaped strings generally, so a key from somewhere else is caught too. It exits non-zero, so it drops into CI unchanged.

## Deployment

`render.yaml` describes the service as a Render Blueprint. Point Render at the repo, supply the four secrets when prompted, and that is the entire setup.

`WEBHOOK_URL` is deliberately not one of them. Render injects `RENDER_EXTERNAL_URL` with the service's own public URL, and `config.py` falls back to it — so there is no second deploy to fill in a URL you could not know in advance. Locally that variable does not exist, so the same code long-polls.

Three things to know about free instances:

- **Do not run a round-the-clock uptime pinger.** Render grants 750 free instance-hours per workspace per month. Keeping one service awake continuously burns roughly 730 of them, which leaves no room for any other free service and almost no margin in a 31-day month — and running out suspends every free service in the workspace until the next month. A sleeping service costs nothing.
- A sleeping service wakes in about a minute, and Telegram retries webhook deliveries, so a message sent to a sleeping bot arrives late rather than never. If you want it responsive during your working day without spending the whole budget, ping it on a schedule that covers only those hours — 13 hours a day is about 390 hours a month.
- The disk is wiped on every restart. Temp files are a cache only; all durable state is in Postgres. In-memory rate limits and any open 20-second batch also reset on restart.

## Known limits

- The bot replies in Ukrainian or Russian only.
- DOCX cannot be reviewed visually — rendering one needs software that does not fit in a free tier. Export to PDF for a layout review.
- Rate limits live in memory, so a restart clears them.
- Language detection for the bot's own canned messages is a letter-frequency guess that defaults to Ukrainian. The model handles its actual replies and gets this right.
- Telegram caps bot downloads at 20 MB.

## License

[GNU AGPL v3](LICENSE).

This is not a preference. PyMuPDF is AGPL-licensed, so any work that links it must be AGPL too. In practice: you may use, modify and self-host this freely, but if you run a modified version as a public service you must publish your source. To relicense permissively you would have to replace PyMuPDF — `pypdfium2` is the usual alternative, at the cost of the layout-aware text extraction this relies on for multi-column CVs.
