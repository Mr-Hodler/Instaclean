"""AUTOMATED TEST: batch sizing, hard cap, config validation."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from instaclean.batch import next_batch_target, plan_batch_size
from instaclean.config import (
    HARD_BATCH_CAP,
    Config,
    check_profile_dir,
    load_config,
    read_env_file,
)


def test_defaults_match_spec():
    c = Config()
    assert (c.batch_min, c.batch_max, c.max_actions_per_session) == (80, 100, 500)
    assert c.dry_run is True
    assert c.initial_test_size == 3
    assert c.debug is False


def test_random_batch_always_within_80_100(cfg):
    rng = random.Random(0)
    sizes = [next_batch_target(rng, cfg, remaining_session=10_000) for _ in range(20_000)]
    assert min(sizes) == 80 and max(sizes) == 100
    assert len(set(sizes)) == 21  # every value in range is reachable


def test_batch_never_exceeds_hard_cap(cfg):
    rng = random.Random(1)
    for _ in range(5000):
        t = next_batch_target(rng, cfg, remaining_session=10**6)
        assert t <= HARD_BATCH_CAP
        assert plan_batch_size(t, available=10**6) <= HARD_BATCH_CAP
    assert plan_batch_size(500, 500) == HARD_BATCH_CAP


def test_batch_respects_session_remaining(cfg):
    rng = random.Random(2)
    assert next_batch_target(rng, cfg, remaining_session=3) == 3
    assert next_batch_target(rng, cfg, remaining_session=0) == 0
    assert next_batch_target(rng, cfg, remaining_session=-5) == 0


def test_fewer_than_min_available_processes_remaining():
    assert plan_batch_size(target=93, available=41) == 41
    assert plan_batch_size(target=93, available=0) == 0


@pytest.mark.parametrize("kw", [
    {"batch_max": 101},
    {"batch_min": 90, "batch_max": 85},
    {"batch_min": 0},
    {"max_actions_per_session": 0},
    {"initial_test_size": 0},
])
def test_invalid_config_rejected(tmp_path, kw):
    with pytest.raises(ValueError):
        Config(profile_dir=tmp_path / "p", **kw).validate()


def test_env_file_and_overrides(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nINSTACLEAN_BATCH_MIN=85\nINSTACLEAN_DRY_RUN='false'\n"
                   f"INSTACLEAN_PROFILE_DIR={tmp_path / 'prof'}\nUNRELATED=1\n")
    assert read_env_file(env)["INSTACLEAN_DRY_RUN"] == "false"
    c = load_config(env_file=env, environ={})
    assert c.batch_min == 85 and c.dry_run is False
    c2 = load_config(env_file=env, environ={"INSTACLEAN_BATCH_MIN": "90"}, dry_run=True)
    assert c2.batch_min == 90 and c2.dry_run is True


def test_missing_env_file_means_dry_run(tmp_path):
    c = load_config(env_file=tmp_path / "nope.env", environ={},
                    profile_dir=tmp_path / "prof")
    assert c.dry_run is True


def test_profile_dir_refuses_personal_browser_data(tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (home / "Library/Application Support/Google/Chrome").mkdir(parents=True)
    repo.mkdir()
    with pytest.raises(ValueError):
        check_profile_dir(home / "Library/Application Support/Google/Chrome", repo, home)
    with pytest.raises(ValueError):
        check_profile_dir(home / ".ssh", repo, home)
    with pytest.raises(ValueError):
        check_profile_dir(home, repo, home)
    with pytest.raises(ValueError):
        check_profile_dir(repo / "src", repo, home)  # inside repo, not ignored
    check_profile_dir(repo / "browser-profile", repo, home)  # ignored folder: ok
    check_profile_dir(home / ".instaclean/browser-profile", repo, home)  # default: ok


def test_default_profile_is_outside_repo():
    from instaclean.config import REPO_ROOT
    p = Config().profile_dir.resolve()
    assert REPO_ROOT.resolve() not in p.parents


def test_doctor_reports_without_side_effects(cfg, store):
    from instaclean.doctor import collect
    rows = collect(cfg, store)
    names = {w for _, w, _ in rows}
    assert {"python", "playwright", "chromium", "profile dir", ".gitignore", "stages"} <= names
    assert not cfg.profile_dir.exists()  # doctor creates nothing
    assert not store.dir.exists()


def test_unattended_requires_explicit_opt_in(tmp_path):
    import argparse

    from instaclean.main import unattended_refusal
    base = Config(profile_dir=tmp_path / "p", dry_run=False)
    ok = argparse.Namespace(real=True, limit=None)
    assert Config().allow_unattended is False
    assert unattended_refusal(base, ok)  # not allowed in .env
    allowed = Config(profile_dir=tmp_path / "p", dry_run=False, allow_unattended=True)
    assert unattended_refusal(allowed, ok) is None
    assert unattended_refusal(allowed, argparse.Namespace(real=False, limit=None))
    assert unattended_refusal(allowed, argparse.Namespace(real=True, limit=3))
    dry = Config(profile_dir=tmp_path / "p", allow_unattended=True)
    assert unattended_refusal(dry, ok)
