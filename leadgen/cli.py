"""Command line entry point.

    python -m leadgen run --location 84101 --radius 30 --out leads.xlsx
    python -m leadgen web
    python -m leadgen reference      # copy the site's Yes / No marks into the scoring tests
    python -m leadgen merge-sites    # list saved leads that are one business (--apply merges them)
    python -m leadgen out-of-area    # list saved leads far outside AARCO's area (--remove, --restore)
    python -m leadgen backup         # copy every saved lead, mark, call and search to a file
    python -m leadgen restore FILE   # what putting a copy back would add (--apply adds it)
"""

import argparse
import sys
from pathlib import Path

from . import config, saved, store
from .envfile import load_dotenv
from .export import to_csv_bytes, to_xlsx_bytes
from .geo import GeocodeError, miles_from_aarco
from .pipeline import SOURCES, PipelineError, SearchParams, run
from .scoring import TIER_LABELS


def _split_keywords(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        out += [k.strip() for k in v.split(",") if k.strip()]
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="leadgen", description="Find businesses likely to run "
                                "large commercial compactors or balers.")
    sub = p.add_subparsers(dest="command")

    r = sub.add_parser("run", help="Generate a lead list")
    r.add_argument("--location", "-l", default=config.DEFAULT_LOCATION,
                   help="ZIP, city, address, or 'lat,lon' (default: AARCO Compactor, "
                        f"{config.OWN_ADDRESS})")
    r.add_argument("--radius", "-r", type=float, default=config.DEFAULT_RADIUS_MILES,
                   help="Search radius in miles (default 30)")
    r.add_argument("--keywords", "-k", nargs="*", default=None,
                   help="Extra keywords to search for (scores always use the defaults), e.g. compactor baler "
                        f"(default: {' '.join(config.DEFAULT_KEYWORDS)})")
    r.add_argument("--source", choices=SOURCES, default="auto",
                   help="auto = OpenStreetMap plus Google and/or Yelp when their key is set; "
                        "both = Google + OpenStreetMap")
    r.add_argument("--min-score", type=int, default=config.DEFAULT_MIN_SCORE,
                   help="Drop leads scoring below this (competitors are always kept)")
    r.add_argument("--grid", type=int, choices=[1, 7, 19], default=1,
                   help="Google/Yelp: split the area into 1, 7, or 19 search cells for more "
                        "results (past 25 miles Yelp needs 7, or searches 25 miles around "
                        "the center when its daily limit cannot cover 7)")
    r.add_argument("--max-requests", type=int, default=None,
                   help="Cap on Google and on Yelp API requests per run (cost control). "
                        "Default: Google, enough for every search (about 100 with --grid 1); "
                        f"Yelp, {config.YELP_DEFAULT_MAX_REQUESTS}. Yelp never passes "
                        f"{config.YELP_DAILY_LIMIT} calls a day in total, counted in the "
                        "website's database: Yelp is only used when DATABASE_URL points at it")
    r.add_argument("--only-keyword-matches", action="store_true",
                   help="Keep only leads that match one of the keywords")
    r.add_argument("--include-closed", action="store_true", help="Keep permanently closed places")
    r.add_argument("--limit", type=int, default=0,
                   help="Keep only the top N prospects (competitors are always kept)")
    r.add_argument("--out", "-o", default="output/leads.xlsx",
                   help="Output file (.xlsx or .csv). Default output/leads.xlsx")
    r.add_argument("--api-key", default="", help="Google Places API key (or set GOOGLE_PLACES_API_KEY)")
    r.add_argument("--yelp-api-key", default="", help="Yelp API key (or set YELP_API_KEY)")
    r.add_argument("--quiet", "-q", action="store_true")

    ref = sub.add_parser("reference", help="Copy the businesses marked Yes / No on the site "
                         "into the scoring reference set (needs DATABASE_URL)")
    ref.add_argument("--out", default=None, help="Reference file (default "
                     "tests/fixtures/scoring_reference.json)")

    ms = sub.add_parser("merge-sites", help="List the saved leads that are one business (two "
                        "rows for one site, saved before the search rules joined them); "
                        "--apply merges each group into one lead, keeping every listing, mark "
                        "and call. Searches never do this on their own")
    ms.add_argument("--apply", action="store_true", help="Merge the listed groups")

    far = sub.add_parser("out-of-area", help="List the saved leads far outside AARCO's area (from a "
                         "search around the wrong place); --remove takes them out of the list, "
                         "--restore puts them back. Leads marked Yes / No or called are never removed")
    far.add_argument("--miles", type=float, default=None,
                     help="Farther than this from AARCO's shop (default: twice AARCO's area, "
                          f"{2 * config.SERVICE_AREA_MILES:g} miles)")
    act = far.add_mutually_exclusive_group()
    act.add_argument("--remove", action="store_true",
                     help="Take them out of the saved list (each is kept aside, so --restore can bring it back)")
    act.add_argument("--restore", action="store_true", help="Put every removed lead back in the saved list")

    bk = sub.add_parser("backup", help="Write a copy of the database (saved leads, Yes / No marks, calls, "
                        "searches, who made each) to a file; changes nothing")
    bk.add_argument("--out", default=None, help="The file to write (default leadgen-backup-<Utah date and "
                    "time>.json.gz in this folder; an existing file is never replaced)")

    rs = sub.add_parser("restore", help="Put a backup's rows back: lists what would be added; --apply adds "
                        "every row the database doesn't have. Never changes or removes a row")
    rs.add_argument("file", help="The backup file (.json.gz, from `python -m leadgen backup`)")
    rs.add_argument("--apply", action="store_true", help="Add the missing rows (one transaction)")

    w = sub.add_parser("web", help="Start the web page")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=5000)
    w.add_argument("--debug", action="store_true")
    return p


def cmd_run(args: argparse.Namespace) -> int:
    params = SearchParams(
        location=args.location, radius_miles=args.radius,
        keywords=config.DEFAULT_KEYWORDS if args.keywords is None else _split_keywords(args.keywords),
        source=args.source, min_score=args.min_score, grid=args.grid,
        max_requests=args.max_requests, only_keyword_matches=args.only_keyword_matches,
        include_closed=args.include_closed, limit=args.limit, api_key=args.api_key,
        yelp_api_key=args.yelp_api_key,
    )
    # The Yelp key is shared, and the website keeps the one count of its calls (at most
    # YELP_DAILY_LIMIT in any 24 hours) in its database. Without that database the
    # calls would be counted in a local file instead, apart from the site's count.
    if not store.database_url() and (args.source == "yelp" or
                                     (args.source == "auto" and params.resolved_keys()[1])):
        if args.source == "yelp":
            print(f"Error: Yelp calls must count against the website's limit of "
                  f"{config.YELP_DAILY_LIMIT} a day, which is kept in its database. Set "
                  "DATABASE_URL to the website's database to use Yelp here, or use "
                  "--source osm.", file=sys.stderr)
            return 2
        params.skip_yelp = True
        print("Note: Yelp was left out, because DATABASE_URL (the website's database, which "
              f"counts the shared key's {config.YELP_DAILY_LIMIT} calls a day) is not set.",
              file=sys.stderr)
    progress = None if args.quiet else (lambda m: print(f"  {m}", file=sys.stderr))
    try:
        result = run(params, progress)
    except (PipelineError, GeocodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if getattr(exc, "detail", ""):
            print(f"Details: {getattr(exc, 'detail', '')}", file=sys.stderr)
        return 2

    away = miles_from_aarco(*result.center)
    if away > config.SERVICE_AREA_MILES:
        print(f"Warning: {result.location_label} is {away:,.0f} miles from AARCO's shop, outside "
              f"AARCO's area (about {config.SERVICE_AREA_MILES:g} miles around it).", file=sys.stderr)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".csv":
        out.write_bytes(to_csv_bytes(result.leads))
    else:
        if out.suffix.lower() != ".xlsx":
            out = out.with_suffix(".xlsx")
        out.write_bytes(to_xlsx_bytes(result.leads, result.run_info(params)))

    for w in result.warnings:
        print(f"Warning: {w}", file=sys.stderr)
    print(f"\n{len(result.leads)} leads near {result.location_label} -> {out}")
    for k, v in result.stats.items():
        print(f"  {k}: {v}")
    if result.leads:
        print("\nTop leads:")
        for lead in result.leads[:15]:
            flag = f"  [{'; '.join(lead.flags)}]" if lead.flags else ""
            print(f"  {lead.score:>3}  {TIER_LABELS[lead.tier]:<13} {lead.name[:40]:<40} "
                  f"{lead.category[:30]:<30} {lead.distance_miles:>5} mi{flag}")
    return 0


def cmd_reference(args: argparse.Namespace) -> int:
    from . import reference
    if not store.database_url():
        print("Note: DATABASE_URL is not set, so the marks come from the local database, "
              "not the website's.", file=sys.stderr)
    try:
        yes, no = reference.export(args.out or reference.DEFAULT_PATH)
    except store.Unavailable as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"{yes} businesses marked Yes and {no} marked No are in the reference set "
          f"({args.out or reference.DEFAULT_PATH}). Run the tests: python -m pytest")
    return 0


def cmd_out_of_area(args: argparse.Namespace) -> int:
    from . import cleanup
    if not store.database_url():
        print("Note: DATABASE_URL is not set, so this is the local database, not the website's.",
              file=sys.stderr)
    try:
        if args.restore:
            n = cleanup.restore()
            print(f"{n} removed lead{'' if n == 1 else 's'} put back in the saved list.")
            return 0
        miles = cleanup.DEFAULT_MILES if args.miles is None else args.miles
        leads = cleanup.far_leads(miles)
        if not leads:
            print(f"No saved lead is farther than {miles:g} miles from AARCO's shop.")
            return 0
        kept = sum(f.kept for f in leads)
        print(f"{len(leads):,} saved lead{'' if len(leads) == 1 else 's'} farther than {miles:g} miles "
              f"from AARCO's shop ({kept:,} marked or called, never removed):")
        print("\n".join(cleanup.summary(leads)))
        if not args.remove:
            print("\nNothing was changed. Add --remove to take them out of the saved list "
                  "(--restore puts them back).")
            return 0
        n = cleanup.remove(leads)
    except store.Unavailable as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"\n{n:,} lead{'' if n == 1 else 's'} taken out of the saved list (kept aside: "
          "`python -m leadgen out-of-area --restore` puts them back).")
    return 0


def _planned(row: saved.PlannedRow) -> str:
    from .localtime import utah
    said = [f"saved {utah(row.saved_at):%Y-%m-%d}"]
    if row.mark:
        said.append(f"marked {row.mark.capitalize()}")
    if row.calls:
        said.append(f"{row.calls} call{'' if row.calls == 1 else 's'}")
    where = f", {row.city}" if row.city else ""
    return f"{row.name[:60]}{where} ({'; '.join(said)})"


def cmd_merge_sites(args: argparse.Namespace) -> int:
    if not store.database_url():
        print("Note: DATABASE_URL is not set, so this is the local database, not the website's.",
              file=sys.stderr)
    try:
        plan = saved.merge_plan()
        if not plan:
            print("No saved leads to merge: every business has one row.")
            return 0
        rows = sum(len(group) - 1 for group in plan)
        print(f"{len(plan):,} business{'' if len(plan) == 1 else 'es'} saved as more than one "
              f"row ({rows:,} row{'' if rows == 1 else 's'} would join the first of their group):")
        for n, group in enumerate(plan, 1):
            print(f"  {n:>3}. {_planned(group[0])}")
            for row in group[1:]:
                print(f"       + {_planned(row)}")
        if not args.apply:
            print("\nNothing was changed. Add --apply to merge them: every listing, mark and call "
                  "is kept, and each merged row is recorded in the merged_leads table.")
            return 0
        n = saved.merge_sites()
    except store.Unavailable as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"\n{n:,} saved lead{'' if n == 1 else 's'} merged into the first row of their group.")
    return 0


def _which_database() -> None:
    if not store.database_url():
        print("Note: DATABASE_URL is not set, so this is the local database, not the website's.",
              file=sys.stderr)


_ONE = {"saved leads": "saved lead", "calls": "call", "searches": "search"}


def _counts_text(counts: dict[str, int]) -> str:
    """"3 saved leads, 2 marked yes, 1 marked no, 1 call, 2 searches"."""
    return ", ".join(f"{n:,} {_ONE.get(what, what) if n == 1 else what}" for what, n in counts.items())


def cmd_backup(args: argparse.Namespace) -> int:
    from . import backup
    _which_database()
    try:
        path, copy = backup.write(Path(args.out) if args.out else None)
    except FileExistsError as exc:
        print(f"Error: {exc.filename} already exists; nothing was written. Pick another name.", file=sys.stderr)
        return 2
    except store.Unavailable as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    rows = sum(len(t["rows"]) for t in copy["tables"].values())
    print(f"Backup written: {path} ({backup.size_text(path)}, {rows:,} rows from {len(copy['tables'])} tables, "
          f"{copy['made']}).")
    print(f"It holds {_counts_text(copy['counts'])}.")
    print("Keep it somewhere safe and private (it holds call notes and phone numbers); never commit it.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from . import backup
    _which_database()
    try:
        copy = backup.read(Path(args.file))
        counts = backup.restore(copy, apply=args.apply)
        now = backup.counts_now() if args.apply else None
    except backup.BackupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except store.Unavailable as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"Backup made {copy.get('made', '?')} from a {copy.get('database', '?')} database; "
          f"it holds {_counts_text(copy.get('counts', {}))}.")
    width = max(len(c.table) for c in counts) if counts else 5
    verb = "added" if args.apply else "to add"
    print(f"  {'table':<{width}}  {'in file':>9}  {'already here':>12}  {verb:>9}")
    for c in counts:
        print(f"  {c.table:<{width}}  {c.in_file:>9,}  {c.already:>12,}  {c.added:>9,}")
    added = sum(c.added for c in counts)
    if not args.apply:
        print(f"\nNothing was changed. Add --apply to add these {added:,} rows (rows already here are "
              "never changed or removed).")
        return 0
    print(f"\n{added:,} rows added; nothing was changed or removed.")
    if now is not None:
        print(f"The database now holds {_counts_text(now)}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    if args.command == "web":
        from .web import create_app
        create_app().run(host=args.host, port=args.port, debug=args.debug)
        return 0
    if args.command == "reference":
        return cmd_reference(args)
    if args.command == "out-of-area":
        return cmd_out_of_area(args)
    if args.command == "merge-sites":
        return cmd_merge_sites(args)
    if args.command == "backup":
        return cmd_backup(args)
    if args.command == "restore":
        return cmd_restore(args)
    if args.command != "run":
        build_parser().print_help()
        return 1
    return cmd_run(args)
