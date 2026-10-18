"""Instagram web UI layer (Playwright).

Only normal navigation, clicks and DOM reads in the visible browser. No HTTP
requests of our own, no private APIs, no cookie access.

The page.evaluate() calls run the fixed JavaScript constants defined in this
file to READ the DOM (list checkboxes/thumbnails, page text); they never build
code from page content.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence, TypeVar
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from . import selectors as S
from .errors import (
    ActionNotSubmitted,
    RetryableError,
    StopCondition,
    StopReason,
    UncertainOutcome,
    with_retry,
)
from .runner import Discovery
from .safety import PageSignals, classify

log = logging.getLogger(__name__)
T = TypeVar("T")

NAV_TIMEOUT_MS = 30_000
UI_TIMEOUT_MS = 10_000
LOAD_MORE_TIMEOUT_MS = 12_000
SPINNER_GRACE_MS = 2_000
SELECT_CONFIRM_TIMEOUT_S = 3.0
REMOVAL_TIMEOUT_S = 30.0
LOGIN_WAIT_S = 600
MAX_LOAD_STALLS = 3

# Lists checkboxes (first selector that matches), tags each with an index
# attribute and returns, per checkbox, the thumbnail src and selection state.
_DISCOVER_JS = """
(args) => {
  // Drop stale tags first so an index can never point at an old element.
  document.querySelectorAll('[' + args.attr + ']').forEach((e) => e.removeAttribute(args.attr));
  let used = null, boxes = [];
  for (const sel of args.selectors) {
    const found = Array.from(document.querySelectorAll(sel));
    if (found.length) { used = sel; boxes = found; break; }
  }
  const stateOf = (b) => {
    const a = b.getAttribute('aria-checked') ?? b.getAttribute('aria-pressed');
    if (a === 'true') return true;
    if (a === 'false') return false;
    if (b instanceof HTMLInputElement) return b.checked;
    return null;
  };
  const token = (b) => {
    let h = 0; const s = b.outerHTML.replace(/ data-instaclean-idx="\\d+"/, '');
    for (let i = 0; i < s.length; i++) { h = (h * 31 + s.charCodeAt(i)) | 0; }
    return String(h);
  };
  const items = boxes.map((b, i) => {
    b.setAttribute(args.attr, String(i));
    let node = b, src = '';
    for (let d = 0; d < 10 && node && !src; d++) {
      node = node.parentElement;
      if (!node) break;
      const imgs = Array.from(node.querySelectorAll('img')).filter((im) => {
        if (b.contains(im)) return false;
        const r = im.getBoundingClientRect();
        return r.width >= args.minPx && r.height >= args.minPx;
      });
      if (imgs.length === 1) src = imgs[0].currentSrc || imgs[0].src || '';
      else if (imgs.length > 1) break;
    }
    const r = b.getBoundingClientRect();
    return { idx: i, src, state: stateOf(b), token: token(b), visible: r.width > 0 && r.height > 0 };
  });
  return { selector: used, items };
}
"""

_COUNT_JS = """
(args) => {
  for (const sel of args.selectors) {
    const n = document.querySelectorAll(sel).length;
    if (n) return n;
  }
  return 0;
}
"""

# True once the number of checkboxes exceeds args.n.
_GREW_JS = """
(args) => {
  for (const sel of args.selectors) {
    const n = document.querySelectorAll(sel).length;
    if (n) return n > args.n;
  }
  return false;
}
"""

# Finds the scroll container holding the list (ancestor of the last tagged
# checkbox), parks it ~400px above its end and returns its centre, so a real
# wheel event can then cross the "near the end" threshold that triggers loading.
_SCROLL_NEAR_END_JS = """
(args) => {
  const last = document.querySelector('[' + args.attr + '="' + args.idx + '"]');
  let el = last ? last.parentElement : null;
  while (el) {
    const cs = getComputedStyle(el);
    if (/(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 5) break;
    el = el.parentElement;
  }
  el = el || document.scrollingElement;
  el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight - args.margin);
  const r = el.getBoundingClientRect();
  const h = Math.min(r.height || innerHeight, innerHeight);
  return {x: r.left + r.width / 2, y: Math.max(r.top, 0) + h / 2};
}
"""

_THUMBS_JS = """
(minPx) => Array.from(document.querySelectorAll('main img, [role="main"] img')).filter((im) => {
  const r = im.getBoundingClientRect(); return r.width >= minPx && r.height >= minPx;
}).length
"""

_TEXT_JS = "() => (document.body ? document.body.innerText : '').slice(0, 20000)"


@dataclass(frozen=True)
class _Item:
    idx: int
    fp: str
    state: bool | None
    token: str


class InstagramLikesUI:
    """Implements runner.LikesUI on the real Instagram web UI."""

    def __init__(
        self,
        page: Page,
        rng: random.Random,
        *,
        origin: str = S.INSTAGRAM_ORIGIN,
        login_wait_s: float = LOGIN_WAIT_S,
        settle_range: tuple[float, float] = (0.3, 1.0),
        notify: Callable[[str], None] = print,
    ) -> None:
        self.page = page
        self.rng = rng
        self.origin = origin.rstrip("/")
        self.login_wait_s = login_wait_s
        self.settle_range = settle_range
        self.notify = notify
        self._closed = False
        self._baseline: dict[str, str] = {}
        page.on("close", lambda _p: self._mark_closed())
        page.context.on("close", lambda _c: self._mark_closed())

    # ---- plumbing -------------------------------------------------------
    def _mark_closed(self) -> None:
        self._closed = True

    def _alive(self) -> None:
        if self._closed or self.page.is_closed():
            raise StopCondition(StopReason.BROWSER_DISCONNECTED, "browser or tab was closed")

    def _nd(self, fn: Callable[[], T], what: str, attempts: int = 3) -> T:
        """Non-destructive step: Playwright timeouts become retryable."""

        def step() -> T:
            self._alive()
            try:
                return fn()
            except PlaywrightTimeout as exc:
                raise RetryableError(f"{what}: timeout") from exc
            except PlaywrightError as exc:
                if "closed" in str(exc).lower():
                    raise StopCondition(StopReason.BROWSER_DISCONNECTED, str(exc)) from exc
                raise RetryableError(f"{what}: {exc}") from exc

        return with_retry(step, attempts=attempts, what=what,
                          on_retry=lambda n, e: log.info("retry %d %s: %s", n, what, e))

    def _settle(self) -> None:
        """Short margin after a condition-based wait. Never the only wait."""
        self.page.wait_for_timeout(int(self.rng.uniform(*self.settle_range) * 1000))

    def _first_visible(self, candidates: Sequence[Locator]) -> Locator | None:
        for loc in candidates:
            try:
                n = loc.count()
            except PlaywrightError:
                continue
            for i in range(n):
                el = loc.nth(i)
                try:
                    if el.is_visible():
                        return el
                except PlaywrightError:
                    continue
        return None

    def _goto(self, url: str) -> None:
        self._nd(lambda: self.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS),
                 f"open {urlparse(url).path}")

    def _path(self) -> str:
        return urlparse(self.page.url).path

    # ---- safety ---------------------------------------------------------
    def _signals(self) -> PageSignals:
        page = self.page
        return PageSignals(
            url=page.url,
            text=page.evaluate(_TEXT_JS),
            frame_urls=tuple(f.url for f in page.frames),
            has_login_form=page.locator(S.LOGIN_FORM).count() > 0,
        )

    def check_safety(self) -> None:
        self._alive()
        try:
            hit = classify(self._signals())
        except PlaywrightError as exc:
            if "closed" in str(exc).lower():
                raise StopCondition(StopReason.BROWSER_DISCONNECTED, str(exc)) from exc
            raise StopCondition(StopReason.UNEXPECTED_UI, f"cannot read page: {exc}") from exc
        if hit:
            raise StopCondition(*hit)

    def diagnostics(self) -> dict[str, object]:
        """Selector match counts. No page content, safe to log."""
        page = self.page
        out: dict[str, object] = {"path": self._path()}
        try:
            for sel in (*S.CHECKBOX_SELECTORS, S.LIKES_LINK, S.YOUR_ACTIVITY_LINK,
                        *S.LOGGED_IN_MARKERS, S.LOGIN_FORM, S.DIALOG, S.SPINNER):
                out[sel] = page.locator(sel).count()
            out["select_button"] = page.get_by_role("button", name=S.SELECT_RE).count() + \
                page.get_by_text(S.SELECT_RE).count()
            out["unlike_button"] = page.get_by_role("button", name=S.UNLIKE_RE).count() + \
                page.get_by_text(S.UNLIKE_RE).count()
            out["thumbnails"] = page.evaluate(_THUMBS_JS, S.MIN_THUMB_PX)
        except PlaywrightError as exc:
            out["error"] = str(exc)
        return out

    def capture_debug(self, directory: Path, label: str) -> None:
        """DEBUG only: screenshot + DOM snapshot (local, gitignored). No cookies."""
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            self.page.screenshot(path=str(directory / f"{stamp}-{label}.png"))
            (directory / f"{stamp}-{label}.html").write_text(self.page.content(), encoding="utf-8")
        except PlaywrightError as exc:
            log.warning("debug capture failed: %s", exc)

    # ---- login + navigation ----------------------------------------------
    def _is_logged_in(self) -> bool:
        path = self._path()
        if path.startswith(("/accounts/login", "/challenge", "/two_factor", "/checkpoint")):
            return False
        if self.page.locator(S.LOGIN_FORM).count() > 0:
            return False
        return any(self.page.locator(m).count() > 0 for m in S.LOGGED_IN_MARKERS)

    def _ensure_logged_in(self) -> None:
        self._goto(self.origin + "/")
        deadline = time.monotonic() + self.login_wait_s
        announced = False
        while True:
            self._alive()
            try:
                if self._is_logged_in():
                    return
            except PlaywrightError:
                pass  # page navigating while the user logs in
            if not announced:
                self.notify("Log in to Instagram in the browser window (including 2FA if asked). "
                            f"Waiting up to {int(self.login_wait_s // 60)} minutes...")
                announced = True
            if time.monotonic() > deadline:
                raise StopCondition(StopReason.LOGIN_REQUIRED, "manual login not completed in time")
            self.page.wait_for_timeout(2000)

    def _navigate_via_ui(self) -> None:
        page = self.page
        likes = self._first_visible([page.locator(S.LIKES_LINK)])
        if likes is None:
            activity = self._first_visible([page.locator(S.YOUR_ACTIVITY_LINK)])
            if activity is None:
                more = self._first_visible([
                    page.get_by_role("link", name=S.MORE_RE),
                    page.get_by_role("button", name=S.MORE_RE),
                    page.get_by_label(S.SETTINGS_RE),
                ])
                if more is None:
                    raise RetryableError("'More' menu not found")
                more.click(timeout=UI_TIMEOUT_MS)
                page.locator(S.YOUR_ACTIVITY_LINK).first.wait_for(state="visible",
                                                                  timeout=UI_TIMEOUT_MS)
                activity = self._first_visible([page.locator(S.YOUR_ACTIVITY_LINK)])
                if activity is None:
                    raise RetryableError("'Your activity' link not found")
            activity.click(timeout=UI_TIMEOUT_MS)
            page.wait_for_url(lambda u: "/your_activity" in urlparse(u).path, timeout=NAV_TIMEOUT_MS)
            likes = self._first_visible([page.locator(S.LIKES_LINK)])
            if likes is None:
                interactions = self._first_visible([page.locator(S.INTERACTIONS_LINK)])
                if interactions is None:
                    raise RetryableError("'Interactions' link not found")
                interactions.click(timeout=UI_TIMEOUT_MS)
                page.locator(S.LIKES_LINK).first.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
                likes = self._first_visible([page.locator(S.LIKES_LINK)])
                if likes is None:
                    raise RetryableError("'Likes' link not found")
        likes.click(timeout=UI_TIMEOUT_MS)
        page.wait_for_url(lambda u: S.is_likes_url(u), timeout=NAV_TIMEOUT_MS)

    def _navigate_to_likes(self) -> None:
        if S.is_likes_url(self.page.url):
            return
        try:
            self._nd(self._navigate_via_ui, "navigate to Likes via menu", attempts=2)
        except StopCondition as exc:
            if exc.reason is not StopReason.SELECTOR_FAILURE:
                raise
            log.info("Menu path not usable here (%s). Opening the Likes page URL directly.",
                     exc.detail)
            self._goto(self.origin + S.LIKES_PATH + "/")
        if not S.is_likes_url(self.page.url):
            raise StopCondition(StopReason.UNEXPECTED_PAGE,
                                f"expected {S.LIKES_PATH}, got {self._path()}")

    def _select_button(self) -> Locator | None:
        return self._first_visible([
            self.page.get_by_role("button", name=S.SELECT_RE),
            self.page.get_by_text(S.SELECT_RE),
        ])

    def _wait_likes_page_loaded(self) -> None:
        """Wait until the Select control or the grid shows up (or time out)."""
        deadline = time.monotonic() + UI_TIMEOUT_MS / 1000 * 2
        while time.monotonic() < deadline:
            self._alive()
            if self._checkbox_count() or self._select_button() is not None:
                break
            self.page.wait_for_timeout(300)
        self._wait_spinner_gone()
        self._settle()

    def ensure_ready(self) -> None:
        self._ensure_logged_in()
        self.check_safety()
        self._navigate_to_likes()
        self._wait_likes_page_loaded()
        self.check_safety()
        log.info("Likes page ready. Diagnostics: %s", self.diagnostics())

    def reload(self) -> None:
        self._nd(lambda: self.page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS),
                 "reload Likes page")
        self._baseline = {}
        if not S.is_likes_url(self.page.url):
            self.check_safety()
            raise StopCondition(StopReason.UNEXPECTED_PAGE, f"after reload: {self._path()}")
        self._wait_likes_page_loaded()

    # ---- discovery ---------------------------------------------------------
    def _checkbox_count(self) -> int:
        return int(self.page.evaluate(_COUNT_JS, {"selectors": list(S.CHECKBOX_SELECTORS)}))

    def _wait_spinner_gone(self) -> None:
        """Best effort. Instagram keeps a loading indicator at the bottom of the
        list while more items exist, so it may never disappear: readiness is
        decided by the Select control / checkbox counts, not by this."""
        spinner = self.page.locator(S.SPINNER)
        try:
            if spinner.count() and spinner.first.is_visible():
                spinner.first.wait_for(state="hidden", timeout=SPINNER_GRACE_MS)
        except PlaywrightTimeout:
            log.debug("loading indicator still visible (infinite scroll footer); continuing")

    def _read_items(self) -> list[_Item]:
        res = self.page.evaluate(_DISCOVER_JS, {"selectors": list(S.CHECKBOX_SELECTORS),
                                                "attr": S.IDX_ATTR, "minPx": S.MIN_THUMB_PX})
        items: list[_Item] = []
        for raw in res["items"]:
            fp = S.fingerprint_from_src(raw["src"]) if raw["visible"] else ""
            items.append(_Item(idx=raw["idx"], fp=fp, state=raw["state"], token=raw["token"]))
        return items

    def _is_selected(self, item: _Item) -> bool:
        if item.state is not None:
            return item.state
        base = self._baseline.setdefault(item.fp, item.token)
        return item.token != base

    def _enter_select_mode(self) -> bool:
        """True when checkboxes are shown. False when no Select control exists."""
        if self._checkbox_count():
            return True
        btn = self._select_button()
        if btn is None:
            return False
        btn.click(timeout=UI_TIMEOUT_MS)
        self.page.wait_for_function(_GREW_JS, arg={"selectors": list(S.CHECKBOX_SELECTORS), "n": 0},
                                    timeout=UI_TIMEOUT_MS)
        self._baseline = {}
        return True

    def _spinner_visible(self) -> bool:
        spinner = self.page.locator(S.SPINNER)
        try:
            return spinner.count() > 0 and spinner.last.is_visible()
        except PlaywrightError:
            return False

    def _load_more(self, current: int) -> None:
        """Make Instagram load the next page of likes.

        Measured on the real page (2026-10-18): the list lives in an inner scroll
        container; jumping straight to its end loads nothing, while parking just
        above the end and then sending a wheel scroll does (27 -> 63 -> 81).
        """
        page = self.page
        try:
            centre = page.evaluate(_SCROLL_NEAR_END_JS,
                                   {"attr": S.IDX_ATTR, "idx": current - 1, "margin": 400})
            page.wait_for_timeout(300)
            page.mouse.move(centre["x"], centre["y"])
            page.mouse.wheel(0, 2500)
        except PlaywrightError:
            pass
        try:
            page.wait_for_function(
                _GREW_JS,
                arg={"selectors": list(S.CHECKBOX_SELECTORS), "n": current},
                timeout=LOAD_MORE_TIMEOUT_MS,
            )
        except PlaywrightTimeout:
            pass  # no growth: caller counts a stall
        self._wait_spinner_gone()

    def discover(self, target: int) -> Discovery:
        def do() -> Discovery:
            if not self._enter_select_mode():
                if self.page.evaluate(_THUMBS_JS, S.MIN_THUMB_PX) == 0:
                    return Discovery(items=[], end_reached=True, unconfirmed_empty=True)
                raise RetryableError("thumbnails visible but no 'Select' control")
            stalls, prev = 0, -1
            while True:
                self._alive()
                items = self._read_items()
                fps: list[str] = []
                for it in items:
                    if it.fp:
                        fps.append(it.fp)
                        self._baseline.setdefault(it.fp, it.token)
                if len(fps) >= target:
                    return Discovery(items=fps, end_reached=False)
                stalls = stalls + 1 if len(items) == prev else 0
                if stalls >= MAX_LOAD_STALLS:
                    skipped = len(items) - len(fps)
                    if skipped:
                        log.warning("%d tiles without a recognizable thumbnail were skipped", skipped)
                    # A visible loading indicator means the list is NOT finished.
                    more = self._spinner_visible()
                    if more:
                        log.warning("List stopped growing at %d items but Instagram still shows "
                                    "a loading indicator: using the loaded items only", len(fps))
                    return Discovery(items=fps, end_reached=not more)
                prev = len(items)
                if items:
                    self._load_more(len(items))
                else:
                    self.page.wait_for_timeout(1000)

        return self._nd(do, "discover liked items")

    # ---- selection -----------------------------------------------------------
    def _select_one(self, fp: str) -> None:
        items = {it.fp: it for it in self._read_items() if it.fp}
        item = items.get(fp)
        if item is None:
            raise RetryableError("item moved or disappeared")  # rediscover
        if self._is_selected(item):
            return
        loc = self.page.locator(f'[{S.IDX_ATTR}="{item.idx}"]')
        loc.scroll_into_view_if_needed(timeout=UI_TIMEOUT_MS)
        loc.click(timeout=UI_TIMEOUT_MS)
        deadline = time.monotonic() + SELECT_CONFIRM_TIMEOUT_S
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(150)
            now = {it.fp: it for it in self._read_items() if it.fp}.get(fp)
            if now is not None and self._is_selected(now):
                return
        # Do not click again: a second click could toggle it back.
        raise StopCondition(StopReason.SELECTION_MISMATCH, "click did not select the item")

    def select(self, fingerprints: Sequence[str]) -> list[str]:
        for fp in fingerprints:
            self._nd(lambda fp=fp: self._select_one(fp), "select item")
            self.check_safety()
        return [it.fp for it in self._read_items() if it.fp and self._is_selected(it)]

    def cancel_selection(self) -> None:
        """Reloading the page is the most reliable way to clear a selection."""
        self.reload()

    # ---- destructive action --------------------------------------------------
    def unlike_selected(self) -> None:
        page = self.page
        try:
            self.check_safety()
        except StopCondition as exc:
            raise ActionNotSubmitted(exc.reason, exc.detail) from exc
        if page.locator(S.DIALOG).count():
            raise ActionNotSubmitted(StopReason.UNEXPECTED_UI, "a dialog is already open")
        button = self._first_visible([page.get_by_role("button", name=S.UNLIKE_RE),
                                      page.get_by_text(S.UNLIKE_RE)])
        if button is None:
            raise ActionNotSubmitted(StopReason.SELECTOR_FAILURE, "Unlike button not found")

        # From the click on, the outcome is uncertain until verified.
        try:
            button.click(timeout=UI_TIMEOUT_MS)
            dialog = page.locator(S.DIALOG).last
            dialog.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
            self.check_safety()
            confirm = self._first_visible([dialog.get_by_role("button", name=S.UNLIKE_RE),
                                           dialog.get_by_text(S.UNLIKE_RE)])
            if confirm is None:
                raise UncertainOutcome("confirmation dialog without a recognizable Unlike button")
            confirm.click(timeout=UI_TIMEOUT_MS)
            page.locator(S.DIALOG).first.wait_for(state="detached",
                                                  timeout=int(REMOVAL_TIMEOUT_S * 1000))
            self._settle()
            self.check_safety()
        except StopCondition:
            raise
        except PlaywrightError as exc:
            raise UncertainOutcome(f"after pressing Unlike: {type(exc).__name__}: {exc}") from exc

    def wait_for_removal(self, fingerprints: Sequence[str]) -> set[str]:
        """Wait (bounded) for the unliked tiles to leave the DOM. Informational."""
        want = set(fingerprints)
        deadline = time.monotonic() + REMOVAL_TIMEOUT_S
        still = want
        while time.monotonic() < deadline:
            self._alive()
            try:
                still = want & {it.fp for it in self._read_items()}
            except PlaywrightError:
                still = want
            if not still:
                break
            self.page.wait_for_timeout(500)
        return still
