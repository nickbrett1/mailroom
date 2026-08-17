"""Definitions: add the IGDB enrichment assets (igdb_matches, game_metadata,
catalog_views) alongside the existing chains."""

from __future__ import annotations

import os

from dagster import AssetSelection, Definitions, ScheduleDefinition, define_asset_job

from mailroom.clients import igdb_resource, msgvault_resource, psn_api_resource
from mailroom.verticals.game_catalog.assets import (
    catalog_views,
    classified_game_items,
    game_metadata,
    igdb_matches,
    owned_games,
    parsed_purchases_digital,
    parsed_purchases_physical,
    psn_api_owned,
    raw_psn_receipts,
    raw_retailer_receipts,
)

# PSN full-library sync — every Tuesday 21:00 local (Essentials drop is the
# first Tuesday; weekly catches late claims + mid-month Extra/Premium adds).
# Degrades to credentials.status=needs_refresh on auth failure and catches up
# on the next valid token (memos/game-catalog-pipeline §PSN sync).
psn_sync_job = define_asset_job("psn_sync", selection=AssetSelection.keys("psn_api_owned"))
psn_sync_schedule = ScheduleDefinition(
    job=psn_sync_job,
    cron_schedule="0 21 * * 2",
    execution_timezone="America/New_York",
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
        igdb_matches,
        game_metadata,
        catalog_views,
    ],
    resources={
        "msgvault": msgvault_resource,
        "igdb": igdb_resource,
        "psn_api": psn_api_resource,
        "db_url": os.environ.get("MAILROOM_DB_URL", "sqlite:////data/mailroom.db"),
    },
    schedules=[psn_sync_schedule],
)
