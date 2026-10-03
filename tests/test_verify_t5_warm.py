"""``scripts/verify_t5_warm.py`` — the T5 warm-up monitor.

Built on a **fixture store** rather than a hand-written sqlite file, because the thing
most worth protecting is that the monitor measures through the *production* reader: if a
future change to ``executor.iv_rank`` alters what counts as an observation (the R9
live+backfill splice, the proxy exclusion, the lookback window), this monitor has to move
with it rather than quietly reporting a stale definition of "warm".
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from executor.iv_rank import (
    MIN_OBSERVATIONS,
    IvObservation,
    IvRankStore,
    moneyness_bucket,
    moneyness_tenor_key,
)

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_t5_warm.py"


def _load():
    """Import the script as a module without installing it as a package.

    Registered in ``sys.modules`` BEFORE execution: the script uses ``@dataclass``, and
    dataclasses resolves string annotations by looking the defining module up there. An
    unregistered module_from_spec loads fine until the first decorator runs, then dies
    with a confusing AttributeError that has nothing to do with the script.
    """
    spec = importlib.util.spec_from_file_location("_verify_t5_warm", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load()


def _key(dte: int, side: str = "call", mny: float = 0.0) -> str:
    """Build a ruled-schema key with the PRODUCTION key function."""
    return moneyness_tenor_key(dte=dte, right=side, moneyness_bucket=mny)


def _obs(key: str, as_of: dt.date, iv: float = 0.20, origin: str = "live") -> IvObservation:
    return IvObservation(
        underlying="SPY",
        as_of=as_of.isoformat(),
        tenor_key=key,
        iv=iv,
        source="alpaca_chain" if origin == "live" else "bars_bs_inversion",
        is_proxy=False,
        expiry="20270116",
        right="call",
        strike=650.0,
        origin=origin,
    )


def _store(tmp_path: Path, rows) -> Path:
    """Write a real IV store through the real writer, then hand back its path."""
    db = tmp_path / "ivrank.db"
    store = IvRankStore.open(db)
    try:
        if rows:
            store.record_many(rows)
    finally:
        store.close()
    return db


def _daily(
    count: int,
    end: dt.date = dt.date(2026, 10, 3),
    *,
    key: str,
    origin: str = "live",
) -> list[IvObservation]:
    """``count`` consecutive sessions of one key, newest at ``end``.

    ``origin`` is threaded through to ``_obs`` on purpose: it selects the ``source``
    value too, and ``UNIQUE (underlying, as_of, tenor_key, source)`` means two origins
    over the same dates are two rows. A helper that dropped it would silently collapse
    the live and backfilled halves into one.
    """
    return [
        _obs(key, end - dt.timedelta(days=i), iv=0.18 + 0.001 * i, origin=origin)
        for i in range(count)
    ]


def _run(mod, argv) -> int:
    return mod.main(argv)


# ---------------------------------------------------------------------------
# the verdict, which is the whole contract of the script
# ---------------------------------------------------------------------------


def test_cold_store_is_not_warm_and_exits_1(mod, tmp_path, capsys):
    db = _store(tmp_path, [_obs(_key(91), dt.date(2026, 10, 1))])
    assert _run(mod, ["--db", str(db)]) == 1
    assert "VERDICT: NOT WARM" in capsys.readouterr().out


def test_a_single_warm_key_makes_both_listed_arms_pass(mod, tmp_path, capsys):
    """Both bands need a key: arm C's (90-180) and arm B's (0).

    Arm B is band 0, so only a `mte0-…` key can ever satisfy it. That arm is cold by
    construction in production (the backfill refuses 0-DTE — vega is zero at expiry), but
    the monitor must still measure it honestly rather than hiding a band it cannot satisfy.
    """
    rows = _daily(MIN_OBSERVATIONS, key=_key(91)) + _daily(
        MIN_OBSERVATIONS, key=_key(0)
    )
    db = _store(tmp_path, rows)
    assert _run(mod, ["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "VERDICT: WARM" in out
    assert "PASS" in out


def test_one_observation_below_the_floor_is_still_not_warm(mod, tmp_path, capsys):
    """The boundary is the whole point: 59 of 60 is still PENDING, and T5 blocks on it."""
    rows = _daily(MIN_OBSERVATIONS - 1, key=_key(91)) + _daily(
        MIN_OBSERVATIONS, key=_key(0)
    )
    db = _store(tmp_path, rows)
    assert _run(mod, ["--db", str(db)]) == 1
    out = capsys.readouterr().out
    assert f"{MIN_OBSERVATIONS - 1}/{MIN_OBSERVATIONS}" in out
    assert "NOT WARM" in out


def test_a_missing_store_is_not_warm_rather_than_an_error(mod, tmp_path, capsys):
    assert _run(mod, ["--db", str(tmp_path / "absent.db")]) == 1
    assert "NOT WARM" in capsys.readouterr().out


def test_min_observations_override_cannot_make_the_verdict_pass(mod, tmp_path, capsys):
    """A what-if floor is for the per-key table. The VERDICT always uses production.

    Without this, `--min-observations 2` would make the monitor report a cold store as
    warm, which is the exact failure mode the script exists to prevent. (The store's own
    floor is >= 2, so 2 is the smallest override that is legal at all.)
    """
    db = _store(tmp_path, [_obs(_key(91), dt.date(2026, 10, 1))])
    assert _run(mod, ["--db", str(db), "--min-observations", "2"]) == 1
    assert "WHAT-IF" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# per-key reporting
# ---------------------------------------------------------------------------


def test_counts_are_reported_per_key_not_aggregated(mod, tmp_path, capsys):
    """The failure this script exists to prevent: a row count read as a warmup progress bar."""
    rows = [_obs(_key(91), dt.date(2026, 10, 3) - dt.timedelta(days=i)) for i in range(30)]
    rows += [_obs(_key(98), dt.date(2026, 10, 3) - dt.timedelta(days=i)) for i in range(30)]
    db = _store(tmp_path, rows)
    _run(mod, ["--db", str(db)])
    out = capsys.readouterr().out
    assert _key(91) in out and _key(98) in out
    # 60 rows total, but neither key reaches the floor.
    assert "30" in out
    assert "NOT WARM" in out


def test_legacy_keys_are_surfaced_and_do_not_count(mod, tmp_path, capsys):
    """R7/R8 orphans must be visible, and must not be mistaken for warmup progress."""
    rows = [_obs("dte42-call-770.00", dt.date(2026, 10, 1))]
    rows += [_obs("20270116-call-650.00", dt.date(2026, 10, 1))]
    rows += _daily(MIN_OBSERVATIONS, key=_key(91)) + _daily(MIN_OBSERVATIONS, key=_key(0))
    db = _store(tmp_path, rows)
    assert _run(mod, ["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "legacy keys" in out
    assert "Orphaned by design" in out
    assert "2" in out


def test_legacy_rows_group_by_scheme_not_by_expiry_date(mod, tmp_path, capsys):
    """Fourteen expiries are ONE scheme. Reporting fourteen schemes would be noise."""
    rows = [
        _obs(f"2027011{n}-call-650.00", dt.date(2026, 10, 1)) for n in range(3)
    ] + [_obs(f"dte{n}-call-650.00", dt.date(2026, 10, 1)) for n in (41, 42)]
    db = _store(tmp_path, rows)
    _run(mod, ["--db", str(db)])
    out = capsys.readouterr().out
    assert "expiry-keyed" in out
    assert "dte<dollars>" in out
    assert "20270110" not in out  # not listed as its own scheme


def test_keys_outside_every_arm_band_are_measured_but_gate_nothing(mod, tmp_path, capsys):
    rows = [_obs(_key(35), dt.date(2026, 10, 1))]
    db = _store(tmp_path, rows)
    assert _run(mod, ["--db", str(db)]) == 1
    out = capsys.readouterr().out
    assert _key(35) in out        # reported
    assert "no key in band" in out  # but it belongs to neither arm


# ---------------------------------------------------------------------------
# R9: the live/backfill splice must be counted, per the ruling
# ---------------------------------------------------------------------------


def test_backfilled_observations_count_toward_the_floor(mod, tmp_path, capsys):
    """RULED (R9): a backfilled observation occupies the same key and counts.

    If this test ever fails, the monitor is no longer measuring what T5 measures — and
    the failure would be silent in production, because the store would look warmer than
    the gate is.
    """
    rows = _daily(30, key=_key(91), origin="live")
    rows += _daily(MIN_OBSERVATIONS - 30, key=_key(91), origin="backfill")
    rows += _daily(MIN_OBSERVATIONS, key=_key(0))  # arm B's band, so the verdict can pass
    db = _store(tmp_path, rows)
    assert _run(mod, ["--db", str(db)]) == 0
    out = capsys.readouterr().out
    # The key must read 60 even though only 30 of those rows are live — that is R9.
    assert f"{_key(91):<28} {MIN_OBSERVATIONS:>5}" in out
    assert "VERDICT: WARM" in out


# ---------------------------------------------------------------------------
# the spot check
# ---------------------------------------------------------------------------


def test_spot_check_names_the_production_key_and_measures_it(mod, tmp_path, capsys):
    rows = _daily(MIN_OBSERVATIONS, key=_key(91)) + _daily(MIN_OBSERVATIONS, key=_key(0))
    db = _store(tmp_path, rows)
    assert _run(mod, ["--db", str(db), "--spot", "650.00"]) == 0
    out = capsys.readouterr().out
    assert "spot check" in out
    expected = moneyness_tenor_key(
        dte=91, right="call", moneyness_bucket=moneyness_bucket(650.0, 650.0)
    )
    assert expected in out


def test_spot_check_reports_pending_when_that_key_is_cold(mod, tmp_path, capsys):
    """A warm key elsewhere in the band does not make the ATM key warm."""
    rows = _daily(MIN_OBSERVATIONS, key=_key(98)) + _daily(MIN_OBSERVATIONS, key=_key(0))
    db = _store(tmp_path, rows)
    assert _run(mod, ["--db", str(db), "--spot", "650.00"]) == 0
    out = capsys.readouterr().out
    assert "PENDING" in out


# ---------------------------------------------------------------------------
# --json
# ---------------------------------------------------------------------------


def test_json_report_is_complete_and_machine_readable(mod, tmp_path, capsys):
    rows = _daily(MIN_OBSERVATIONS, key=_key(91)) + _daily(MIN_OBSERVATIONS, key=_key(0))
    db = _store(tmp_path, rows)
    out_path = tmp_path / "t5.json"
    assert _run(mod, ["--db", str(db), "--json", str(out_path)]) == 0

    payload = json.loads(out_path.read_text())
    assert payload["warm"] is True
    assert payload["min_observations"] == MIN_OBSERVATIONS
    assert {a["arm"] for a in payload["arms"]} == {"B", "C"}
    assert all(a["status"] == "PASS" for a in payload["arms"])
    assert {k["tenor_key"] for k in payload["keys"]} == {_key(91), _key(0)}
    assert "report written to" in capsys.readouterr().out


def test_json_report_says_cold_when_cold(mod, tmp_path):
    db = _store(tmp_path, [_obs(_key(91), dt.date(2026, 10, 1))])
    out_path = tmp_path / "t5.json"
    assert _run(mod, ["--db", str(db), "--json", str(out_path)]) == 1
    assert json.loads(out_path.read_text())["warm"] is False


# ---------------------------------------------------------------------------
# the key regex and legacy classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "mte0-call-mny+0.00",
        "mte91-call-mny+0.00",
        "mte98-put-mny-2.00",
        "mte105-call-mny+4.00",
    ],
)
def test_ruled_keys_are_recognised(mod, key):
    assert mod.KEY_RE.match(key) is not None


@pytest.mark.parametrize(
    "key",
    [
        "dte42-call-770.00",
        "20270116-call-650.00",
        "mte-call-mny+0.00",
        "mte91-call-770.00",
        "",
    ],
)
def test_legacy_keys_are_not_mistaken_for_ruled_keys(mod, key):
    assert mod.KEY_RE.match(key) is None


@pytest.mark.parametrize(
    "key,label",
    [
        ("dte42-call-770.00", "dte"),
        ("20270116-call-650.00", "expiry"),
        ("weird-key", "other"),
    ],
)
def test_legacy_classification(mod, key, label):
    assert mod._legacy_label(key) == label