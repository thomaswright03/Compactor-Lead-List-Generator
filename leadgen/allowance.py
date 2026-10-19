"""Sign-up mode's search allowance: how many searches a company's plan gives it each
calendar month (config.PLANS), and whether it may search at all (its trial or plan).

AARCO's own site doesn't use this: it keeps one search per Utah day (daily.py). In
sign-up mode each company also keeps one search a day (its search history is kept by
day, daily.py), and on top of that its plan's searches a month, counted from its own
search history: a search that failed and gave its day back doesn't count.
"""

import datetime as dt
import time
from dataclasses import dataclass

from . import config, localtime, store
from .company import Company


@dataclass(frozen=True)
class Allowance:
    used: int
    limit: int
    resets: str                   # "November 1"

    @property
    def left(self) -> int:
        return max(0, self.limit - self.used)

    def text(self) -> str:
        """The Find leads page's line about it."""
        searches = "search" if self.left == 1 else "searches"
        return (f"{self.left} of {self.limit} {searches} left this month; "
                f"the count starts again on {self.resets}.")


def plan_of(company: Company) -> dict[str, object]:
    return config.PLANS.get(company.plan) or config.PLANS["trial"]


def _month_start(now: dt.datetime) -> str:
    return now.strftime("%Y-%m-")


def _next_month(now: dt.datetime) -> dt.date:
    return (now.replace(day=28) + dt.timedelta(days=4)).replace(day=1).date()


def used_this_month() -> int:
    """The searches this company ran this calendar month (Utah time), from its own data."""
    month = _month_start(localtime.now())
    with store.connect() as db:
        row = db.one("SELECT COUNT(*) FROM searches WHERE day LIKE ?", (month + "%",))
    return int(row[0]) if row else 0


def allowance(company: Company) -> Allowance:
    now = localtime.now()
    limit = plan_of(company).get("searches", 0)
    resets = _next_month(now)
    return Allowance(used_this_month(), int(limit) if isinstance(limit, int) else 0,
                     f"{resets:%B} {resets.day}")


def can_search(company: Company) -> str | None:
    """Why this company can't search now (plain words for the page), or None when it can:
    its plan is canceled or its payment overdue, or its free trial has ended."""
    if company.status == "canceled":
        return "Your Lead Finder plan is canceled, so new searches are off. Your saved leads are still here."
    if company.status == "past_due":
        return ("Your last payment didn't go through, so new searches are paused until it does. "
                "Your saved leads are still here.")
    if company.status == "trialing" and company.trial_ends_at is not None and time.time() > company.trial_ends_at:
        return "Your free trial has ended, so new searches are off. Your saved leads are still here."
    return None


def refusal(company: Company) -> str | None:
    """Why a search can't start now (can_search, or this month's searches are used up), or None."""
    why = can_search(company)
    if why:
        return why
    left = allowance(company)
    if left.left <= 0:
        return (f"This month's {left.limit} searches are used up. The count starts again on "
                f"{left.resets}.")
    return None
