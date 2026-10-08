"""Telling someone when the live site has a problem, instead of leaving it in the logs.

Every failed or incomplete search and every error the site logs is recorded in
the database (the Find leads page shows the last 7 days under the search
history) and sent on every channel that is set up, so whoever looks after the site
hears about it without watching Render's Logs tab:

- by email (mail.py: LEADGEN_SMTP_USER and LEADGEN_SMTP_PASSWORD set) to
  LEADGEN_ALERT_EMAIL, by default the owner's address (mail.ALERT_EMAIL);
- to LEADGEN_ALERT_WEBHOOK when it is set: an incoming-webhook URL of Slack,
  Microsoft Teams, Google Chat or Discord.

Each channel is tried on its own (one failing never stops the other). The same
problem is reported at most once every THROTTLE_SECONDS, at most EMAILS_PER_HOUR
emails go out in any hour (the rest are only recorded and logged), reporting
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
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from . import THREAD_PREFIX, config, mail, store
from .localtime import date_time_text

WEBHOOK_ENV = "LEADGEN_ALERT_WEBHOOK"
SITE = config.SITE_NAME
THROTTLE_SECONDS = 15 * 60
# At most this many alert emails in any hour, so a burst of errors can't flood the inbox.
EMAILS_PER_HOUR = 10
EMAIL_SUBJECT = "Lead Finder problem: "
# The Find leads page lists the problems of this many days.
RECENT_SECONDS = 7 * 24 * 3600
MAX_TEXT = 500

# Tests switch reporting off unless they are about it (see tests/conftest.py).
ON = True
# Tests report in the foreground, so they can look at the result straight away.
BACKGROUND = True

log = logging.getLogger(__name__)
_last: dict[str, float] = {}
_emailed: list[float] = []          # when the alert emails of the last hour went out
_lock = threading.Lock()
_reporting = threading.local()


def report(kind: str, text: str) -> None:
    """Record a problem ("search" or "error") and send it on every channel that is set up."""
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
        threading.Thread(target=_deliver, args=(kind, text, now), daemon=True,
                         name=f"{THREAD_PREFIX}alert").start()
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
        message = f"{SITE}: {text} ({date_time_text(at)}, Utah time)"
        url = os.environ.get(WEBHOOK_ENV, "").strip()
        if url:
            try:
                _send(url, message)
            except Exception as exc:  # noqa: BLE001 - a report must never break anything
                log.warning("Sending the problem to %s failed: %s", WEBHOOK_ENV,
                            exc.__class__.__name__)
        if mail.set_up():
            if not _email_allowed(at):
                log.warning("Not emailing the problem: %d alert emails went out in the last hour",
                            EMAILS_PER_HOUR)
            else:
                try:
                    _email(EMAIL_SUBJECT + text[:80], message)
                except Exception as exc:  # noqa: BLE001 - a report must never break anything
                    log.warning("Emailing the problem failed: %s", exc)
    finally:
        _reporting.on = False


def _email_allowed(now: float) -> bool:
    """Whether another alert email may go out now (fewer than EMAILS_PER_HOUR in the last
    hour); counts it when so."""
    with _lock:
        _emailed[:] = [at for at in _emailed if now - at < 3600]
        if len(_emailed) >= EMAILS_PER_HOUR:
            return False
        _emailed.append(now)
        return True


def _email(subject: str, body: str) -> str:
    """Email one message (mail.py), noting for /healthz whether it was delivered; returns
    the address it went to."""
    from . import health
    try:
        to = mail.send(subject, body)
    except Exception as exc:
        health.record("alerts", False, f"Email: {exc}")
        raise
    health.record("alerts", True)
    return to


def _send(url: str, message: str) -> None:
    """Post one message, noting for /healthz (health.py) whether it was delivered."""
    from . import health
    try:
        _post(url, message)
    except Exception as exc:
        # The error's class and words only: never the address.
        health.record("alerts", False, f"{exc.__class__.__name__}: {exc}")
        raise
    health.record("alerts", True)


def _post(url: str, message: str) -> None:
    if not url.startswith("https://") and not _on_this_machine(url):
        raise ValueError("the webhook address must start with https://")
    # "text" is read by Slack, Teams and Google Chat; "content" by Discord.
    body = json.dumps({"text": message, "content": message}).encode()
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as res:     # https only (checked above)
        res.read()


def _on_this_machine(url: str) -> bool:
    """A plain http:// address on this machine (127.0.0.1, localhost): nothing leaves it,
    so the tests and a local try can use a local stand-in for the chat service."""
    parts = urlsplit(url)
    return parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")


@dataclass
class Sent:
    """What send_test sent, where it was delivered, and where it failed (and why)."""
    message: str
    delivered: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def send_test(where: str = "") -> Sent:
    """Send a test message now on every channel that is set up (`python -m leadgen
    test-alert`, and the administrator's "Send a test alert"); `where` says where it was
    sent from. Raises Unset when neither email nor a webhook is set up; a channel that
    fails is listed in `failed` (its error's class and words, never an address or a
    password)."""
    url = os.environ.get(WEBHOOK_ENV, "").strip()
    if not url and not mail.set_up():
        raise Unset(f"No alert is set up: neither email ({mail.USER_ENV} and {mail.PASSWORD_ENV}) "
                    f"nor a webhook ({WEBHOOK_ENV}) is set.")
    where = f" {where}" if where else ""
    sent = Sent(f"{SITE}: Test alert sent{where} at {date_time_text(time.time())}, Utah time. "
                    "Problem reports from the site reach you here.")
    if mail.set_up():
        try:
            to = _email("Lead Finder: test alert", sent.message)
            sent.delivered.append(f"by email to {to}")
        except Exception as exc:  # noqa: BLE001 - said on the page; the other channel still runs
            sent.failed.append(f"by email: {exc}")
    if url:
        try:
            _send(url, sent.message)
            sent.delivered.append(f"to the webhook ({WEBHOOK_ENV})")
        except Exception as exc:  # noqa: BLE001 - the error's class and words, never the address
            sent.failed.append(f"to the webhook: {exc.__class__.__name__}: {exc}")
    return sent


class Unset(RuntimeError):
    """Neither email nor a webhook is set up, so there is nothing to send to."""


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
