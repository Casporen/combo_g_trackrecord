# combo_G — live track record

Verifiable, tamper-evident archive of the live trading account of the **combo_G**
strategy on HyperLiquid, plus the static site that renders it (GitHub Pages, repo root).

Everything is built from the **public** HyperLiquid API only (the account address is
on-chain public; no keys, no private data, no contact with any trading engine).

| What | Where |
|---|---|
| Static site (what you're seeing on Pages) | `index.html` |
| Full methodology (1 page) | `data/METHODOLOGY_TRACKRECORD.md` |
| Immutable daily exports (fills, funding, ledger) | `data/exports/YYYY-MM-DD.json` |
| State snapshots (equity, positions) | `data/snapshots/` |
| Daily NAV series, net of all fees + TWR | `data/nav/nav_daily.csv` |
| Timestamped trade log (round-trips) | `data/exports/trades-YYYY-MM-DD.json` |
| Append-only SHA256 hashes | `data/SHA256SUMS` (+ errata note) |
| Documented breaks in the live process | `data/REGIME_CHANGES.md` |

## Verify it yourself
```
cd data
sha256sum -c SHA256SUMS                 # file integrity (append-only hash table)
python export_daily.py --verify         # same check, script version
python build_nav.py                     # rebuild NAV from raw exports + cross-check
                                        # against live on-chain equity (±1%)
```
Every fill carries its HyperLiquid tx hash — cross-checkable on-chain, independently
of this repository. The commit dates the hash; the hash pins the content.

## Disclaimer
Data is provided as is, without warranty of any kind. Nothing in this repository
constitutes investment advice or a solicitation to invest.
