"""Free fallback source: OpenStreetMap via the Overpass API (no key needed).

Coverage is thinner than Google (fewer phone numbers and websites) but it
often includes warehouses and industrial buildings that Google lists poorly,
and building outlines give a rough size signal.
"""

import logging
import math
import queue
import re
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

from .. import config
from ..geo import METERS_PER_MILE, haversine_miles
from ..http import HttpError, cache_get, cache_put, request_json
from ..models import Lead
from . import SourceError

log = logging.getLogger(__name__)

# Tag keys copied onto each lead so the scorer can classify it.
CATEGORY_KEYS = ["shop", "amenity", "building", "industrial", "craft", "man_made", "office",
                 "tourism", "leisure", "aeroway", "healthcare", "landuse", "military"]
# Area-type landuse tags produce huge, mostly unnamed polygons; only landfills are queried.
_SKIP_QUERY = {("landuse", "industrial"), ("landuse", "residential"), ("landuse", "military"),
               ("landuse", "logistics")}
_NAME_TERMS = [
    "compactor", "compaction", "baler", "baling", "recycl", "waste", "disposal", "sanitation",
    "distribution", "warehouse", "logistics", "fulfillment", "cold storage", "freight",
    "manufactur", "industries", "fabrication", "packaging", "printing", "corrugated",
    "foods", "meats", "dairy", "bottling", "brewing", "hospital", "medical center",
    "university", "college", "apartments",
]
SQFT_PER_M2 = 10.7639
_NOT_PLACES = ["highway", "waterway", "railway", "route", "boundary", "natural",
               "public_transport", "place", "power"]
MAX_BUILDING_SQFT = 3_000_000   # bigger bounding boxes are campuses/resorts, not buildings
# A mirror is only asked when at least this many seconds of the map-data time are left.
MIN_SECONDS_LEFT = 5
# An area to search, as Overpass writes it: (south, west, north, east) in degrees.
Box = tuple[float, float, float, float]
MILES_PER_DEGREE_LAT = 69.05


class PartialResult(SourceError):
    """The map data came back for only part of the area: `leads` and `warnings` hold
    what the parts that answered found (the search keeps them, marked incomplete)."""

    def __init__(self, message: str, leads: list[Lead], warnings: list[str],
                 coverage: str = "") -> None:
        super().__init__(message)
        self.leads, self.warnings = leads, warnings
        self.coverage = coverage          # "about 7 of 9 areas": how much of the radius answered


def _tag_filters() -> tuple[dict[str, set[str]], set[str]]:
    """Group category tags into Overpass filters: {key: [values]} and bare keys."""
    by_key: dict[str, set[str]] = {}
    any_value: set[str] = set()
    for cat in config.CATEGORIES:
        if not cat.query_osm:
            continue
        for key, value in cat.osm_tags:
            if (key, value) in _SKIP_QUERY:
                continue
            if value is None:
                any_value.add(key)
            else:
                by_key.setdefault(key, set()).add(value)
    return by_key, any_value


def _ql_string(text: str) -> str:
    return (text.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\t", "\\t"))


def build_query(lat: float, lon: float, radius_miles: float, keywords: Sequence[str] = (),
                timeout: int | None = None, box: Box | None = None) -> str:
    """The Overpass query for the circle, or (with box) for the part of it inside the box."""
    r = int(radius_miles * METERS_PER_MILE)
    around = f"(around:{r},{lat:.6f},{lon:.6f})"
    if box is not None:
        around += "({:.6f},{:.6f},{:.6f},{:.6f})".format(*box)
    by_key, any_value = _tag_filters()
    parts = []
    for key in sorted(by_key):
        values = "|".join(sorted(by_key[key]))
        parts.append(f'nwr["{key}"~"^({values})$"]["name"]{around};')
    for key in sorted(any_value):
        parts.append(f'nwr["{key}"]["name"]{around};')
    terms = list(_NAME_TERMS) + [k.lower() for k in keywords if k.strip()]
    # Escape twice: once for the regex, once for the Overpass QL string literal
    # (which would otherwise eat the regex backslashes).
    regex = "|".join(sorted({_ql_string(re.escape(t)) for t in terms}))
    # Name matches skip roads, canals, routes etc. ("University Pkwy", "Waste Ditch").
    not_places = "".join(f'[!"{k}"]' for k in _NOT_PLACES)
    parts.append(f'nwr["name"~"{regex}",i]{not_places}{around};')
    body = "\n  ".join(parts)
    timeout = int(timeout or config.OVERPASS_PART_SECONDS)
    return (f"[out:json][timeout:{timeout}];\n(\n  {body}\n)->.all;\n"
            "node.all->.n;\n.n out body;\n"
            "(way.all; relation.all;)->.w;\n.w out tags bb;")


def _footprint_sqft(bounds: dict[str, float] | None) -> int | None:
    if not bounds:
        return None
    dlat = (bounds["maxlat"] - bounds["minlat"]) * 111320
    mid = math.radians((bounds["maxlat"] + bounds["minlat"]) / 2)
    dlon = (bounds["maxlon"] - bounds["minlon"]) * 111320 * math.cos(mid)
    area = dlat * dlon * SQFT_PER_M2
    return int(round(area, -2)) if area > 0 else None


def _pretty(tag_value: str) -> str:
    return tag_value.replace("_", " ").replace(";", ", ").title()


_LIFECYCLE = ("disused:", "abandoned:", "was:", "demolished:")
_USE_KEYS = [k for k in CATEGORY_KEYS if k != "building"]


def _only_former_use(tags: dict[str, str]) -> bool:
    """True when a lifecycle prefix (disused:shop=...) records the only use left."""
    former = any(k.startswith(_LIFECYCLE) and k.split(":", 1)[1] in _USE_KEYS for k in tags)
    return former and not any(k in tags for k in _USE_KEYS)


def _fetched_by_tag(tags: dict[str, str]) -> bool:
    by_key, any_value = _tag_filters()
    return (any(v in values for k, values in by_key.items() for v in tags.get(k, "").split(";"))
            or any(k in tags for k in any_value))


# Labels for one part of a complex ("Building B", "Bldg 3", "Tower 2", "C Building"):
# not a business, and the complex itself is its own element when it is mapped.
_PART_LABEL = re.compile(
    r"^(?:(?:building|bldg\.?|block|wing|tower|unit|suite|ste\.?|phase|annex|hall)\s*[#-]?\s*"
    r"[0-9a-z]{1,4}|[0-9a-z]{1,3}\s*[-]?\s*(?:building|bldg\.?|wing|tower))$", re.I)


def is_part_label(name: str) -> bool:
    """True for a name that only labels part of a complex, like "Building B"."""
    return bool(_PART_LABEL.match(name.strip()))


def parse_element(el: dict[str, Any]) -> Lead | None:
    tags = el.get("tags", {})
    name = tags.get("name", "").strip()
    # Skip unnamed features and per-building labels like "B", "12" or "Building B"
    # inside a complex (but keep brands like "3M").
    if len(name) < 2 or not any(ch.isalpha() for ch in name) or is_part_label(name):
        return None
    # Places that no longer operate.
    if "(historical)" in name.lower() or _only_former_use(tags) or any(
            tags.get(k) == "yes" for k in ("disused", "abandoned", "demolished")):
        return None
    # Roads, rivers, place labels etc. only slip in through the name query; an
    # element a category-tag query fetched (a rail depot, an apartment node) stays.
    if any(k in tags for k in _NOT_PLACES) and not _fetched_by_tag(tags):
        return None
    if "lat" in el and "lon" in el:
        lat, lon = el["lat"], el["lon"]
    elif "bounds" in el:
        b = el["bounds"]
        lat, lon = (b["minlat"] + b["maxlat"]) / 2, (b["minlon"] + b["maxlon"]) / 2
    else:
        return None
    raw = [f"{k}={tags[k]}" for k in CATEGORY_KEYS if k in tags]
    for k in ("brand", "operator"):
        if k in tags:
            raw.append(f"{k}={tags[k]}")
    primary = ""
    for k in CATEGORY_KEYS:
        if k in tags and tags[k] not in ("yes",):
            primary = _pretty(tags[k])
            break
    street = " ".join(x for x in [tags.get("addr:housenumber", ""), tags.get("addr:street", "")] if x)
    etype = el.get("type", "node")
    footprint = None
    if etype in ("way", "relation") and ("building" in tags or "shop" in tags):
        footprint = _footprint_sqft(el.get("bounds"))
        if footprint and footprint > MAX_BUILDING_SQFT:
            footprint = None
    return Lead(
        name=name,
        lat=lat,
        lon=lon,
        source="osm",
        source_id=f"{etype}/{el.get('id')}",
        address=street,
        city=tags.get("addr:city", ""),
        state=tags.get("addr:state", ""),
        zip=tags.get("addr:postcode", ""),
        phone=tags.get("phone", tags.get("contact:phone", "")),
        website=tags.get("website", tags.get("contact:website", "")),
        raw_categories=raw,
        primary_category=primary,
        footprint_sqft=footprint,
        business_status="CLOSED_PERMANENTLY" if tags.get("disused") == "yes" else "",
        map_url=f"https://www.openstreetmap.org/{etype}/{el.get('id')}",
    )


def _parts(lat: float, lon: float, radius_miles: float) -> list[Box]:
    """The search circle's bounding square cut into a grid of boxes no wider than
    config.OVERPASS_PART_MILES, keeping only the boxes that reach into the circle."""
    n = max(1, math.ceil(2 * radius_miles / config.OVERPASS_PART_MILES))
    dlat = radius_miles / MILES_PER_DEGREE_LAT
    dlon = dlat / max(0.01, math.cos(math.radians(lat)))
    south, west = lat - dlat, lon - dlon
    step_lat, step_lon = 2 * dlat / n, 2 * dlon / n
    boxes = []
    for i in range(n):
        for j in range(n):
            box = (south + i * step_lat, west + j * step_lon,
                   south + (i + 1) * step_lat, west + (j + 1) * step_lon)
            # The box's point nearest the centre is inside the circle.
            near_lat = min(max(lat, box[0]), box[2])
            near_lon = min(max(lon, box[1]), box[3])
            if haversine_miles(lat, lon, near_lat, near_lon) <= radius_miles:
                boxes.append(box)
    return boxes


def _quarters(box: Box) -> list[Box]:
    s, w, n, e = box
    mid_lat, mid_lon = (s + n) / 2, (w + e) / 2
    return [(s, w, mid_lat, mid_lon), (s, mid_lon, mid_lat, e),
            (mid_lat, w, n, mid_lon), (mid_lat, mid_lon, n, e)]


def _answer_key(query: str) -> str:
    return "overpass-answer\n" + query


def _fetch(query: str, deadline: float, first: int = 0,
           said: Callable[[int, int], None] | None = None) -> tuple[Any, list[str]]:
    """Ask the mirrors (starting with mirror `first`) for one query's answer before
    `deadline` (time.monotonic()); returns (the answer, errors of mirrors that failed
    first) or raises SourceError when none answered.

    A mirror that fails is followed at once by the next one; a mirror that is slow to
    answer is not waited out: after config.OVERPASS_STAGGER_SECONDS the next one is
    asked as well (the slow one may still answer), and the first good answer wins.
    """
    # An answer from any mirror today serves a re-run, whichever mirror it asks first.
    known = cache_get(_answer_key(query))
    if known is not None:
        return known, []
    endpoints = list(config.OVERPASS_ENDPOINTS)
    endpoints = endpoints[first % len(endpoints):] + endpoints[:first % len(endpoints)]
    answers: queue.Queue[tuple[str, Any, BaseException | None]] = queue.Queue()
    errors: list[str] = []
    asked, waiting, next_at = 0, 0, 0.0

    def ask(endpoint: str, left: float) -> None:
        try:
            # Connecting gets at most 10 s; the answer may take the rest of the time.
            data = request_json("POST", endpoint, data={"data": query},
                                timeout=(min(10.0, left), left), retries=1,
                                cache_key_extra="overpass",
                                cacheable=lambda d: not d.get("remark"))
        except HttpError as exc:
            answers.put((endpoint, None, exc))
        except Exception as exc:  # noqa: BLE001 - reported like a failed server
            answers.put((endpoint, None, exc))
        else:
            answers.put((endpoint, data, None))

    while True:
        now = time.monotonic()
        left = deadline - now
        if asked < len(endpoints) and (waiting == 0 or now >= next_at):
            endpoint = endpoints[asked]
            if left < MIN_SECONDS_LEFT:
                errors.append("out of time before trying " + endpoint)
                if waiting == 0:
                    break
                asked = len(endpoints)            # no time for more; wait for the ones asked
                continue
            asked += 1
            waiting += 1
            next_at = now + config.OVERPASS_STAGGER_SECONDS
            if said:
                said(asked, len(endpoints))
            log.info("OpenStreetMap: querying %s", endpoint.split("/")[2])
            threading.Thread(target=ask, args=(endpoint, left), daemon=True).start()
            continue
        if waiting == 0 or left <= 0:
            if waiting:
                errors.append("no server answered in time")
            break
        until = left if asked >= len(endpoints) else min(left, next_at - now)
        try:
            endpoint, data, exc = answers.get(timeout=max(0.01, until))
        except queue.Empty:
            continue
        waiting -= 1
        if exc is not None:
            log.warning("OpenStreetMap server failed: %s", exc)
            errors.append(str(exc))
            continue
        if data.get("remark") and not data.get("elements"):
            log.warning("OpenStreetMap server %s: %s", endpoint, data["remark"])
            errors.append(f"{endpoint}: {data['remark']}")
            continue
        if not data.get("remark"):
            cache_put(_answer_key(query), data)
        return data, errors
    raise SourceError(" | ".join(errors) or "no server answered")


# One part of the area still to ask: (its box, or None for the whole circle; its
# share of the area; how many times it has been split into quarters).
Part = tuple[Box | None, float, int]


class _Parts:
    """The state of one map-data search across its rounds: what was found, which
    parts are still missing, and the progress counts the page shows."""

    def __init__(self, lat: float, lon: float, radius_miles: float, keywords: Sequence[str],
                 progress: Callable[[str], None] | None) -> None:
        self.lat, self.lon, self.radius, self.keywords = lat, lon, radius_miles, keywords
        self.progress = progress
        self.boxes = _parts(lat, lon, radius_miles)
        self.whole = len(self.boxes) == 1
        self.found: dict[str, Lead] = {}
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.missing: list[Part] = []            # parts no server answered for (yet)
        self.done = 0                            # parts that answered
        self.started = 0                         # queries sent (spreads parts over mirrors)
        self.settled, self.total, self.first_total = 0, 0, 0
        self.again = ""                          # progress wording during a catch-up round

    def first(self) -> list[Part]:
        return ([(None, 1.0, 0)] if self.whole
                else [(b, 1 / len(self.boxes), 0) for b in self.boxes])

    def query(self, box: Box | None) -> str:
        return build_query(self.lat, self.lon, self.radius, self.keywords, box=box)

    def areas(self) -> None:
        # Counts only go forward: parts finish out of order, and a part that failed
        # counts as finished once it is replaced by its four smaller quarters.
        if self.progress and self.total > 1:
            split = (", some areas asked again in smaller parts"
                     if self.total > self.first_total and not self.again else "")
            self.progress(f"OpenStreetMap: {self.again or 'searching the free map data'} "
                          f"({self.settled} of {self.total} areas done{split})")

    def said(self) -> Callable[[int, int], None]:
        def note(server: int, servers: int) -> None:
            if self.progress and self.total == 1:
                self.progress(f"OpenStreetMap: {self.again or 'searching the free map data'} "
                              f"(server {server} of {servers})")
        return note

    def ask(self, n: int, box: Box | None, deadline: float) -> tuple[Any, list[str]]:
        # The query's text doesn't depend on the time left, so a part answered on an
        # earlier run (or round) today comes from the cache.
        budget = min(deadline, time.monotonic() + config.OVERPASS_PART_SECONDS)
        return _fetch(self.query(box), budget, first=n, said=self.said())

    def split_before(self, box: Box | None) -> bool:
        """An earlier run today had to ask this part in quarters."""
        return box is not None and any(
            cache_get(_answer_key(self.query(q))) is not None for q in _quarters(box))

    def quarters(self, part: Part) -> list[Part]:
        box, share, depth = part
        return [(q, share / 4, depth + 1) for q in _quarters(box or self.boxes[0])]

    def answered(self, data: Any) -> None:
        self.done += 1
        for lead in map(parse_element, data.get("elements", [])):
            if lead:
                self.found.setdefault(lead.source_id, lead)
        if data.get("remark"):
            log.warning("OpenStreetMap note: %s", data["remark"])
            note = "The map data service returned only part of its results (it was busy)."
            if note not in self.warnings:
                self.warnings.append(note)

    def failed(self, part: Part, exc: BaseException, todo: list[Part]) -> None:
        self.errors.append(str(exc))
        if part[2] < config.OVERPASS_SPLITS:
            # Smaller parts are what busy servers still answer: ask again, split.
            todo += self.quarters(part)
            self.total += 4
        else:
            self.missing.append(part)

    def run_round(self, todo: list[Part], seconds: float) -> None:
        """Ask for the parts in todo, config.OVERPASS_PARALLEL at a time, within
        `seconds`; parts no server answered for end up in self.missing."""
        deadline = time.monotonic() + seconds
        self.settled, self.total = 0, len(todo)
        self.first_total = self.total
        self.areas()
        with ThreadPoolExecutor(max_workers=config.OVERPASS_PARALLEL) as pool:
            running: dict[Any, Part] = {}
            while todo or running:
                if not self._start(pool, running, todo, deadline):
                    break
                finished, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in finished:
                    part = running.pop(future)
                    self.settled += 1
                    try:
                        data, _ = future.result()
                    except Exception as exc:  # noqa: BLE001 - SourceError, or a thread that broke
                        self.failed(part, exc, todo)
                    else:
                        self.answered(data)
                    self.areas()

    def _start(self, pool: ThreadPoolExecutor, running: dict[Any, Part], todo: list[Part],
               deadline: float) -> bool:
        """Start parts until config.OVERPASS_PARALLEL run; False when none is running."""
        while todo and len(running) < config.OVERPASS_PARALLEL:
            if deadline - time.monotonic() < MIN_SECONDS_LEFT:
                self.missing += todo
                self.errors.append("out of time before asking for every part")
                todo.clear()
                break
            part = todo.pop(0)
            if part[2] < config.OVERPASS_SPLITS and self.split_before(part[0]):
                todo += self.quarters(part)          # go straight to the quarters
                self.total += 3
                continue
            running[pool.submit(self.ask, self.started, part[0], deadline)] = part
            self.started += 1
        return bool(running)

    def missing_share(self) -> float:
        return sum(share for _, share, _ in self.missing)


def search(lat: float, lon: float, radius_miles: float, keywords: Sequence[str] = (),
           progress: Callable[[str], None] | None = None,
           stats: dict[str, Any] | None = None) -> tuple[list[Lead], list[str]]:
    """Return (leads, warnings).

    One big query for a whole 30-mile circle is what the free public map servers
    most often refuse or time out on, so a wide search is cut into parts (a grid of
    boxes up to config.OVERPASS_PART_MILES wide), asked config.OVERPASS_PARALLEL at a
    time, each part starting at a different mirror to spread the load. A part that
    fails is asked again once as four smaller quarters. The first round gets
    config.OVERPASS_DEADLINE_SECONDS; parts still missing after it (busy or throttling
    servers) are asked again automatically, up to config.OVERPASS_RETRY_ROUNDS more
    rounds, each after a pause of config.OVERPASS_RETRY_PAUSE_SECONDS and with
    config.OVERPASS_RETRY_SECONDS of its own. Answers are cached, so a later round (or
    a re-run the same day) asks only for what is still missing. `stats`, when given,
    records how many parts had to be asked again.

    Raises SourceError when no part answered, and PartialResult (holding what was
    found) when only some did.
    """
    run = _Parts(lat, lon, radius_miles, keywords, progress)
    run.run_round(run.first(), config.OVERPASS_DEADLINE_SECONDS)
    retried = 0
    for n in range(1, config.OVERPASS_RETRY_ROUNDS + 1):
        if not run.missing:
            break
        todo, run.missing = run.missing, []
        retried = max(retried, len(todo))
        log.info("OpenStreetMap: asking again for %d part(s) (round %d)", len(todo), n + 1)
        run.again = (f"asking again for the areas the busy map servers missed "
                     f"(try {n} of {config.OVERPASS_RETRY_ROUNDS})")
        if progress:
            progress(f"OpenStreetMap: {run.again}, in a moment")
        time.sleep(config.OVERPASS_RETRY_PAUSE_SECONDS)   # let a throttling server cool down
        run.run_round(todo, config.OVERPASS_RETRY_SECONDS)
    if stats is not None and retried:
        stats["osm areas asked again"] = (
            f"{retried} (all answered)" if not run.missing else f"{retried} (some never answered)")
    return _result(run)


def _result(run: _Parts) -> tuple[list[Lead], list[str]]:
    leads = list(run.found.values())
    if not run.done:
        raise SourceError("All OpenStreetMap (Overpass) servers failed: " + " | ".join(run.errors))
    missing = run.missing_share()
    if missing > 0.001:
        share = max(1, round(missing * 100))
        n_boxes = len(run.boxes)
        answered = min(n_boxes - 1, round((1 - missing) * n_boxes))
        coverage = (f"about {answered} of {n_boxes} areas" if n_boxes > 1
                    else f"about {100 - share}% of the area")
        raise PartialResult(
            f"OpenStreetMap answered for only part of the area (about {share}% missing): "
            + " | ".join(run.errors[-5:]), leads, run.warnings, coverage)
    return leads, run.warnings
