"""Definitions: add the IGDB enrichment assets (igdb_matches, game_metadata,
catalog_views) alongside the existing chains."""

from __future__ import annotations

import os

from dagster import (
    AssetSelection,
    Definitions,
    ScheduleDefinition,
    define_asset_job,
    resource,
)

from mailroom.clients import igdb_resource, msgvault_resource, psn_api_resource


@resource
def db_url_resource(context) -> str:  # type: ignore[no-untyped-def]
    """The store location as a Dagster RESOURCE (not a plain string).

    Dagster only injects @resource-wrapped entries into asset contexts; a
    plain-value `Definitions(resources={"db_url": "sqlite:///..."})` entry is
    not attached, which made every materialization fail with
    `Unknown resource db_url` (memos/mailroom-deploy-bugs BUG-1).
    """
    return os.environ.get("MAILROOM_DB_URL", "sqlite:////data/mailroom.db")
from mailroom.verticals.game_catalog.assets import (
    catalog_quality_repairs,
    catalog_views,
    classified_game_items,
    dedupe_owned_games,
    game_covers,
    game_metadata,
    igdb_matches,
    owned_games,
    parsed_purchases_digital,
    parsed_purchases_physical,
    psn_api_owned,
    psn_playtime,
    raw_psn_receipts,
    raw_retailer_receipts,
)

# Catalog daily — email receipts -> owned_games -> incremental IGDB enrichment.
# New purchases are matched, deduped, enriched and in the views within 24h.
# igdb_matches runs WITHOUT recheck here (only rows with igdb_id IS NULL hit the
# IGDB API), so a daily tick is cheap; game_metadata is paced + resumable.
catalog_daily_job = define_asset_job(
    "catalog_daily",
    selection=AssetSelection.keys(
        "raw_psn_receipts",
        "raw_retailer_receipts",
        "parsed_purchases_digital",
        "parsed_purchases_physical",
        "classified_game_items",
        "owned_games",
        "igdb_matches",
        "dedupe_owned_games",
        "game_metadata",
        "game_covers",
        "catalog_views",
    ),
)
catalog_daily_schedule = ScheduleDefinition(
    job=catalog_daily_job,
    cron_schedule="0 6 * * *",
    execution_timezone="America/New_York",
)

# PSN full-library sync — every Tuesday 21:00 local (Essentials drop is the
# first Tuesday; weekly catches late claims + mid-month Extra/Premium adds).
# Runs the incremental enrichment tail after the sync so Tuesday's claims are
# matched + enriched the same night. Degrades to credentials.status=
# needs_refresh on auth failure and catches up on the next valid token
# (memos/game-catalog-pipeline §PSN sync).
psn_sync_job = define_asset_job(
    "psn_sync",
    selection=AssetSelection.keys(
        "psn_api_owned",
        "psn_playtime",
        "igdb_matches",
        "dedupe_owned_games",
        "game_metadata",
        "game_covers",
        "catalog_views",
    ),
)
psn_sync_schedule = ScheduleDefinition(
    job=psn_sync_job,
    cron_schedule="0 21 * * 2",
    execution_timezone="America/New_York",
)

# Catalog recheck — re-match EVERY owned row with the exact-name/platform
# matcher + refresh metadata + views (heals wrong IGDB picks like Elden Ring
# -> Nightreign). catalog_quality_repairs runs after the re-match to split
# jammed rows / retire junk that the matcher can't undo. Trigger from the UI
# (Jobs tab -> Launch) or via the igdb_matches asset's config (Materialize
# with config {"recheck": true}).
catalog_recheck_job = define_asset_job(
    "catalog_recheck",
    selection=AssetSelection.keys(
        "igdb_matches",
        "dedupe_owned_games",
        "catalog_quality_repairs",
        "game_metadata",
        "game_covers",
        "catalog_views",
    ),
    config={"ops": {"igdb_matches": {"config": {"recheck": True}}}},
)

definitions = Definitions(
    assets=[
        raw_psn_receipts,
        parsed_purchases_digital,
        raw_retailer_receipts,
        parsed_purchases_physical,
        classified_game_items,
        owned_games,
        psn_api_owned,
        psn_playtime,
        igdb_matches,
        dedupe_owned_games,
        catalog_quality_repairs,
        game_metadata,
        game_covers,
        catalog_views,
    ],
    resources={
        "msgvault": msgvault_resource,
        "igdb": igdb_resource,
        "psn_api": psn_api_resource,
        "db_url": db_url_resource,
    },
    jobs=[catalog_recheck_job],
    schedules=[catalog_daily_schedule, psn_sync_schedule],
)
