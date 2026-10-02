"""leadgen/data/places.json can be rebuilt by script from the Census Gazetteer files
(python -m leadgen.build_places), in exactly the shape the repository holds."""

import json
import zipfile

from leadgen import build_places, places

PLACE_HEADER = ("USPS\tGEOID\tANSICODE\tNAME\tLSAD\tFUNCSTAT\tALAND\tAWATER\tALAND_SQMI\tAWATER_SQMI\t"
                "INTPTLAT\tINTPTLONG                                                     ")
ZCTA_HEADER = "GEOID\tALAND\tAWATER\tALAND_SQMI\tAWATER_SQMI\tINTPTLAT\tINTPTLONG             "


def _place(state, name, sqmi, lat, lon):
    return f"{state}\t49\t0\t{name}\t25\tA\t0\t0\t{sqmi}\t0.\t{lat}\t{lon}                    "


def _gazetteer(tmp_path):
    """A few rows in the Gazetteer's own layout: the place file zipped, the ZCTA file as text."""
    rows = [PLACE_HEADER,
            _place("UT", "Layton city", "22.1", "41.077286", "-111.962216"),
            _place("UT", "Kearns metro township", "5.081", "40.652523", "-111.996263"),
            _place("ID", "Aberdeen city", "1.03", "42.944098", "-112.838381"),
            _place("CO", "Denver city", "153.1", "39.762011", "-104.876365")]       # 370 miles away
    place_zip = tmp_path / "2023_Gaz_place_national.zip"
    with zipfile.ZipFile(place_zip, "w") as archive:
        archive.writestr("2023_Gaz_place_national.txt", "\n".join(rows) + "\n")
    zcta = tmp_path / "2023_Gaz_zcta_national.txt"
    zcta.write_text("\n".join([ZCTA_HEADER, "84041\t0\t0\t25.2\t0.\t41.088135\t-111.97212   ",
                               "80202\t0\t0\t1.4\t0.\t39.75\t-104.99   "]) + "\n")
    return place_zip, zcta


def test_the_committed_table_is_in_the_scripts_shape():
    text = places.DATA.read_text(encoding="utf-8")
    table = json.loads(text)
    assert build_places.render(table) == text                   # byte for byte, one row per line
    assert table["about"] == build_places.about(2023)
    assert table["places"] == sorted(table["places"]) and table["zips"] == sorted(table["zips"])
    assert not any(name.endswith((" city", " town", " CDP", " metro township")) for name, *_ in table["places"])


def test_rows_are_filtered_named_rounded_and_sorted(tmp_path):
    place_zip, zcta = _gazetteer(tmp_path)
    table = build_places.build(build_places.read_rows(place_zip), build_places.read_rows(zcta))
    assert table["places"] == [["Aberdeen", "ID", 42.9441, -112.83838, 0.57],
                               ["Kearns", "UT", 40.65252, -111.99626, 1.27],
                               ["Layton", "UT", 41.07729, -111.96222, 2.65]]
    assert table["zips"] == [["84041", 41.08814, -111.97212, 2.83]]


def test_the_command_writes_the_file_and_check_says_if_it_would_change(tmp_path, capsys):
    place_zip, zcta = _gazetteer(tmp_path)
    out = tmp_path / "places.json"
    assert build_places.main([str(place_zip), str(zcta), "--out", str(out)]) == 0
    assert "3 places and 1 ZIP areas" in capsys.readouterr().out
    assert json.loads(out.read_text())["places"][0][0] == "Aberdeen"
    assert build_places.main([str(place_zip), str(zcta), "--out", str(out), "--check"]) == 0
    out.write_text("{}")
    assert build_places.main([str(place_zip), str(zcta), "--out", str(out), "--check"]) == 1
    assert out.read_text() == "{}"                               # --check writes nothing
