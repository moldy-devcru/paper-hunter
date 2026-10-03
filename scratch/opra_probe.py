"""OPRA 403 characterisation probe — 2026-10-03.

Investigation only. No production code touched. Credentials are read in-process from
~/.config/paper-hunter/alpaca.env and never printed, never written, never passed as
argv. Every request is appended to scratch/opra_probe_timeline.jsonl with endpoint,
params-minus-secrets, status, response body (truncated) and timestamp.

Budget is HARD-CAPPED at 25 API calls. The counter refuses the 26th.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ENV_PATH = Path.home() / ".config/paper-hunter/alpaca.env"
LOG_PATH = Path(__file__).with_name("opra_probe_timeline.jsonl")
DATA_BASE = "https://data.alpaca.markets"
TRADING_BASE = "https://paper-api.alpaca.markets"
BUDGET = 25

_calls = 0


def _creds() -> tuple[str, str]:
    key = secret = None
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith("ALPACA_PAPER_KEY="):
            key = line.split("=", 1)[1].strip().strip("'\"")
        elif line.startswith("ALPACA_PAPER_SECRET="):
            secret = line.split("=", 1)[1].strip().strip("'\"")
    if not key or not secret:
        raise SystemExit("credentials not found in expected env file")
    return key, secret


KEY, SECRET = _creds()
HEADERS = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET}


def probe(tag: str, base: str, path: str, params: dict | None = None) -> dict:
    """One budgeted API call. Returns a record; never raises on HTTP error."""
    global _calls
    if _calls >= BUDGET:
        raise SystemExit(f"probe budget exhausted ({BUDGET}); refusing call {tag}")
    _calls += 1

    url = f"{base}{path}"
    if params:
        clean = {k: str(v) for k, v in params.items() if v is not None}
        url = f"{url}?{urllib.parse.urlencode(clean)}"

    started = time.monotonic()
    rec = {
        "n": _calls,
        "tag": tag,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "endpoint": path,
        "params": {k: ("<100 symbols>" if k == "symbols" else v) for k, v in (params or {}).items()},
    }
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "paper-hunter-opra-probe/0.1")
    for name, value in HEADERS.items():
        req.add_header(name, value)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
            rec["status"] = resp.status
            rec["rate_headers"] = {
                k: v for k, v in resp.headers.items() if "rate" in k.lower() or "retry" in k.lower()
            }
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict) and "bars" in parsed:
                    bars = parsed.get("bars") or {}
                    rec["result"] = f"200 symbols_with_data={len(bars)} next_page={bool(parsed.get('next_page_token'))}"
                elif isinstance(parsed, dict) and "snapshots" in parsed:
                    snaps = parsed.get("snapshots") or {}
                    rec["result"] = f"200 snapshots={len(snaps)}"
                    for _u, chain in list(snaps.items())[:1]:
                        if isinstance(chain, dict):
                            rec["chain_contracts"] = len(chain.get("snapshots") or chain.get("contracts") or [])
                elif isinstance(parsed, dict) and "id" in parsed:
                    rec["result"] = f"200 account id={parsed.get('id')[:8]}... status={parsed.get('status')}"
                else:
                    rec["result"] = "200 " + ",".join(list(parsed)[:6]) if isinstance(parsed, dict) else "200"
            except json.JSONDecodeError:
                rec["result"] = "200 non-json " + body[:120]
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        rec["status"] = exc.code
        rec["rate_headers"] = {
            k: v for k, v in exc.headers.items() if "rate" in k.lower() or "retry" in k.lower()
        }
        rec["body_raw"] = raw[:400]
        try:
            rec["body_json"] = json.loads(raw)
        except json.JSONDecodeError:
            pass
        rec["result"] = f"{exc.code} {raw[:200]}"
    except Exception as exc:  # transport failure
        rec["status"] = 0
        rec["result"] = f"transport: {type(exc).__name__}: {exc}"

    rec["elapsed_s"] = round(time.monotonic() - started, 2)
    with LOG_PATH.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(
        f"[{rec['n']:02d}] {rec['ts']} {tag:<28} {rec['endpoint']:<34} "
        f"-> {rec['status']} {rec.get('result','')[:110]}"
    )
    return rec


def ladder_symbols() -> list[str]:
    """A realistic backfill chunk: near-the-money SPY contracts, ±0.5% around 700."""
    out = []
    for strike in range(696, 705):
        for right in ("C", "P"):
            out.append(f"SPY260918{right}{strike:08d}")
    return out


if __name__ == "__main__":
    mode = os.environ.get("PROBE_MODE", "matrix")
    if mode == "matrix":
        # 1 — options/bars, the smallest known-good shape from the depth probe.
        probe(
            "M1/options-bars-1sym",
            DATA_BASE,
            "/v1beta1/options/bars",
            {
                "symbols": "SPY260918C00700000",
                "timeframe": "1Day",
                "start": "2026-06-15",
                "end": "2026-09-18T23:59:59Z",
                "limit": 10000,
            },
        )
        # 2 — a DIFFERENT options endpoint, same credential.
        probe(
            "M2/options-snapshots", DATA_BASE, "/v1beta1/options/snapshots/SPY",
            {"feed": "indicative", "limit": 2},
        )
        # 3 — equities, same credential, different product entirely.
        probe(
            "M3/stocks-bars-iex", DATA_BASE, "/v2/stocks/SPY/bars",
            {"timeframe": "1Day", "start": "2026-09-01", "end": "2026-09-18", "limit": 10},
        )
        # 4 — account, proves the credential is live and what account class it is.
        probe("M4/account", TRADING_BASE, "/v2/account")
        # 5 — the backfill-shaped chunk (100 symbols, 100-day window).
        probe(
            "M5/options-bars-100sym", DATA_BASE, "/v1beta1/options/bars",
            {
                "symbols": ",".join(ladder_symbols()),
                "timeframe": "1Day",
                "start": "2026-06-15",
                "end": "2026-09-30T23:59:59Z",
                "limit": 10000,
            },
        )

    if mode == "burst":
        # The EXACT chunk 0 the backfill requests, repeated fast. If a burst allowance
        # exists, this is where it shows.
        syms = json.load(open(Path(__file__).with_name("opra_ladder_symbols.json")))[:100]
        for i in range(int(os.environ.get("BURST_N", "15"))):
            probe(
                f"B{i:02d}/chunk0-replay", DATA_BASE, "/v1beta1/options/bars",
                {
                    "symbols": ",".join(syms),
                    "timeframe": "1Day",
                    "start": "2026-06-15",
                    "end": "2026-09-30T23:59:59Z",
                    "limit": 10000,
                },
            )

    if mode == "cooldown":
        syms = json.load(open(Path(__file__).with_name("opra_ladder_symbols.json")))[:100]
        args = {
            "symbols": ",".join(syms), "timeframe": "1Day",
            "start": "2026-06-15", "end": "2026-09-30T23:59:59Z", "limit": 10000,
        }
        probe("C1/first", DATA_BASE, "/v1beta1/options/bars", args)
        wait = int(os.environ.get("COOLDOWN_S", "210"))
        print(f"... idling {wait}s ...")
        time.sleep(wait)
        probe("C2/after-cooldown", DATA_BASE, "/v1beta1/options/bars", args)
    if mode == "shape":
        # C1 — a different options DATA endpoint, same credential.
        probe("S1/options-trades", DATA_BASE, "/v1beta1/options/trades",
              {"symbols": "SPY260918C00750000", "start": "2026-09-01T00:00:00Z",
               "end": "2026-09-18T23:59:59Z", "limit": 5})
        # C2 — the literal OPRA feed param: is the OPRA *feed* entitlement distinct?
        probe("S2/options-bars-feed-opra", DATA_BASE, "/v1beta1/options/bars",
              {"symbols": "SPY260918C00750000", "timeframe": "1Day", "feed": "opra",
               "start": "2026-06-15", "end": "2026-09-18T23:59:59Z", "limit": 100})
        # C3 — OPRA snapshot feed (docs call indicative the OPRA-derived Basic feed).
        probe("S3/snapshots-feed-opra", DATA_BASE, "/v1beta1/options/snapshots/SPY",
              {"feed": "opra", "limit": 2})
        # C4 — CONTROL: the known-honest 403. Proves the harness still surfaces a 403,
        # so "no 403 observed above" is evidence and not a blind probe.
        probe("S4/CONTROL-stocks-sip-recent", DATA_BASE, "/v2/stocks/SPY/bars",
              {"timeframe": "1Day", "feed": "sip", "limit": 5})
        # C5 — same 100-symbol chunk as the burst, but 410-day window (the shape run 1 used).
        probe("S5/chunk0-410day-window", DATA_BASE, "/v1beta1/options/bars",
              {"symbols": ",".join(json.load(open(Path(__file__).with_name(
                  "opra_ladder_symbols.json")))[:100]),
               "timeframe": "1Day", "start": "2025-09-01", "end": "2026-09-30T23:59:59Z",
               "limit": 10000})

    if mode == "repro":
        probe("R1/opra-snapshot-REPEAT", DATA_BASE, "/v1beta1/options/snapshots/SPY",
              {"feed": "opra", "limit": 2})

    if mode == "rootcause":
        # THE decisive A/B: the backfill's exact request shape, with `end` as the only
        # variable. Today-end (what backfill_iv_rank.py sends) vs 20 minutes ago (what
        # Alpaca says Basic is entitled to). Both are the same chunk0, same credentials.
        syms = ",".join(json.load(open(Path(__file__).with_name(
            "opra_ladder_symbols.json")))[:100])
        today = datetime.now(timezone.utc).date()
        probe("X1/options-bars-END=TODAY-2359 (backfill shape)", DATA_BASE,
              "/v1beta1/options/bars",
              {"symbols": syms, "timeframe": "1Day",
               "start": "2026-06-15", "end": f"{today.isoformat()}T23:59:59Z", "limit": 10000})
        stale = (datetime.now(timezone.utc) - __import__("datetime").timedelta(minutes=20))
        probe("X2/options-bars-END=now-20min (same chunk0)", DATA_BASE,
              "/v1beta1/options/bars",
              {"symbols": syms, "timeframe": "1Day",
               "start": "2026-06-15", "end": stale.isoformat(timespec="seconds").replace("+00:00", "Z"),
               "limit": 10000})
