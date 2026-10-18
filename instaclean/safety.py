"""Stop-condition detection. Pure classification of page signals.

Detection only: Instaclean never tries to solve, hide from or work around any
of these. On a match the session stops and the browser stays open.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .errors import StopReason


@dataclass(frozen=True)
class PageSignals:
    url: str
    text: str = ""
    frame_urls: tuple[str, ...] = ()
    has_login_form: bool = False


_URL_RULES: tuple[tuple[str, StopReason], ...] = (
    ("/challenge", StopReason.IDENTITY_VERIFICATION),
    ("/checkpoint", StopReason.IDENTITY_VERIFICATION),
    ("/auth_platform", StopReason.IDENTITY_VERIFICATION),
    ("/accounts/suspended", StopReason.SECURITY_WARNING),
    ("/accounts/disabled", StopReason.SECURITY_WARNING),
    ("/accounts/login", StopReason.LOGIN_REQUIRED),
    ("/two_factor", StopReason.LOGIN_REQUIRED),
)

_CAPTCHA_FRAME_RE = re.compile(r"recaptcha|hcaptcha|arkoselabs|funcaptcha|captcha", re.I)


def _rx(*phrases: str) -> re.Pattern[str]:
    return re.compile("|".join(phrases), re.I)


# Order matters: most specific first.
_TEXT_RULES: tuple[tuple[StopReason, re.Pattern[str]], ...] = (
    (StopReason.CAPTCHA, _rx(r"captcha", r"i'm not a robot", r"non sono un robot")),
    (
        StopReason.ACTION_BLOCKED,
        _rx(
            r"action blocked",
            r"azione bloccata",
            r"acción bloqueada",
            r"action bloquée",
            r"aktion blockiert",
            r"ação bloqueada",
        ),
    ),
    (
        StopReason.SECURITY_WARNING,
        _rx(
            r"automated behaviou?r",
            r"comportamento automatizzato",
            r"comportamento automatico",
            r"we suspended your account",
            r"abbiamo sospeso il tuo account",
            r"your account has been (?:disabled|suspended)",
            r"il tuo account è stato (?:disattivato|sospeso)",
        ),
    ),
    (
        StopReason.IDENTITY_VERIFICATION,
        _rx(
            r"confirm it's you",
            r"conferma che sei tu",
            r"confirm your identity",
            r"conferma la tua identità",
            r"verify your account",
            r"verifica il tuo account",
            r"suspicious (?:login|activity)",
            r"attività sospetta",
            r"accesso sospetto",
        ),
    ),
    (
        StopReason.RATE_LIMIT,
        _rx(
            r"try again later",
            r"riprova più tardi",
            r"inténtalo de nuevo más tarde",
            r"réessayez plus tard",
            r"versuche es später",
            r"tente novamente mais tarde",
            r"please wait a few minutes",
            r"attendi qualche minuto",
            r"we limit how often",
            r"limitiamo la frequenza",
            r"we restrict certain activity",
            r"limitiamo determinate attività",
        ),
    ),
    (
        StopReason.UNEXPECTED_UI,
        _rx(r"something went wrong", r"si è verificato un problema", r"qualcosa è andato storto"),
    ),
)


def _normalize(text: str) -> str:
    return text.replace("’", "'").replace(" ", " ")


def classify(signals: PageSignals, *, expect_logged_in: bool = True) -> tuple[StopReason, str] | None:
    """Return (reason, detail) if the page shows a stop condition, else None."""
    url = urlparse(signals.url)
    host = url.hostname or ""
    if not (host == "instagram.com" or host.endswith(".instagram.com")):
        return StopReason.UNEXPECTED_PAGE, f"not on instagram.com (host={host or 'none'})"

    path = url.path.lower()
    for fragment, reason in _URL_RULES:
        if fragment in path:
            if reason is StopReason.LOGIN_REQUIRED and not expect_logged_in:
                continue
            return reason, f"url path {url.path}"

    for frame_url in signals.frame_urls:
        if _CAPTCHA_FRAME_RE.search(frame_url):
            return StopReason.CAPTCHA, "captcha frame present"

    text = _normalize(signals.text)
    for reason, pattern in _TEXT_RULES:
        m = pattern.search(text)
        if m:
            return reason, f"page text matched '{m.group(0)}'"

    if expect_logged_in and signals.has_login_form:
        return StopReason.LOGIN_REQUIRED, "login form visible"
    return None
