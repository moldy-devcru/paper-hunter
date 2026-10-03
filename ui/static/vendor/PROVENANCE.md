# Vendored third-party code

Everything the terminal loads at runtime lives in this directory. There is no CDN
reference anywhere in `index.html` or `app.js`, and `tests/test_ui_static.py` fails the
build if one appears: this is a LAN tool that must work with the internet cable pulled.

## lightweight-charts

| field | value |
| --- | --- |
| library | TradingView Lightweight Charts (standalone production bundle) |
| version | 5.2.1 |
| file | `lightweight-charts.standalone.production.js` |
| bytes | 197922 |
| sha256 | `e21cc5caa0226ef30bd8549c50b9ef926615f2a4ee6b4e486353477a55f598cf` |
| source | `https://cdn.jsdelivr.net/npm/lightweight-charts@5.2.1/dist/lightweight-charts.standalone.production.js` |
| npm dist-tag | `latest` at 5.2.1 on 2026-10-02 (the v5 line) |
| license | Apache License 2.0 (see the banner inside the file) |
| retrieved | 2026-10-02 |

Why the standalone build and not the ESM one: `lightweight-charts.standalone.production.js`
is a single UMD-ish file that sets `window.LightweightCharts` and needs no bundler, no
`import` map and no `node_modules`. The whole point of the no-build-chain decision in
`docs/ui-design.md` is that a research tool should be auditable by reading files, and this
is the variant a browser can load with one `<script>` tag.

**# INTERPRETATION: the spec calls this library MIT. It is not.** `docs/ui-design.md`
says "TradingView lightweight-charts (MIT, vendored locally)"; the shipped bundle's
license banner is Apache License 2.0, which is a permissive license with a patent grant
and a NOTICE-file obligation, not MIT. The vendoring decision stands and nothing changes
functionally, but the spec line is wrong and is corrected here rather than quietly
inherited. If this ever leaves the LAN box, Apache-2.0's attribution requirement applies
(keep this file, keep the banner in the bundle).

Re-vendoring (the only way to change the pinned version):

```sh
VER=5.2.1
curl -fsSL "https://cdn.jsdelivr.net/npm/lightweight-charts@${VER}/dist/lightweight-charts.standalone.production.js" \
  -o ui/static/vendor/lightweight-charts.standalone.production.js
head -c 200 ui/static/vendor/lightweight-charts.standalone.production.js   # must be the license banner
sha256sum ui/static/vendor/lightweight-charts.standalone.production.js      # must match the table above
```

Then update the version, bytes and sha256 rows. `tests/test_ui_static.py` compares the
file's real sha256 against the value recorded here, so a half-finished re-vendor fails
loudly instead of shipping an unrecorded binary.
