# data/events/ — the T5 event calendar

Consumed by `data/event_calendar.py`. Its one job: answer `is_event_day(date)` so the
deterministic executor can apply the **T5 hard veto** from the brief — *"no
earnings/FOMC day entries — event calendar checked daily, hard veto"*.

```
data/events/2026-Q4.yaml   ← one file per quarter, committed to git
```

## File shape

```yaml
quarter: 2026-Q4
updated: 2026-10-02
notes: [...]
events:
  - date: 2026-10-28          # ISO, the veto key
    kind: fomc                # fomc | cpi | opex | earnings
    label: "FOMC decision"    # journal-ready text
    release_time_et: "14:00"  # optional
    veto: true                # optional; defaults per kind (fomc/cpi = true, opex = false)
    verified: true            # false = NOT confirmed against the issuing authority
    source: https://...       # where the date came from
    note: "..."               # optional
```

Unknown keys are rejected (`extra="forbid"`), same discipline as the rulebook: a typo
that silently disabled a veto would be worse than a crash.

## Free sources (no API keys, no scraping of anything behind a login)

| Event | Source | URL |
|---|---|---|
| FOMC meetings | Federal Reserve, published calendar | https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm |
| CPI releases | BLS release schedule (annual, published ahead of time) | https://www.bls.gov/schedule/news_release/cpi.htm |
| CPI exact dates | BLS "Schedule of Selected Releases" for the current year | https://www.bls.gov/schedule/news_release/current_year.asp |
| Market holidays | NYSE holiday calendar (moves CPI/FOMC-adjacent dates) | https://www.nyse.com/markets/hours-calendars |
| OPEX | Third Friday of the month (monthly equity/index expiry); Cboe confirms | https://www.cboe.com/tradable_products/equity_indices_options/ |

## Refresh procedure (weekly, by hand for now)

The brief calls this "a static file refreshed weekly". Until a cron job exists, it is a
human ritual — roughly five minutes, once a week:

1. **Open the two authoritative pages** (FOMC calendar, BLS release schedule). Nothing
   else is authoritative; aggregators are for cross-checking, not sourcing.
2. **Diff against the current file.** Only the forward window matters — the experiment
   needs dates from the window start onward, not history.
3. **Add new events** as dated entries above, with `source:` filled in and
   `verified: true` only after you have seen the date on the issuing authority's page.
4. **Flip `verified: false` → `true`** for anything you have now confirmed, and set
   `updated:` to the date you did it.
5. **Commit** with a conventional message, e.g.
   `chore(events): add 2027-Q1 FOMC + CPI dates` or
   `chore(events): verify Q4 CPI dates against BLS`.

Then check the two things that would silently break the veto:

```python
from data.event_calendar import EventCalendar
cal = EventCalendar.load_dir()
print(len(cal), cal.coverage())
print(cal.unverified())     # anything still needing eyes
```

A calendar file with **zero** events raises `EventError` rather than loading empty —
`is_event_day` returning False forever would be the exact failure T5 exists to prevent.

## Open items (deliberately not invented here)

- **Component earnings — CLOSED by operator ruling 2026-10-02.** SPY has no earnings, and
  the operator ruled that any particular ticker's earnings are irrelevant to an index
  IV-regime decision, so the earnings veto is dropped rather than merely unbuilt:
  `checklist.t5_options_chain.event_calendar.earnings_veto` now reads
  `enabled: false, disabled_by: operator_ruling_2026-10-02`. FOMC and CPI remain hard
  vetoes. Re-opening this is a rulebook + loader change, not a calendar edit.
- **FOMC day 1 vs day 2.** Both days are vetoed here. The brief says "FOMC day";
  two-day meetings with a mid-week decision day make day 1 equally hostile to IV.
  Narrow this to day 2 only if the operator says so — it is a rulebook change
  (new strategy version), not a calendar edit.
- **Quarter boundaries.** FOMC/CPI events straddle quarters (a December meeting with a
  January statement is not one; but OPEX and CPI land near month ends). Keep one file
  per quarter and duplicate a date into two files if that ever happens — duplicates are
  allowed and the journal records both causes.