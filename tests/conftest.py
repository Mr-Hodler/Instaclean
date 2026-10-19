"""Shared fakes. No network, no browser.

Tests must never call instaclean.main.main(): it uses the REAL repo folders
(checkpoints/, logs/) and the real profile. Use SessionRunner with the tmp_path
`store`/`cfg` fixtures instead.
"""

from __future__ import annotations

import random
from dataclasses import replace
from pathlib import Path
from typing import Sequence

import pytest

from instaclean.checkpoint import CheckpointStore
from instaclean.config import Config
from instaclean.errors import ActionNotSubmitted, StopCondition, StopReason, UncertainOutcome
from instaclean.runner import Discovery


class FakeLikesUI:
    """In-memory Likes page. Knobs simulate Instagram misbehaving."""

    def __init__(self, n: int, *, load_step: int = 30) -> None:
        self.liked = [f"img_{i:05d}_n.jpg" for i in range(n)]
        self.load_step = load_step
        self.selected: list[str] = []
        self.unlike_calls = 0
        self.calls: list[str] = []
        # failure knobs
        self.select_cap: int | None = None  # page refuses selecting more than this
        self.ignore_unlike = False  # unlike click has no effect (-> failed)
        self.raise_on_unlike: StopCondition | None = None
        self.raise_before_unlike: StopCondition | None = None
        self.safety_after_batches: dict[int, StopReason] = {}
        self.short_coverage = False  # verification sees too little of the list
        self.select_extra = False  # page selects one item too many
        self.ready_error: StopCondition | None = None
        self.delayed: list[str] = []  # unliked server-side but still listed
        self.delay_reloads = 0  # reloads before delayed items disappear
        self.waits: list[float] = []

    def ensure_ready(self) -> None:
        self.calls.append("ensure_ready")
        if self.ready_error:
            raise self.ready_error

    def check_safety(self) -> None:
        reason = self.safety_after_batches.get(self.unlike_calls)
        if reason:
            raise StopCondition(reason, "fake signal")

    def discover(self, target: int) -> Discovery:
        self.calls.append(f"discover:{target}")
        loaded = self.liked[: min(len(self.liked), max(target, self.load_step))]
        end = len(loaded) >= len(self.liked)
        if self.short_coverage:
            loaded, end = loaded[:1], False
        return Discovery(items=list(loaded), end_reached=end)

    def select(self, fingerprints: Sequence[str]) -> list[str]:
        self.calls.append(f"select:{len(fingerprints)}")
        fps = list(fingerprints)
        if self.select_cap is not None:
            fps = fps[: self.select_cap]
        if self.select_extra:
            extra = [f for f in self.liked if f not in fps][:1]
            fps += extra
        self.selected = fps
        return list(fps)

    def cancel_selection(self) -> None:
        self.calls.append("cancel")
        self.selected = []

    def unlike_selected(self) -> None:
        if self.raise_before_unlike:
            raise self.raise_before_unlike
        self.unlike_calls += 1
        self.calls.append(f"unlike:{len(self.selected)}")
        if self.raise_on_unlike:
            raise self.raise_on_unlike
        if not self.ignore_unlike:
            gone = set(self.selected)
            if self.delay_reloads:
                self.delayed = [f for f in self.liked if f in gone][: len(gone) // 5 or 1]
                gone -= set(self.delayed)
            self.liked = [f for f in self.liked if f not in gone]
        self.selected = []

    def wait_for_removal(self, fingerprints: Sequence[str]) -> set[str]:
        return set(fingerprints) & set(self.liked)

    def reload(self) -> None:
        self.calls.append("reload")
        if self.delayed:
            self.delay_reloads -= 1
            if self.delay_reloads <= 0:
                gone = set(self.delayed)
                self.liked = [f for f in self.liked if f not in gone]
                self.delayed = []

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)


@pytest.fixture
def store(tmp_path: Path) -> CheckpointStore:
    return CheckpointStore(tmp_path / "checkpoints")


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(profile_dir=tmp_path / "profile", data_dir=tmp_path)


def real(cfg: Config, **kw: object) -> Config:
    return replace(cfg, dry_run=False, **kw)


def unlock(store: CheckpointStore, *stages: str) -> None:
    for s in stages:
        store.mark_stage(s)


@pytest.fixture
def rng() -> random.Random:
    return random.Random(1234)


__all__ = ["FakeLikesUI", "real", "unlock", "ActionNotSubmitted", "UncertainOutcome"]
