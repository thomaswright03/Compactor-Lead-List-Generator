# Scoring evaluation

How well the rule-based scorer (`leadgen/scoring.py` and the `scoring_*.py` modules
beside it, with the categories, weights, brands and name words in `leadgen/config.py` and
the `config_*.py` modules it takes in) ranks businesses, measured on the
reference set `tests/fixtures/scoring_reference.json`. Run it any time with:

```bash
python -m leadgen scoring-report
```

The scoring rules carry a version, `SCORING_VERSION` in `leadgen/config.py`, raised by
hand whenever a change moves scores or tiers (say so in the commit). The report and the
public `/healthz` page both show it, so a saved score can be matched to the rules that
made it. Calibration against the real Yes / No outcomes is out of scope by the owner's
decision; the checks below are what the scoring is held to.

## Current numbers (rules version 2, 2026-10-08)

```
Typical listings (45, with whether each runs a baler or compactor)
Tier  Businesses  Have equipment  Don't  Flagged
A              6               6      0        0
B              6               6      0        0
C              4               3      1        0
D             29               0     29        1
Known to have equipment in tier A or B: 12 of 15 (80%; the tests need 75%)
Lowest score with equipment: 30; highest without: 23 (the tests need the first above the second)

Businesses AARCO's staff marked Yes / No on the site (0)
None yet: the site's Yes / No marks haven't been copied in (operator runbook, "Monthly: the scoring check").

Also held to by the tests: not_campus (8), not_production (5), name_traps (11), generic_names (2), needs_name (4), own_company (18)
```

## Rules versions

- **1**: the rules as first versioned.
- **2** (2026-10-08): AARCO's own names and the competitors' (`config.SELF_ALIASES`,
  `config.COMPETITORS`) match only as whole words, or written as one word opening the
  name or a part of the website, as the parent company's names already did
  (`scoring_flags._holds_alias`). Under version 1 they matched inside other words, so "Marco
  Compactor Repair" was flagged as AARCO's own and "Repro Balers" as Pro Baler; such a
  listing now gets the type and flag its category gives. Scores and tiers don't move;
  only those listings' type and flag do. Saved rows are not rewritten: a row flagged
  under version 1 is shown as version 2 reads it (`scoring.current_type_flags` in
  `saved._ready`), and the next search that finds it saves it that way. The reference
  file's `own_company` list gained "Arco Compactor" and the two names that must not
  match, "Marco Compactor Repair" and "Monarco Compactor".

## What the numbers mean

**Typical listings** are 45 hand-picked listings of the kinds a search finds (grocery
chains, warehouse stores, hospitals, plants, nail salons, gas stations...), each with
whether a business like it runs a baler or compactor (`has_equipment`). Scored with the
rules as they are now:

- Every listing known to have equipment lands in tier A, B or C, and none known not to
  reaches A or B. Tier D (29 listings, the "weak" tier, below the default minimum score
  of 20) holds only businesses without equipment; the one flagged there is a competitor
  listing, which is flagged and never a prospect.
- 12 of the 15 with equipment (80%) are in tier A or B. The test
  `test_reference_businesses_rank_where_they_should` fails below 75%, and fails as soon
  as any listing without equipment reaches A or B.
- The lowest score of a listing with equipment (30) is above the highest score of one
  without (23): the two groups don't overlap. The same test holds the scoring to that.

**Businesses AARCO's staff marked Yes / No** are real businesses from the site
(`confirmed` in the file), each with the tier it had when it was copied. The table
counts them per tier under the rules as they are now, and **Moved tier** names each
whose tier changed since the copy. The test `test_confirmed_businesses_keep_their_tier`
fails if a business marked Yes dropped a tier or one marked No rose one; a move the
other way is allowed and only listed here. The list is empty until the first copy from
the live site (below).

**Also held to by the tests** are the file's other lists, each a kind of mistake the
scoring once made and must not make again (places named after a university that aren't
the campus, shops whose name has a production word, names holding another business's
name, generic names, listings with nothing to call, AARCO's own listings); the developer
overview's "The scoring reference file" says what each checks.

## Refreshing the reference set

The `confirmed` list comes from the site's marks. Once a month (operator runbook,
"Monthly: the scoring check") the owner opens **Find leads** → **For the site
administrator** → **Scoring check** → **Download the scoring check file** and sends it to
whoever looks after the code. It holds only the facts the scoring reads (name, source,
categories, review counts, footprint, website), never phone numbers, addresses or call
notes. With `DATABASE_URL` set to the site's database, `python -m leadgen reference`
writes the same file. Put it in place of `tests/fixtures/scoring_reference.json`, then:

```bash
python -m leadgen scoring-report      # the numbers above, with the new confirmed list
python -m pytest tests/test_scoring.py
```

Commit the file with the report's numbers written into this page. If a business moved
the wrong way, the weights in `leadgen/config.py` need looking at; a change that moves
scores raises `SCORING_VERSION`.

## Changing the rules

1. Change the rule or weight in `leadgen/config.py` (or the `config_*.py` it takes in) or
   `leadgen/scoring.py` (or the `scoring_*.py` beside it).
2. Run `python -m leadgen scoring-report` and `python -m pytest tests/test_scoring.py
   tests/test_classify_rules.py`.
3. Raise `SCORING_VERSION` when scores or tiers moved, update the numbers on this
   page, and say in the commit what moved and why.
