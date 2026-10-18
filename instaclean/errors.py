"""Error taxonomy.

Three families, and the runner treats them very differently:

- RetryableError  : non-destructive hiccup (selector timeout, page still
                    loading, stale element). Safe to retry a few times.
- StopCondition   : something unexpected or a signal from Instagram
                    (CAPTCHA, Action Blocked, login, unexpected UI...).
                    The session stops. Never retried.
- UncertainOutcome: a destructive action may or may not have happened.
                    The session stops and the batch is NOT repeated.
"""

from __future__ import annotations

from enum import Enum
from typing import Callable, TypeVar

T = TypeVar("T")


class StopReason(str, Enum):
    CAPTCHA = "captcha"
    IDENTITY_VERIFICATION = "identity_verification"
    LOGIN_REQUIRED = "login_required"
    ACTION_BLOCKED = "action_blocked"
    RATE_LIMIT = "rate_limit"
    SECURITY_WARNING = "security_warning"
    UNEXPECTED_UI = "unexpected_ui"
    UNEXPECTED_PAGE = "unexpected_page"
    SELECTOR_FAILURE = "selector_failure"
    SELECTION_MISMATCH = "selection_mismatch"
    VERIFICATION_FAILED = "verification_failed"
    UNCERTAIN_RESULT = "uncertain_result"
    BROWSER_DISCONNECTED = "browser_disconnected"
    CHECKPOINT_ERROR = "checkpoint_error"
    STAGE_GATE = "stage_gate"
    COOLDOWN = "cooldown"
    USER_ABORT = "user_abort"
    INTERNAL_ERROR = "internal_error"


class InstacleanError(Exception):
    """Base class for all Instaclean errors."""


class RetryableError(InstacleanError):
    """Non-destructive failure that is safe to retry."""


class StopCondition(InstacleanError):
    """Stop the session now. Never retried."""

    def __init__(self, reason: StopReason, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason = reason
        self.detail = detail


class UncertainOutcome(StopCondition):
    """A destructive action may or may not have been applied."""

    def __init__(self, detail: str) -> None:
        super().__init__(StopReason.UNCERTAIN_RESULT, detail)


class ActionNotSubmitted(StopCondition):
    """Stop raised before the destructive click: nothing was sent."""


def with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    what: str = "operation",
    on_retry: Callable[[int, RetryableError], None] | None = None,
) -> T:
    """Run fn, retrying ONLY RetryableError. Anything else propagates as is.

    After the last failed attempt a SELECTOR_FAILURE stop is raised
    ("selector failure ripetuto").
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    last: RetryableError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except RetryableError as exc:
            last = exc
            if on_retry is not None and attempt < attempts:
                on_retry(attempt, exc)
    raise StopCondition(
        StopReason.SELECTOR_FAILURE, f"{what} failed after {attempts} attempts: {last}"
    )
