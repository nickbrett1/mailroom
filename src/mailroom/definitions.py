"""Dagster definitions for mailroom (single code location, all verticals)."""

from __future__ import annotations

import os

from dagster import Definitions

from mailroom.clients import igdb_resource, msgvault_resource
from mailroom.verticals.game_catalog.assets import (
    catalog_views,
    classified_game_items,
    owned_games,
    parsed_purchases_digital,
    parsed_purchases_physical,
    raw_psn_receipts,
    raw_retailer_receipts,
)

definitions = Definitions(
    assets=[
        raw_psn_receipts,
        parsed_purchases_digital,
        raw_retailer_receipts,
        parsed_purchases_physical,
        classified_game_items,
        owned_games,
        catalog_views,
    ],
    resources={
        "msgvault": msgvault_resource,
        "igdb": igdb_resource,
        "db_url": os.environ.get("MAILROOM_DB_URL", "sqlite:////data/mailroom.db"),
    },
)
