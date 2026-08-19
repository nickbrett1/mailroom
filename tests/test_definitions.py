from mailroom.definitions import definitions


def test_db_url_is_a_resource_not_a_plain_string(monkeypatch):
    """BUG-1 regression (the REAL one): assets must declare required_resource_keys.

    build_op_context(resources=...) masks the failure — it injects resources
    explicitly, unlike a real run. Only executing a job through Definitions
    (resource resolution like the webserver) proves the fix
    (memos/mailroom-deploy-bugs BUG-1 — first fix shipped incomplete: the
    resources were registered but assets didn't declare them)."""
    import tempfile

    from dagster import AssetSelection, Definitions, define_asset_job

    from mailroom.verticals.game_catalog import assets

    monkeypatch.setenv("MAILROOM_DB_URL", f"sqlite:///{tempfile.mktemp(suffix='.db')}")
    res = definitions.get_repository_def().get_top_level_resources()
    assert "db_url" in res, "db_url must be a registered resource"
    assert "psn_api" in res and "msgvault" in res and "igdb" in res

    job = define_asset_job("t", selection=AssetSelection.keys("catalog_views"))
    d = Definitions(assets=[assets.catalog_views], resources=res, jobs=[job])
    assert d.get_job_def("t").execute_in_process().success

    # the PSN sync path exercises psn_api_resource init (cross-resource db_url)
    job2 = define_asset_job("t2", selection=AssetSelection.keys("psn_api_owned"))
    d2 = Definitions(assets=[assets.psn_api_owned], resources=res, jobs=[job2])
    assert d2.get_job_def("t2").execute_in_process().success


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


def test_schedules_and_jobs_registered():
    """The catalog must self-update: daily email->enrichment chain, weekly PSN
    sync with the enrichment tail, and a manual full-recheck job."""
    repo = definitions.get_repository_def()
    job_names = {j.name for j in repo.get_all_jobs()}
    assert "catalog_daily" in job_names
    assert "psn_sync" in job_names
    assert "catalog_recheck" in job_names

    schedules = {s.name: s for s in repo.schedule_defs}
    assert schedules["catalog_daily_schedule"].cron_schedule == "0 6 * * *"
    assert schedules["psn_sync_schedule"].cron_schedule == "0 21 * * 2"

    # daily: the full email chain + incremental enrichment (no psn_api_owned)
    daily = definitions.get_job_def("catalog_daily")
    daily_keys = {k.to_user_string() for k in daily.asset_layer.selected_asset_keys}
    assert {"raw_psn_receipts", "owned_games", "igdb_matches", "dedupe_owned_games",
            "game_metadata", "catalog_views"} <= daily_keys
    assert "psn_api_owned" not in daily_keys

    # weekly: PSN sync + enrichment tail (no email receipts)
    sync = definitions.get_job_def("psn_sync")
    sync_keys = {k.to_user_string() for k in sync.asset_layer.selected_asset_keys}
    assert {"psn_api_owned", "igdb_matches", "dedupe_owned_games", "game_metadata",
            "catalog_views"} <= sync_keys
    assert "raw_psn_receipts" not in sync_keys

    # full recheck stays manual + recheck-only
    recheck = definitions.get_job_def("catalog_recheck")
    recheck_keys = {k.to_user_string() for k in recheck.asset_layer.selected_asset_keys}
    assert {"igdb_matches", "game_metadata", "catalog_views"} <= recheck_keys
