"""Sign-up mode's accounts: the companies that signed up, the people who log in, and the
one-time links emailed to them (store.ACCOUNT_SCHEMA).

They live in the base database (store.for_company(None)), never in a company's own data.
A password is kept only as werkzeug.security's salted hash, an email address in lower
case, and a link's token only as its SHA-256, so a copy of the database can't be used to
log in or to open a link.
"""

import hashlib
import json
import secrets
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

from . import config, plans, store
from .company import Company

MIN_PASSWORD = 10                 # characters
MAX_PASSWORD = 200
MAX_NAME = 80
MAX_EMAIL = 254
TRIAL_DAYS = plans.TRIAL_DAYS
STATUSES = ("trialing", "active", "past_due", "canceled")
# How long each kind of emailed link works.
TOKEN_HOURS = {"verify": 7 * 24, "reset": 2, "invite": 7 * 24}

# A hash checked when no one has the email typed, so a wrong email takes as long as a
# wrong password (the login doesn't tell which accounts exist by its timing).
_DUMMY_HASH = generate_password_hash("not a real password")


class AccountError(ValueError):
    """Something typed can't be used; the message is for the page."""


@dataclass(frozen=True)
class User:
    id: str
    email: str
    password_hash: str
    name: str
    company_id: str
    role: str
    created_at: float
    email_verified_at: float | None

    @property
    def verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def owner(self) -> bool:
        return self.role == "owner"


@contextmanager
def _base() -> Iterator[store.Db]:
    """A connection to the base database, where the accounts are, whichever company this
    thread works on."""
    with store.for_company(None), store.connect() as db:
        yield store.with_accounts(db)


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def email_of(text: object) -> str:
    """An email address as kept: trimmed, in lower case. Raises AccountError when it
    doesn't look like one."""
    email = str(text or "").strip().lower()
    local, at, domain = email.partition("@")
    if (not at or not local or "." not in domain or domain.startswith(".") or domain.endswith(".")
            or len(email) > MAX_EMAIL or any(c.isspace() for c in email) or "@" in domain):
        raise AccountError("Enter a valid email address.")
    return email


def name_of(text: object, what: str = "your name") -> str:
    """A name as typed: one line, at most MAX_NAME characters. Raises AccountError when empty."""
    name = " ".join("".join(c for c in str(text or "") if c.isprintable()).split())[:MAX_NAME]
    if not name:
        raise AccountError(f"Enter {what}.")
    return name


def check_new_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"Choose a password of at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise AccountError(f"Choose a password of at most {MAX_PASSWORD} characters.")
    return password


def short_name(name: str) -> str:
    """The brand block's name on a phone: the whole name when short, else its first word."""
    return name if len(name) <= 16 else name.split()[0][:16]


# ---- companies

_COMPANY_COLUMNS = ("id, name, short_name, address, lat, lon, radius_miles, search_words, competitors, "
                    "created_at, plan, status, trial_ends_at, stripe_customer_id, stripe_subscription_id")


def _company(row: store.Row) -> Company:
    (cid, name, short, address, lat, lon, radius, words, competitors, created, plan, status,
     trial_ends, customer, subscription) = row
    return Company(id=cid, name=name, short_name=short, address=address or "",
                   lat=None if lat is None else float(lat), lon=None if lon is None else float(lon),
                   radius_miles=float(radius), search_words=tuple(json.loads(words or "[]")),
                   competitors=tuple(json.loads(competitors or "[]")), created_at=float(created),
                   plan=plan, status=status, trial_ends_at=None if trial_ends is None else float(trial_ends),
                   stripe_customer_id=customer, stripe_subscription_id=subscription)


def get_company(company_id: str) -> Company | None:
    with _base() as db:
        row = db.one(f"SELECT {_COMPANY_COLUMNS} FROM companies WHERE id = ?", (company_id,))
    return _company(row) if row else None


def update_company(company_id: str, **changes: Any) -> Company:
    """Change a company's own details (name, short_name, address, lat, lon, radius_miles,
    search_words, competitors) or its plan (plan, status, trial_ends_at, the Stripe ids)."""
    allowed = set(_COMPANY_COLUMNS.split(", ")) - {"id", "created_at"}
    if not changes or set(changes) - allowed:
        raise ValueError(f"unknown company fields: {sorted(set(changes) - allowed)}")
    if "status" in changes and changes["status"] not in STATUSES:
        raise ValueError("unknown status")
    values = {k: json.dumps(list(v)) if k in ("search_words", "competitors") else v for k, v in changes.items()}
    with _base() as db:
        db.run(f"UPDATE companies SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
               (*values.values(), company_id))
    company = get_company(company_id)
    if company is None:
        raise ValueError("no such company")
    return company


# ---- users

_USER_COLUMNS = "id, email, password_hash, name, company_id, role, created_at, email_verified_at"


def _user(row: store.Row | None) -> User | None:
    if row is None:
        return None
    uid, email, hashed, name, company_id, role, created, verified = row
    return User(uid, email, hashed, name, company_id, role, float(created),
                None if verified is None else float(verified))


def user_by_id(user_id: str) -> User | None:
    with _base() as db:
        return _user(db.one(f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (user_id,)))


def user_by_email(email: str) -> User | None:
    with _base() as db:
        return _user(db.one(f"SELECT {_USER_COLUMNS} FROM users WHERE email = ?", (email.strip().lower(),)))


def company_users(company_id: str) -> list[User]:
    with _base() as db:
        rows = db.all(f"SELECT {_USER_COLUMNS} FROM users WHERE company_id = ? ORDER BY created_at", (company_id,))
    return [u for r in rows if (u := _user(r))]


def _insert_user(db: store.Db, email: str, password: str, name: str, company_id: str, role: str,
                 verified: bool = False) -> User:
    now = time.time()
    user = User(new_id(), email, generate_password_hash(password), name, company_id, role, now,
                now if verified else None)
    got = db.one(f"INSERT INTO users ({_USER_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                 "ON CONFLICT (email) DO NOTHING RETURNING id",
                 (user.id, user.email, user.password_hash, user.name, user.company_id, user.role,
                  user.created_at, user.email_verified_at))
    if got is None:
        raise AccountError("There is already an account with that email address. Log in instead, "
                           "or reset your password.")
    return user


def sign_up(company_name: str, name: str, email: str, password: str) -> tuple[Company, User]:
    """A new company on a free trial (TRIAL_DAYS), with its owner. Raises AccountError
    when something typed can't be used (the email already has an account, say)."""
    company_name = name_of(company_name, "your company's name")
    name, email, password = name_of(name), email_of(email), check_new_password(password)
    now = time.time()
    company = Company(id=new_id(), name=company_name, short_name=short_name(company_name),
                      radius_miles=config.DEFAULT_RADIUS_MILES, created_at=now, plan="trial",
                      status="trialing", trial_ends_at=now + TRIAL_DAYS * 86400)
    with _base() as db, db.transaction():
        user = _insert_user(db, email, password, name, company.id, "owner")
        db.run(f"INSERT INTO companies ({_COMPANY_COLUMNS}) VALUES ({', '.join('?' * 15)})",
               (company.id, company.name, company.short_name, "", None, None, company.radius_miles,
                "[]", "[]", now, company.plan, company.status, company.trial_ends_at, None, None))
    return company, user


def join(company_id: str, email: str, name: str, password: str) -> User:
    """A teammate who accepted an invite: a member of the company, the email already
    shown to be theirs (the invite went to it)."""
    name, email, password = name_of(name), email_of(email), check_new_password(password)
    with _base() as db:
        return _insert_user(db, email, password, name, company_id, "member", verified=True)


def check_login(email: str, password: str) -> User | None:
    """The user with this email and password, or None (the same time taken either way)."""
    try:
        user = user_by_email(email_of(email))
    except AccountError:
        user = None
    hashed = user.password_hash if user else _DUMMY_HASH
    ok = check_password_hash(hashed, password or "")
    return user if user and ok else None


def set_password(user_id: str, password: str) -> None:
    hashed = generate_password_hash(check_new_password(password))
    with _base() as db:
        db.run("UPDATE users SET password_hash = ? WHERE id = ?", (hashed, user_id))


def mark_verified(user_id: str) -> None:
    with _base() as db:
        db.run("UPDATE users SET email_verified_at = ? WHERE id = ? AND email_verified_at IS NULL",
               (time.time(), user_id))


# ---- the emailed links

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def make_token(kind: str, email: str, user_id: str | None = None, company_id: str | None = None) -> str:
    """A new link's token (shown once, in the email); only its hash is kept."""
    if kind not in TOKEN_HOURS:
        raise ValueError("unknown token kind")
    token, now = secrets.token_urlsafe(32), time.time()
    with _base() as db:
        db.run("INSERT INTO tokens (hash, kind, user_id, email, company_id, expires_at, used_at, created_at) "
               "VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
               (_hash(token), kind, user_id, email, company_id, now + TOKEN_HOURS[kind] * 3600, now))
    return token


@dataclass(frozen=True)
class Token:
    kind: str
    user_id: str | None
    email: str
    company_id: str | None


def peek_token(token: str, kind: str) -> Token | None:
    """The link's details while it is unused and unexpired, else None (nothing changes)."""
    with _base() as db:
        row = db.one("SELECT kind, user_id, email, company_id FROM tokens WHERE hash = ? AND kind = ? "
                     "AND used_at IS NULL AND expires_at > ?", (_hash(str(token)), kind, time.time()))
    return Token(*row) if row else None


def use_token(token: str, kind: str) -> Token | None:
    """The link's details, marking it used (once only: a second use gets None)."""
    with _base() as db:
        row = db.one("UPDATE tokens SET used_at = ? WHERE hash = ? AND kind = ? AND used_at IS NULL "
                     "AND expires_at > ? RETURNING kind, user_id, email, company_id",
                     (time.time(), _hash(str(token)), kind, time.time()))
    return Token(*row) if row else None


def open_invites(company_id: str) -> list[str]:
    """The emails invited to the company whose invite is still unused and unexpired."""
    with _base() as db:
        rows = db.all("SELECT DISTINCT email FROM tokens WHERE kind = 'invite' AND company_id = ? "
                      "AND used_at IS NULL AND expires_at > ? ORDER BY email", (company_id, time.time()))
    return [r[0] for r in rows]
