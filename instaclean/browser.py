"""Browser lifecycle: one Chromium with a dedicated persistent profile.

- The profile lives outside the repo (default ~/.instaclean/browser-profile, 0700).
- No stealth, no fake user agent, no extensions, downloads disabled.
- Cookies are never read or exported by Instaclean.
- Password saving and autofill are switched off in the profile, so a manual
  login never leaves the password on disk.
- The login can be made to expire: the profile is wiped after N days (or after
  every run), which logs this Mac out locally.

Browser modes (INSTACLEAN_BROWSER):
  visible     normal window (default)
  background  normal window, minimized right after start
  headless    no window at all; Instagram sees "HeadlessChrome" (not hidden)
"""

from __future__ import annotations

import json
import logging
import os
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .config import REPO_ROOT, Config, check_profile_dir

log = logging.getLogger(__name__)

BROWSER_MODES = ("visible", "background", "headless")
SESSION_MARKER = "instaclean-session.json"

# Chromium preferences forced on the dedicated profile before every launch.
_HARDENED_PREFS = {
    "credentials_enable_service": False,  # never offer to save the password
    "profile": {"password_manager_enabled": False},
    "autofill": {"profile_enabled": False, "credit_card_enabled": False},
}


def prepare_profile_dir(path: Path) -> Path:
    path = Path(path).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _merge(dst: dict, src: dict) -> dict:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _merge(dst[key], value)
        else:
            dst[key] = value
    return dst


def harden_profile(profile: Path) -> None:
    """Turn off password saving and autofill in the dedicated profile."""
    prefs_path = profile / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8")) if prefs_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        prefs = {}
    if not isinstance(prefs, dict):
        prefs = {}
    _merge(prefs, _HARDENED_PREFS)
    prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
    os.chmod(prefs_path, 0o600)


# ---- login age and expiry ------------------------------------------------------------
def _marker(profile: Path) -> Path:
    return Path(profile).expanduser() / SESSION_MARKER


def record_login(profile: Path) -> None:
    """Remember when this profile was first seen logged in (once per login)."""
    marker = _marker(profile)
    if marker.exists():
        return
    marker.write_text(json.dumps({"logged_in_at": datetime.now(timezone.utc).isoformat()}),
                      encoding="utf-8")
    os.chmod(marker, 0o600)


def login_age_days(profile: Path, now: datetime | None = None) -> float | None:
    marker = _marker(profile)
    try:
        at = datetime.fromisoformat(json.loads(marker.read_text(encoding="utf-8"))["logged_in_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    now = now or datetime.now(timezone.utc)
    return (now - at).total_seconds() / 86400


def wipe_profile(profile: Path, *, home: Path | None = None) -> bool:
    """Delete the dedicated profile (local logout). Never follows symlinks.

    Refuses anything check_profile_dir() would refuse (personal browser data,
    the repo, the home folder).
    """
    profile = Path(profile).expanduser()
    check_profile_dir(profile, REPO_ROOT, home)
    if not profile.exists():
        return False
    if profile.is_symlink() or not profile.is_dir():
        raise ValueError(f"refusing to wipe {profile}: not a real directory")
    for root, dirs, files in os.walk(profile, topdown=False, followlinks=False):
        for name in files:
            os.unlink(os.path.join(root, name))
        for name in dirs:
            p = os.path.join(root, name)
            if stat.S_ISLNK(os.lstat(p).st_mode):
                os.unlink(p)
            else:
                os.rmdir(p)
    os.rmdir(profile)
    log.warning("Browser profile wiped: this Mac is logged out of Instagram (%s)", profile)
    return True


def expire_login_if_needed(cfg: Config, now: datetime | None = None) -> bool:
    """Wipe the profile when the login is older than session_max_days (0 = never)."""
    if cfg.session_max_days <= 0:
        return False
    age = login_age_days(cfg.profile_dir, now)
    if age is None or age < cfg.session_max_days:
        return False
    log.warning("Login is %.1f days old (limit %d): expiring it", age, cfg.session_max_days)
    return wipe_profile(cfg.profile_dir)


# ---- launch ---------------------------------------------------------------------------
@contextmanager
def open_browser(cfg: Config, *, force_visible: bool = False) -> Iterator[tuple[BrowserContext, Page]]:
    mode = "visible" if force_visible else cfg.browser_mode
    profile = prepare_profile_dir(cfg.profile_dir)
    harden_profile(profile)
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=(mode == "headless"),
            accept_downloads=False,
            viewport={"width": 1280, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            if mode == "background":
                _minimize(context, page)
            yield context, page
        finally:
            try:
                context.close()
            except Exception:
                pass  # already closed by the user


def _minimize(context: BrowserContext, page: Page) -> None:
    """Minimize the window. Playwright keeps background tabs un-throttled."""
    try:
        cdp = context.new_cdp_session(page)
        win = cdp.send("Browser.getWindowForTarget")
        cdp.send("Browser.setWindowBounds",
                 {"windowId": win["windowId"], "bounds": {"windowState": "minimized"}})
    except Exception as exc:  # cosmetic: never fail a run because of it
        log.info("could not minimize the browser window: %s", exc)
