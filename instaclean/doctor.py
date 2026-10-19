"""`python -m instaclean --doctor`: read-only environment check.

Reads only: Python/venv info, installed package versions, the Playwright
Chromium path, the profile/runtime folders' permissions, .env, .gitignore and
Instaclean's own checkpoints. Changes nothing, opens no browser window, makes
no network request.
"""

from __future__ import annotations

import stat
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .checkpoint import CheckpointStore
from .config import REPO_ROOT, Config, check_profile_dir
from .errors import StopCondition
from .runner import check_cooldown

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"
REQUIRED_IGNORES = (".env", "logs/", "checkpoints/", "debug/", "browser-profile/", ".venv/")


def _pinned(lock: Path, name: str) -> str | None:
    if not lock.is_file():
        return None
    for line in lock.read_text(encoding="utf-8").splitlines():
        if line.lower().startswith(name + "=="):
            return line.split("==", 1)[1].split()[0]
    return None


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def collect(cfg: Config, store: CheckpointStore) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []

    def add(status: str, what: str, detail: str) -> None:
        out.append((status, what, detail))

    v = sys.version_info
    add(OK if v >= (3, 11) else FAIL, "python", f"{v.major}.{v.minor}.{v.micro}")
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    add(OK if in_venv else WARN, "virtualenv", sys.prefix if in_venv else "not in a venv")

    lock = REPO_ROOT / "requirements.lock"
    for pkg in ("playwright", "greenlet", "pyee"):
        want = _pinned(lock, pkg)
        try:
            have = version(pkg)
        except PackageNotFoundError:
            add(FAIL, pkg, "not installed")
            continue
        add(OK if have == want else WARN, pkg, f"installed {have}, lock {want}")

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            exe = Path(pw.chromium.executable_path)
        add(OK if exe.exists() else FAIL, "chromium",
            str(exe) if exe.exists() else "missing: run .venv/bin/playwright install chromium")
    except Exception as exc:  # report, never crash the doctor
        add(FAIL, "chromium", f"cannot query Playwright: {exc}")

    try:
        check_profile_dir(cfg.profile_dir, REPO_ROOT)
        if cfg.profile_dir.exists():
            m = _mode(cfg.profile_dir)
            add(OK if m == 0o700 else WARN, "profile dir",
                f"{cfg.profile_dir} mode {oct(m)}" + ("" if m == 0o700 else " (expected 0o700)"))
        else:
            add(OK, "profile dir", f"{cfg.profile_dir} (created on first run)")
    except ValueError as exc:
        add(FAIL, "profile dir", str(exc))

    for d in (cfg.logs_dir, cfg.checkpoints_dir, cfg.debug_dir):
        if d.exists():
            m = _mode(d)
            add(OK if not m & 0o077 else WARN, d.name + "/", f"mode {oct(m)}")

    env = REPO_ROOT / ".env"
    if env.exists():
        m = _mode(env)
        add(OK if not m & 0o022 else WARN, ".env", f"present, mode {oct(m)}")
    else:
        add(OK, ".env", "absent: defaults apply (dry run only)")
    add(OK, "mode", "DRY RUN" if cfg.dry_run else "REAL allowed with --real")
    add(OK, "limits", f"batch {cfg.batch_min}-{cfg.batch_max}, session {cfg.max_actions_per_session}, "
        f"pause {cfg.min_hours_between_sessions}h, after block {cfg.block_cooldown_hours}h")
    add(OK if cfg.active_hours else WARN, "active hours",
        f"{cfg.active_hours} local, start jitter {cfg.start_jitter_minutes} min"
        if cfg.active_hours else "off: real sessions can start at any hour")
    add(OK, "browser", f"{cfg.browser_mode}, content {cfg.content}, "
        f"unattended {'allowed' if cfg.allow_unattended else 'off'}")
    from .browser import login_age_days
    age = login_age_days(cfg.profile_dir)
    if cfg.session_max_days == 0:
        add(WARN, "login expiry", "off (INSTACLEAN_SESSION_MAX_DAYS=0)")
    elif age is None:
        add(OK, "login expiry", f"{cfg.session_max_days} days (no login recorded yet)")
    else:
        left = cfg.session_max_days - age
        add(OK if left > 1 else WARN, "login expiry",
            f"login {age:.1f} days old, expires in {max(left, 0):.1f} days")

    gi = REPO_ROOT / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    missing = [p for p in REQUIRED_IGNORES if p not in lines]
    add(OK if not missing else FAIL, ".gitignore", "complete" if not missing else f"missing {missing}")

    try:
        add(OK, "stages", ", ".join(store.stages()) or "none yet (start with a dry run)")
        if not cfg.dry_run:
            try:
                check_cooldown(store, cfg)
                add(OK, "cooldown", "real mode not paused")
            except StopCondition as exc:
                add(WARN, "cooldown", exc.detail)
    except StopCondition as exc:
        add(FAIL, "checkpoints", exc.detail)
    return out


def run_doctor(cfg: Config, store: CheckpointStore) -> int:
    rows = collect(cfg, store)
    for status, what, detail in rows:
        print(f"[{status}] {what:<14} {detail}")
    failed = any(s == FAIL for s, _, _ in rows)
    print("\nResult:", "problems found" if failed else "environment OK")
    return 1 if failed else 0
