"""Every locator used against Instagram lives here, in priority order.

Preference: structural attributes (href, role, aria) > visible text in several
languages > nothing else. No screen coordinates, no XPath.

These are best guesses from the public web UI and MUST be confirmed by the
manual dry run (see README). If Instagram changes its markup, this is the only
file that should need editing.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import urlparse

INSTAGRAM_HOST = "www.instagram.com"
INSTAGRAM_ORIGIN = f"https://{INSTAGRAM_HOST}"
LIKES_PATH = "/your_activity/interactions/likes"

# ---- structural, language independent -------------------------------------
LIKES_LINK = f'a[href*="{LIKES_PATH}"]'
INTERACTIONS_LINK = 'a[href*="/your_activity/interactions"]'
YOUR_ACTIVITY_LINK = 'a[href*="/your_activity"]'
LOGGED_IN_MARKERS = ('a[href*="/direct/inbox"]',)
LOGIN_FORM = 'input[name="username"]'
DIALOG = '[role="dialog"]'
SPINNER = '[role="progressbar"]'

# Checkbox shown on each tile after pressing "Select". First match wins.
CHECKBOX_SELECTORS = (
    '[data-testid="bulk_action_checkbox"]',
    '[aria-label="Toggle checkbox"]',
    '[role="checkbox"]',
    'input[type="checkbox"]',
)

# Attribute Instaclean puts on discovered checkboxes to click them by index.
IDX_ATTR = "data-instaclean-idx"
# Minimum rendered size of a thumbnail (filters out icons inside the tile).
MIN_THUMB_PX = 40


# ---- visible text, multi-language ------------------------------------------
def _exact(*words: str) -> re.Pattern[str]:
    return re.compile(r"^\s*(?:" + "|".join(re.escape(w) for w in words) + r")\s*$", re.I)


SELECT_RE = _exact(
    "Select", "Seleziona", "Seleccionar", "Sélectionner", "Auswählen", "Selecionar", "Selecteren"
)
UNLIKE_RE = _exact(
    "Unlike",
    "Non mi piace più",
    "Ya no me gusta",
    "Je n’aime plus",
    "Je n'aime plus",
    "Gefällt mir nicht mehr",
    "Descurtir",
    "Vind ik niet meer leuk",
)
MORE_RE = _exact("More", "Altro", "Más", "Plus", "Mehr", "Mais", "Meer")
SETTINGS_RE = _exact(
    "Settings", "Impostazioni", "Configuración", "Paramètres", "Einstellungen", "Configurações"
)

# Tiles carry an aria-label like "Photo, 3 of 18, by ..." / "Carousel with 5
# items, ..." / "Video, ...". Only the part before the first comma is read
# (never the author). Videos on the Likes page are reels.
_POST_KIND_RE = re.compile(
    r"^(photo|foto|image|immagine|imagen|bild|carousel|carosello|carrusel|carrousel|karussell)", re.I)
_REEL_KIND_RE = re.compile(r"^(video|vídeo|vidéo|reel|clip)", re.I)


def content_kind(label: str) -> str:
    """'post', 'reel' or 'unknown' from the first segment of a tile label."""
    head = (label or "").split(",")[0].strip()
    if _POST_KIND_RE.match(head):
        return "post"
    if _REEL_KIND_RE.match(head):
        return "reel"
    return "unknown"


def wanted(kind: str, content: str) -> bool:
    """Whether a tile of this kind is in scope. Unknown kinds only for 'all'."""
    if content == "all":
        return True
    return kind == {"posts": "post", "reels": "reel"}[content]


_FP_RE = re.compile(r"^[\w.\-]{6,200}$")


def fingerprint_from_src(src: str) -> str:
    """Identity of a liked item: file name of its thumbnail on the CDN.

    Query strings (signatures, sizes) and CDN host change between loads; the
    file name does not. Returns "" when no usable identity exists, so the item
    is treated as not selectable.
    """
    if not src:
        return ""
    name = PurePosixPath(urlparse(src).path).name
    return name if _FP_RE.match(name) else ""


def is_likes_url(url: str) -> bool:
    u = urlparse(url)
    return u.hostname == INSTAGRAM_HOST and u.path.rstrip("/").startswith(LIKES_PATH)
