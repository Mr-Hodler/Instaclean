"""AUTOMATED TEST: checkpoint persistence and crash recovery."""

from __future__ import annotations

import json
import os
import stat

import pytest
from conftest import FakeLikesUI, real, unlock

from instaclean.checkpoint import CheckpointStore
from instaclean.errors import StopCondition, StopReason, UncertainOutcome
from instaclean.runner import SessionRunner
from instaclean.verifier import classify_batch, fp_hash


def test_roundtrip_and_permissions(store):
    s = store.new_session(mode="real", stage="normal", action_limit=500)
    s.successful = 187
    s.batches_completed = 2
    s.last_batch_size = 94
    store.save_session(s)
    loaded = store.last_session()
    assert loaded.successful == 187 and loaded.last_batch_size == 94
    mode = stat.S_IMODE(os.stat(store.session_path(s.session_id)).st_mode)
    assert mode == 0o600
    assert not list(store.dir.glob("*.tmp"))


def test_checkpoint_has_spec_fields(store):
    s = store.new_session(mode="real", stage="normal", action_limit=500)
    data = json.loads(store.session_path(s.session_id).read_text())
    for key in ("session_id", "started_at", "successful", "failed", "uncertain",
                "batches_completed", "last_batch_size", "status", "attempted"):
        assert key in data


def test_corrupt_checkpoint_stops_and_is_not_overwritten(store):
    store.dir.mkdir(parents=True)
    store.state_path.write_text("{not json")
    with pytest.raises(StopCondition) as e:
        store.load_state()
    assert e.value.reason is StopReason.CHECKPOINT_ERROR
    assert store.state_path.read_text() == "{not json"


def test_saved_after_every_batch(cfg, store, rng):
    unlock(store, "dry_run", "real_test", "manual_verified", "first_batch")
    ui = FakeLikesUI(1000)
    snapshots = []
    orig = store.save_session

    def spy(s):
        orig(s)
        snapshots.append((s.batches_completed, s.successful))

    store.save_session = spy
    s = SessionRunner(ui, real(cfg), store, rng).run(confirm=lambda _s: True)
    completed = sorted({b for b, _ in snapshots})
    assert completed == list(range(0, s.batches_completed + 1))


def test_no_personal_data_in_checkpoint(cfg, store, rng):
    unlock(store, "dry_run")
    ui = FakeLikesUI(30)
    ui.raise_on_unlike = UncertainOutcome("x")
    s = SessionRunner(ui, real(cfg), store, rng).run(limit=3, confirm=lambda _s: True)
    text = store.session_path(s.session_id).read_text()
    assert "img_" not in text and ".jpg" not in text  # only hashes
    assert all(len(h) == 16 for h in s.pending_batch["fp_hashes"])


def _crash_with_pending(cfg, store, rng, n=30):
    unlock(store, "dry_run")
    ui = FakeLikesUI(n)
    ui.raise_on_unlike = UncertainOutcome("crash")
    first = SessionRunner(ui, real(cfg), store, rng).run(limit=3, confirm=lambda _s: True)
    assert first.pending_batch
    return ui, first


def test_recovery_verifies_without_repeating(cfg, store, rng):
    ui, first = _crash_with_pending(cfg, store, rng)
    # The unlike did go through before the "crash"
    ui.liked = ui.liked[3:]
    ui.raise_on_unlike = None
    calls_before = ui.unlike_calls
    rec = SessionRunner(ui, real(cfg), store, rng).run(limit=3, confirm=lambda _s: True)
    assert rec.status == "recovered" and rec.session_id == first.session_id
    assert (rec.successful, rec.failed, rec.uncertain) == (3, 0, 0)
    assert rec.pending_batch is None
    assert ui.unlike_calls == calls_before  # nothing retried


def test_recovery_detects_items_still_liked(cfg, store, rng):
    ui, _ = _crash_with_pending(cfg, store, rng)
    ui.raise_on_unlike = None  # the unlike never happened
    rec = SessionRunner(ui, real(cfg), store, rng).run(limit=3, confirm=lambda _s: True)
    assert (rec.successful, rec.failed, rec.uncertain) == (0, 3, 0)
    assert len(ui.liked) == 30


def test_recovery_stop_keeps_pending(cfg, store, rng):
    ui, _ = _crash_with_pending(cfg, store, rng)
    ui.ready_error = StopCondition(StopReason.LOGIN_REQUIRED, "logged out")
    rec = SessionRunner(ui, real(cfg), store, rng).run(limit=3, confirm=lambda _s: True)
    assert rec.status == "recovery_stopped" and rec.pending_batch
    assert rec.uncertain == 3


def test_interrupted_session_without_pending_is_marked(cfg, store, rng):
    s = store.new_session(mode="dry_run", stage="dry_run", action_limit=500)
    assert s.status == "running"
    SessionRunner(FakeLikesUI(5), cfg, store, rng).run()
    assert store.load_session(s.session_id).status == "interrupted"


# ---- verifier -------------------------------------------------------------------
def test_classify_batch_cases():
    batch = ["a", "b", "c"]
    assert classify_batch(batch, ["d", "e", "f"], False, 3).clean
    out = classify_batch(batch, ["a", "d", "e"], False, 3)
    assert (out.successful, out.failed, out.uncertain) == (2, 1, 0)
    out = classify_batch(batch, ["d"], False, 3)  # not enough of the list seen
    assert (out.successful, out.failed, out.uncertain) == (0, 0, 3)
    out = classify_batch(batch, ["d"], True, 3)  # end of list reached: covered
    assert out.clean


def test_fp_hash_is_stable_and_short():
    assert fp_hash("x_n.jpg") == fp_hash("x_n.jpg") and len(fp_hash("x")) == 16
