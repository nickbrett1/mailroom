"""mailroom — email → facts pipeline platform.

Vertical #1: game_catalog (definitive PlayStation owned-games store).

Pattern per vertical: source adapter → normalize → classify/merge into entity
store → enrich → serve. Heavy logic lives in plain library functions; Dagster
assets are thin orchestration shells (see memo memos/game-catalog-pipeline).
"""

__version__ = "0.1.0"
