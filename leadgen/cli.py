"""Command line entry point.

    python -m leadgen run --location 84101 --radius 30 --out leads.xlsx
    python -m leadgen web
"""

import argparse
import sys
from pathlib import Path

from . import config
from .envfile import load_dotenv
from .export import to_csv_bytes, to_xlsx_bytes
from .geo import GeocodeError
from .pipeline import SOURCES, PipelineError, SearchParams, run
from .scoring import TIER_LABELS


def _split_keywords(values):
    out = []
    for v in values or []:
        out += [k.strip() for k in v.split(",") if k.strip()]
    return out


def build_parser():
    p = argparse.ArgumentParser(prog="leadgen", description="Find businesses likely to run "
                                "large commercial compactors or balers.")
    sub = p.add_subparsers(dest="command")

    r = sub.add_parser("run", help="Generate a lead list")
    r.add_argument("--location", "-l", default=config.DEFAULT_LOCATION,
                   help="ZIP, city, address, or 'lat,lon' (default: Arco Compactor, "
                        f"{config.OWN_ADDRESS})")
    r.add_argument("--radius", "-r", type=float, default=config.DEFAULT_RADIUS_MILES,
                   help="Search radius in miles (default 30)")
    r.add_argument("--keywords", "-k", nargs="*", default=None,
                   help="Extra keywords to search for and boost, e.g. compactor baler "
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
                        f"{config.YELP_DAILY_LIMIT} calls a day in total")
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

    w = sub.add_parser("web", help="Start the web page")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=5000)
    w.add_argument("--debug", action="store_true")
    return p


def cmd_run(args):
    params = SearchParams(
        location=args.location, radius_miles=args.radius,
        keywords=config.DEFAULT_KEYWORDS if args.keywords is None else _split_keywords(args.keywords),
        source=args.source, min_score=args.min_score, grid=args.grid,
        max_requests=args.max_requests, only_keyword_matches=args.only_keyword_matches,
        include_closed=args.include_closed, limit=args.limit, api_key=args.api_key,
        yelp_api_key=args.yelp_api_key,
    )
    progress = None if args.quiet else (lambda m: print(f"  {m}", file=sys.stderr))
    try:
        result = run(params, progress)
    except (PipelineError, GeocodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

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


def main(argv=None):
    load_dotenv()
    args = build_parser().parse_args(argv)
    if args.command == "web":
        from .web import create_app
        create_app().run(host=args.host, port=args.port, debug=args.debug)
        return 0
    if args.command != "run":
        build_parser().print_help()
        return 1
    return cmd_run(args)
