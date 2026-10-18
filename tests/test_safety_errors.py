"""AUTOMATED TEST: stop-condition classification, retry policy, selectors."""

from __future__ import annotations

import pytest

from instaclean.errors import RetryableError, StopCondition, StopReason, UncertainOutcome, with_retry
from instaclean.safety import PageSignals, classify
from instaclean.selectors import fingerprint_from_src, is_likes_url

LIKES = "https://www.instagram.com/your_activity/interactions/likes/"


def test_normal_likes_page_is_clean():
    assert classify(PageSignals(url=LIKES, text="Likes\nNewest to oldest\nSelect")) is None


@pytest.mark.parametrize("url,reason", [
    ("https://www.instagram.com/challenge/abc/", StopReason.IDENTITY_VERIFICATION),
    ("https://www.instagram.com/accounts/login/?next=/", StopReason.LOGIN_REQUIRED),
    ("https://www.instagram.com/accounts/suspended/", StopReason.SECURITY_WARNING),
    ("https://example.com/your_activity", StopReason.UNEXPECTED_PAGE),
    ("about:blank", StopReason.UNEXPECTED_PAGE),
])
def test_url_rules(url, reason):
    assert classify(PageSignals(url=url))[0] is reason


@pytest.mark.parametrize("text,reason", [
    ("Action Blocked\nThis action was blocked.", StopReason.ACTION_BLOCKED),
    ("Azione bloccata", StopReason.ACTION_BLOCKED),
    ("Try Again Later. We limit how often you can do certain things", StopReason.RATE_LIMIT),
    ("Riprova più tardi", StopReason.RATE_LIMIT),
    ("Confirm it’s you", StopReason.IDENTITY_VERIFICATION),
    ("Conferma la tua identità", StopReason.IDENTITY_VERIFICATION),
    ("We suspect automated behavior on your account", StopReason.SECURITY_WARNING),
    ("Please complete the CAPTCHA", StopReason.CAPTCHA),
    ("Something went wrong", StopReason.UNEXPECTED_UI),
])
def test_text_rules_multilanguage(text, reason):
    assert classify(PageSignals(url=LIKES, text=text))[0] is reason


def test_captcha_iframe_detected():
    sig = PageSignals(url=LIKES, frame_urls=("https://www.google.com/recaptcha/api2/anchor",))
    assert classify(sig)[0] is StopReason.CAPTCHA


def test_login_form_only_matters_when_logged_in_expected():
    sig = PageSignals(url="https://www.instagram.com/accounts/login/", has_login_form=True)
    assert classify(sig)[0] is StopReason.LOGIN_REQUIRED
    assert classify(sig, expect_logged_in=False) is None


def test_with_retry_retries_only_retryable():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RetryableError("timeout")
        return "ok"

    assert with_retry(flaky, attempts=3) == "ok" and len(calls) == 3


def test_with_retry_gives_up_as_selector_failure():
    with pytest.raises(StopCondition) as e:
        with_retry(lambda: (_ for _ in ()).throw(RetryableError("nope")), attempts=2, what="x")
    assert e.value.reason is StopReason.SELECTOR_FAILURE


@pytest.mark.parametrize("exc", [UncertainOutcome("u"),
                                 StopCondition(StopReason.ACTION_BLOCKED),
                                 StopCondition(StopReason.CAPTCHA)])
def test_destructive_or_stop_errors_never_retried(exc):
    calls = []

    def fn():
        calls.append(1)
        raise exc

    with pytest.raises(type(exc)):
        with_retry(fn, attempts=5)
    assert len(calls) == 1


def test_fingerprint_from_src():
    src = ("https://scontent-zrh1-1.cdninstagram.com/v/t51.2885-15/"
           "123456_789_n.jpg?stp=dst-jpg&_nc_ht=x&oh=abc&oe=def")
    assert fingerprint_from_src(src) == "123456_789_n.jpg"
    other_host = src.replace("scontent-zrh1-1", "scontent-fra5-2").replace("oh=abc", "oh=zzz")
    assert fingerprint_from_src(other_host) == "123456_789_n.jpg"
    assert fingerprint_from_src("") == ""
    assert fingerprint_from_src("data:image/png;base64,AAAA") == ""


def test_is_likes_url():
    assert is_likes_url(LIKES)
    assert is_likes_url("https://www.instagram.com/your_activity/interactions/likes")
    assert not is_likes_url("https://www.instagram.com/your_activity/interactions/comments/")
    assert not is_likes_url("https://evil.example/your_activity/interactions/likes/")
