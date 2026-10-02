"""analysis/ — weekly rollups, NO-SHOT counterfactual scoring, pre-registered scorecard.

Pure reads over a journal connection. ``rollup`` is the weekly/monthly review half of
the brief; ``shadow_roll`` is the costless 4th simulation that gives prediction #3
something to be compared against.

The package has no write path into the decision ledger: a review module that could
edit the journal would be an experiment grading its own homework. The only writes in
here are shadow-roll rows, which are hypotheticals with no order behind them and live
in their own append-only tables (journal/schema.sql).
"""
