"""paper-hunter UI — read-only research terminal (Phase U1: backend only).

Nothing in this package may write to the journal or the IV store. The UI is a window
onto the experiment, not a door into it: see ``ui/api.py`` for the read-only
enforcement and the test that asserts the app has zero non-GET routes.

Submodules
----------
``aggregate``  session-aware timeframe bucketing (algo-adjacent math — reviewed)
``barcache``   the mutable, rebuildable bar cache at ``data/barcache.db``
``api``        the FastAPI app
"""
