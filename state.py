"""Per-user state that only matters while the process lives: batching buffers,
rate-limit clocks, and the payload held hostage by an admin decision.

shortcut: in-memory only, so a Render restart drops an open 20s batch and resets
rate-limit clocks. Move to Redis/Postgres if restarts ever get frequent enough to matter.
"""
import time
from collections import defaultdict, deque

from config import MSG_PER_MIN, PDF_PER_MIN, PNG_COOLDOWN

BATCHES: dict[int, dict] = {}      # chat_id -> {"texts": [], "files": [], "photos": [], "task": Task}
AWAITING: dict[int, dict] = {}     # chat_id -> batch frozen until an admin decides
STATUS: dict[int, str] = {}        # write-through cache of sessions.status

_msgs: dict[int, deque] = defaultdict(deque)
_pdfs: dict[int, deque] = defaultdict(deque)
_png: dict[int, float] = {}
_alerts: dict[tuple, float] = {}


def _hit(clock: dict, chat_id: int, limit: int, window: int = 60) -> bool:
    """True if this event is within the limit; records it when it is."""
    now = time.time()
    q = clock[chat_id]
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def allow_message(chat_id: int) -> bool:
    return _hit(_msgs, chat_id, MSG_PER_MIN)


def allow_pdf(chat_id: int) -> bool:
    return _hit(_pdfs, chat_id, PDF_PER_MIN)


def allow_png(chat_id: int) -> bool:
    now = time.time()
    if now - _png.get(chat_id, 0) < PNG_COOLDOWN:
        return False
    _png[chat_id] = now
    return True


def alert_once(chat_id: int, kind: str, window: int = 60) -> bool:
    """Keeps a spammer from flooding the admin group with identical alerts."""
    now = time.time()
    if now - _alerts.get((chat_id, kind), 0) < window:
        return False
    _alerts[(chat_id, kind)] = now
    return True
