"""Post-batch verification. Pure logic, no browser.

A click is never counted as a success. After a reload of the Likes page we
look again at the first `coverage_needed` positions of the list (newest first).
Any item that was still liked would reappear there, so:

- item present again                          -> failed (still liked)
- item absent AND enough of the list loaded   -> successful
- item absent BUT not enough of the list seen -> uncertain
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class BatchOutcome:
    attempted: int
    successful: int
    failed: int
    uncertain: int

    @property
    def clean(self) -> bool:
        return self.failed == 0 and self.uncertain == 0 and self.successful == self.attempted


def fp_hash(fingerprint: str) -> str:
    """Short one-way id for checkpoints, so no media URLs are stored on disk."""
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]


def hashes(fingerprints: Iterable[str]) -> list[str]:
    return [fp_hash(f) for f in fingerprints]


def classify_batch(
    batch: Sequence[str],
    after: Sequence[str],
    after_end_reached: bool,
    coverage_needed: int,
) -> BatchOutcome:
    """Compare the attempted batch with the list observed after reload.

    `batch` and `after` must use the same identity (both raw or both hashed).
    """
    after_set = set(after)
    present = [b for b in batch if b in after_set]
    absent = len(batch) - len(present)
    covered = after_end_reached or len(after) >= coverage_needed
    return BatchOutcome(
        attempted=len(batch),
        successful=absent if covered else 0,
        failed=len(present),
        uncertain=0 if covered else absent,
    )
