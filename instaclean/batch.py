"""Batch sizing. Pure functions, no browser.

The random size in [BATCH_MIN, BATCH_MAX] only varies how many items go into
one bulk action. It is not a mechanism to evade Instagram's abuse systems.
"""

from __future__ import annotations

import random

from .config import HARD_BATCH_CAP, Config


def next_batch_target(rng: random.Random, cfg: Config, remaining_session: int) -> int:
    """How many items we would like to load/select for the next batch.

    Never above HARD_BATCH_CAP (100), never above what the session still allows.
    Returns 0 when the session budget is exhausted.
    """
    if remaining_session <= 0:
        return 0
    target = rng.randint(cfg.batch_min, cfg.batch_max)
    return min(target, HARD_BATCH_CAP, remaining_session)


def plan_batch_size(target: int, available: int) -> int:
    """Final batch size: the target, reduced to what is actually available.

    If fewer than BATCH_MIN likes remain, the remaining ones are processed.
    """
    if target <= 0 or available <= 0:
        return 0
    return min(target, available, HARD_BATCH_CAP)
