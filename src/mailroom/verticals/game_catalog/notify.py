"""ntfy notifications for the game catalog.

Sends a push to the user's phone/desktop when NEW games are added to the
collection (memos/game-catalog-notify). Fired from the catalog_games asset:
after the canonical `games` table is rebuilt, any title that wasn't there
before this run is a genuinely new game (new receipt parsed, a PSN/PS+ sync
added a claim), and gets a single grouped ntfy message.

Config (env):
  - NTFY_URL          ntfy server root, default https://ntfy.sh (self-hosted
                      NAS instance by setting, e.g. http://ntfy:80).
  - NTFY_NEW_GAME_TOPIC  topic to publish to, default
                      new-game-nickbrett-9831.
  - NTFY_NEW_GAME_ENABLED  set to a non-empty false value ('0', 'false',
                      'off') to disable publishing entirely.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

_DEFAULT_TOPIC = "new-game-nickbrett-9831"
# Empty when the feature is disabled (sentinel so notify_new_games short-circuits).
_DISABLED = {"0", "false", "off", "no", ""}


def ntfy_endpoint() -> tuple[str, str] | None:
    """(base_url, topic) for the new-game notification, or None if disabled."""
    if os.environ.get("NTFY_NEW_GAME_ENABLED", "1").strip().lower() in _DISABLED:
        return None
    base = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
    topic = os.environ.get("NTFY_NEW_GAME_TOPIC", _DEFAULT_TOPIC).strip().lstrip("/")
    if not topic:
        return None
    return base, topic


def _new_game_key(game: dict[str, Any]) -> str:
    """A stable identity for diffing new vs existing games.

    Prefer the IGDB id (the catalog's canonical key); fall back to the
    normalized title for unmatched games.
    """
    return str(game.get("igdb_id") or game.get("normalized_title") or game.get("title") or "")


def notify_new_games(
    new_games: list[dict[str, Any]],
    *,
    had_prior_games: bool,
    client: httpx.Client | None = None,
) -> bool:
    """Publish a single ntfy message listing `new_games`.

    Skips (returns False) when there's nothing to report, when the store was
    empty before this run (the initial backfill — not "new"), or when ntfy is
    disabled/unreachable. Best-effort: a failed publish never raises.
    """
    if not new_games or not had_prior_games:
        return False
    endpoint = ntfy_endpoint()
    if endpoint is None:
        return False
    base, topic = endpoint
    plural = "" if len(new_games) == 1 else "s"
    body = "\n".join(f"• {g.get('title')}" for g in new_games)
    try:
        client = client or httpx.Client(timeout=10.0)
        resp = client.post(
            f"{base}/{topic}",
            content=body,
            headers={
                "Title": f"{len(new_games)} new game{plural} in the collection",
                "Tags": "video_game",
            },
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError:
        return False
