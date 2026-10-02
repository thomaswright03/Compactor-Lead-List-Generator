"""Rebuild data/places.json (the towns and ZIP code areas places.py looks up) from the US
Census Bureau's Gazetteer files.

    python -m leadgen.build_places --download            # fetch the 2023 files, rebuild
    python -m leadgen.build_places PLACE.zip ZCTA.zip    # from files already downloaded
    python -m leadgen.build_places --check PLACE.zip ZCTA.zip   # only say if it differs

The two inputs are the Gazetteer's national place file and ZIP Code Tabulation Area
(ZCTA) file, as the zip archives the Census Bureau publishes or the .txt files inside:

    https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/2023_Gaz_place_national.zip
    https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/2023_Gaz_zcta_national.zip

(for a later year, pass --year and the matching files). The filter and shape:

- every place and ZCTA whose internal point (INTPTLAT, INTPTLONG) is within 200 miles
  of Salt Lake City (40.7608, -111.8910; Haversine, geo.haversine_miles), which covers
  Utah and the edges of Idaho, Wyoming, Colorado and Nevada;
- a place's name loses its legal description (NAME "Layton city" -> "Layton"; also
  " town", " CDP" and " metro township");
- latitude and longitude rounded to 5 decimals, and a radius in miles of a circle with
  the same land area (sqrt(ALAND_SQMI / pi), 2 decimals);
- places sorted by name then state, ZIP areas by ZIP code; one row per line.

Run against the 2023 files, it writes a file byte for byte identical to the one in the
repository (checked when this script was written; `git diff leadgen/data/places.json`
shows nothing). Nothing else changes: saved leads are not touched, and places.py reads
the new table the next time the site starts.
"""

import argparse
import csv
import io
import json
import math
import re
import sys
import tempfile
import zipfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from .geo import haversine_miles

YEAR = 2023
URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{year}_Gazetteer/{year}_Gaz_{kind}_national.zip"
CENTRE = (40.7608, -111.8910)           # Salt Lake City (geo.KNOWN_PLACES)
MILES = 200.0
OUT = Path(__file__).resolve().parent / "data" / "places.json"
# The legal description at the end of a place's NAME ("Layton city", "Kearns metro township").
LEGAL = re.compile(r" (city|town|CDP|metro township)$")


def about(year: int = YEAR) -> str:
    """The table's own description of where it came from (its "about")."""
    return (f"Towns (US Census Bureau {year} Gazetteer place file, {URL.format(year=year, kind='place')}) "
            f"and ZIP code areas ({year} Gazetteer ZCTA file, {year}_Gaz_zcta_national.zip) whose centre is "
            f"within {MILES:g} miles of Salt Lake City. places: [name without 'city' / 'town' / 'CDP' / "
            "'metro township', state, latitude, longitude, radius in miles of a circle of the same land "
            "area]; zips: [ZIP, latitude, longitude, radius]. Used by leadgen/places.py.")


def read_rows(path: Path) -> Iterator[dict[str, str]]:
    """The rows of a Gazetteer file (tab separated, its header names padded with spaces),
    from the .txt file or the .zip archive that holds it."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            name = next(n for n in archive.namelist() if n.endswith(".txt"))
            text = archive.read(name).decode("utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    rows = csv.reader(io.StringIO(text), delimiter="\t")
    header = [h.strip() for h in next(rows)]
    for row in rows:
        if row:
            yield dict(zip(header, (v.strip() for v in row)))


def _near(row: dict[str, str]) -> tuple[float, float] | None:
    lat, lon = float(row["INTPTLAT"]), float(row["INTPTLONG"])
    return (lat, lon) if haversine_miles(CENTRE[0], CENTRE[1], lat, lon) <= MILES else None


def _radius(row: dict[str, str]) -> float:
    return round(math.sqrt(float(row["ALAND_SQMI"]) / math.pi), 2)


def build(place_rows: Iterator[dict[str, str]], zcta_rows: Iterator[dict[str, str]],
          year: int = YEAR) -> dict[str, Any]:
    """The table from the Gazetteer's place and ZCTA rows."""
    places, zips = [], []
    for row in place_rows:
        if (point := _near(row)) is not None:
            places.append([LEGAL.sub("", row["NAME"]), row["USPS"],
                           round(point[0], 5), round(point[1], 5), _radius(row)])
    for row in zcta_rows:
        if (point := _near(row)) is not None:
            zips.append([row["GEOID"], round(point[0], 5), round(point[1], 5), _radius(row)])
    return {"about": about(year), "places": sorted(places), "zips": sorted(zips)}


def render(table: dict[str, Any]) -> str:
    """The file's text: compact JSON with one place or ZIP area per line (so a diff shows
    which ones changed), no newline at the end."""
    def one(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return ('{"about":' + one(table["about"])
            + ',"places":[' + ",\n".join(one(p) for p in table["places"])
            + '],"zips":[' + ",\n".join(one(z) for z in table["zips"]) + "]}")


def download(year: int, folder: Path) -> tuple[Path, Path]:
    """Fetch the year's two Gazetteer archives into folder (about 2 MB)."""
    import requests

    from . import config
    paths = []
    for kind in ("place", "zcta"):
        url = URL.format(year=year, kind=kind)
        print(f"Downloading {url}")
        resp = requests.get(url, headers={"User-Agent": config.HTTP_USER_AGENT}, timeout=120)
        resp.raise_for_status()
        path = folder / url.rsplit("/", 1)[1]
        path.write_bytes(resp.content)
        paths.append(path)
    return paths[0], paths[1]


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m leadgen.build_places",
                                description="Rebuild leadgen/data/places.json from the Census Gazetteer files.")
    p.add_argument("files", nargs="*", type=Path, metavar="FILE",
                   help="the Gazetteer place file then the ZCTA file (.zip or .txt)")
    p.add_argument("--download", action="store_true", help="fetch the two files from census.gov first")
    p.add_argument("--year", type=int, default=YEAR, help=f"the Gazetteer year (default {YEAR})")
    p.add_argument("--out", type=Path, default=OUT, help="where to write (default: leadgen/data/places.json)")
    p.add_argument("--check", action="store_true", help="write nothing; exit 1 if the file would change")
    args = p.parse_args(argv)
    if args.download == bool(args.files) or (args.files and len(args.files) != 2):
        p.error("give the place file and the ZCTA file, or --download")
    with tempfile.TemporaryDirectory() as folder:
        place, zcta = download(args.year, Path(folder)) if args.download else args.files
        text = render(build(read_rows(place), read_rows(zcta), args.year))
    table = json.loads(text)
    counts = f"{len(table['places'])} places and {len(table['zips'])} ZIP areas"
    data = text.encode("utf-8")              # bytes: the same file on every system
    same = args.out.exists() and args.out.read_bytes() == data
    if args.check:
        print(f"{args.out}: {'unchanged' if same else 'would change'} ({counts})")
        return 0 if same else 1
    args.out.write_bytes(data)
    print(f"Wrote {args.out}: {counts}{' (unchanged)' if same else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
