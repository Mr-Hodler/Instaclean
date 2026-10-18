"""Mock of the Likes page, served through Playwright request routing.

This is NOT Instagram's real markup: it implements the structure that
instaclean/selectors.py expects, so the Playwright layer can be exercised
end to end. Every request is answered locally or aborted; nothing leaves the
machine.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

GIF = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")

HOME = """<!doctype html><html><body>
<nav><a href="/direct/inbox/">Messages</a>
<div role="button" id="more" onclick="document.getElementById('menu').style.display='block'">More</div>
<div id="menu" style="display:none"><a href="/your_activity/">Your activity</a></div></nav>
</body></html>"""

ACTIVITY = """<!doctype html><html><body>
<a href="/direct/inbox/">Messages</a>
<a href="/your_activity/interactions/">Interactions</a>
</body></html>"""

INTERACTIONS = """<!doctype html><html><body>
<a href="/direct/inbox/">Messages</a>
<a href="/your_activity/interactions/likes/">Likes</a>
</body></html>"""

LIKES = """<!doctype html><html><head><style>
.tile{position:relative;width:120px;height:120px;margin:4px}
.tile img{width:120px;height:120px;display:block}
.box{position:absolute;top:4px;right:4px;width:24px;height:24px;background:#fff}
</style></head><body>
<a href="/direct/inbox/">Messages</a>
<main>
  <div id="toolbar">__SELECT__</div>
  <div id="grid"></div>
  <div id="bar" style="display:none"><div role="button" id="unlike">Unlike</div></div>
  <div role="progressbar" data-visualcompletion="loading-state">loading</div>
</main>
<script>
const ALL = __ITEMS__; const CFG = __CFG__;
const PAGE = 12; let shown = 0, selectMode = false, loading = false;
const grid = document.getElementById('grid');
function selectedBoxes(){ return Array.from(document.querySelectorAll('.box svg[data-on="1"]')); }
function updateBar(){ document.getElementById('bar').style.display = selectedBoxes().length ? 'block' : 'none'; }
function addBox(tile){
  const b = document.createElement('div');
  b.className = 'box'; b.setAttribute('aria-label', 'Toggle checkbox'); b.setAttribute('role', 'button');
  b.innerHTML = '<svg data-on="0" width="24" height="24"><circle r="8" cx="12" cy="12"/></svg>';
  b.onclick = (e) => {
    e.stopPropagation();
    const s = b.querySelector('svg');
    if (s.dataset.on === '0' && CFG.selectCap !== null && selectedBoxes().length >= CFG.selectCap) return;
    s.dataset.on = s.dataset.on === '1' ? '0' : '1'; updateBar();
  };
  tile.prepend(b);
}
function renderMore(){
  const next = ALL.slice(shown, shown + PAGE);
  for (const fp of next) {
    const t = document.createElement('div'); t.className = 'tile'; t.dataset.fp = fp;
    t.innerHTML = '<img alt="" src="https://scontent-xyz.cdninstagram.com/v/t51.2885-15/' + fp + '?stp=a&oh=b">';
    if (selectMode) addBox(t);
    grid.appendChild(t);
  }
  shown += next.length;
}
renderMore();
window.addEventListener('scroll', () => {
  if (loading || shown >= ALL.length) return;
  if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 300) {
    loading = true; setTimeout(() => { renderMore(); loading = false; }, 150);
  }
});
const sel = document.getElementById('select');
if (sel) sel.onclick = () => {
  if (selectMode) { location.reload(); return; }
  selectMode = true; sel.textContent = 'Cancel';
  document.querySelectorAll('.tile').forEach(addBox);
};
document.getElementById('unlike').onclick = () => {
  const d = document.createElement('div'); d.setAttribute('role', 'dialog');
  d.innerHTML = '<p>Unlike these posts?</p><div role="button" id="confirm">Unlike</div><div role="button">Cancel</div>';
  document.body.appendChild(d);
  document.getElementById('confirm').onclick = async () => {
    const fps = selectedBoxes().map((s) => s.closest('.tile').dataset.fp);
    const r = await fetch('/__fixture/unlike', {method: 'POST', body: JSON.stringify(fps)});
    const res = await r.json();
    d.remove();
    if (res.blocked) {
      const a = document.createElement('div'); a.setAttribute('role', 'alert');
      a.textContent = 'Action Blocked. Try Again Later.'; document.body.appendChild(a); return;
    }
    fps.forEach((fp) => document.querySelector('.tile[data-fp="' + fp + '"]').remove());
    updateBar();
  };
};
</script></body></html>"""


class FakeInstagram:
    def __init__(self, n: int, *, select_cap: int | None = None, block_unlike: bool = False) -> None:
        self.liked = [f"{1000 + i}_{i * 7}_n.jpg" for i in range(n)]
        self.select_cap = select_cap
        self.block_unlike = block_unlike
        self.unlike_requests = 0
        self.external_requests: list[str] = []

    def handle(self, route) -> None:
        req = route.request
        u = urlparse(req.url)
        host = u.hostname or ""
        if host.endswith("cdninstagram.com"):
            route.fulfill(status=200, content_type="image/gif", body=GIF)
            return
        if host != "www.instagram.com":
            self.external_requests.append(req.url)
            route.abort()
            return
        path = u.path.rstrip("/") or "/"
        if path == "/__fixture/unlike":
            self.unlike_requests += 1
            fps = json.loads(req.post_data or "[]")
            if not self.block_unlike:
                self.liked = [f for f in self.liked if f not in set(fps)]
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"blocked": self.block_unlike}))
            return
        pages = {"/": HOME, "/your_activity": ACTIVITY,
                 "/your_activity/interactions": INTERACTIONS}
        if path == "/your_activity/interactions/likes":
            select = '<div role="button" id="select">Select</div>' if self.liked else ""
            html = (LIKES.replace("__SELECT__", select)
                    .replace("__ITEMS__", json.dumps(self.liked))
                    .replace("__CFG__", json.dumps({"selectCap": self.select_cap})))
        else:
            html = pages.get(path, "<html><body>not found</body></html>")
        route.fulfill(status=200, content_type="text/html", body=html)
