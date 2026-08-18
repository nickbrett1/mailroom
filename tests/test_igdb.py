"""IGDB enrichment asset tests (mocked client): igdb_matches (external_games
for digital, name search for physical), game_metadata (paced, resumable),
catalog_views read model."""

from __future__ import annotations

import json
import tempfile

from dagster import build_op_context

from mailroom.db import connect, init_db, upsert_owned_game
from mailroom.verticals.game_catalog import assets


class _StubIgdb:
    def __init__(self, external: dict | None = None, search: dict | None = None, details: dict | None = None):
        self.external = external or {}
        self.search = search or {}
        self.details = details or {}

    def game_by_external_psn_uid(self, uid: str) -> int | None:
        return self.external.get(uid)

    def search_game(self, name: str) -> list[dict]:
        for key, games in self.search.items():
            if key in name:
                return games
        return []

    def game_details(self, game_id: int) -> dict:
        return self.details.get(game_id, {})


def _seed_game(conn, title, platform="playstation 5", psn_content_id=None, igdb_id=None, source="psn_receipt"):
    upsert_owned_game(
        conn,
        {
            "title": title,
            "normalized_title": assets.normalize_title(title),
            "platform": platform,
            "format": "digital" if psn_content_id else "physical",
            "ownership_class": "purchased",
            "retailer": None,
            "order_number": None,
            "item_id": None,
            "condition": None,
            "psn_content_id": psn_content_id,
            "igdb_id": igdb_id,
            "acquisition_date": None,
            "price": None,
            "source": source,
            "source_ref": f"t:{title}",
            "status": "owned",
            "is_owned": 1,
            "provenance": f"{source}:{title}",
        },
    )


def _ctx(db, stub):
    return build_op_context(resources={"db_url": db, "igdb": stub})


def test_igdb_search_term_strips_noise():
    assert assets.igdb_search_term("Cyberpunk 2077 - PlayStation 4") == "cyberpunk 2077"
    assert assets.igdb_search_term("SEALED Wildermyth for Sony PlayStation 5 (PS5) w/ Monster") == "wildermyth"
    assert assets.igdb_search_term("Persona 5 Royal - PlayStation 5") == "persona 5 royal"
    assert assets.igdb_search_term("ABZÛ - PlayStation 4") == "abzu"  # accents kept


def test_igdb_search_terms_fallbacks():
    terms = assets.igdb_search_terms("DIRT5")
    assert "dirt 5" in terms  # digit-split recovers the space
    terms = assets.igdb_search_terms("God of War III Remastered Standard Edition - PlayStation 4")
    assert terms[0] == "god war iii standard"  # stripped
    assert "god of war iii remastered standard edition - playstation 4" in terms  # raw fallback
    terms = assets.igdb_search_terms("CoffeeTalk")
    assert terms[0] == "coffeetalk"


def test_igdb_matches_falls_back_when_stripped_term_misses():
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "DIRT5", platform="playstation 4")
    conn.close()

    # stripped term "dirt5" matches nothing; digit-split "dirt 5" does
    stub = _StubIgdb(search={"dirt 5": [{"id": 18623, "name": "DIRT 5"}]})
    assets.igdb_matches(_ctx(f"sqlite:///{db}", stub))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT * FROM owned_games").fetchone()
    assert row["igdb_id"] == 18623
    conn.close()


def test_igdb_matches_digital_via_external_and_physical_via_search():
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "God of War", psn_content_id="UP9000-CUSA07408_00-00000000GODOFWAR")
    _seed_game(conn, "Cyberpunk 2077 - PlayStation 4", platform="playstation 4")
    _seed_game(conn, "Some Obscure Disc Game - PlayStation 5")
    conn.close()

    stub = _StubIgdb(
        external={"UP9000-CUSA07408_00-00000000GODOFWAR": 1942},
        search={"cyberpunk 2077": [{"id": 41494, "name": "Cyberpunk 2077"}]},
    )
    assets.igdb_matches(_ctx(f"sqlite:///{db}", stub))

    conn = connect(f"sqlite:///{db}")
    rows = {r["title"]: r for r in conn.execute("SELECT * FROM owned_games").fetchall()}
    assert rows["God of War"]["igdb_id"] == 1942
    assert rows["Cyberpunk 2077 - PlayStation 4"]["igdb_id"] == 41494
    assert rows["Some Obscure Disc Game - PlayStation 5"]["igdb_id"] is None
    m = conn.execute("SELECT confidence, matched_title FROM igdb_matches WHERE owned_game_id = ?", (rows["God of War"]["id"],)).fetchone()
    assert m["confidence"] == "high"
    conn.close()


def test_game_metadata_paced_resumable_and_catalog_view():
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    _seed_game(conn, "God of War", igdb_id=1942)
    _seed_game(conn, "Cyberpunk 2077", igdb_id=41494)
    conn.close()

    stub = _StubIgdb(details={1942: {"id": 1942, "name": "God of War", "total_rating": 90.0}})
    assets.game_metadata(_ctx(f"sqlite:///{db}", stub))
    assets.catalog_views(_ctx(f"sqlite:///{db}", stub))

    conn = connect(f"sqlite:///{db}")
    payload = conn.execute("SELECT payload FROM game_metadata WHERE igdb_id = 1942").fetchone()["payload"]
    assert json.loads(payload)["total_rating"] == 90.0
    # resumable: 41494 has no details -> not stored; re-run doesn't refetch 1942
    assets.game_metadata(_ctx(f"sqlite:///{db}", stub))
    assert conn.execute("SELECT COUNT(*) n FROM game_metadata").fetchone()["n"] == 1
    # catalog view joins metadata
    row = conn.execute("SELECT game_id, title, igdb_id, igdb_payload FROM catalog_views WHERE title = 'God of War'").fetchone()
    assert row is not None
    assert row["igdb_payload"] is not None
    conn.close()


def test_owned_games_receipts_path_supplies_ownership_class():
    """Regression: the receipts path of owned_games must supply ownership_class
    (latent bug from the schema change — ProgrammingError otherwise)."""
    db = tempfile.mktemp(suffix=".db")
    conn = connect(f"sqlite:///{db}")
    init_db(conn)
    conn.execute(
        """INSERT INTO classified_game_items (source, order_number, item_key, title, platform, classification, reason)
           VALUES ('gamestop', 'o1', 'o1:game', 'Cyberpunk 2077 - PlayStation 4', 'playstation 4', 'playstation_game', 'platform match')"""
    )
    conn.commit()
    conn.close()

    assets.owned_games(_ctx(f"sqlite:///{db}", _StubIgdb()))
    conn = connect(f"sqlite:///{db}")
    row = conn.execute("SELECT format, ownership_class FROM owned_games").fetchone()
    assert row["format"] == "physical"
    assert row["ownership_class"] == "purchased"
    conn.close()
