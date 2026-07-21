# Methodology — combo_G track record

## Scope
- **Account**: `0xD2725bfEC6C67b3ef943550DE1d947959Ea1B05E` (HyperLiquid mainnet), account
  DEDICATED to the combo_G strategy (signal frozen 2026-05-05). No other activity on
  this account.
- **Inception**: 2026-05-19 (single deposit of $749.32 USDC). No other external flow to date.
- **Reference capital**: the account's NAV. Returns are time-weighted (TWR),
  hence insensitive to future deposits/withdrawals.

## Data sources (public, verifiable by anyone)
Everything comes from the public HyperLiquid API (`api.hyperliquid.xyz/info`), queryable by
anyone with the account address — no private data, no keys:
- `userFillsByTime`: every fill (price, size, side, fees, closedPnl, ms timestamp, tx hash).
- `userFunding`: every funding payment (signed, USDC).
- `userNonFundingLedgerUpdates`: deposits / withdrawals / transfers.
- `clearinghouseState` + `spotClearinghouseState`: current positions and balances.

## Daily NAV construction (net of ALL costs)
For each UTC end of day `d`:

```
NAV(d) = cumulative deposits − cumulative withdrawals
       + Σ closedPnl (fills)          ← realized PnL
       − Σ fees (fills)               ← trading fees (taker/maker)
       + Σ funding (signed payments)  ← funding paid/received
       + uPnL(d)                      ← open positions marked at the HL 1h close of midnight UTC
```

Daily TWR return: `r(d) = NAV(d) / (NAV(d−1) + external_flows(d)) − 1`.
The series lives in `nav/nav_daily.csv`. No cost is excluded: trading fees and funding
are fully deducted; there are no management fees nor any other cost on this account.

### Equity technical note (unified account + isolated margin)
On HL, the margin of an isolated position (the `hold` of the spot USDC balance) **includes
accrued uPnL** (verified empirically on 2026-06-10: hold = initial margin + uPnL, and the
spot total tracks uPnL tick by tick). The correct instantaneous equity is therefore:
`equity = (spot USDC total − hold) + perp accountValue` (free cash + perp account value).

## Verification (independent cross-check)
The NAV rebuilt from flows (formula above, live uPnL) is cross-checked against the current
equity read directly from the account. **Tolerance: ±1%.**
Reference run (2026-06-10T17:00Z): rebuilt NAV $754.5839 vs live equity $754.6012,
gap **0.0023%** ($0.017). The `build_nav.py` script refuses to produce the series if the gap
exceeds 1% or if a day is missing from the exports (non-continuous series = no NAV).

## Immutability & enforceable timestamping
- A daily export (`exports/YYYY-MM-DD.json`) is **never rewritten** (the script refuses).
  A day is only exported once finished (UTC) — never a partial export.
- Every file is **SHA256**-hashed into `SHA256SUMS` (append-only); everything is committed
  to this git repo: **the commit dates the hash, the hash pins the content**. Check with:
  `python export_daily.py --verify` (or `sha256sum -c SHA256SUMS`).
- **Honest limitation**: the exports from 2026-05-19 to 2026-06-09 are a *backfill* performed
  on 2026-06-10 (the HL API provides full history) — their commit only proves anteriority
  from 2026-06-10 onward. Day-by-day timestamping proof holds for every later export.
  The content itself remains verifiable at any time against the public HL API.
- Defense in depth: fills carry HL tx hashes, cross-checkable on-chain/explorer
  independently of this repo.

## Reproduction
```
python export_daily.py --backfill 2026-05-19 <yesterday>   # rebuild exports from the API
python build_nav.py                                         # rebuild NAV + ±1% verification
python export_daily.py --verify                             # verify the hashes
```
