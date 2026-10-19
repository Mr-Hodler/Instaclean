"""AUTOMATED TEST: session flow against an in-memory fake page.

Covers dry-run, batches, session limit, < 80 left, empty list, selection
mismatch, verification failure, uncertain outcome, stop conditions, stage gate.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import FakeLikesUI, real, unlock

from instaclean.errors import ActionNotSubmitted, StopCondition, StopReason, UncertainOutcome
from instaclean.runner import SessionRunner, check_gate, mark_manual_verified, stage_for

YES = lambda _s: True  # noqa: E731


def run(ui, cfg, store, rng, limit=None, confirm=YES):
    return SessionRunner(ui, cfg, store, rng).run(limit=limit, confirm=confirm)


# ---- dry run ------------------------------------------------------------------
def test_dry_run_selects_but_never_unlikes(cfg, store, rng):
    ui = FakeLikesUI(250)
    s = run(ui, cfg, store, rng)
    assert s.status == "dry_run_ok" and s.mode == "dry_run"
    assert ui.unlike_calls == 0 and len(ui.liked) == 250
    assert 80 <= s.dry_run_selected <= 100
    assert s.attempted == 0 and s.successful == 0
    assert "cancel" in ui.calls
    assert "dry_run" in store.stages()


def test_dry_run_does_not_ask_confirmation(cfg, store, rng):
    asked = []
    run(FakeLikesUI(10), cfg, store, rng, confirm=lambda s: asked.append(s) or False)
    assert asked == []


# ---- stage gate ----------------------------------------------------------------
def test_stage_mapping(cfg):
    assert stage_for(cfg, None) == "dry_run"
    rc = real(cfg)
    assert stage_for(rc, 3) == "real_test"
    assert stage_for(rc, 20) == "first_batch"
    assert stage_for(rc, None) == "normal"
    assert stage_for(rc, 1000) == "normal"


def test_real_mode_blocked_without_previous_stages(cfg, store, rng):
    rc = real(cfg)
    with pytest.raises(StopCondition) as e:
        check_gate(store, rc, 3)
    assert e.value.reason is StopReason.STAGE_GATE
    with pytest.raises(StopCondition):
        run(FakeLikesUI(10), rc, store, rng, limit=3)
    unlock(store, "dry_run")
    check_gate(store, rc, 3)
    with pytest.raises(StopCondition):
        check_gate(store, rc, 20)  # needs manual verification
    with pytest.raises(StopCondition):
        mark_manual_verified(store)  # needs real_test
    unlock(store, "real_test")
    mark_manual_verified(store)
    check_gate(store, rc, 20)
    with pytest.raises(StopCondition):
        check_gate(store, rc, None)  # needs first_batch
    unlock(store, "first_batch")
    check_gate(store, rc, None)


def test_full_progression_never_auto_advances(cfg, store, rng):
    ui = FakeLikesUI(400)
    run(ui, cfg, store, rng)  # dry run
    s = run(ui, real(cfg), store, rng, limit=3)
    assert s.successful == 3 and s.status == "session_limit_reached"
    assert "real_test" in store.stages() and "manual_verified" not in store.stages()
    with pytest.raises(StopCondition):
        run(ui, real(cfg), store, rng, limit=20)
    mark_manual_verified(store)
    s = run(ui, real(cfg), store, rng, limit=20)
    assert s.successful == 20 and "first_batch" in store.stages()


def test_real_mode_requires_confirmation(cfg, store, rng):
    unlock(store, "dry_run")
    ui = FakeLikesUI(10)
    with pytest.raises(StopCondition) as e:
        run(ui, real(cfg), store, rng, limit=3, confirm=lambda _s: False)
    assert e.value.reason is StopReason.USER_ABORT
    assert ui.unlike_calls == 0


# ---- real batches -----------------------------------------------------------
def test_three_like_test(cfg, store, rng):
    unlock(store, "dry_run")
    ui = FakeLikesUI(50)
    s = run(ui, real(cfg), store, rng, limit=3)
    assert (s.attempted, s.successful, s.failed, s.uncertain) == (3, 3, 0, 0)
    assert len(ui.liked) == 47 and ui.unlike_calls == 1


def test_normal_batches_random_80_100_until_session_limit(cfg, store, rng):
    unlock(store, "dry_run", "real_test", "manual_verified", "first_batch")
    ui = FakeLikesUI(2000)
    s = run(ui, real(cfg), store, rng)
    assert s.status == "session_limit_reached"
    assert s.attempted == 500 and s.successful == 500
    sizes = [b["size"] for b in s.batches]
    assert sum(sizes) == 500
    assert all(sz <= 100 for sz in sizes)
    assert all(80 <= sz <= 100 for sz in sizes[:-1])  # only the last can be cut by the cap
    assert len(ui.liked) == 1500


def test_custom_session_limit(cfg, store, rng):
    unlock(store, "dry_run", "real_test", "manual_verified", "first_batch")
    ui = FakeLikesUI(2000)
    s = run(ui, real(cfg, max_actions_per_session=130), store, rng)
    assert s.attempted == 130 and s.status == "session_limit_reached"


def test_fewer_than_80_left_processed_then_completed(cfg, store, rng):
    unlock(store, "dry_run", "real_test", "manual_verified", "first_batch")
    ui = FakeLikesUI(150)
    s = run(ui, real(cfg), store, rng)
    assert s.status == "completed"
    assert s.successful == 150 and ui.liked == []
    assert s.batches[-1]["size"] < 80


def test_empty_list(cfg, store, rng):
    ui = FakeLikesUI(0)
    s = run(ui, cfg, store, rng)
    assert s.status == "completed" and ui.unlike_calls == 0


# ---- anomalies --------------------------------------------------------------
def test_selection_mismatch_stops_before_unlike(cfg, store, rng):
    unlock(store, "dry_run", "real_test", "manual_verified", "first_batch")
    ui = FakeLikesUI(300)
    ui.select_cap = 50  # page refuses to select more than 50
    s = run(ui, real(cfg), store, rng)
    assert s.status == "stopped" and s.stop_reason == "selection_mismatch"
    assert ui.unlike_calls == 0 and s.attempted == 0


def test_extra_selected_item_stops(cfg, store, rng):
    unlock(store, "dry_run")
    ui = FakeLikesUI(30)
    ui.select_extra = True
    s = run(ui, real(cfg), store, rng, limit=3)
    assert s.stop_reason == "selection_mismatch" and ui.unlike_calls == 0


def test_verification_failure_items_still_liked(cfg, store, rng):
    unlock(store, "dry_run")
    ui = FakeLikesUI(30)
    ui.ignore_unlike = True
    s = run(ui, real(cfg), store, rng, limit=3)
    assert s.status == "stopped" and s.stop_reason == "verification_failed"
    assert (s.attempted, s.successful, s.failed) == (3, 0, 3)
    assert ui.unlike_calls == 1  # never retried
    assert "real_test" not in store.stages()


def test_unverifiable_coverage_is_uncertain_and_stops(cfg, store, rng):
    unlock(store, "dry_run")
    ui = FakeLikesUI(30)
    real_discover = ui.discover
    calls = {"n": 0}

    def discover(target):
        calls["n"] += 1
        ui.short_coverage = calls["n"] > 1  # only the post-unlike check is short
        return real_discover(target)

    ui.discover = discover
    s = run(ui, real(cfg), store, rng, limit=3)
    assert s.status == "uncertain" and s.stop_reason == "uncertain_result"
    assert s.uncertain == 3 and s.successful == 0 and ui.unlike_calls == 1


def test_uncertain_unlike_stops_and_keeps_pending(cfg, store, rng):
    unlock(store, "dry_run")
    ui = FakeLikesUI(30)
    ui.raise_on_unlike = UncertainOutcome("dialog never closed")
    s = run(ui, real(cfg), store, rng, limit=3)
    assert s.status == "uncertain" and s.uncertain == 3 and s.attempted == 3
    assert s.pending_batch and len(s.pending_batch["fp_hashes"]) == 3
    assert ui.unlike_calls == 1


def test_action_not_submitted_is_not_counted(cfg, store, rng):
    unlock(store, "dry_run")
    ui = FakeLikesUI(30)
    ui.raise_before_unlike = ActionNotSubmitted(StopReason.SELECTOR_FAILURE, "no button")
    s = run(ui, real(cfg), store, rng, limit=3)
    assert s.status == "stopped" and s.attempted == 0 and s.uncertain == 0
    assert s.pending_batch is None


@pytest.mark.parametrize("reason", [
    StopReason.CAPTCHA, StopReason.ACTION_BLOCKED, StopReason.RATE_LIMIT,
    StopReason.LOGIN_REQUIRED, StopReason.IDENTITY_VERIFICATION, StopReason.SECURITY_WARNING,
])
def test_stop_conditions_halt_between_batches(cfg, store, rng, reason):
    unlock(store, "dry_run", "real_test", "manual_verified", "first_batch")
    ui = FakeLikesUI(1000)
    ui.safety_after_batches = {1: reason}  # appears while verifying the first batch
    s = run(ui, real(cfg), store, rng)
    assert s.status == "uncertain" and s.stop_reason == reason.value
    assert ui.unlike_calls == 1
    assert s.pending_batch and s.uncertain == s.attempted  # verified later by recovery


def test_stop_during_unlike_counts_batch_as_uncertain(cfg, store, rng):
    unlock(store, "dry_run")
    ui = FakeLikesUI(30)
    ui.raise_on_unlike = StopCondition(StopReason.ACTION_BLOCKED, "toast")
    s = run(ui, real(cfg), store, rng, limit=3)
    assert s.stop_reason == "action_blocked" and s.uncertain == 3 and s.pending_batch


def test_browser_disconnected_at_start(cfg, store, rng):
    ui = FakeLikesUI(30)
    ui.ready_error = StopCondition(StopReason.BROWSER_DISCONNECTED, "closed")
    s = run(ui, cfg, store, rng)
    assert s.status == "stopped" and s.stop_reason == "browser_disconnected"


def test_unexpected_exception_stops_with_error(cfg, store, rng):
    ui = FakeLikesUI(30)

    def boom(_t):
        raise KeyError("x")

    ui.discover = boom
    s = run(ui, cfg, store, rng)
    assert s.status == "error" and s.stop_reason == "internal_error"


# ---- pacing / cooldown ----------------------------------------------------------
def _ready_all(store):
    unlock(store, "dry_run", "real_test", "manual_verified", "first_batch")


def test_block_stop_triggers_cooldown(cfg, store, rng):
    from datetime import datetime, timedelta, timezone

    from instaclean.runner import check_cooldown
    _ready_all(store)
    ui = FakeLikesUI(1000)
    ui.safety_after_batches = {1: StopReason.ACTION_BLOCKED}
    run(ui, real(cfg), store, rng)
    assert store.load_state()["last_block"]["reason"] == "action_blocked"
    rec = run(ui, real(cfg), store, rng)  # recovery check is non-destructive: allowed
    assert rec.status == "recovered" and ui.unlike_calls == 1
    with pytest.raises(StopCondition) as e:
        run(FakeLikesUI(10), real(cfg), store, rng)
    assert e.value.reason is StopReason.COOLDOWN
    later = datetime.now(timezone.utc) + timedelta(hours=73)
    check_cooldown(store, real(cfg), now=later)  # allowed again
    check_cooldown(store, cfg)  # dry run is never paused


def test_pause_between_normal_sessions(cfg, store, rng):
    from datetime import datetime, timedelta, timezone

    from instaclean.runner import check_cooldown
    _ready_all(store)
    run(FakeLikesUI(1000), real(cfg), store, rng)
    with pytest.raises(StopCondition) as e:
        check_cooldown(store, real(cfg))
    assert e.value.reason is StopReason.COOLDOWN
    check_cooldown(store, real(cfg), now=datetime.now(timezone.utc) + timedelta(hours=12, minutes=1))
    check_cooldown(store, real(cfg, min_hours_between_sessions=0))


def test_small_test_runs_do_not_start_the_pause(cfg, store, rng):
    from instaclean.runner import check_cooldown
    unlock(store, "dry_run")
    run(FakeLikesUI(30), real(cfg), store, rng, limit=3)
    check_cooldown(store, real(cfg))  # 3 < BATCH_MIN: no pause needed


def _at(hour: int, minute: int = 0):
    from datetime import datetime
    return datetime.now().astimezone().replace(hour=hour, minute=minute, second=0, microsecond=0)


def test_active_hours_allow_morning_midmorning_evening(cfg):
    from instaclean.runner import check_active_hours
    c = real(cfg, active_hours="07:00-09:30,10:00-12:00,18:30-22:00")
    for h, m in ((7, 0), (9, 29), (10, 45), (18, 30), (21, 59)):
        check_active_hours(c, now=_at(h, m))


def test_active_hours_refuse_night_noon_and_gaps(cfg):
    from instaclean.runner import check_active_hours
    c = real(cfg, active_hours="07:00-09:30,10:00-12:00,18:30-22:00")
    for h, m in ((0, 0), (3, 0), (6, 59), (9, 30), (12, 0), (14, 30), (22, 0), (23, 30)):
        with pytest.raises(StopCondition) as e:
            check_active_hours(c, now=_at(h, m))
        assert e.value.reason is StopReason.OUTSIDE_ACTIVE_HOURS


def test_active_hours_never_restrict_dry_run_or_empty_setting(cfg):
    from instaclean.runner import check_active_hours
    check_active_hours(replace(cfg, active_hours="07:00-08:00"), now=_at(3))
    check_active_hours(real(cfg, active_hours=""), now=_at(3))


def test_runner_refuses_real_session_outside_active_hours(cfg, store, rng):
    from datetime import datetime
    _ready_all(store)
    now = datetime.now().astimezone()
    minute = now.hour * 60 + now.minute
    # a one-minute window that is certainly not "now"
    other = (minute + 120) % (24 * 60 - 1)
    window = f"{other // 60:02d}:{other % 60:02d}-{(other + 1) // 60:02d}:{(other + 1) % 60:02d}"
    ui = FakeLikesUI(1000)
    with pytest.raises(StopCondition) as e:
        run(ui, real(cfg, active_hours=window), store, rng)
    assert e.value.reason is StopReason.OUTSIDE_ACTIVE_HOURS
    assert ui.unlike_calls == 0


def test_non_block_stop_does_not_trigger_block_cooldown(cfg, store, rng):
    unlock(store, "dry_run")
    ui = FakeLikesUI(30)
    ui.ignore_unlike = True
    run(ui, real(cfg), store, rng, limit=3)
    assert "last_block" not in store.load_state()


def test_stray_retryable_error_is_a_classified_stop(cfg, store, rng):
    from instaclean.errors import RetryableError
    ui = FakeLikesUI(30)

    def flaky():
        raise RetryableError("page still loading")

    ui.ensure_ready = flaky
    s = run(ui, cfg, store, rng)
    assert s.status == "stopped" and s.stop_reason == "unexpected_ui"


def test_partial_load_processes_loaded_items(cfg, store, rng):
    from instaclean.runner import Discovery
    unlock(store, "dry_run", "real_test", "manual_verified", "first_batch")
    ui = FakeLikesUI(500)
    orig = ui.discover
    ui.discover = lambda t: Discovery(items=orig(t).items[:27], end_reached=False)
    s = run(ui, real(cfg, max_actions_per_session=27), store, rng)
    assert s.batches[0]["size"] == 27 and s.successful == 27


# ---- asynchronous Unlike on Instagram's side ---------------------------------------
def test_slow_server_side_unlike_is_rechecked_not_repeated(cfg, store, rng):
    unlock(store, "dry_run", "real_test", "manual_verified")
    ui = FakeLikesUI(100)
    ui.delay_reloads = 2  # still listed on the first reload, gone on the second
    s = run(ui, real(cfg), store, rng, limit=20)
    assert (s.successful, s.failed, s.uncertain) == (20, 0, 0)
    assert ui.unlike_calls == 1  # the action was not repeated
    assert cfg.verify_recheck_seconds in ui.waits
    assert s.pending_batch is None


def test_still_liked_after_rechecks_stops_and_stays_pending(cfg, store, rng):
    unlock(store, "dry_run")
    ui = FakeLikesUI(100)
    ui.ignore_unlike = True
    s = run(ui, real(cfg), store, rng, limit=3)
    assert s.stop_reason == "verification_failed" and s.failed == 3
    assert s.pending_batch and s.pending_batch["counted"]["failed"] == 3
    assert ui.calls.count("reload") == 1 + cfg.verify_rechecks
    # Later Instagram catches up: the next run re-checks (read-only) and fixes the counts
    ui.liked = ui.liked[3:]
    rec = run(ui, real(cfg), store, rng, limit=3)
    assert rec.status == "recovered"
    assert (rec.successful, rec.failed, rec.uncertain) == (3, 0, 0)
    assert ui.unlike_calls == 1
