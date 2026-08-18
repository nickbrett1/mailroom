from mailroom.definitions import definitions


def test_db_url_is_a_resource_not_a_plain_string(monkeypatch):
    """BUG-1 regression: Dagster only injects @resource entries — a plain-value
    resources={...} entry silently fails every materialization."""
    import tempfile

    from dagster import build_op_context

    monkeypatch.setenv("MAILROOM_DB_URL", f"sqlite:///{tempfile.mktemp(suffix='.db')}")
    res = definitions.get_repository_def().get_top_level_resources()
    assert "db_url" in res, "db_url must be a registered resource"
    assert "psn_api" in res and "msgvault" in res and "igdb" in res
    ctx = build_op_context(resources=res, partition_key="2026-08-18")
    from mailroom.verticals.game_catalog import assets

    assets.catalog_views(ctx)  # runs only if db_url is injected


def test_definitions_load():
    graph = definitions.resolve_asset_graph()
    keys = {k.to_user_string() for k in graph.get_all_asset_keys()}
    assert {
        "raw_psn_receipts",
        "parsed_purchases_digital",
        "raw_retailer_receipts",
        "parsed_purchases_physical",
        "classified_game_items",
        "owned_games",
        "catalog_views",
    } <= keys
    resources = definitions.get_repository_def().get_top_level_resources()
    assert "msgvault" in resources
    assert "igdb" in resources
