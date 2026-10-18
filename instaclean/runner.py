"""Session loop. Talks to the page only through the LikesUI protocol, so the
whole flow (batches, limits, verification, checkpoints, stops, recovery) is
tested without Instagram.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol, Sequence

from .batch import next_batch_target, plan_batch_size
from .checkpoint import CheckpointStore, SessionState, utcnow
from .config import HARD_BATCH_CAP, Config
from .errors import (
    ActionNotSubmitted,
    RetryableError,
    StopCondition,
    StopReason,
    UncertainOutcome,
)
from .verifier import classify_batch, hashes

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Discovery:
    items: list[str]  # fingerprints of selectable liked items, page order
    end_reached: bool  # True when the list stopped growing
    unconfirmed_empty: bool = False  # no "Select" control and no thumbnails


class LikesUI(Protocol):
    def ensure_ready(self) -> None: ...
    def check_safety(self) -> None: ...
    def discover(self, target: int) -> Discovery: ...
    def select(self, fingerprints: Sequence[str]) -> list[str]: ...
    def cancel_selection(self) -> None: ...
    def unlike_selected(self) -> None: ...
    def wait_for_removal(self, fingerprints: Sequence[str]) -> set[str]: ...
    def reload(self) -> None: ...


# ---- stage gate ---------------------------------------------------------------
STAGE_PREREQ = {
    "dry_run": None,
    "real_test": "dry_run",
    "first_batch": "manual_verified",
    "normal": "first_batch",
}
STAGE_HELP = {
    "dry_run": "run a dry run first:  python -m instaclean",
    "manual_verified": "check the 3 posts by hand, then:  python -m instaclean --mark-verified",
    "first_batch": "run a small first batch:  python -m instaclean --real --limit 20",
}


def stage_for(cfg: Config, limit: int | None) -> str:
    if cfg.dry_run:
        return "dry_run"
    if limit is not None and limit <= cfg.initial_test_size:
        return "real_test"
    if limit is not None and limit <= HARD_BATCH_CAP:
        return "first_batch"
    return "normal"


def check_gate(store: CheckpointStore, cfg: Config, limit: int | None) -> str:
    """Raise STAGE_GATE unless the previous manual stage is recorded."""
    stage = stage_for(cfg, limit)
    needed = STAGE_PREREQ[stage]
    if needed and needed not in store.stages():
        raise StopCondition(
            StopReason.STAGE_GATE, f"'{stage}' requires '{needed}' first: {STAGE_HELP[needed]}"
        )
    return stage


# Stops that mean "Instagram is pushing back": real mode then waits longer.
BLOCK_REASONS = {
    StopReason.ACTION_BLOCKED.value,
    StopReason.RATE_LIMIT.value,
    StopReason.CAPTCHA.value,
    StopReason.IDENTITY_VERIFICATION.value,
    StopReason.SECURITY_WARNING.value,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def check_cooldown(store: CheckpointStore, cfg: Config, now: datetime | None = None) -> None:
    """Refuse REAL mode during the self-imposed pauses. Dry runs are never paused.

    - after a block-type stop: block_cooldown_hours (default 72h)
    - after a real session of normal size (>= BATCH_MIN actions):
      min_hours_between_sessions (default 12h)
    """
    if cfg.dry_run:
        return
    now = now or _now()
    state = store.load_state()
    block = state.get("last_block")
    if block:
        until = datetime.fromisoformat(block["at"]) + timedelta(hours=cfg.block_cooldown_hours)
        if now < until:
            raise StopCondition(
                StopReason.COOLDOWN,
                f"last stop was '{block['reason']}': real mode allowed again after "
                f"{until.isoformat(timespec='minutes')} (UTC)",
            )
    last = state.get("last_real_session")
    if last and int(last.get("attempted", 0)) >= cfg.batch_min:
        until = datetime.fromisoformat(last["ended_at"]) + timedelta(
            hours=cfg.min_hours_between_sessions)
        if now < until:
            raise StopCondition(
                StopReason.COOLDOWN,
                f"pause between sessions: real mode allowed again after "
                f"{until.isoformat(timespec='minutes')} (UTC)",
            )


def mark_manual_verified(store: CheckpointStore) -> None:
    if "real_test" not in store.stages():
        raise StopCondition(StopReason.STAGE_GATE, "run the 3-like real test before --mark-verified")
    store.mark_stage("manual_verified")


# ---- runner -----------------------------------------------------------------------
class SessionRunner:
    def __init__(
        self,
        ui: LikesUI,
        cfg: Config,
        store: CheckpointStore,
        rng: random.Random,
    ) -> None:
        self.ui = ui
        self.cfg = cfg
        self.store = store
        self.rng = rng

    # Public entry point -------------------------------------------------------
    def run(
        self, *, limit: int | None = None, confirm: Callable[[str], bool] = lambda _s: False
    ) -> SessionState:
        last = self.store.last_session()
        if last is not None and last.pending_batch:
            return self.recover(last)
        if last is not None and last.status == "running":
            last.status = "interrupted"
            self.store.save_session(last)

        stage = check_gate(self.store, self.cfg, limit)
        check_cooldown(self.store, self.cfg)
        max_actions = self.cfg.max_actions_per_session
        if limit is not None:
            max_actions = min(max_actions, limit)

        if not self.cfg.dry_run:
            if max_actions <= self.cfg.batch_min:
                batches = f"one batch of {max_actions}"
            else:
                batches = f"batches of {self.cfg.batch_min}-{self.cfg.batch_max}"
            summary = f"REAL MODE: up to {max_actions} unlikes, {batches} (stage: {stage})"
            if not confirm(summary):
                raise StopCondition(StopReason.USER_ABORT, "real mode not confirmed")

        session = self.store.new_session(
            mode="dry_run" if self.cfg.dry_run else "real", stage=stage, action_limit=max_actions
        )
        log.info("Session %s started (%s, stage %s)", session.session_id, session.mode, stage)
        try:
            self.ui.ensure_ready()
            self._loop(session, max_actions)
        except StopCondition as exc:
            session.status = "uncertain" if session.uncertain else "stopped"
            session.stop_reason = exc.reason.value
            session.stop_detail = exc.detail
            log.error("STOP: %s", exc)
        except RetryableError as exc:  # escaped its retry loop: classified stop
            session.status = "uncertain" if session.uncertain else "stopped"
            session.stop_reason = StopReason.UNEXPECTED_UI.value
            session.stop_detail = str(exc)
            log.error("STOP: unexpected UI state: %s", exc)
        except Exception as exc:  # unexpected: record and stop, never continue
            session.status = "error"
            session.stop_reason = StopReason.INTERNAL_ERROR.value
            session.stop_detail = f"{type(exc).__name__}: {exc}"
            log.exception("Unexpected error, session stopped")
        finally:
            self.store.save_session(session)

        self._record_stage(session)
        self._record_pacing(session)
        return session

    def _record_pacing(self, s: SessionState) -> None:
        if s.mode == "real" and s.attempted > 0:
            self.store.record_real_session(utcnow(), s.attempted)
        if s.stop_reason in BLOCK_REASONS:
            self.store.record_block(utcnow(), s.stop_reason)

    # Batch loop -----------------------------------------------------------
    def _loop(self, s: SessionState, max_actions: int) -> None:
        while True:
            remaining = max_actions - s.attempted
            if remaining <= 0:
                log.warning("SESSION LIMIT REACHED (%d actions)", s.attempted)
                s.status = "session_limit_reached"
                return

            target = next_batch_target(self.rng, self.cfg, remaining)
            self.ui.check_safety()
            disc = self.ui.discover(target)

            if not disc.items:
                if disc.unconfirmed_empty:
                    log.warning("No likes found and no 'Select' control: check the page manually")
                    s.status = "empty_unconfirmed"
                    return
                if disc.end_reached:
                    log.info("No likes left on the page")
                    s.status = "completed"
                    return
                raise StopCondition(StopReason.UNEXPECTED_UI, "no items and list not finished")

            if len(disc.items) < target and not disc.end_reached:
                # Instagram did not load more in time. Work on what is loaded:
                # verification then checks exactly that part of the list.
                log.warning("Only %d of %d items loaded; smaller batch", len(disc.items), target)
            if len(set(disc.items)) != len(disc.items):
                raise StopCondition(StopReason.UNEXPECTED_UI, "duplicate item identities on page")

            size = plan_batch_size(target, len(disc.items))
            batch = disc.items[:size]
            log.info("Batch %d: target %d, available %d, size %d",
                     s.batches_completed + 1, target, len(disc.items), size)

            selected = self.ui.select(batch)
            if set(selected) != set(batch) or len(selected) != len(batch):
                self.ui.cancel_selection()
                raise StopCondition(
                    StopReason.SELECTION_MISMATCH,
                    f"expected {len(batch)} selected, page shows {len(selected)}",
                )

            if self.cfg.dry_run:
                self.ui.cancel_selection()
                s.dry_run_selected = size
                s.last_batch_size = size
                s.status = "dry_run_ok"
                log.info("DRY RUN: %d items selected and verified, Unlike NOT pressed", size)
                return

            self._unlike_and_verify(s, batch, coverage=len(disc.items))

    def _unlike_and_verify(self, s: SessionState, batch: list[str], coverage: int) -> None:
        size = len(batch)
        # Persist intent BEFORE the destructive click (crash-safe).
        s.pending_batch = {"size": size, "fp_hashes": hashes(batch),
                           "coverage": coverage, "started_at": utcnow()}
        s.attempted += size
        self.store.save_session(s)

        try:
            self.ui.unlike_selected()
        except ActionNotSubmitted:
            s.attempted -= size
            s.pending_batch = None
            self.store.save_session(s)
            raise
        except StopCondition:
            s.uncertain += size
            s.pending_batch["counted_uncertain"] = True
            self.store.save_session(s)
            raise

        try:
            still = self.ui.wait_for_removal(batch)
            if still:
                log.info("%d items still visible right after Unlike; verifying after reload",
                         len(still))
            self.ui.reload()
            self.ui.check_safety()
            after = self.ui.discover(coverage)
        except StopCondition:
            # Verification could not finish: the batch stays pending for recovery.
            s.uncertain += size
            s.pending_batch["counted_uncertain"] = True
            self.store.save_session(s)
            raise
        out = classify_batch(hashes(batch), hashes(after.items), after.end_reached, coverage)

        s.successful += out.successful
        s.failed += out.failed
        s.uncertain += out.uncertain
        s.last_batch_size = size
        s.pending_batch = None
        s.batches.append({"size": size, "successful": out.successful, "failed": out.failed,
                          "uncertain": out.uncertain, "at": utcnow()})
        if out.clean:
            s.batches_completed += 1
        self.store.save_session(s)
        log.info("Batch verified: attempted %d, successful %d, failed %d, uncertain %d",
                 out.attempted, out.successful, out.failed, out.uncertain)

        if out.failed:
            raise StopCondition(StopReason.VERIFICATION_FAILED, f"{out.failed} items still liked")
        if out.uncertain:
            raise UncertainOutcome(f"{out.uncertain} items could not be verified")

    # Stages -----------------------------------------------------------------
    def _record_stage(self, s: SessionState) -> None:
        if s.mode == "dry_run":
            if s.status == "dry_run_ok":
                self.store.mark_stage("dry_run")
            return
        clean = s.attempted > 0 and s.successful == s.attempted and not s.failed and not s.uncertain
        ok_end = s.status in ("session_limit_reached", "completed")
        if clean and ok_end and s.stage in ("real_test", "first_batch"):
            self.store.mark_stage(s.stage)

    # Recovery ---------------------------------------------------------------
    def recover(self, last: SessionState) -> SessionState:
        """Non-destructive check of a batch left unverified by a crash/stop.

        Never repeats the Unlike. Updates the old session and stops.
        """
        p = last.pending_batch or {}
        log.warning("Session %s has an unverified batch of %s items: verifying, no actions",
                    last.session_id, p.get("size"))
        if p.get("counted_uncertain"):
            last.uncertain -= int(p["size"])
        try:
            self.ui.ensure_ready()
            after = self.ui.discover(int(p["coverage"]))
            out = classify_batch(p["fp_hashes"], hashes(after.items), after.end_reached,
                                 int(p["coverage"]))
        except StopCondition as exc:
            if p.get("counted_uncertain"):
                last.uncertain += int(p["size"])
            last.stop_reason = exc.reason.value
            last.stop_detail = f"recovery stopped: {exc.detail}"
            last.status = "recovery_stopped"  # pending_batch kept: next run retries the CHECK
            self.store.save_session(last)
            if exc.reason.value in BLOCK_REASONS:
                self.store.record_block(utcnow(), exc.reason.value)
            log.error("Recovery STOP: %s", exc)
            return last
        last.successful += out.successful
        last.failed += out.failed
        last.uncertain += out.uncertain
        last.recovery = {"at": utcnow(), "successful": out.successful,
                         "failed": out.failed, "uncertain": out.uncertain}
        last.pending_batch = None
        last.status = "recovered"
        self.store.save_session(last)
        log.warning("Recovery: successful %d, still liked %d, uncertain %d. Nothing was retried. "
                    "Run again to continue.", out.successful, out.failed, out.uncertain)
        return last
