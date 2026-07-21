# REGIME_CHANGES — documented breaks in the generating process of the live series

> Any change to the live engine that alters the distribution of trades or of the NAV is
> recorded here. Read before any inference on the series (PSR/MinTRL).

| Date (UTC) | Change | Expected effect on the series |
|---|---|---|
| 2026-05-22 | combo_G goes live (capital $749.32) | series start |
| 2026-06-09 | Fix of a realized-PnL double-count at position close + SL price rounding | capital/peak correct after closes; no per-trade % impact |
| 2026-06-10 | Reconcile guard against transient API glitches (implausible equity reads ignored) | removes phantom capital jumps from the series |
| 2026-06-11 | **Sizing on FREE SPOT cash** (spot total − hold) instead of total equity (+uPnL) | position sizes ≤ before whenever uPnL>0 or positions open → **lower daily NAV vol from 06-11 onward**; per-trade % return unchanged (the MinTRL validation unit is quasi-immune) |

Note: the reporting/drawdown equity figure remains `spot_total + upnl` (operator choice,
06-11) — known bias, tripwire: revisit if |uPnL| > 5% of capital or before scaling.
