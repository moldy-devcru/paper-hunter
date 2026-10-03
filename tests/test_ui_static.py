"""The static terminal: served, vendored, and provably offline-capable.

These are not interesting tests. They are the ones that stop a future edit from
breaking the LAN promise in a way nobody notices until mads is on the train:

1. **The files the browser needs are actually served by the app** — ``/``, ``/app.js``,
   ``/style.css`` and the vendored library, with the right content types. A page that
   200s while its module 404s is a blank screen, and a blank screen is indistinguishable
   from "the server is down".
2. **The vendored bundle is the bundle we recorded.** The sha256 in
   ``static/vendor/PROVENANCE.md`` is compared against the file's real hash, so a
   half-finished re-vendor, a truncated download, or an HTML error page saved with a
   ``.js`` name fails here instead of at 09:31 on a trading day.
3. **No CDN, anywhere.** ``index.html`` and ``app.js`` are grepped for ``http://``,
   ``https://`` and the CDN hostnames. This is the LAN-offline requirement expressed as
   an assertion rather than a memory.

The other assertions are the spec's literal checklist — ten timeframes, five pages,
five indicator toggles — pinned so that dropping one is a test failure, not a shrug.
Everything runs offline: no server is started, no network is touched.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ui.api import app

STATIC = Path(__file__).resolve().parent.parent / "ui" / "static"
INDEX_HTML = STATIC / "index.html"
APP_JS = STATIC / "app.js"
STYLE_CSS = STATIC / "style.css"
VENDOR_DIR = STATIC / "vendor"
VENDOR_JS = VENDOR_DIR / "lightweight-charts.standalone.production.js"
PROVENANCE = VENDOR_DIR / "PROVENANCE.md"

TIMEFRAMES = ("5m", "10m", "15m", "30m", "1h", "4h", "1d", "1w", "1M", "1Q")
PAGES = ("terminal", "arms", "ledger", "hunt", "calibration")
TOGGLES = ("ema50", "ema200", "bb", "rsi", "macd", "volume")
EXTERNAL = re.compile(r"https?://|unpkg\.com|jsdelivr\.net|cdnjs\.", re.IGNORECASE)


@pytest.fixture
def client() -> TestClient:
    """A TestClient with the default context. No database is opened for static files."""
    with TestClient(app) as test_client:
        yield test_client


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. the files exist and are served
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [INDEX_HTML, APP_JS, STYLE_CSS, VENDOR_JS, PROVENANCE],
    ids=lambda p: p.name,
)
def test_static_files_are_present(path: Path) -> None:
    assert path.exists(), f"{path} is missing from the package"
    assert path.stat().st_size > 0, f"{path} is empty"


def test_index_is_served_as_html(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "paper-hunter terminal" in body
    assert 'src="app.js"' in body
    assert 'href="style.css"' in body
    # The library is loaded from the vendor directory, relative — never from a CDN.
    assert "vendor/lightweight-charts.standalone.production.js" in body


def test_app_js_is_served_as_javascript(client: TestClient) -> None:
    response = client.get("/app.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "LightweightCharts" in response.text


def test_style_css_is_served_as_css(client: TestClient) -> None:
    response = client.get("/style.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]


def test_vendor_bundle_is_served(client: TestClient) -> None:
    response = client.get("/vendor/lightweight-charts.standalone.production.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert response.text.startswith("/*!")


def test_unknown_static_paths_are_404(client: TestClient) -> None:
    """No directory walk: a path the app did not name explicitly does not resolve."""
    assert client.get("/nope.js").status_code == 404
    assert client.get("/vendor/other.js").status_code == 404


# ---------------------------------------------------------------------------
# 2. the vendored library is the library we recorded
# ---------------------------------------------------------------------------


def test_provenance_records_version_and_sha256() -> None:
    text = read(PROVENANCE)
    digest = hashlib.sha256(VENDOR_JS.read_bytes()).hexdigest()
    assert digest in text, "PROVENANCE.md does not record the vendored file's real sha256"
    match = re.search(r"\|\s*version\s*\|\s*([0-9][^\s|]*)", text)
    assert match, "PROVENANCE.md has no version row"
    assert match.group(1) in text  # the version is also named in the file
    assert "Apache" in text, (
        "the bundle is Apache-2.0, not the MIT the spec claimed; the record must say so"
    )


def test_vendored_bundle_is_javascript_not_an_error_page() -> None:
    text = VENDOR_JS.read_text(encoding="utf-8", errors="replace")
    assert not text.lstrip().startswith("<"), "an HTML error page was saved as the bundle"
    assert "window.LightweightCharts" in text
    assert "createChart" in text
    assert '"5.2.1"' in text, "bundle version does not match the recorded version"
    assert len(text) > 100_000, "the standalone production bundle is ~198KB; this is not it"


def test_page_loads_the_vendored_bundle_relatively() -> None:
    """The script tag owns the bundle; app.js consumes the global it defines."""
    html = read(INDEX_HTML)
    assert 'src="vendor/lightweight-charts.standalone.production.js"' in html
    assert html.index("lightweight-charts") < html.index("app.js"), (
        "the classic script must load before the module that uses its global"
    )
    assert "window.LightweightCharts" in read(APP_JS)
    assert 'type="module"' in html


# ---------------------------------------------------------------------------
# 3. LAN-offline: no external references at runtime
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [INDEX_HTML, APP_JS, STYLE_CSS], ids=lambda p: p.name)
def test_no_cdn_references_in_served_assets(path: Path) -> None:
    """A LAN tool that fetches from a CDN is broken the moment the internet is not."""
    hits = EXTERNAL.findall(read(path))
    # The Apache banner URL lives in the vendored bundle, not in files we author, and
    # the files we author must carry no URL at all — including in comments.
    assert hits == [], f"{path.name} references an external resource: {hits}"


@pytest.mark.parametrize("path", [INDEX_HTML, APP_JS], ids=lambda p: p.name)
def test_no_write_verbs_in_the_frontend(path: Path) -> None:
    """The UI has no door: nothing in the frontend even names a write method."""
    text = read(path).upper()
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        assert f"'{verb}'" not in text and f'"{verb}"' not in text, f"{path.name} names {verb}"


# ---------------------------------------------------------------------------
# 4. the spec's checklist, pinned
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tf", TIMEFRAMES)
def test_every_timeframe_has_a_button(tf: str) -> None:
    assert f'data-tf="{tf}"' in read(INDEX_HTML)
    assert f'"{tf}"' in read(APP_JS)


def test_timeframe_list_is_complete_and_ordered() -> None:
    """The spec's order is the button order, so a stray re-sort is a visible failure."""
    match = re.search(r"const TF_LIST = \[(.*?)\];", read(APP_JS), re.DOTALL)
    assert match, "TF_LIST not found in app.js"
    assert tuple(re.findall(r'"([^"]+)"', match.group(1))) == TIMEFRAMES


@pytest.mark.parametrize("page", PAGES)
def test_every_page_has_a_tab_and_a_route(page: str) -> None:
    html = read(INDEX_HTML)
    assert f'href="#/{page}"' in html
    assert f'data-page="{page}"' in html
    assert f"  {page}:" in read(APP_JS)


@pytest.mark.parametrize("page", PAGES[1:])
def test_pending_pages_say_pending(page: str) -> None:
    """U3/U4 placeholders must name their phase instead of pretending to render."""
    body = read(APP_JS)
    block = re.search(rf"  {page}: \{{(.*?)\n  \}},", body, re.DOTALL)
    assert block, f"no PAGES entry for {page}"
    assert re.search(r'phase: "U[34]"', block.group(1)), f"{page} has no phase label"
    assert re.search(r'body: "[^"]+"', block.group(1)), f"{page} has no explanation"


@pytest.mark.parametrize("name", TOGGLES)
def test_every_indicator_has_a_toggle(name: str) -> None:
    assert f'data-ind="{name}"' in read(INDEX_HTML)
    assert f'"{name}"' in read(APP_JS)


def test_arm_colors_match_the_spec() -> None:
    """A gray, B amber, C blue, exception magenta — and the values are pinned here."""
    body = read(APP_JS)
    block = re.search(r"const ARM_COLOR = \{(.*?)\};", body, re.DOTALL)
    assert block, "ARM_COLOR map not found"
    colors = dict(re.findall(r'(\w+):\s*"(#[0-9a-fA-F]{6})"', block.group(1)))
    assert colors == {
        "A": "#9aa4b2",
        "B": "#e0a63a",
        "C": "#4c9aff",
        "EXCEPTION": "#e05fd0",
    }


def test_relvol_threshold_is_one_point_five() -> None:
    """T4: >= 1.5x the 20-bar average is what "loud" means here, and it is written down."""
    body = read(APP_JS)
    assert "const RELVOL_MULT = 1.5;" in body
    assert "const RELVOL_WINDOW = 20;" in body


def test_widening_the_window_can_only_ever_go_left() -> None:
    """Regression pin for a real bug found in the U2 smoke run.

    The first version widened from ``now - 5 days``, which at 21:00 ET is a day LATER
    than the server's ET-anchored default window: the chart asked for a narrower window
    and silently dropped a session (104 bars became 78). It also fired on the initial
    ``fitContent()``, where ``from === 0`` is normal rather than a pan. Both guards are
    load-bearing, and neither is visible from Python — so they are pinned here at the
    source level instead of being rediscovered in a browser.
    """
    body = read(APP_JS)
    assert "if (!state.userMoved) return;" in body, "widen must require real operator input"
    assert "range.from >= 0" in body, "widen must not trigger on the initial fit (from === 0)"
    assert "state.startFloor" in body, "widen must anchor on the earliest window displayed"
    assert "iso >= state.startFloor" in body, "a widen that narrows the window must be refused"
    assert "MAX_WIDENS = 5" in body, "unbounded widening is how you pull a year of 1m bars"


def test_chart_container_has_a_flex_parent() -> None:
    """Third smoke-run bug, pinned.

    ``.chart-wrap`` carries ``flex: 1``, but its parent was the ``<section>`` page, a
    plain block — so the flex had no flex parent to grow inside, the container computed
    to 0px tall, and the terminal rendered as a strip of chrome over an empty
    rectangle while nineteen canvases sat 2px high. The companion half of the fix is
    the ``:not([hidden])`` guard: an id-level ``display: flex`` outranks the UA
    stylesheet's ``[hidden] { display: none }``, and without it both pages render.
    """
    css = read(STYLE_CSS)
    assert "#view > .page:not([hidden])" in css, "hidden pages must still be hideable"
    assert re.search(r"#view > \.page \{[^}]*display: none", css), "a hidden page must not show"
    assert re.search(r"#view > \.page \{[^}]*flex-direction: column", css), (
        "a block page leaves .chart-wrap with no flex parent to grow inside"
    )
    assert re.search(r"\.chart-wrap \{[^}]*flex: 1 1 0", css)


def test_readout_is_redrawn_when_indicator_rows_land() -> None:
    """Second smoke-run bug, pinned.

    ``applyBars`` paints the OHLCV line, then ``applyIndicators`` replaces the rows it
    reads from. Without a re-render the line keeps showing the previous payload's
    values — or dashes, when a widen made the bar set longer than the row set.
    """
    body = read(APP_JS)
    tail = body.split("function applyIndicators")[1].split("\nfunction ")[0]
    assert "renderReadout(" in tail, "applyIndicators must repaint the readout"


def test_polling_is_poll_not_websocket() -> None:
    """The spec says no websockets in v1 and 30-60s poll; a socket would break both."""
    body = read(APP_JS)
    assert "POLL_MS = 45_000" in body
    assert "visibilitychange" in body, "refresh must pause when the tab is hidden"
    for banned in ("WebSocket", "EventSource", "new Worker"):
        assert banned not in body, f"{banned} is a v1 non-goal"


def test_frontend_only_calls_get_endpoints() -> None:
    """Every fetch in the frontend is a GET path under /api/ — no invented endpoints."""
    paths = set(re.findall(r"getJSON\(\s*[`\"]([^`\"]+)", read(APP_JS)))
    assert paths, "no endpoints referenced at all — the page would be decoration"
    for path in paths:
        assert path.startswith("/api/"), f"{path} is not an API path"
    roots = {path.split("?")[0] for path in paths}
    assert {"/api/health", "/api/bars", "/api/indicators", "/api/signals"} <= roots
