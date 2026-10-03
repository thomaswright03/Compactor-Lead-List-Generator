"""The saved list's server side: leads, Yes / No marks, calls, stats and downloads."""

import bisect
import copy
import logging
import math
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from flask import Blueprint, Response, abort, jsonify, request
from flask.typing import ResponseReturnValue

from .. import calls, config, contacts, daily, marks, places, reference, saved, stats, store
from ..export import saved_list_info, to_csv_bytes, to_xlsx_bytes
from ..localtime import date_time_text
from ..models import Lead
from ..pipeline import EXEMPT_TYPES
from .auth import admin_open
from .common import (
    MARKS_DOWN,
    LoadError,
    Undos,
    changed_uids,
    db_message,
    lead_json,
    load_saved,
    same_origin,
    state,
    unavailable,
    with_marks_and_calls,
)

log = logging.getLogger("leadgen.web")

# A page asking "what changed since" looks back this much further (seconds), so a
# change stamped just before the previous answer but saved just after it (a big
# search's save takes a while) is never missed. Sending a lead twice is harmless.
SINCE_OVERLAP = 120.0

bp = Blueprint("leads", __name__)


# The page's views of the saved list (its tabs): prospects not yet checked, marked
# Yes, marked No; competitors and AARCO's own listing (never asked Yes / No);
# businesses closed for good; every lead; and the businesses with a call logged
# (the Calls page). A closed business keeps its mark (so it stays under Yes or No,
# flagged) but is no longer offered for checking.
VIEWS = ("unchecked", "yes", "no", "competitors", "closed", "all", "called")
# Column sorts: the value to sort by. Equal values keep the saved order (best first).
SORTS: dict[str, Callable[[Lead], Any]] = {
    "score": lambda l: l.score,
    "name": lambda l: l.name.lower(),
    "city": lambda l: (places.town_of(l) or "~").lower(),
    "miles": lambda l: 1e9 if l.distance_miles is None else l.distance_miles,
    "called": lambda l: l.last_call_at or 0,
}
MAX_LIMIT = 5000
# Rows in one answer when the page doesn't say: the list grows with every search and
# is kept for good, so an answer is always one page of it ("Show more" asks for the next).
PAGE_SIZE = 100


def is_prospect(lead: Lead) -> bool:
    return lead.lead_type not in EXEMPT_TYPES


def views_of(lead: Lead) -> list[str]:
    """The tabs (VIEWS) a lead is listed under: every lead under "all"; under "called"
    once a call is logged; under "closed" when closed for good; competitors and AARCO's
    own listing under "competitors"; a prospect under its Yes / No, or, not marked and
    not closed, under "unchecked"."""
    closed = saved.is_closed(lead)
    tabs = ["all"]
    if lead.call_count:
        tabs.append("called")
    if closed:
        tabs.append("closed")
    if not is_prospect(lead):
        tabs.append("competitors")
    elif lead.has_baler in ("yes", "no"):
        tabs.append(lead.has_baler)
    elif not closed:
        tabs.append("unchecked")
    return tabs


def has_phone(lead: Lead) -> bool:
    """A number to call: the listing's own, or one the team verified."""
    return bool(lead.phone.strip() or lead.verified_phone.strip())


def filter_text(lead: Lead) -> str:
    """What the filter box looks in: the lead's name, town, ZIP, category, address, type,
    flags and verified contact name, lowercased."""
    return " ".join([lead.name, places.town_of(lead), lead.zip, lead.category, lead.address,
                     lead.lead_type, " ".join(lead.flags), lead.contact_name]).lower()


def matches(lead: Lead, q: str, tier: str, phone: bool = False, more: str = "",
            text: str | None = None) -> bool:
    """The page's filter box, tier choice and "Has phone" tick. Several words typed
    ("walmart layton") match a lead when each is somewhere in its name, town, ZIP,
    category, address, type, flags or verified contact name (or in `more`: on the Calls
    page, its calls' summaries, outcomes and callers), in any order and any case (q is
    lowercased). text: the lead's filter_text, when already known."""
    if tier and lead.tier != tier:
        return False
    if phone and not has_phone(lead):
        return False
    if not q:
        return True
    text = filter_text(lead) if text is None else text
    if more:
        text += " " + more
    return all(word in text for word in q.split())


def order(rows: list[Lead], view: str, sort: str, desc: bool) -> list[Lead]:
    """rows (in the saved list's order, best first) in the order the page asked for. Equal
    values keep the saved order."""
    if sort != "score" or not desc:
        return sorted(rows, key=SORTS[sort], reverse=desc)      # stable: ties keep the best-first order
    if view == "unchecked":
        # Among equal scores, the leads that can be phoned from the page come first.
        return sorted(rows, key=lambda l: (-l.score, not has_phone(l)))
    return rows


# What the saved list shows depends on these tables only: any change to them changes one
# of these figures. The first LIST_FIGURES are the saved rows and the merges; the rest the
# marks, calls and verified contacts, and who made them.
FIGURES = (("leads", "COUNT(*)"), ("leads", "MAX(last_seen)"), ("leads", "SUM(last_seen)"),
           ("merged_leads", "COUNT(*)"),
           ("marks", "COUNT(*)"), ("mark_changes", "COUNT(*)"), ("mark_changes", "MAX(at)"),
           ("mark_changes", "COUNT(undone_at)"), ("mark_changes", "MAX(undone_at)"),
           ("calls", "COUNT(*)"), ("calls", "MAX(at)"), ("call_undos", "COUNT(*)"),
           ("lead_contacts", "COUNT(*)"), ("lead_contacts", "MAX(at)"), ("made_by", "COUNT(*)"))
LIST_FIGURES = 4
FINGERPRINT = "SELECT " + ", ".join(f"(SELECT {what} FROM {table})" for table, what in FIGURES)


Rank = Callable[[Lead], tuple[Any, ...]]


def _sums(leads: Iterable[Lead]) -> tuple[int, int, int, int]:
    """What these leads account for in the marks, calls and contacts tables: Yes / No
    clicks not undone, marks, calls, verified-contact saves."""
    clicks = marked = called = saves = 0
    for l in leads:
        clicks += l.mark_clicks
        marked += bool(l.has_baler)
        called += l.call_count
        saves += l.contact_saves
    return clicks, marked, called, saves


def _tallies(key: tuple[Any, ...], sums: tuple[int, int, int, int]) -> tuple[int, ...]:
    """The rows of the marks, calls and contacts tables that no lead in the list accounts
    for (a lead taken off the list keeps its calls, say), and the people recorded beyond
    one per click and call. A Yes / No, a call, a contact or an undo leaves these as they
    were; rows that came in some other way (a restore from a backup) change them."""
    figure = dict(zip(FIGURES, key, strict=False))

    def count(table: str, what: str = "COUNT(*)") -> int:
        return int(figure.get((table, what)) or 0)
    clicks, marked, called, saves = sums
    return (count("mark_changes") - count("mark_changes", "COUNT(undone_at)") - clicks,
            count("marks") - marked, count("calls") - called, count("lead_contacts") - saves,
            count("made_by") - count("mark_changes") - count("calls") - count("call_undos"))


class Listing:
    """The saved list as the Leads and Calls pages read it, kept between requests.

    Reading every saved lead and adding its mark, latest call and verified contact
    takes time in step with the list's length, and the list only grows (it is kept for
    good). So it is done once (`read`), with each tab's rows and counts and, when first
    asked for, each sort order and each lead's filter text, and reused by every request
    until something it shows changes in the database (`FINGERPRINT`, one quick query,
    tells), whether the site or the command line changed it. After a Yes / No, a call, a
    verified contact or an undo only the leads they changed are read again, and put in
    their places (`updated`). Its leads are shared by the requests: read, never changed.
    The undo offers run out with time, so each request reads them itself.
    """

    def __init__(self, key: tuple[Any, ...], read_at: float, leads: list[Lead], place: dict[str, int],
                 by_uid: dict[str, Lead], tabs: dict[str, list[str]], views: dict[str, list[Lead]],
                 sums: tuple[int, int, int, int]) -> None:
        self.key, self.read_at = key, read_at
        self.leads = leads                  # in the saved order (best first)
        self.place = place                  # each lead's place in it
        self.by_uid, self.tabs, self.views, self.sums = by_uid, tabs, views, sums
        outcomes = dict.fromkeys(calls.OUTCOMES, 0)
        for lead in views["called"]:
            if lead.call_outcome in outcomes:
                outcomes[lead.call_outcome] += 1
        self.counts: dict[str, Any] = {**{v: len(views[v]) for v in VIEWS}, "outcomes": outcomes}
        self._ordered: dict[tuple[str, str, bool], list[Lead]] = {}
        self._text: dict[str, str] | None = None
        self._call_text: dict[str, str] | None = None
        self._lock = threading.Lock()

    @classmethod
    def read(cls, key: tuple[Any, ...], read_at: float, leads: list[Lead]) -> "Listing":
        """The list from every saved lead (best first, with marks, calls and contacts)."""
        tabs = {l.uid: views_of(l) for l in leads}
        views: dict[str, list[Lead]] = {v: [] for v in VIEWS}
        for lead in leads:
            for tab in tabs[lead.uid]:
                views[tab].append(lead)
        return cls(key, read_at, leads, {l.uid: i for i, l in enumerate(leads)}, {l.uid: l for l in leads},
                   tabs, views, _sums(leads))

    def in_view(self, uid: str, view: str) -> bool:
        return view in self.tabs.get(uid, ())

    def ordered(self, view: str, sort: str, desc: bool) -> list[Lead]:
        """A tab's rows in the order asked for (worked out once)."""
        with self._lock:
            rows = self._ordered.get((view, sort, desc))
            if rows is None:
                rows = self._ordered[(view, sort, desc)] = order(self.views[view], view, sort, desc)
        return rows

    def rank(self, view: str, sort: str, desc: bool) -> Rank | None:
        """A key that puts a tab's rows in the order `order` gives them (the value, then the
        saved order), so a changed lead can be put in its place; None for a descending
        sort by text, which has no such key."""
        place = self.place
        if sort == "score" and desc:
            if view == "unchecked":
                return lambda l: (-l.score, not has_phone(l), place[l.uid])
            return lambda l: (place[l.uid],)
        value = SORTS[sort]
        if not desc:
            return lambda l: (value(l), place[l.uid])
        if sort in ("score", "miles", "called"):              # numbers: descending is the negated value
            return lambda l: (-value(l), place[l.uid])
        return None

    def text(self) -> dict[str, str]:
        """{uid: filter_text}, worked out once."""
        with self._lock:
            if self._text is None:
                self._text = {l.uid: filter_text(l) for l in self.leads}
            return self._text

    def call_text(self) -> dict[str, str]:
        """calls.search_text(), read once (raises LoadError when it can't be read)."""
        with self._lock:
            if self._call_text is None:
                try:
                    self._call_text = calls.search_text()
                except Exception as exc:
                    log.error("Reading the calls for the filter failed", exc_info=True)
                    raise LoadError(db_message(exc)) from exc
            return self._call_text

    def in_order(self, uids: Iterable[str]) -> list[Lead]:
        """These saved leads, in the saved order."""
        return sorted((self.by_uid[u] for u in set(uids) if u in self.by_uid), key=lambda l: self.place[l.uid])

    def updated(self, key: tuple[Any, ...], read_at: float) -> "Listing | None":
        """This list with the leads whose mark, calls or verified contact changed since it
        was read read again and put in their places; None when the saved rows themselves
        changed (a search, a merge), when many leads changed, or when rows came in some
        other way than a click (`_tallies`): then the whole list is read again."""
        if key[:LIST_FIGURES] != self.key[:LIST_FIGURES]:
            return None
        try:
            since = self.read_at - SINCE_OVERLAP
            uids = marks.changed_since(since) | calls.changed_since(since) | contacts.changed_since(since)
            fresh = [copy.copy(self.by_uid[u]) for u in uids if u in self.by_uid]
            if len(fresh) > max(50, len(self.leads) // 10):
                return None
            with_marks_and_calls(fresh)
        except Exception:
            log.warning("Reading the changed leads failed; reading the whole list", exc_info=True)
            return None
        old = [self.by_uid[l.uid] for l in fresh]
        before, after = _sums(old), _sums(fresh)
        sums = (self.sums[0] - before[0] + after[0], self.sums[1] - before[1] + after[1],
                self.sums[2] - before[2] + after[2], self.sums[3] - before[3] + after[3])
        if _tallies(key, sums) != _tallies(self.key, self.sums):
            return None
        leads, by_uid, tabs = self.leads.copy(), self.by_uid.copy(), self.tabs.copy()
        for lead in fresh:
            leads[self.place[lead.uid]] = by_uid[lead.uid] = lead
            tabs[lead.uid] = views_of(lead)
        views = {v: self._moved(self.views[v], v, fresh, tabs, lambda l: (self.place[l.uid],)) for v in VIEWS}
        found = Listing(key, read_at, leads, self.place, by_uid, tabs, views, sums)
        with self._lock:                  # (other requests may be adding to them meanwhile)
            ordered, text = list(self._ordered.items()), self._text
        for (view, sort, desc), rows in ordered:
            rank = self.rank(view, sort, desc)
            if rank is not None:
                found._ordered[(view, sort, desc)] = self._moved(rows, view, fresh, tabs, rank)
        if text is not None:
            found._text = {**text, **{l.uid: filter_text(l) for l in fresh}}
        return found

    def _moved(self, rows: list[Lead], view: str, fresh: list[Lead], tabs: dict[str, list[str]],
               rank: Rank | None) -> list[Lead]:
        """rows (a tab's, in `rank`'s order) with the changed leads (fresh) taken out where
        they were and put where they now belong, if they are in the tab now (tabs)."""
        touched = [l for l in fresh if view in self.tabs.get(l.uid, ()) or view in tabs[l.uid]]
        if not touched or rank is None:
            return rows
        rows = rows.copy()
        for lead in touched:
            if view in self.tabs.get(lead.uid, ()):
                was = self.by_uid[lead.uid]
                del rows[bisect.bisect_left(rows, rank(was), key=rank)]
            if view in tabs[lead.uid]:
                bisect.insort(rows, lead, key=rank)
        return rows


_listing: tuple[str, Listing] | None = None       # (the database, its list as last read)
_listing_lock = threading.Lock()


def listing() -> Listing:
    """The saved list as it is now (Listing): as last read when nothing changed, with the
    changed leads read again after a click, or read in full. Raises LoadError with a plain
    message when it can't be read."""
    global _listing
    read_at = time.time()             # before the fingerprint: a change after it is read again
    try:
        with store.connect() as db:
            key, fingerprint = db.key, tuple(db.one(FINGERPRINT) or ())
    except Exception as exc:
        log.error("Loading the saved leads failed", exc_info=True)
        raise LoadError(db_message(exc)) from exc
    with _listing_lock:
        kept = _listing[1] if _listing and _listing[0] == key else None
        if kept and kept.key == fingerprint:
            return kept
        found = kept.updated(fingerprint, read_at) if kept else None
        if found is None:
            leads, _ = load_saved()
            found = Listing.read(fingerprint, read_at, leads)
        _listing = (key, found)
        return found


def undo_offers() -> Undos:
    """The marks and calls that can still be undone (they run out with time)."""
    try:
        return {"mark": marks.pending_undos(), "call": calls.pending_undos()}
    except Exception as exc:
        log.error("Loading the marks or calls failed", exc_info=True)
        raise LoadError(MARKS_DOWN) from exc


def _recent(found: Listing, undo: Undos) -> list[dict[str, Any]]:
    """The leads with a mark or call that can still be undone (Recent changes)."""
    return [lead_json(l, undo) for l in found.in_order(set(undo.get("mark", {})) | set(undo.get("call", {})))]


@bp.get("/leads")
def saved_leads() -> ResponseReturnValue:
    """The saved list as the page shows it: one view (tab), filtered, sorted, and
    one page of its rows, plus every tab's count (exact, over the whole list). The
    page asks for the rows it shows and "Show more" for the next page, so opening it
    stays quick however long the list grows.

    ?tab= one of VIEWS (default all), q= filter text, tier=, phone=1, lead= one business
    by its id (the Map page's link), sort= / dir=,
    offset= (default 0) and limit= (default PAGE_SIZE, at most MAX_LIMIT): the rows
    from offset on; keep= uids shown even outside the tab (rows just marked, which
    stay put for a few seconds; on the first page only, and a refresh sends them in
    full too).

    The Calls page asks for tab=called: its filter also looks in the calls'
    summaries, outcomes and callers, outcome= keeps the businesses whose latest call
    went that way, and "call_counts" are its tabs' counts (every called business
    and each outcome, the filter applied), however many businesses were called.

    With ?since=<the "now" of an earlier answer> only the leads whose details, mark
    or calls changed since then come back: in full when they belong in the view
    asked about, as just {"key", "in_view": false} when they don't (so the page can
    drop them if it shows them), with "removed": the ones that left the list. When
    more leads changed than the page shows (`limit`, e.g. after a search touched
    them all), the answer is {"reload": true} with the counts instead, and the page
    asks for its rows again: a refresh never sends more than one page of rows.
    """
    now = time.time()
    since = request.args.get("since", type=float)
    view = request.args.get("tab", "all")
    if view not in VIEWS:
        view = "all"
    q = (request.args.get("q") or "").strip().lower()[:200]
    tier = request.args.get("tier") or ""
    phone = request.args.get("phone") == "1"
    outcome = request.args.get("outcome") or ""
    if outcome not in calls.OUTCOMES or view != "called":
        outcome = ""
    # One business only (the Map page's "Open on the Leads page" link), by its id.
    only = (request.args.get("lead") or "")[:64]
    fresh = since is None or not math.isfinite(since) or since <= 0
    try:
        uids: set[str] = set()
        if since is not None and not fresh:
            uids = changed_uids(since - SINCE_OVERLAP)
            if not uids:
                return jsonify({"leads": [], "changes": True, "removed": [], "now": now})
        found = listing()
        undo = undo_offers()
        more = found.call_text() if view == "called" and q else {}
        text = found.text() if q else {}
    except LoadError as exc:
        return jsonify({"error": str(exc)}), 503

    words = q.split()

    def filtered(l: Lead) -> bool:
        # matches(), with each lead's filter text read once (Listing.text).
        if (only and l.uid != only) or (outcome and l.call_outcome != outcome) or (tier and l.tier != tier):
            return False
        if phone and not has_phone(l):
            return False
        if not words:
            return True
        hay = text[l.uid]
        if (said := more.get(l.uid)):
            hay += " " + said
        return all(word in hay for word in words)
    narrowed = bool(only or outcome or q or tier or phone)
    extra = {"call_counts": _call_counts(found, q, more)} if view == "called" else {}
    if fresh:
        return jsonify({**_page(found, undo, view, filtered, narrowed), **extra, "now": now})
    shown = {l.uid for l in found.views[view] if filtered(l)} if narrowed else set()

    def is_shown(uid: str) -> bool:
        return uid in shown if narrowed else found.in_view(uid, view)
    total = len(shown) if narrowed else len(found.views[view])
    body = {"changes": True, "total": total, "counts": found.counts,
            "recent": _recent(found, undo), **extra, "now": now}
    kept = uids & found.by_uid.keys()
    limit = request.args.get("limit", type=int) or PAGE_SIZE
    if len(kept) > max(1, min(limit, MAX_LIMIT)):
        return jsonify({**body, "reload": True, "leads": [], "removed": []})
    changed = found.in_order(kept)
    pinned = set((request.args.get("keep") or "").split(",")[:50]) - {""}
    wanted = {uid for uid in kept if is_shown(uid) or uid in pinned}
    return jsonify({**body, "removed": sorted(uids - kept),
                    "leads": [{**lead_json(l, undo), "in_view": is_shown(l.uid)}
                              if l.uid in wanted
                              else {"key": l.uid, "in_view": False} for l in changed]})


def _call_counts(found: Listing, q: str, more: dict[str, str]) -> dict[str, Any]:
    """The Calls page's tab counts: every called business the filter keeps, and how
    many of them each outcome holds (their latest call's)."""
    outcomes = dict.fromkeys(calls.OUTCOMES, 0)
    text = found.text() if q else {}
    called = [l for l in found.views["called"]
              if matches(l, q, "", False, more.get(l.uid, ""), text.get(l.uid))]
    for lead in called:
        if lead.call_outcome in outcomes:
            outcomes[lead.call_outcome] += 1
    return {"all": len(called), "outcomes": outcomes}


def _page(found: Listing, undo: Undos, view: str, filtered: Callable[[Lead], bool],
          narrowed: bool) -> dict[str, Any]:
    offset = max(0, request.args.get("offset", type=int) or 0)
    keep = set((request.args.get("keep") or "").split(",")[:50]) - {""} if not offset else set()
    sort = request.args.get("sort") or "score"
    if sort not in SORTS:
        sort = "score"
    default_desc = sort in ("score", "called")
    desc = {"asc": False, "desc": True}.get(request.args.get("dir") or "", default_desc)
    outside = {uid for uid in keep if uid in found.by_uid and not found.in_view(uid, view)}
    if outside:
        # Rows just marked that left the tab stay put for a few seconds: in their place.
        rows = order([l for l in found.leads if (found.in_view(l.uid, view) or l.uid in outside) and filtered(l)],
                     view, sort, desc)
    else:
        rows = found.ordered(view, sort, desc)
        if narrowed:
            rows = [l for l in rows if filtered(l)]
    limit = max(1, min(request.args.get("limit", type=int) or PAGE_SIZE, MAX_LIMIT))
    shown = rows[offset:offset + limit]
    return {"leads": [lead_json(l, undo) for l in shown], "total": len(rows), "offset": offset,
            "counts": found.counts, "recent": _recent(found, undo)}


# Every mark and call says who made it (so colleagues know whom to ask), as the page asks.
NO_NAME = ("Enter your name first (Your name, in the side bar, or under Menu on a phone), so the team can see "
           "who made this change. Nothing was saved.")


@bp.post("/calls")
def log_call() -> ResponseReturnValue:
    """Record a call to a lead: its outcome and the conversation notes (kept for good)."""
    if not same_origin():
        abort(403)
    data = request.get_json(silent=True) or {}
    uid = str(data.get("key") or "")
    if not uid or len(uid) > 64:
        return jsonify({"error": "Unknown lead"}), 400
    call_id = str(data["id"]) if data.get("id") else None
    if not store.person(data.get("by")):
        return jsonify({"error": NO_NAME}), 400
    try:
        call = calls.log_call(uid, str(data.get("outcome") or ""), str(data.get("notes") or ""),
                              call_id, str(data.get("by") or ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.error("Saving a call failed", exc_info=True)
        return jsonify({"error": db_message(exc)}), 503       # the page says it wasn't saved
    return jsonify({"ok": True, "now": time.time(),
                    "call": {**call, "when": date_time_text(call["at"]),
                             "undo": {"id": call["id"],
                                      "until": call["at"] + calls.UNDO_SECONDS}}})


@bp.post("/contact")
def save_contact() -> ResponseReturnValue:
    """Save a verified phone number and who to ask for on a lead, with who saved them
    (contacts.py). Kept apart from the listing's own phone, which stays as it was, and
    never changed by a search."""
    if not same_origin():
        abort(403)
    data = request.get_json(silent=True) or {}
    uid = str(data.get("key") or "")
    if not uid or len(uid) > 64:
        return jsonify({"error": "Unknown lead"}), 400
    if not store.person(data.get("by")):
        return jsonify({"error": NO_NAME}), 400
    try:
        contact = contacts.save(uid, str(data.get("phone") or ""), str(data.get("contact") or ""),
                                str(data.get("by") or ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.error("Saving a verified contact failed", exc_info=True)
        return jsonify({"error": db_message(exc)}), 503
    return jsonify({"ok": True, "now": time.time(), "contact": contact.as_json(contacts.saves(uid))})


@bp.get("/calls/<uid>")
def call_history(uid: str) -> ResponseReturnValue:
    try:
        return jsonify({"calls": calls.history(uid[:64]), "marks": marks.history(uid[:64]),
                        "contacts": contacts.history(uid[:64])})
    except Exception as exc:
        log.error("Loading a call history failed", exc_info=True)
        return jsonify({"error": f"Couldn't load the calls. {db_message(exc)}"}), 503


@bp.get("/stats")
def stats_data() -> ResponseReturnValue:
    try:
        leads, _ = load_saved()
    except LoadError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({**stats.summarize(leads), "saved": len(leads),
                    "min_score": config.DEFAULT_MIN_SCORE})


@bp.post("/mark")
def mark() -> ResponseReturnValue:
    """Save whether a business has a baler ("yes" or "no"). A mark is kept for good:
    it can be switched but not cleared (except by undoing a click within
    marks.UNDO_SECONDS), and later searches keep it with the business."""
    if not same_origin():
        abort(403)
    data = request.get_json(silent=True) or {}
    uid, value = str(data.get("key") or ""), str(data.get("value") or "")
    if not uid or len(uid) > 64 or value not in marks.VALUES:
        return jsonify({"error": "Bad mark"}), 400
    if not store.person(data.get("by")):
        return jsonify({"error": NO_NAME}), 400
    try:
        undo = marks.set_mark(uid, value, str(data.get("by") or ""))
    except ValueError:
        return jsonify({"error": "That business isn't in the saved list. Reload the "
                                 "page."}), 404
    except Exception as exc:
        log.error("Saving a mark failed", exc_info=True)
        # The page already says the answer wasn't saved; this says why and what to do.
        return jsonify({"error": db_message(exc)}), 503
    return jsonify({"ok": True, "undo": undo, "now": time.time()})


def _undo(fn: Callable[[str], bool], what: str) -> ResponseReturnValue:
    if not same_origin():
        abort(403)
    change = str((request.get_json(silent=True) or {}).get("id") or "")
    if not change or len(change) > 64:
        return jsonify({"error": "Bad undo"}), 400
    try:
        ok = fn(change)
    except Exception as exc:
        log.error("Undoing a %s failed", what, exc_info=True)
        return jsonify({"error": db_message(exc)}), 503
    if not ok:
        return jsonify({"error": "It's too late to undo that (after 5 minutes, or once "
                                 "there's a newer change)."}), 409
    return jsonify({"ok": True})


@bp.post("/mark/undo")
def undo_mark() -> ResponseReturnValue:
    """Undo a Yes/No click made in the last few minutes (a misclick), by its id."""
    return _undo(marks.undo, "mark")


@bp.post("/calls/undo")
def undo_call() -> ResponseReturnValue:
    """Undo a call saved in the last few minutes (a misclick)."""
    return _undo(calls.undo, "call")


@bp.get("/download/scoring-reference.json")
def scoring_reference() -> ResponseReturnValue:
    """The scoring check file (tests/fixtures/scoring_reference.json) with every business
    marked Yes / No on the site in it (only the facts the scoring reads: no phone
    numbers, addresses or call notes), for the developer to commit (reference.py)."""
    if not admin_open():
        abort(403, "Unlock the administrator's section on the Find leads page first.")
    try:
        data, _, _ = reference.build()
    except Exception as exc:
        log.error("Building the scoring check file failed", exc_info=True)
        unavailable(db_message(exc))
    return Response(reference.dumps(data), mimetype="application/json", headers={
        "Content-Disposition": "attachment; filename=scoring_reference.json"})


@bp.get("/download/<job_id>.<fmt>")
def download(job_id: str, fmt: str) -> ResponseReturnValue:
    """A search's leads, or with job id "saved" every saved lead."""
    if fmt not in ("csv", "xlsx"):
        abort(404)
    if job_id == "saved":
        try:
            leads, _ = load_saved()
        except LoadError as exc:
            unavailable(str(exc))
        # When the first and latest search started, as the search history shows them;
        # leads saved without a search on record (the command line) go by when they were saved.
        searched = True
        try:
            first, latest = daily.search_times()
            if first is None:
                searched = False
                first, latest = saved.date_range()
        except Exception:
            log.warning("Reading the search dates failed", exc_info=True)
            first = latest = None                  # the file is still useful without them
        info = saved_list_info(leads, first, latest, searched)
    else:
        job = state().jobs.get(job_id)
        if not job or job["state"] != "done":
            abort(404)
        try:
            leads = contacts.apply(calls.apply(marks.apply(job["result"].leads)))
        except Exception:
            log.error("Loading marks for a download failed", exc_info=True)
            unavailable(MARKS_DOWN)
        info = job["result"].run_info(job["params"])
    # Named with the Utah date, so the copies people keep can be told apart.
    disposition = f"attachment; filename=compactor-leads-{daily.today()}.{fmt}"
    if fmt == "csv":
        return Response(to_csv_bytes(leads), mimetype="text/csv",
                        headers={"Content-Disposition": disposition})
    data = to_xlsx_bytes(leads, info)
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": disposition})
