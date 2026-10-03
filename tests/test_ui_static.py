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

U3 widened the grep-based assertions from three files to every file in ``ui/static/``,
which is the point of splitting the frontend into ES modules: a guarantee that was
expressed as "app.js contains no URL" silently stopped meaning anything the moment the
code it described moved into a sibling file. Behavioural coverage of the pure logic
lives in ``tests/test_static_logic.py``; what is pinned here is the wiring — the handlers,
the ids, the routes — because wiring needs a document to be meaningful.

Everything runs offline: no server is started, no network is touched.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ui.api import STATIC_MODULES, app

STATIC = Path(__file__).resolve().parent.parent / "ui" / "static"
INDEX_HTML = STATIC / "index.html"
APP_JS = STATIC / "app.js"
STYLE_CSS = STATIC / "style.css"
VENDOR_DIR = STATIC / "vendor"
VENDOR_JS = VENDOR_DIR / "lightweight-charts.standalone.production.js"
PROVENANCE = VENDOR_DIR / "PROVENANCE.md"

#: Every JavaScript file we author. U3 added seven modules; the LAN-offline and
#: read-only guarantees are asserted over the whole set, not over a sample.
JS_FILES = sorted(STATIC.glob("*.js"))
MODULES = [path for path in JS_FILES if path.name != "app.js"]

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


@pytest.mark.parametrize("route", [route for route, _ in STATIC_MODULES])
def test_every_es_module_is_served(client: TestClient, route: str) -> None:
    """One explicit GET route per module. A module with no route is a 404 in the browser
    and a blank page, and a module that is a directory walk would be a hole in the
    zero-non-GET-routes assertion — so the list lives in ui/api.py and is tested here."""
    response = client.get(route)
    assert response.status_code == 200, f"{route} is not served"
    assert "javascript" in response.headers["content-type"]
    assert response.text.strip(), f"{route} is empty"


def test_every_module_on_disk_has_a_route() -> None:
    """The reverse direction: a new file that nobody routed is dead weight in the tree
    and a 404 waiting to happen."""
    routed = {path.name for _, path in STATIC_MODULES}
    on_disk = {path.name for path in MODULES}
    assert on_disk - routed == set(), f"unrouted modules: {sorted(on_disk - routed)}"
    assert routed - on_disk == set(), f"routes point at missing files: {sorted(routed - on_disk)}"


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


@pytest.mark.parametrize("path", [INDEX_HTML, STYLE_CSS, *JS_FILES], ids=lambda p: p.name)
def test_no_cdn_references_in_served_assets(path: Path) -> None:
    """A LAN tool that fetches from a CDN is broken the moment the internet is not."""
    hits = EXTERNAL.findall(read(path))
    # The Apache banner URL lives in the vendored bundle, not in files we author, and
    # the files we author must carry no URL at all — including in comments.
    assert hits == [], f"{path.name} references an external resource: {hits}"


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_no_write_verbs_in_the_frontend(path: Path) -> None:
    """The UI has no door: nothing in the frontend can issue a write.

    U2 asserted this by grepping for quoted ``'POST'``/``'PUT'``/... which was fine when
    the only frontend file was a chart, and wrong the moment the Hunt page appeared:
    ``DIRECTION_ORDER = ["call", "put"]`` is a list of OPTION DIRECTIONS, and the naive
    grep called it a PUT. The property worth pinning is the mechanism, not the word —
    an explicit HTTP verb, a request body, a beacon, or an editable field — so that is
    what this asserts now.
    """
    text = read(path)
    for pattern in (
        r"method\s*:",
        r"XMLHttpRequest",
        r"navigator\.sendBeacon",
        r"contenteditable",
        r"\.submit\(",
    ):
        assert re.search(pattern, text) is None, f"{path.name} can write: matched {pattern!r}"
    # The only fetch in the stack is net.js's, and it passes no body — asserted there
    # rather than here, since a `body:` key elsewhere is prose (the placeholder text for
    # a not-yet-built page literally says "body:").


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
    assert f'"page-{page}"' in html or page == "terminal", f"{page} has no section"


def test_every_spec_page_is_built_and_the_placeholder_markup_is_gone() -> None:
    """U2 built the terminal, U3 arms/ledger/hunt, U4 calibration. All five are real pages
    now, so PAGES is a plain list and the phase/placeholder markup has been deleted with
    it: a "not built yet" badge that can never render is a place for a stale claim to
    live, and the calibration page must fail HERE if it ever regresses to one."""
    body = read(APP_JS)
    match = re.search(r"const PAGES = \[(.*?)\];", body, re.DOTALL)
    assert match, "PAGES is no longer the page list"
    assert tuple(re.findall(r'"([^"]+)"', match.group(1))) == PAGES
    for page in PAGES:
        assert f"page === \"{page}\"" in body, f"{page} has no branch in route()"
        assert f'id="page-{page}"' in read(INDEX_HTML), f"{page} has no section"
    html = read(INDEX_HTML)
    assert "page-placeholder" not in html and "ph-body" not in html
    assert 'phase: "U4"' not in body, "the last placeholder claim must not survive U4"
    assert "page-placeholder" not in body and "ph-title" not in body, (
        "route() must not carry a dead placeholder branch"
    )


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
    """Every fetch in the frontend is a GET path under /api/ — no invented endpoints.

    Scanned across ALL modules since U3: this assertion used to read app.js alone, which
    would have kept passing while arms.js and hunt.js quietly grew their own callers.
    """
    paths: set[str] = set()
    for path in JS_FILES:
        text = read(path)
        # Both call styles are scanned: getJSON("/api/...") directly, and the entries of
        # a getAll({plan: "/api/...", ...}) batch. Matching only the first would have
        # missed every U3 page, since all three load their panels in one batch.
        paths |= set(re.findall(r"getJSON\(\s*[`\"]([^`\"]+)", text))
        paths |= set(re.findall(r"[`\"](/api/[a-z]+)", text))
    assert paths, "no endpoints referenced at all — the page would be decoration"
    for path in paths:
        assert path.startswith("/api/"), f"{path} is not an API path"
    roots = {path.split("?")[0] for path in paths}
    assert {"/api/health", "/api/bars", "/api/indicators", "/api/signals"} <= roots
    # The three U3 pages are pinned by name so a renamed endpoint is a failure here.
    assert {"/api/arms", "/api/trades", "/api/huntplan", "/api/noshots", "/api/histogram"} <= roots


def test_the_fetch_helper_refuses_a_non_api_path() -> None:
    """Read-only enforcement, frontend half: the one fetch helper checks its argument."""
    net = read(STATIC / "net.js")
    assert 'if (typeof path !== "string" || !path.startsWith("/api/"))' in net
    assert "fetch(path, { headers:" in net, "the only fetch in the stack must be a bare GET"
    assert re.search(r"fetch\([^)]*\b(method|body)\s*:", net) is None, (
        "no fetch call in the stack may name a method or a body"
    )


# ---------------------------------------------------------------------------
# 5. U3 page wiring
# ---------------------------------------------------------------------------


def test_marker_click_opens_a_popup_instead_of_logging() -> None:
    """The U2 placeholder was a console.log. U3 replaces it with the anchored popup, and
    the popup's dismissal is wired exactly once at startup."""
    body = read(APP_JS)
    click = body.split("function onClick")[1].split("\nfunction ")[0]
    assert "openMarkerPopup(" in click, "a marker click must open the popup"
    assert "console.log" not in click, "the U2 placeholder must be gone"
    assert "closeMarkerPopup()" in click, "clicking empty chart space closes the popup"
    assert "wireMarkerPopupDismissal()" in body


def test_the_popup_closes_on_escape_and_outside_click() -> None:
    markers = read(STATIC / "markers.js")
    assert 'event.key === "Escape"' in markers
    assert "mousedown" in markers
    assert "node.contains(event.target)" in markers, (
        "the click that opened the popup must not immediately close it"
    )
    assert "isMarkerPopupOpen()" in markers, "dismissal handlers must no-op when nothing is open"


def test_the_popup_joins_noshots_client_side_by_journal_id() -> None:
    """NO-SHOT rows are not decisions, so /api/signals does not carry them. The join is
    `noshots.counterfactual_entry_ref -> decisions.id` and it happens in the browser."""
    body = read(APP_JS)
    assert 'getAll({ noshots: `/api/noshots?${symbolAndWindow()}&limit=2000` })' in body
    assert "applySignals(signals, noshots.data)" in body, "both payloads reach one renderer"
    assert "indexNoshots(sightings)" in body and "noshotsForDecision(state.noshotIndex" in body
    assert "NO-SHOT" in body, "near-misses are labelled on the chart, not implied"


def test_the_arms_page_keeps_the_exception_path_out_of_the_arm_numbers() -> None:
    arms = read(STATIC / "arms.js")
    assert '["A", "B", "C"]' in arms, "the three cards are A/B/C"
    assert "exceptionBlock(" in arms, "the exception path gets its own block"
    assert "never summed into A/B/C" in arms, "and says so on the page, not only in a comment"
    assert "mismatchesByArm" in arms, "mismatch flags are rendered"
    assert "no_bankroll" not in arms.split("function card(")[0], "flag text comes from the endpoint"


def test_open_positions_are_never_marked_to_market() -> None:
    """The journal holds no marks. A `last`/`unrealized` cell that computed a number from
    the entry price alone would be a fabricated P&L, so both render as dashes with the
    reason in the tooltip, and the view model refuses to carry a mark at all."""
    arms = read(STATIC / "arms.js")
    assert '<td class="r num muted">${DASH}</td>' in arms, "last and unrealized render as dashes"
    model = read(STATIC / "model.js")
    assert "last: null" in model and "unrealized: null" in model
    assert "not computed" in model
    positions = arms.split("const positions = ")[1].split("const vsControl")[0]
    assert "unreal" in positions.lower()
    for banned in ("* position.qty", "position.entry *", "pxOf(position.entry) *"):
        assert banned not in positions, f"the positions table must not derive P&L: {banned}"


def test_the_ledger_page_has_no_edit_affordance() -> None:
    """Append-only, and the page says so. No form posts, no editable cell, no PUT."""
    ledger = read(STATIC / "ledger.js")
    assert "correction of #" in ledger, "references render as correction-of links"
    assert "referenceChain(" in ledger
    for banned in ("contenteditable", "PUT", "PATCH", "DELETE", "onblur"):
        assert banned not in ledger, f"the ledger has no {banned}"
    html = read(INDEX_HTML)
    for field in ("ledger-q", "ledger-arm", "ledger-kind", "ledger-from", "ledger-to"):
        assert f'id="{field}"' in html, f"the {field} control is missing"


def test_the_hunt_page_dates_itself_and_renders_all_three_panels() -> None:
    html = read(INDEX_HTML)
    for node in ("hunt-date", "hunt-plan", "hunt-noshots", "hunt-histogram"):
        assert f'id="{node}"' in html, f"the hunt page is missing #{node}"
    hunt = read(STATIC / "hunt.js")
    assert "America/New_York" in hunt, "the date picker defaults to the ET session, not UTC"
    assert "/api/huntplan?date=" in hunt and "/api/noshots?from=" in hunt
    assert "/api/histogram?veto=weekly" in hunt
    assert "allSettled" in read(STATIC / "net.js"), (
        "one dead panel must not take the hunt page down with it"
    )


def test_the_veto_histogram_is_plain_divs_and_says_why() -> None:
    """The spec allowed lightweight-charts or plain SVG/divs. The choice is divs, and it
    is documented in the module rather than left as an unexplained difference."""
    model = read(STATIC / "model.js")
    assert "Rendered as plain stacked DIVs" in model
    assert "flex:${segment.value}" in read(STATIC / "hunt.js"), "segments are flex-proportioned"
    assert 'class="hbar"' in read(STATIC / "hunt.js")


# ---------------------------------------------------------------------------
# 6. U4: the calibration page wiring
# ---------------------------------------------------------------------------


def test_the_calibration_page_has_all_four_panels() -> None:
    """Four spec'd panels, four ids. A panel that lost its container renders nothing and
    reports nothing, which is the failure mode a page most needs to avoid."""
    html = read(INDEX_HTML)
    for node in ("calib-flow", "calib-distance", "calib-iv", "calib-checklist", "calib-stamp"):
        assert f'id="{node}"' in html, f"the calibration page is missing #{node}"


def test_the_calibration_page_reads_one_endpoint_and_starts_stops_cleanly() -> None:
    """One endpoint, one poll, start/stop pair — the same contract arms.js and hunt.js
    follow, so a hidden page is not polling the journal behind the operator's back."""
    calib = read(STATIC / "calibration.js")
    app = read(APP_JS)
    assert 'getJSON("/api/calibration")' in calib
    assert "/api/" in calib and "fetch(" not in calib, "all fetching goes through net.js"
    for symbol in ("startCalibration", "stopCalibration"):
        assert f"export function {symbol}" in calib
    assert "from \"./calibration.js\"" in app
    route = app.split("function route")[1].split("\nfunction ")[0]
    assert "stopCalibration()" in route, "leaving the page must stop its poll"
    assert "startCalibration()" in route, "entering the page must start it"
    # Charts are canvas objects with a lifecycle; leaking them on every poll is the bug
    # arms.js already handles, so the same teardown has to exist here.
    assert "teardownCharts()" in calib and "chart.remove()" in calib


def test_the_calibration_page_uses_the_pure_view_models() -> None:
    """The arithmetic lives in model.js so Node can score it (tests/test_static_logic.py).
    A renderer that recomputed a percentile or a mean inline would be a second definition
    of a number T6's threshold is frozen from, and it would be untested."""
    calib = read(STATIC / "calibration.js")
    for fn in ("flowVolume", "distanceHistogram", "ivHistory", "windowChecklist"):
        assert f"{fn}(" in calib, f"the page must render through model.{fn}"
    model = read(STATIC / "model.js")
    for fn in ("percentile", "percentileSeries", "trailingMean"):
        assert f"export function {fn}" in model
    # The page must not compute a competing P90 of its own.
    assert "Math.pow" not in calib and ".sort((a, b) => a - b)" not in calib


def test_the_calibration_page_states_the_forming_p90_gap_in_words() -> None:
    """The spec's requirement in its own words: with 1-2 rows, show what exists AND the
    honest 'needs N more sessions' state. A green chart over two sessions is the failure."""
    calib = read(STATIC / "calibration.js")
    assert "needs ${model.needs} more" in calib
    assert "forming" in calib
    assert "model.complete" in calib, "the complete case has to be reachable"
    # UX pass 1 reworded the empty state into a one-liner ("No flow_baseline sessions
    # yet" + a "?" disclosure), so the pin follows the new capitalisation. The property
    # being asserted is unchanged: the empty case still names what is missing.
    assert "No flow_baseline sessions yet" in calib, "and the empty case has to say so"


def test_the_calibration_page_separates_proxy_from_real_in_colour_and_style() -> None:
    """Provenance honesty is the whole point of the IV panel, and it has to survive a
    monochrome print: dashed AND a different colour, not one of the two."""
    charts = read(STATIC / "charts.js")
    assert "PROVENANCE_COLOR = { real:" in charts
    assert "lineStyle: segment.provenance === \"proxy\" ? 2 : 0" in charts, (
        "the proxy segment must be a different line style, not only a different colour"
    )
    assert "segment.provenance" in charts and "segments" in charts, (
        "one line per segment: a single series would interpolate across the seam"
    )
    calib = read(STATIC / "calibration.js")
    assert "ivHistory(" in calib
    assert "guardConflicts" in calib, "guard disagreements are a finding and are rendered"
    assert "dashed" in calib, "the legend marks proxy segments as dashed too"


def test_the_calibration_page_marks_the_threshold_it_actually_applied() -> None:
    """From the store when there is a session (deep_otm_threshold_pct), the working 3%
    otherwise — and it says which, because the working value is the INPUT to calibration
    and never a frozen threshold."""
    calib = read(STATIC / "calibration.js")
    assert "deep_otm_threshold_pct" in calib
    assert "WORKING_DEEP_OTM_PCT" in calib
    assert "no session recorded yet" in calib
    assert "threshold" in calib and "▲ threshold" in calib


def test_the_window_checklist_is_read_only_and_claims_nothing() -> None:
    """Every item carries a state and a reason, the items that need a human are rendered
    as not-assessable rather than ticked, and there is no affordance that could tick
    anything: this page reports the operator's document, it does not edit it."""
    calib = read(STATIC / "calibration.js")
    assert "windowChecklist(payload)" in calib
    assert "CHECK_GLYPH" in calib and "todo" in calib and "unknown" in calib
    assert "read-only status view" in calib
    assert "can be checked off from this screen" in calib
    for banned in ("contenteditable", "onclick", ".submit("):
        assert banned not in calib, f"the checklist is read-only, found {banned!r}"
    assert "pending_calibrations" in calib, (
        "the checklist is rendered from the rulebook's own pending list, not a hardcoded one"
    )


def test_the_mirrored_constants_are_declared_as_mirrors() -> None:
    """WORKING_DEEP_OTM_PCT and MIN_IV_OBSERVATIONS exist so the page can label something
    before the store has a row to read it from. They must name their source, or a future
    edit treats them as a second source of truth for a frozen threshold."""
    model = read(STATIC / "model.js")
    assert "executor.soak.DEFAULT_DEEP_OTM_PCT" in model
    assert "executor.iv_rank.MIN_OBSERVATIONS" in model
    assert "export const WORKING_DEEP_OTM_PCT = 3;" in model
    assert "export const MIN_IV_OBSERVATIONS = 60;" in model
    assert "ui.api._percentile" in model, "the percentile must name the server function it mirrors"


def test_the_calibration_module_is_routed_and_has_no_cdn_reference() -> None:
    """The generic guarantees, restated for the new file because a new file is exactly
    where they would stop holding: routed (a module with no route is a 404), served, and
    free of any external reference."""
    routes = {route for route, _ in STATIC_MODULES}
    assert "/calibration.js" in routes
    text = read(STATIC / "calibration.js")
    assert EXTERNAL.findall(text) == []
    for pattern in (r"method\s*:", r"XMLHttpRequest", r"navigator\.sendBeacon", r"\.submit\("):
        assert re.search(pattern, text) is None, f"calibration.js can write: {pattern!r}"
