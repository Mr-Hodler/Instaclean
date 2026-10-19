"""AUTOMATED TEST (mock DOM): the real Playwright layer + runner against a
local imitation of the Likes page. Proves the code paths work; it does NOT
prove the selectors match the real Instagram (that is MANUAL TEST REQUIRED).
"""

from __future__ import annotations

import random
from dataclasses import replace

import pytest
from conftest import real, unlock

pytest.importorskip("playwright")
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from fake_instagram import FakeInstagram  # noqa: E402
from instaclean.errors import StopCondition, StopReason  # noqa: E402
from instaclean.instagram import InstagramLikesUI  # noqa: E402
from instaclean.runner import SessionRunner  # noqa: E402

pytestmark = pytest.mark.browser
YES = lambda _s: True  # noqa: E731


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(headless=True)
        except PlaywrightError as exc:
            pytest.skip(f"Chromium not installed: {exc}")
        yield b
        b.close()


@pytest.fixture
def make_page(browser):
    contexts = []

    def make(fake: FakeInstagram):
        ctx = browser.new_context(viewport={"width": 900, "height": 700})
        ctx.route("**/*", fake.handle)
        contexts.append(ctx)
        page = ctx.new_page()
        return page

    yield make
    for c in contexts:
        c.close()


@pytest.fixture(autouse=True)
def fast_end_of_list(monkeypatch):
    import instaclean.instagram as ig
    monkeypatch.setattr(ig, "LOAD_MORE_TIMEOUT_MS", 1500)


def small(cfg, **kw):
    return replace(cfg, batch_min=5, batch_max=8, **kw)


def ui_for(page):
    return InstagramLikesUI(page, random.Random(7), login_wait_s=5, settle_range=(0.0, 0.05),
                            notify=lambda _m: None)


def test_navigation_via_menu_and_dry_run(make_page, cfg, store):
    fake = FakeInstagram(30)
    page = make_page(fake)
    s = SessionRunner(ui_for(page), small(cfg), store, random.Random(1)).run()
    assert s.status == "dry_run_ok" and 5 <= s.dry_run_selected <= 8
    assert fake.unlike_requests == 0 and len(fake.liked) == 30
    assert page.url.rstrip("/").endswith("/your_activity/interactions/likes")
    assert fake.external_requests == []


def test_real_three_then_verified(make_page, cfg, store):
    unlock(store, "dry_run")
    fake = FakeInstagram(30)
    s = SessionRunner(ui_for(make_page(fake)), small(real(cfg)), store,
                      random.Random(2)).run(limit=3, confirm=YES)
    assert (s.status, s.attempted, s.successful, s.failed, s.uncertain) == \
        ("session_limit_reached", 3, 3, 0, 0)
    assert len(fake.liked) == 27 and fake.unlike_requests == 1


def test_real_batches_with_infinite_scroll_until_empty(make_page, cfg, store):
    unlock(store, "dry_run", "real_test", "manual_verified", "first_batch")
    fake = FakeInstagram(30)  # 12 per scroll page -> lazy loading exercised
    s = SessionRunner(ui_for(make_page(fake)), small(real(cfg)), store,
                      random.Random(3)).run(confirm=YES)
    assert s.successful == 30 and s.failed == 0 and s.uncertain == 0
    assert fake.liked == []
    assert s.status == "empty_unconfirmed"  # no Select control left: user checks by hand
    assert all(b["size"] <= 8 for b in s.batches)


def test_selection_limit_stops_without_unlike(make_page, cfg, store):
    unlock(store, "dry_run", "real_test", "manual_verified", "first_batch")
    fake = FakeInstagram(30, select_cap=4)
    s = SessionRunner(ui_for(make_page(fake)), small(real(cfg)), store,
                      random.Random(4)).run(confirm=YES)
    assert s.stop_reason == "selection_mismatch" and s.attempted == 0
    assert fake.unlike_requests == 0


def test_action_blocked_is_uncertain_then_recovery_finds_still_liked(make_page, cfg, store):
    unlock(store, "dry_run")
    fake = FakeInstagram(30, block_unlike=True)
    page = make_page(fake)
    s = SessionRunner(ui_for(page), small(real(cfg)), store,
                      random.Random(5)).run(limit=3, confirm=YES)
    assert s.stop_reason == "action_blocked" and s.uncertain == 3 and s.pending_batch

    fake.block_unlike = False
    page2 = make_page(fake)
    rec = SessionRunner(ui_for(page2), small(real(cfg)), store,
                        random.Random(6)).run(limit=3, confirm=YES)
    assert rec.status == "recovered"
    assert (rec.successful, rec.failed, rec.uncertain) == (0, 3, 0)
    assert fake.unlike_requests == 1  # recovery never re-submits


def test_captcha_frame_detected(make_page):
    fake = FakeInstagram(5)
    page = make_page(fake)
    page.goto("https://www.instagram.com/your_activity/interactions/likes/")
    page.set_content('<a href="/direct/inbox/">x</a>'
                     '<iframe src="https://www.instagram.com/recaptcha/frame"></iframe>')
    page.wait_for_timeout(300)
    with pytest.raises(StopCondition) as e:
        ui_for(page).check_safety()
    assert e.value.reason is StopReason.CAPTCHA


def test_closed_browser_is_detected(make_page):
    page = make_page(FakeInstagram(5))
    ui = ui_for(page)
    page.close()
    with pytest.raises(StopCondition) as e:
        ui.check_safety()
    assert e.value.reason is StopReason.BROWSER_DISCONNECTED


def test_list_that_stops_loading_is_not_treated_as_finished(make_page, cfg, store):
    """Real-world case: Instagram keeps the loading indicator but loads nothing."""
    fake = FakeInstagram(40, stall_loading=True)  # only the first 12 ever render
    ui = ui_for(make_page(fake))
    ui.ensure_ready()
    disc = ui.discover(30)
    assert len(disc.items) == 12
    assert disc.end_reached is False  # spinner still visible: not the end


def test_real_batch_works_on_loaded_items_when_list_stalls(make_page, cfg, store):
    unlock(store, "dry_run", "real_test", "manual_verified", "first_batch")
    fake = FakeInstagram(40, stall_loading=True)
    s = SessionRunner(ui_for(make_page(fake)), replace(real(cfg), batch_min=20, batch_max=25),
                      store, random.Random(9)).run(confirm=YES)
    assert s.batches and s.batches[0]["size"] == 12
    assert s.batches[0]["successful"] == 12 and s.failed == 0


def test_posts_only_skips_reels(make_page, cfg, store):
    unlock(store, "dry_run", "real_test", "manual_verified")
    fake = FakeInstagram(30)  # every 3rd tile is a Photo, the rest are Video (reels)
    photos = {fp for i, fp in enumerate(fake.liked) if i % 3 == 0}
    ui = InstagramLikesUI(make_page(fake), random.Random(7), login_wait_s=5,
                          settle_range=(0.0, 0.05), notify=lambda _m: None, content="posts")
    s = SessionRunner(ui, replace(real(cfg), batch_min=5, batch_max=5,
                                  post_unlike_grace_seconds=0, verify_recheck_seconds=0),
                      store, random.Random(8)).run(limit=5, confirm=YES)
    assert s.successful == 5 and s.failed == 0
    removed = photos - set(fake.liked)
    assert len(removed) == 5
    assert all(fp in fake.liked for i, fp in enumerate(FakeInstagram(30).liked) if i % 3)  # no reel touched


def test_headless_style_run_stops_when_logged_out(make_page):
    fake = FakeInstagram(5, logged_in=False)
    ui = InstagramLikesUI(make_page(fake), random.Random(1), login_wait_s=60,
                          notify=lambda _m: None, interactive_login=False)
    import time
    t0 = time.monotonic()
    with pytest.raises(StopCondition) as e:
        ui.ensure_ready()
    assert e.value.reason is StopReason.LOGIN_REQUIRED
    assert time.monotonic() - t0 < 30  # does not wait for a login nobody can do
