"""Emailing the owner when something isn't working: the site's problem reports (alerts.py)
and, from the GitHub workflows, a failed check, backup or restore rehearsal.

Standard library only, and nothing from the leadgen package, so a workflow can run it
without installing anything:

    python3 leadgen/mail.py --subject "Lead Finder: CI failed on main" "What happened..."

Settings (the environment):

    LEADGEN_SMTP_USER       the address that signs in to send (e.g. a Gmail address)
    LEADGEN_SMTP_PASSWORD   its password (for Google: an app password)
    LEADGEN_SMTP_HOST       default smtp.gmail.com
    LEADGEN_SMTP_PORT       default 587 (STARTTLS); 465 means SSL from the start
    LEADGEN_SMTP_FROM       default LEADGEN_SMTP_USER
    LEADGEN_ALERT_EMAIL     whom to tell; default ALERT_EMAIL below

Email is set up only when both the user and the password are set. Neither is ever
printed or logged: a failure is reported by its class and words, with both taken out.
As a script: not set up prints a one-line notice and exits 0 (a missing secret never
turns a workflow red); a send that fails prints why and exits 1.
"""

import argparse
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

# Whom the alerts go to (the owner's choice, 2026-10-08); LEADGEN_ALERT_EMAIL in the
# environment replaces it.
ALERT_EMAIL = "t@thomasewright.com"

USER_ENV = "LEADGEN_SMTP_USER"
PASSWORD_ENV = "LEADGEN_SMTP_PASSWORD"
HOST_ENV = "LEADGEN_SMTP_HOST"
PORT_ENV = "LEADGEN_SMTP_PORT"
FROM_ENV = "LEADGEN_SMTP_FROM"
TO_ENV = "LEADGEN_ALERT_EMAIL"
DEFAULT_HOST = "smtp.gmail.com"
DEFAULT_PORT = 587
SSL_PORT = 465
TIMEOUT_SECONDS = 15


class Unset(RuntimeError):
    """LEADGEN_SMTP_USER or LEADGEN_SMTP_PASSWORD is not set, so no email can be sent."""


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def set_up() -> bool:
    """Whether email can be sent: both the user and the password are set."""
    return bool(_env(USER_ENV) and os.environ.get(PASSWORD_ENV, ""))


def recipient() -> str:
    return _env(TO_ENV) or ALERT_EMAIL


def not_set_up_text() -> str:
    return f"Email is not set up ({USER_ENV} and {PASSWORD_ENV} are not both set)."


def send(subject: str, body: str) -> str:
    """Send one plain-text email to the recipient; returns the address it went to.
    Raises Unset when email is not set up, else a RuntimeError naming the failure's
    class and words (never the user or the password)."""
    if not set_up():
        raise Unset(not_set_up_text())
    host = _env(HOST_ENV) or DEFAULT_HOST
    user, password = _env(USER_ENV), _password(host)
    to = recipient()
    message = EmailMessage()
    message["From"] = _env(FROM_ENV) or user
    message["To"] = to
    message["Subject"] = " ".join(subject.split())
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="lead-finder")
    message.set_content(body)
    try:
        port = int(_env(PORT_ENV) or DEFAULT_PORT)
        context = ssl.create_default_context()
        server: smtplib.SMTP
        if port == SSL_PORT:
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_SECONDS, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT_SECONDS)
        with server:
            if port != SSL_PORT:
                server.starttls(context=context)
            server.login(user, password)
            server.send_message(message)
    except Exception as exc:  # noqa: BLE001 - reported by class and words, secrets taken out
        raise RuntimeError(reason(exc)) from None
    return to


def _password(host: str) -> str:
    """The password as set; for Gmail without spaces, since Google shows an app password in
    four groups of four letters and people often paste it that way."""
    password = os.environ.get(PASSWORD_ENV, "")
    return "".join(password.split()) if host.lower() == DEFAULT_HOST else password


def reason(exc: BaseException) -> str:
    """The failure's class and words, with the SMTP user and password taken out."""
    text = f"{exc.__class__.__name__}: {exc}"
    raw = os.environ.get(PASSWORD_ENV, "")
    for secret in (raw, "".join(raw.split()), _env(USER_ENV)):
        if secret:
            text = text.replace(secret, "***")
    return " ".join(text.split())[:300]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Email the Lead Finder's owner (LEADGEN_ALERT_EMAIL, "
                                     f"default {ALERT_EMAIL}).")
    parser.add_argument("--subject", required=True)
    parser.add_argument("body", nargs="+", help="the message (several arguments are separate lines)")
    args = parser.parse_args(argv)
    if not set_up():
        print(f"No email sent: {not_set_up_text()}")
        return 0
    try:
        to = send(args.subject, "\n".join(args.body))
    except RuntimeError as exc:
        print(f"The email could not be sent: {exc}", file=sys.stderr)
        return 1
    print(f"Emailed {to}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
