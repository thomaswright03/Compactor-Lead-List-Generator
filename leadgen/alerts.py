"""Telling someone when the live site has a problem, instead of leaving it in the logs.

Every failed or incomplete search and every error the site logs is recorded in
the database (the Find leads page shows the last 7 days under the search
history) and, when LEADGEN_ALERT_WEBHOOK is set, posted to that address: an
incoming-webhook URL of Slack, Microsoft Teams, Google Chat or Discord, so
whoever looks after the site hears about it without watching Render's Logs tab.

The same problem is reported at most once every THROTTLE_SECONDS, reporting
happens in the background (a page never waits for it), and a report that can't
be delivered is only logged. Reports hold plain messages and error class names,
never request data or settings.
"""

import json
import logging
import os
import threading
import time
import urllib.request
import uuid
from typing import Any

from . import store
from .localtime import date_time_text

WEBHOOK_ENV = "LEADGEN_ALERT_WEBHOOK"
SITE = "Arco Compactor Lead Finder"
THROTTLE_SECONDS = 15 * 60
# The Find leads page lists the problems of this many days.
RECENT_SECONDS = 7 * 24 * 3600
MAX_TEXT = 500

# Tests switch reporting off unless they are about it (see tests/conftest.py).
ON = True
# Tests report in the foreground, so they can look at the result straight away.
BACKGROUND = True

log = logging.getLogger(__name__)
_last: dict[str, float] = {}
_lock = threading.Lock()
_reporting = threading.local()


def report(kind: str, text: str) -> None:
    """Record a problem ("search" or "error") and send it to the webhook, if one is set."""
    if not ON:
        return
    text = " ".join(text.split())[:MAX_TEXT]
    now = time.time()
    key = f"{kind}:{text}"
    with _lock:
        if now - _last.get(key, 0.0) < THROTTLE_SECONDS:
            return
        _last[key] = now
        if len(_last) > 200:              # forget the oldest; the throttle is only a guard
            for old in sorted(_last, key=_last.__getitem__)[:100]:
                del _last[old]
    if BACKGROUND:
        threading.Thread(target=_deliver, args=(kind, text, now), daemon=True).start()
    else:
        _deliver(kind, text, now)


def _deliver(kind: str, text: str, at: float) -> None:
    _reporting.on = True
    try:
        try:
            with store.connect() as db:
                db.run("INSERT INTO problems (id, at, kind, text) VALUES (?, ?, ?, ?)",
                       (uuid.uuid4().hex, at, kind, text))
        except Exception as exc:  # noqa: BLE001 - often the very problem being reported
            log.warning("Recording a problem failed: %s", exc.__class__.__name__)
        url = os.environ.get(WEBHOOK_ENV, "").strip()
        if url:
            try:
                _post(url, f"{SITE}: {text} ({date_time_text(at)}, Utah time)")
            except Exception as exc:  # noqa: BLE001 - a report must never break anything
                log.warning("Sending the problem to %s failed: %s", WEBHOOK_ENV,
                            exc.__class__.__name__)
    finally:
        _reporting.on = False


def _post(url: str, message: str) -> None:
    if not url.startswith("https://"):
        raise ValueError("the webhook address must start with https://")
    # "text" is read by Slack, Teams and Google Chat; "content" by Discord.
    body = json.dumps({"text": message, "content": message}).encode()
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as res:     # https only (checked above)
        res.read()


def recent(now: float | None = None) -> dict[str, Any]:
    """The last RECENT_SECONDS' problems: how many, and the latest few (newest first)."""
    since = (now or time.time()) - RECENT_SECONDS
    with store.connect() as db:
        total = db.one("SELECT COUNT(*) FROM problems WHERE at > ?", (since,))
        rows = db.all("SELECT at, kind, text FROM problems WHERE at > ? ORDER BY at DESC LIMIT 5",
                      (since,))
    return {"count": int(total[0]) if total else 0,
            "latest": [{"when": date_time_text(at), "kind": kind, "text": text}
                       for at, kind, text in rows]}


class _Handler(logging.Handler):
    """Reports every error the site logs (the message and the error's class name)."""

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_reporting, "on", False) or record.name == __name__:
            return
        try:
            text = record.getMessage()
            if record.exc_info and record.exc_info[0]:
                text += f" ({record.exc_info[0].__name__})"
            report("error", text)
        except Exception:  # noqa: BLE001 - reporting must never break the page
            self.handleError(record)


def install() -> None:
    """Report the errors of every leadgen module's log (once, however often it is called)."""
    logger = logging.getLogger("leadgen")
    if not any(isinstance(h, _Handler) for h in logger.handlers):
        logger.addHandler(_Handler(level=logging.ERROR))
