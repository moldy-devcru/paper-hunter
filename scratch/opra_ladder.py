"""Offline: rebuild the exact chunk the backfill requests. No API calls."""
import json, math, datetime as dt

# SPY monthly expiries, 3rd Friday. Enough to cover a 90-180 DTE band.
def third_friday(y, m):
    d = dt.date(y, m, 1)
    offset = (4 - d.weekday()) % 7
    return d + dt.timedelta(days=offset + 14)

def candidate_expiries(session, dte_min, dte_max):
    out = []
    y = session.year
    for mm in range(1, 13):
        for yy in (y, y + 1):
            e = third_friday(yy, mm)
            dte = (e - session).days
            if dte_min <= dte <= dte_max:
                out.append(e)
    return sorted(out)

raw = json.load(open('scratch/spy_closes.json'))
closes = {}
for ts, c in raw:
    closes[dt.datetime.fromtimestamp(ts, dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=-4))).date()] = c

start, end = dt.date(2026, 6, 15), dt.date(2026, 9, 30)
syms, seen = [], set()
for i in range((end - start).days + 1):
    s = start + dt.timedelta(days=i)
    if s.weekday() >= 5:
        continue
    spot = closes.get(s)
    if spot is None:
        continue
    exps = candidate_expiries(s, 90, 180)
    if not exps:
        continue
    e = exps[0]
    low = spot * (1 - 0.75 / 100); high = spot * (1 + 0.75 / 100)
    k = math.ceil(low)
    while k <= high + 1e-9:
        for r in ("C", "P"):
            y = f"SPY{e.strftime('%y%m%d')}{r}{int(round(k*1000)):08d}"
            if y not in seen:
                seen.add(y); syms.append(y)
        k += 1
print("distinct symbols:", len(syms))
for i in range(0, len(syms), 100):
    ch = syms[i:i+100]
    print(f"--- chunk {i//100} n={len(ch)} first={ch[0]} last={ch[-1]}")
json.dump(syms, open('scratch/opra_ladder_symbols.json', 'w'))
