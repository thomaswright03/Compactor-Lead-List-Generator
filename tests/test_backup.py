"""Backups: `python -m leadgen backup` copies every saved lead, mark, call and search to a
file, and `python -m leadgen restore` puts a copy back only on request, adding what is
missing and never changing or removing a row, on SQLite and on Postgres alike."""

import gzip
import json
import os

import pytest

from leadgen import backup, calls, cli, config, daily, http, marks, saved, store, switches
from leadgen.models import Lead
from leadgen.scoring import score_lead


def _lead(name, sid, **kw):
    kw.setdefault("lat", 40.72)
    kw.setdefault("lon", -111.9)
    kw.setdefault("raw_categories", ["shop=supermarket"])
    lead = Lead(name=name, source="osm", source_id=sid, **kw)
    score_lead(lead, config.DEFAULT_KEYWORDS)
    return lead


def _seed():
    """A little of everything the team does: leads, marks (one undone), calls with who made
    them, a search and a failed one, a switch."""
    leads = [_lead("Smith's Marketplace", "s1", phone="8015550100"),
             _lead("Costco Wholesale", "c1", lat=40.75, raw_categories=["shop=wholesale"]),
             _lead("Hampton Inn", "h1", lat=40.78, raw_categories=["tourism=hotel"])]
    saved.save_search(leads, config.DEFAULT_KEYWORDS)
    marks.set_mark(leads[0].uid, "yes", by="Dana")
    undo = marks.set_mark(leads[1].uid, "no", by="Lee")
    marks.undo(undo["id"])
    marks.set_mark(leads[1].uid, "yes", by="Lee")
    marks.set_mark(leads[2].uid, "no", by="Dana")
    calls.log_call(leads[0].uid, "Follow Up", "Spoke to Jim; 60-yd compactor, lease ends March", by="Dana")
    calls.log_call(leads[1].uid, "Interested", "", by="Lee")
    day, _ = daily.claim({"location": "876 Fortune Rd, Salt Lake City, UT 84104", "radius": 30})
    daily.finish(day, {"leads": 3, "new": 3, "details": {"leads kept": 3}})
    daily.release("2026-09-01", "The map data service didn't answer.")
    with store.connect() as db:
        db.run("INSERT INTO search_failures (id, day, at, info) VALUES (?, ?, ?, ?)",
               ("f1", "2026-09-01", 1756700000.25, json.dumps({"failed": True, "reason": "Timed out"})))
    switches.set_switch(config.YELP_OFF_ENV, True, "Thomas")
    return leads


def _everything():
    """Every backed-up table's rows, as sets (row order differs between databases)."""
    with store.connect() as db:
        return {t: {tuple(r) for r in db.all(f"SELECT {', '.join(backup._columns(db, t))} FROM {t}")}
                for t in backup.TABLES}


def _empty_tables():
    with store.connect() as db:
        for table in store.TABLES:
            db.run(f"DELETE FROM {table}")


def test_every_table_with_the_teams_work_is_copied_and_has_a_key():
    assert set(backup.TABLES) == set(store.TABLES) - set(backup.SKIPPED)
    assert {"leads", "marks", "mark_changes", "made_by", "calls", "searches", "search_failures"} <= set(backup.TABLES)
    assert all(backup.KEYS[t] for t in backup.TABLES)
    assert backup.KEYS["usage"] == ("name", "day")


def test_a_copy_put_back_into_an_empty_database_brings_everything_back(tmp_path):
    _seed()
    before = _everything()
    pages = (sorted(l.name for l in saved.load()), marks.get_all(l.uid for l in saved.load()),
             len(daily.history()["searches"]))
    path, copy = backup.write(tmp_path / "copy.json.gz")
    assert copy["counts"] == {"saved leads": 3, "marked yes": 2, "marked no": 1, "calls": 2, "searches": 2}
    assert "cache" not in copy["tables"] and "search_runs" not in copy["tables"]
    _empty_tables()
    assert saved.load() == []
    # Without --apply nothing is written; it says what would be added.
    planned = backup.restore(backup.read(path))
    assert saved.load() == [] and all(c.added == c.in_file and not c.already for c in planned)
    added = backup.restore(backup.read(path), apply=True)
    assert {c.table: c.added for c in added}["calls"] == 2
    assert _everything() == before
    leads = saved.load()
    assert (sorted(l.name for l in leads), marks.get_all(l.uid for l in leads),
            len(daily.history()["searches"])) == pages
    smith = next(l for l in leads if l.name == "Smith's Marketplace")
    latest = calls.history(smith.uid)[0]
    assert latest["notes"].startswith("Spoke to Jim") and latest["by"] == "Dana"
    assert marks.history(smith.uid)[0]["by"] == "Dana"
    assert backup.counts_now() == copy["counts"]


def test_a_restore_never_changes_or_removes_a_row(tmp_path):
    leads = _seed()
    path, _ = backup.write(tmp_path / "copy.json.gz")
    # After the copy: a mark changed, a call and a business added, a search failure removed by hand.
    marks.set_mark(leads[0].uid, "no", by="Lee")
    calls.log_call(leads[2].uid, "Not Interested", "Uses the city's dumpsters", by="Lee")
    saved.save_search([_lead("Walmart Supercenter", "w1", lat=40.6)], config.DEFAULT_KEYWORDS)
    with store.connect() as db:
        db.run("DELETE FROM search_failures WHERE id = ?", ("f1",))
    after = _everything()
    counts = {c.table: c for c in backup.restore(backup.read(path), apply=True)}
    now = _everything()
    # Every row there before is still there, unchanged; only the missing one came back.
    assert after["marks"] == now["marks"] and marks.get_all([leads[0].uid]) == {leads[0].uid: "no"}
    assert all(after[t] <= now[t] for t in backup.TABLES)
    assert counts["search_failures"].added == 1 and sum(c.added for c in counts.values()) == 1
    assert {l.name for l in saved.load()} >= {"Walmart Supercenter", "Smith's Marketplace"}
    assert len(calls.history(leads[2].uid)) == 1
    # Putting the same copy back again adds nothing.
    assert sum(c.added for c in backup.restore(backup.read(path), apply=True)) == 0


@pytest.mark.skipif(not os.environ.get("LEADGEN_TEST_DATABASE_URL"), reason="needs LEADGEN_TEST_DATABASE_URL")
def test_a_copy_moves_between_postgres_and_sqlite(tmp_path, monkeypatch):
    """The live site's Postgres copied into a new SQLite file, and that back into an empty
    Postgres: nothing is lost either way."""
    _seed()
    before = _everything()
    path, _ = backup.write(tmp_path / "from-postgres.json.gz")
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.setattr(http, "CACHE_DIR", tmp_path / "new-sqlite")
    assert not store.database_url()
    backup.restore(backup.read(path), apply=True)
    assert _everything() == before
    again, _ = backup.write(tmp_path / "from-sqlite.json.gz")
    monkeypatch.setenv("DATABASE_URL", os.environ["LEADGEN_TEST_DATABASE_URL"])
    _empty_tables()
    backup.restore(backup.read(again), apply=True)
    assert _everything() == before


def test_the_commands_back_up_and_restore_on_request_only(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed()
    assert cli.main(["backup"]) == 0
    out = capsys.readouterr().out
    [made] = list(tmp_path.glob("leadgen-backup-*.json.gz"))
    assert f"Backup written: {made.name}" in out and "3 saved leads, 2 marked yes, 1 marked no, 2 calls" in out
    assert json.loads(gzip.decompress(made.read_bytes()))["format"] == "leadgen-backup"
    # An existing file is never replaced.
    assert cli.main(["backup", "--out", made.name]) == 2
    assert "already exists" in capsys.readouterr().err
    _empty_tables()
    assert cli.main(["restore", made.name]) == 0
    out = capsys.readouterr().out
    assert "Nothing was changed" in out and saved.load() == []
    assert cli.main(["restore", made.name, "--apply"]) == 0
    out = capsys.readouterr().out
    assert "nothing was changed or removed" in out
    assert "The database now holds 3 saved leads, 2 marked yes, 1 marked no, 2 calls, 2 searches" in out
    assert len(saved.load()) == 3


def test_a_file_that_is_not_a_backup_is_refused(tmp_path, capsys):
    other = tmp_path / "leads.json"
    other.write_text('{"leads": []}')
    assert cli.main(["restore", str(other)]) == 2
    assert "is not a Lead Finder backup" in capsys.readouterr().err
    encrypted = tmp_path / "copy.json.gz.gpg"
    encrypted.write_bytes(b"\x8c\x0d\x04\x09\x03\x02 not readable")
    assert cli.main(["restore", str(encrypted)]) == 2
    assert "decrypt it first" in capsys.readouterr().err
    newer = tmp_path / "newer.json"
    newer.write_text(json.dumps({"format": "leadgen-backup", "version": 1, "tables": {"robots": {}}}))
    with pytest.raises(backup.BackupError, match="newer version"):
        backup.read(newer)
