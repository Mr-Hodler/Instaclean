"""Browser lifecycle: one visible Chromium with a dedicated persistent profile.

- The profile lives outside the repo (default ~/.instaclean/browser-profile, 0700).
- No stealth, no fake user agent, no extensions, downloads disabled.
- Cookies are never read or exported by Instaclean.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .config import Config


def prepare_profile_dir(path: Path) -> Path:
    path = Path(path).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


@contextmanager
def open_browser(cfg: Config) -> Iterator[tuple[BrowserContext, Page]]:
    profile = prepare_profile_dir(cfg.profile_dir)
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            accept_downloads=False,
            viewport={"width": 1280, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            yield context, page
        finally:
            try:
                context.close()
            except Exception:
                pass  # already closed by the user
