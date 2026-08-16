"""Platform-gate classifier for line items.

Hard rule (mailroom memo): PlayStation only. Non-PlayStation items are
retained raw in order_items but never enter owned_games. Ambiguous → review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PLAYSTATION_PLATFORMS = {"ps4", "ps5", "psvita", "vita", "ps3", "playstation"}
NON_PLAYSTATION = {"switch", "xbox", "pc", "steam", "epic", "nintendo", "xbox series", "series x", "series s"}
ACCESSORY_HINTS = (
    "cover plate",
    "controller",
    "dual sense",
    "dualsense",
    "headset",
    "charging",
    "stand",
    "console",  # console bundles ("PlayStation 5 Spider-Man Ultimate Edition Bundle")
    "bundle",
    "gift card",
)
# Non-game merch (LRG books/magazines/trading cards/soundtracks) — retained raw,
# never catalogued, sent to review rather than classified as hardware.
NON_GAME_HINTS = ("trading card", "soundtrack", "vinyl", "art of", "art book", "strategy guide", "comic", "poster")


@dataclass
class Classification:
    classification: str  # playstation_game | non_playstation | accessory_hardware | needs_review
    platform: str | None = None
    reason: str | None = None


def classify_item(title: str, platform_hint: str | None = None, variant: str | None = None) -> Classification:
    """Classify a line item via the platform gate.

    Signals (strongest first): explicit platform_hint/variant (Shopify variant,
    GameFly '(PS5)'), retail ' - PlayStation 5' suffix, marketplace
    'for PlayStation 5', console keywords. Accessories excluded or reviewed.
    """
    text = f"{title} {platform_hint or ''} {variant or ''}".lower()

    # Accessory/hardware first — they often mention PlayStation.
    # Word-boundary matching (optional plural) so 'stand' doesn't hit
    # 'Standard Edition' or 'controller' miss 'controllers'.
    for h in ACCESSORY_HINTS:
        if re.search(rf"\b{re.escape(h)}s?\b", text):
            return Classification("accessory_hardware", reason=f"accessory/hardware hint '{h}'")

    # Non-game merch (books, cards, soundtracks) — never catalogue.
    for h in NON_GAME_HINTS:
        if re.search(rf"\b{re.escape(h)}s?\b", text):
            return Classification("needs_review", reason=f"non-game hint '{h}'")

    # Explicit non-PlayStation platform.
    for p in NON_PLAYSTATION:
        if p in text:
            return Classification("non_playstation", platform=p, reason=f"platform keyword '{p}'")

    # PlayStation detection: suffix ' - playstation 5', '(ps5)', 'for playstation', variant 'PS5'.
    ps_match = re.search(r"(?:-|for)\s*(playstation\s*[45]|ps\s*[45]|psvita|vita|ps3)", text)
    paren_match = re.search(r"\(ps\s*([45])\)", text)
    if variant and variant.strip().upper() in {"PS4", "PS5", "PSVITA", "PS3", "VITA"}:
        return Classification("playstation_game", platform=variant.strip().upper(), reason="Shopify variant")
    if ps_match:
        return Classification("playstation_game", platform=ps_match.group(1), reason="platform match")
    if paren_match:
        return Classification("playstation_game", platform=f"playstation {paren_match.group(1)}", reason="parenthetical platform")
    if "playstation" in text or "ps4" in text or "ps5" in text:
        return Classification("playstation_game", platform="playstation", reason="keyword")

    return Classification("needs_review", reason="platform ambiguous")
