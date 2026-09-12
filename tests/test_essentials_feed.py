"""essentials_feed tests: parse the Fandom monthly-list wikitext and merge new
lineup rows into essentials_lineup idempotently (ongoing upkeep for new months).
"""

from __future__ import annotations

import tempfile

from mailroom.db import connect, init_db
from mailroom.verticals.game_catalog.essentials_feed import (
    merge_new_lineup_rows,
    parse_yearly_wikitext,
)

FIXTURE = """
<onlyinclude>
{| class="wikitable sortable"
! rowspan="2"|Game
! Platform(s)
! Date added
! Date removed
|-
! [[PlayStation 5|PS5]]
! [[PlayStation 4|PS4]]
|-
| 900
| 1
|''[[Fake Game One]]''
| {{yes|PS5}} || {{yes|PS4}} || {{no|}}
| rowspan="3" |{{dts|2026-11-3}}
| rowspan="3" |{{dts|2026-12-1}}
|-
| 901
| 2
|''[[Fake Game Two]]''
| {{no|}} || {{yes|PS4}} || {{no|}}
|-
| 902
| 3
|''[[Fake Game Three]]''
| {{yes|PS5}} || {{no|}} || {{no|}}
| rowspan="2" |January 6, 2027
| rowspan="2" |February 2, 2027
|-
| 903
| 4
|''[[Fake Game Four]]''
| {{yes|PS5}} || {{yes|PS4}} || {{no|}}
|}
"""


def test_parse_yearly_wikitext():
    rows = parse_yearly_wikitext(FIXTURE)
    assert [r["title"] for r in rows] == [
        "Fake Game One", "Fake Game Two", "Fake Game Three", "Fake Game Four",
    ]
    one, two, three, four = rows
    assert one["added"] == "2026-11-03" and one["removed"] == "2026-12-01"
    assert one["ps4"] and one["ps5"]
    # date carried across the rowspan'd month group
    assert two["added"] == "2026-11-03"
    assert two["ps4"] and not two["ps5"]
    # plain-text (current-year) date form
    assert three["added"] == "2027-01-06"
    assert four["added"] == "2027-01-06"


def test_parse_ignores_citation_dates():
    """The reference block on a month's first row carries the blog date and
    'Retrieved on <date>' — neither is the row's available_from/available_to.
    Regression: the September 2026 page (Wobbly Life / MLB The Show 26) is
    exactly this shape."""
    fixture = """
{| class="wikitable"
! Game
! Platform(s)
! Date added
! Date removed
|-
| 898
| 27
|''[[Sniper Elite: Resistance]]''
| {{yes|PS5}} || {{yes|PS4}} || {{no|}}
| colspan="1" rowspan="4" |September 1, 2026
| colspan="1" rowspan="4" | -
| rowspan="4" |<ref>[https://blog.playstation.com/2026/08/26/ps-plus-september/ PlayStation Plus Monthly Games for September] (September 1, 2026). Retrieved on September 8, 2026.</ref>
|-
| 900
| 29
|''[[Wobbly Life]]''
| {{yes|PS5}} || {{yes|PS4}} || {{no|}}
|}
"""
    rows = parse_yearly_wikitext(fixture)
    assert [r["title"] for r in rows] == ["Sniper Elite: Resistance", "Wobbly Life"]
    sniper, wobbly = rows
    assert sniper["added"] == "2026-09-01"
    assert sniper["removed"] is None  # '-' cell + stripped <ref>, not the blog/retrieved dates
    assert wobbly["added"] == "2026-09-01"  # carried across the rowspan'd month group
    assert wobbly["removed"] is None


def test_merge_new_lineup_rows_idempotent_and_platform_split():
    conn = connect(f"sqlite:///{tempfile.mkdtemp()}/feed.db")
    init_db(conn)
    rows = parse_yearly_wikitext(FIXTURE)

    added = merge_new_lineup_rows(conn, rows)
    # Fake One -> 2 rows (PS5+PS4), Fake Two -> 1 (PS4), Three -> 1 (PS5),
    # Four -> 2 (PS5+PS4) = 6 rows.
    assert added == 6
    n = conn.execute("SELECT COUNT(*) AS n FROM essentials_lineup").fetchone()["n"]
    assert n == 6

    # The multi-platform games produced one row per offered platform.
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM essentials_lineup WHERE normalized_title = 'fake game one'"
    ).fetchone()["n"] == 2

    # Re-merge is a no-op.
    assert merge_new_lineup_rows(conn, rows) == 0
    conn.close()
