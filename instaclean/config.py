"""Configuration: defaults, .env loading, validation.

The defaults below are operational limits chosen by the user. They are NOT
Instagram thresholds and do not make automation "safe" from restrictions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping

# Defaults (edit here or override in .env).
BATCH_MIN = 80
BATCH_MAX = 100
MAX_ACTIONS_PER_SESSION = 500
DRY_RUN = True
INITIAL_TEST_SIZE = 3
DEBUG = False
# Pacing between REAL sessions (hours). Self-imposed, conservative; they do not
# hide automation and are not Instagram's numbers.
MIN_HOURS_BETWEEN_SESSIONS = 12.0
BLOCK_COOLDOWN_HOURS = 72.0
# Instagram applies a bulk Unlike asynchronously: give the server time before
# deciding an item is still liked. Only the CHECK is repeated, never the action.
POST_UNLIKE_GRACE_SECONDS = 5.0
VERIFY_RECHECK_SECONDS = 45.0
VERIFY_RECHECKS = 2

# Hard cap enforced in code: no configuration can raise a batch above this.
HARD_BATCH_CAP = 100

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_DIR = Path("~/.instaclean/browser-profile")

# Directory names that .gitignore excludes; a profile inside the repo must
# live under one of them.
_IGNORED_PROFILE_PARTS = {"profiles", "browser-profile", "sessions", "auth", ".instaclean"}

# Never point the automation at a personal browser profile or secrets.
_FORBIDDEN_PROFILE_ROOTS = (
    "~/Library/Application Support",
    "~/Library/Safari",
    "~/Library/Keychains",
    "~/Library/Cookies",
    "~/.ssh",
    "~/.gnupg",
)


@dataclass(frozen=True)
class Config:
    batch_min: int = BATCH_MIN
    batch_max: int = BATCH_MAX
    max_actions_per_session: int = MAX_ACTIONS_PER_SESSION
    dry_run: bool = DRY_RUN
    initial_test_size: int = INITIAL_TEST_SIZE
    debug: bool = DEBUG
    min_hours_between_sessions: float = MIN_HOURS_BETWEEN_SESSIONS
    block_cooldown_hours: float = BLOCK_COOLDOWN_HOURS
    post_unlike_grace_seconds: float = POST_UNLIKE_GRACE_SECONDS
    verify_recheck_seconds: float = VERIFY_RECHECK_SECONDS
    verify_rechecks: int = VERIFY_RECHECKS
    allow_unattended: bool = False
    profile_dir: Path = field(default_factory=lambda: DEFAULT_PROFILE_DIR.expanduser())
    data_dir: Path = REPO_ROOT

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def checkpoints_dir(self) -> Path:
        return self.data_dir / "checkpoints"

    @property
    def debug_dir(self) -> Path:
        return self.data_dir / "debug"

    def validate(self) -> "Config":
        if not 1 <= self.batch_min <= self.batch_max:
            raise ValueError("require 1 <= BATCH_MIN <= BATCH_MAX")
        if self.batch_max > HARD_BATCH_CAP:
            raise ValueError(f"BATCH_MAX cannot exceed {HARD_BATCH_CAP}")
        if self.max_actions_per_session < 1:
            raise ValueError("MAX_ACTIONS_PER_SESSION must be >= 1")
        if not 1 <= self.initial_test_size <= HARD_BATCH_CAP:
            raise ValueError("INITIAL_TEST_SIZE must be between 1 and 100")
        if self.min_hours_between_sessions < 0 or self.block_cooldown_hours < 0:
            raise ValueError("cooldown hours must be >= 0")
        if self.verify_rechecks < 0 or self.verify_recheck_seconds < 0 \
                or self.post_unlike_grace_seconds < 0:
            raise ValueError("verification timings must be >= 0")
        check_profile_dir(self.profile_dir, REPO_ROOT)
        return self


def check_profile_dir(path: Path, repo_root: Path, home: Path | None = None) -> None:
    """Refuse profile locations that could expose personal data or end up in Git."""
    home = (home or Path.home()).resolve()
    p = Path(path).expanduser().resolve()
    if p in (home, Path("/"), repo_root.resolve()):
        raise ValueError(f"unsafe profile dir: {p}")
    for root in _FORBIDDEN_PROFILE_ROOTS:
        r = Path(root.replace("~", str(home), 1)).resolve()
        if p == r or r in p.parents:
            raise ValueError(f"profile dir must not be inside {r} (personal browser data)")
    repo = repo_root.resolve()
    if repo in p.parents:
        rel_parts = set(p.relative_to(repo).parts)
        if not rel_parts & _IGNORED_PROFILE_PARTS:
            raise ValueError("profile dir inside the repo must be under a gitignored folder")


def parse_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"not a boolean: {value!r}")


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE reader (no dependency). Missing file -> {}."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


_ENV_KEYS = {
    "INSTACLEAN_BATCH_MIN": ("batch_min", int),
    "INSTACLEAN_BATCH_MAX": ("batch_max", int),
    "INSTACLEAN_MAX_ACTIONS_PER_SESSION": ("max_actions_per_session", int),
    "INSTACLEAN_DRY_RUN": ("dry_run", parse_bool),
    "INSTACLEAN_INITIAL_TEST_SIZE": ("initial_test_size", int),
    "INSTACLEAN_DEBUG": ("debug", parse_bool),
    "INSTACLEAN_MIN_HOURS_BETWEEN_SESSIONS": ("min_hours_between_sessions", float),
    "INSTACLEAN_BLOCK_COOLDOWN_HOURS": ("block_cooldown_hours", float),
    "INSTACLEAN_VERIFY_RECHECK_SECONDS": ("verify_recheck_seconds", float),
    "INSTACLEAN_VERIFY_RECHECKS": ("verify_rechecks", int),
    "INSTACLEAN_ALLOW_UNATTENDED": ("allow_unattended", parse_bool),
    "INSTACLEAN_PROFILE_DIR": ("profile_dir", lambda v: Path(v).expanduser()),
}


def load_config(
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
    **overrides: object,
) -> Config:
    """Defaults < .env file < process environment < explicit overrides."""
    merged: dict[str, str] = {}
    if env_file is not None:
        merged.update(read_env_file(env_file))
    env = os.environ if environ is None else environ
    merged.update({k: v for k, v in env.items() if k in _ENV_KEYS})

    values: dict[str, object] = {}
    for key, raw in merged.items():
        if key in _ENV_KEYS:
            name, conv = _ENV_KEYS[key]
            values[name] = conv(raw)
    values.update({k: v for k, v in overrides.items() if v is not None})
    return replace(Config(), **values).validate()
