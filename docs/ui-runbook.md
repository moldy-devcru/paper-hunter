# paper-hunter terminal — operator runbook

The read-only research view of Operation Hunting Blind: candlestick terminal, arms, ledger,
hunt, calibration. Five tabs, one FastAPI process, one static SPA, no build chain.

- **Design spec:** [`ui-design.md`](ui-design.md) (what each page is for and why)
- **Window-start checklist (the operator's document):** [`ratification.md`](ratification.md) §(d)
- **Unit file:** [`../scripts/paper-hunter-ui.service`](../scripts/paper-hunter-ui.service)

---

## 1. Reaching it

```
http://<LAN-IP>:8478
```

Find the host's LAN IP:

```bash
hostname -I | awk '{print $1}'
```

On **yharnam** (the GPU/soak box that holds this checkout) that currently prints
`192.168.7.73` — the same subnet as shinralabs (`192.168.7.210`), so any other LAN host can
reach it. If the IP changes when the host gets a new DHCP lease, the URL changes with it;
that is expected and is why the address is documented as "find it", not "memorise it".

Same-host smoke test (does the process answer at all):

```bash
curl -s http://127.0.0.1:8478/api/health | head -c 400
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8478/
```

Second-host smoke test (does the bind and the network path actually work — this is the one
that catches a `127.0.0.1` bind and a firewall):

```bash
curl -s http://192.168.7.73:8478/api/health | head -c 200
```

### Port and exposure

`8478`, bound to `0.0.0.0` — i.e. every interface this host has. That is deliberate for a
LAN tool and it is only safe *as long as nothing else is reachable from outside*. Check
what is actually protecting you before assuming something is:

```bash
systemctl is-active nftables firewalld   # currently both inactive on yharnam
```

There is currently **no host firewall enabled on yharnam**, so the boundary is "whatever
can route to 192.168.7.0/24". If this host ever gets a routable interface (a public IP, a
docker bridge that forwards, a cloud metadata network), the exposure changes with it.

**There is no authentication on this service.** Nothing in the design hides it; it is a
research window on the operator's own LAN. If it ever needs to be reachable from outside
the LAN, the answer is auth **first** — a reverse proxy that terminates auth in front of
8478 (and this is exactly why the process binds a plain port with no TLS: it is not
designed to be the thing that faces the internet). Do not "just" forward the port; do not
"just" add a password in the UI; the app has no login surface and adding one to a
zero-non-GET-routes service means designing it properly, with the read-only assertions
still intact.

---

## 2. Installing and restarting

```bash
sudo cp scripts/paper-hunter-ui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now paper-hunter-ui.service
```

Restart after a code change:

```bash
sudo systemctl restart paper-hunter-ui.service
journalctl -u paper-hunter-ui.service -n 20
```

Stop / start / status:

```bash
sudo systemctl stop    paper-hunter-ui.service
sudo systemctl start   paper-hunter-ui.service
systemctl status paper-hunter-ui.service
```

`Restart=on-failure`, so a crash comes back on its own after 5s; a clean `stop` stays
stopped. Logs are plain journal lines under the `paper-hunter-ui` syslog identifier.

### Environment

**None is required.** The server starts with no credentials and serves the bar cache
cache-only: `ui.api.UISettings.allow_fetch` defaults to `False`, and
`ui.barcache.open_cache` only builds a live Alpaca client when `ALPACA_PAPER_KEY` and
`ALPACA_PAPER_SECRET` are present *and* fetching was asked for. The unit's
`EnvironmentFile=-/etc/paper-hunter/soak.env` is prefixed with `-` precisely because it is
optional: if the soak's credentials file happens to exist, the view can see the same two
variables; if it does not, nothing changes. Those credentials are paper-only, and the
terminal needs them for nothing it does today.

---

## 3. How it stays read-only

Three independent layers, so "we were careful" is a property rather than a promise:

1. **Every database is opened `file:…?mode=ro`.** `ui.api.open_ro` is the only way that
   module gets a connection; a write against one raises
   `sqlite3.OperationalError: attempt to write a readonly database`, and a test asserts
   that, so a future edit cannot quietly downgrade it to a normal `sqlite3.connect`.
2. **Zero non-GET routes.** `tests/test_ui_api.py::test_app_has_zero_non_get_routes` walks
   the route table and fails on any POST/PUT/PATCH/DELETE — or on any route with no
   method set at all, which is what a `StaticFiles` mount would produce. The static
   surface is therefore a list of named files, each with its own explicit GET route, in
   `ui.api.STATIC_MODULES`.
3. **The frontend can only GET.** All fetches go through `ui/static/net.js`, which refuses
   any path not under `/api/` and passes no body; the test suite greps every file in
   `ui/static/` for write mechanisms (explicit verbs, `XMLHttpRequest`, `sendBeacon`,
   editable fields, form submits).

And the unit adds a fourth, outside the app: `ProtectSystem=strict` with
`ReadWritePaths=/opt/paper-hunter/data`, so even a code-level bug cannot reach the
rulebook or the source tree.

No order placement, ever: the executor is a separate process and the UI is a window, not
a door. If you ever find yourself wanting the UI to place an order, the answer is a
different tool, not an endpoint.

---

## 4. Rebuilding the bar cache

`data/barcache.db` is the one mutable thing in the design, and it is a cache: a rebuildable
copy of a public feed that holds no experiment state. It lives inside the append-only
zone's filesystem but is explicitly outside the zone's *meaning* — if it is deleted, the
only thing lost is a copy of data that can be fetched again.

```bash
sudo systemctl stop paper-hunter-ui.service
rm /opt/paper-hunter/data/barcache.db     # prefer `mv` to a temp name over `rm` here
sudo systemctl start paper-hunter-ui.service
curl -s http://127.0.0.1:8478/api/health | python3 -m json.tool | head -20
```

The terminal refills it from the cache on the next request; to actively re-pull history it
must be given credentials *and* a request with `fetch=true` (`/api/bars?...&fetch=true`),
which the UI never sends. So a deleted cache is a genuinely empty chart until something
fills it — the page will say "no bars in the window" rather than draw a flat line, which
is the honest state.

There is no `cache-clear` button. A read-only service cannot delete its own cache, and
giving it one would have been the first write endpoint.

---

## 5. What "degraded" health means

`/api/health` reports `status: "degraded"` when a *database file is missing* — currently
that is `data/journal.db` and `data/ivrank.db` on a host where neither exists yet. That is
the expected pre-window state, **not** a broken server:

| field | meaning |
|---|---|
| `status: "ok"` | every expected database file is present and was opened `mode=ro` |
| `status: "degraded"` | at least one is missing; `dbs.<name>.present` says which, and `notes.<name>` says why |
| `dbs.barcache` | always present (`mode: "rw (cache, not a record)"`); `series` lists what is cached, `fetch_calls` counts live pulls |
| `feed_provenance` | `live_client: false` means cache-only, and `label` states SIP vs IEX-partial |

The pages render their empty states from the same facts rather than erroring: the terminal
overlays "no bars in the window", the Ledger shows zero rows, the Calibration page says how
many sessions the soak has written (zero) and what that means for the forming P90.

A `degraded` that becomes `ok` is the goal of the soak's first run; a `degraded` that
appears *after* it was `ok` means a database was moved or deleted — that one is worth
looking at.

---

## 6. Calibration page, specifically

The page is a status view, not a control surface. Four panels:

- **Per-session deep-OTM volume** — call/put bars per session, the trailing
  20-session mean as a line, and the P90 as a rule. With 1–2 sessions the P90 banner says
  *forming* and states how many sessions are missing. The P90 number is the server's own
  (`ui.api._percentile`, linear interpolation); the page never recomputes a competing one.
- **Threshold-distance histogram** — the 1pp buckets the soak stores, calls over puts, with
  the threshold the soak actually applied (from `flow_baseline.deep_otm_threshold_pct`)
  marked. Until the first session exists, it marks the working 3% and says so; the working
  value is the *input* to calibration, never a frozen threshold.
- **IV history** — one line per `underlying|tenor_key`, with the VIX proxy seed drawn
  **dashed and in a different colour** from real chain observations, and the seam between
  them reported explicitly. The ratification's VIX-proxy reconciliation item is the reason:
  a proxy row must never be readable as a SPY rank.
- **Window-start checklist** — read-only, rendered from what the store can actually prove.
  Items that need a human (which tenor T5 reads, the sign-off, the document freeze) are
  shown as not assessable rather than ticked, because a checklist that renders green ticks
  from six numbers would be read as clearance. Nothing on this page can be checked off.

The VIX warmup threshold, the working 3%, and `MIN_OBSERVATIONS = 60` are mirrored as
constants in `ui/static/model.js` purely for labelling. They are documented there as
mirrors of `executor.soak.DEFAULT_DEEP_OTM_PCT` and `executor.iv_rank.MIN_OBSERVATIONS`,
and the value actually applied is read from the store whenever the store has one.

---

## 7. Tests

```bash
.venv/bin/python -m pytest -q          # everything, offline
.venv/bin/python -m pytest tests/test_ui_static.py tests/test_static_logic.py tests/test_ui_deploy.py -q
```

`tests/test_ui_deploy.py` covers the things that must not rot quietly: the unit file exists,
parses as an INI, and carries the hardening directives and the right bind/port; the runbook
has its required sections; every ES module has a route and no CDN reference. All of it
runs offline — no server is started, no port is bound, no unit is installed.