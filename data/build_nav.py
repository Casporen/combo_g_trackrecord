#!/usr/bin/env python3
"""
build_nav.py — Rebuilds the daily NAV series, net of all fees, from the immutable
exports, and VERIFIES it against the account's current live equity (±1%).

NAV(end of day d, UTC) =
      Σ deposits(≤d) − Σ withdrawals(≤d)        [ledger_updates]
    + Σ closedPnl(≤d)                           [fills]
    − Σ fees(≤d)                                [fills]
    + Σ funding(≤d)                             [funding payments]
    + uPnL(d)                                   [open positions marked at the
                                                 HL 1h close of midnight UTC]

Output: nav/nav_daily.csv (date, nav, ret_twr, flows, realized_cum, fees_cum,
funding_cum, upnl_eod, position) + verification verdict on stdout.

Usage: python build_nav.py [--no-verify]
"""
import argparse
import json
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ACCOUNT = "0xD2725bfEC6C67b3ef943550DE1d947959Ea1B05E"
HL_INFO = "https://api.hyperliquid.xyz/info"
ROOT = Path(__file__).resolve().parent
EXPORTS = ROOT / "exports"
NAV_CSV = ROOT / "nav" / "nav_daily.csv"
CTX = ssl.create_default_context()
VERIFY_TOL = 0.01  # ±1%

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def hl_post(payload, retries=5):
    req = urllib.request.Request(HL_INFO, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    for a in range(retries):
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            if a == retries - 1:
                raise RuntimeError(f"HL info failed: {payload.get('type')}: {e}")
            time.sleep(2 ** a)


def dedup_funding(lst):
    """Dedup on (coin, time): a midnight payment shows up in 2 consecutive daily
    exports (userFunding windows are inclusive) → without this, funding double-counts."""
    seen, out = set(), []
    for x in lst:
        k = (x.get("delta", {}).get("coin"), x.get("time"))
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
    return out


def load_exports():
    # excludes our own outputs (trades.json / trades-*.json are NOT daily exports)
    days = [p for p in sorted(EXPORTS.glob("*.json")) if not p.name.startswith("trades")]
    if not days:
        sys.exit("No exports in exports/ — run export_daily.py first")
    fills, funding, ledger = [], [], []
    day_strs = []
    for p in days:
        doc = json.loads(p.read_text(encoding="utf-8"))
        assert doc["account"].lower() == ACCOUNT.lower()
        day_strs.append(doc["day_utc"])
        fills.extend(doc["fills"])
        funding.extend(doc["funding"])
        ledger.extend(doc["ledger_updates"])
    funding = dedup_funding(funding)
    fills.sort(key=lambda f: f["time"])
    # continuity guard: no missing day between the first and last export
    d0 = datetime.strptime(day_strs[0], "%Y-%m-%d")
    d1 = datetime.strptime(day_strs[-1], "%Y-%m-%d")
    expected = set()
    d = d0
    while d <= d1:
        expected.add(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    missing = sorted(expected - set(day_strs))
    if missing:
        sys.exit(f"MISSING exports (series not continuous, NAV refused): {missing}")
    return day_strs, fills, funding, ledger


def position_track(fills):
    """Replays the fills → per-coin position state {size_signed, entry_avg} over time.
    Returns the list [(time_ms, positions_dict_copy)] after each fill."""
    pos = {}  # coin -> [size_signed, entry_avg]
    timeline = []
    for f in fills:
        coin, px, sz = f["coin"], float(f["px"]), float(f["sz"])
        delta = sz if f["side"] == "B" else -sz
        size, entry = pos.get(coin, [0.0, 0.0])
        new_size = size + delta
        if size == 0 or (size > 0) == (delta > 0):
            # opening or increasing: weighted average
            entry = (abs(size) * entry + abs(delta) * px) / (abs(size) + abs(delta))
        elif abs(round(new_size, 10)) < 1e-9:
            new_size, entry = 0.0, 0.0  # full close
        # partial reduction: entry unchanged; sign flip: entry = fill price
        elif (new_size > 0) != (size > 0):
            entry = px
        pos[coin] = [new_size, entry]
        if abs(new_size) < 1e-9:
            del pos[coin]
        timeline.append((f["time"], {c: v[:] for c, v in pos.items()}))
    return timeline


def positions_at(timeline, t_ms):
    last = {}
    for t, p in timeline:
        if t > t_ms:
            break
        last = p
    return last


_candle_cache = {}


def mark_price(coin, t_ms):
    """Close of the HL 1h candle ending at t_ms (midnight UTC)."""
    key = (coin, t_ms)
    if key in _candle_cache:
        return _candle_cache[key]
    res = hl_post({"type": "candleSnapshot",
                   "req": {"coin": coin, "interval": "1h",
                           "startTime": t_ms - 3_600_000, "endTime": t_ms + 1}})
    px = None
    for c in res or []:
        if int(c["t"]) == t_ms - 3_600_000:
            px = float(c["c"])
    if px is None and res:
        px = float(res[-1]["c"])
    if px is None:
        raise RuntimeError(f"No candle for {coin} @ {t_ms}")
    _candle_cache[key] = px
    return px


def upnl_at(timeline, t_ms):
    """Portfolio uPnL at t_ms, positions marked at the 1h candle."""
    pos = positions_at(timeline, t_ms)
    total = 0.0
    detail = []
    for coin, (size, entry) in pos.items():
        mark = mark_price(coin, t_ms)
        total += size * (mark - entry)  # short: size<0 → gain if mark<entry
        detail.append(f"{coin}:{size:+g}@{entry:g}")
    return total, ";".join(detail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args()

    day_strs, fills, funding, ledger = load_exports()
    timeline = position_track(fills)

    # cash events, sorted by time
    def ev_flows(t_max_ms):
        dep = wd = 0.0
        for l in ledger:
            if l["time"] >= t_max_ms:
                continue
            d = l["delta"]
            if d.get("type") == "deposit":
                dep += float(d.get("usdc", 0))
            elif d.get("type") == "withdraw":
                wd += float(d.get("usdc", 0))
        return dep, wd

    rows = []
    prev_nav = None
    d0 = datetime.strptime(day_strs[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    d_end = datetime.strptime(day_strs[-1], "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    d = d0 + timedelta(days=1)  # first NAV = end of the first exported day
    while d <= d_end:
        t_ms = int(d.timestamp() * 1000)
        dep, wd = ev_flows(t_ms)
        realized = sum(float(f["closedPnl"]) for f in fills if f["time"] < t_ms)
        fees = sum(float(f["fee"]) for f in fills if f["time"] < t_ms)
        fund = sum(float(x["delta"]["usdc"]) for x in funding if x["time"] < t_ms)
        upnl, pos_str = upnl_at(timeline, t_ms)
        nav = dep - wd + realized - fees + fund + upnl
        day_str = (d - timedelta(days=1)).strftime("%Y-%m-%d")
        # flows OF the day (for TWR): deposits/withdrawals within [d-1, d)
        dep_prev, wd_prev = ev_flows(int((d - timedelta(days=1)).timestamp() * 1000))
        flows_day = (dep - dep_prev) - (wd - wd_prev)
        if prev_nav is None or prev_nav + flows_day <= 0:
            ret = ""
        else:
            ret = f"{nav / (prev_nav + flows_day) - 1:.6f}"
        rows.append([day_str, f"{nav:.4f}", ret, f"{flows_day:.2f}",
                     f"{realized:.4f}", f"{fees:.4f}", f"{fund:.4f}",
                     f"{upnl:.4f}", pos_str])
        prev_nav = nav
        d += timedelta(days=1)

    NAV_CSV.parent.mkdir(exist_ok=True)
    header = "date,nav_usd,ret_twr,flows_usd,realized_pnl_cum,fees_cum,funding_cum,upnl_eod,open_positions"
    NAV_CSV.write_text(header + "\n" + "\n".join(",".join(r) for r in rows) + "\n",
                       encoding="utf-8", newline="\n")
    nav_last = float(rows[-1][1])
    print(f"nav/nav_daily.csv: {len(rows)} days, NAV({rows[-1][0]} EOD) = ${nav_last:,.2f}")

    if args.no_verify:
        return

    # ── Verification: NAV rebuilt NOW vs live equity ────────────────────────
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    state = hl_post({"type": "clearinghouseState", "user": ACCOUNT})
    spot = hl_post({"type": "spotClearinghouseState", "user": ACCOUNT})
    upnl_live = sum(float(p["position"].get("unrealizedPnl") or 0)
                    for p in state.get("assetPositions", []))
    spot_total = sum(float(b.get("total", 0)) for b in spot.get("balances", [])
                     if b.get("coin") == "USDC")
    spot_hold = sum(float(b.get("hold", 0)) for b in spot.get("balances", [])
                    if b.get("coin") == "USDC")
    # equity = free spot cash + perp account value (isolated margin already includes
    # uPnL — verified empirically 2026-06-10, see METHODOLOGY)
    equity_live = (spot_total - spot_hold) + float(state["marginSummary"]["accountValue"])

    # flows/PnL after the last export's end → completed live via the API
    t_last = int(d_end.timestamp() * 1000)
    fills_tail = hl_post({"type": "userFillsByTime", "user": ACCOUNT,
                          "startTime": t_last, "endTime": now_ms,
                          "aggregateByTime": False}) or []
    fund_tail = hl_post({"type": "userFunding", "user": ACCOUNT,
                         "startTime": t_last, "endTime": now_ms}) or []
    ledger_tail = hl_post({"type": "userNonFundingLedgerUpdates", "user": ACCOUNT,
                           "startTime": t_last, "endTime": now_ms}) or []
    dep, wd = ev_flows(t_last)
    for l in ledger_tail:
        dt = l["delta"]
        if dt.get("type") == "deposit":
            dep += float(dt.get("usdc", 0))
        elif dt.get("type") == "withdraw":
            wd += float(dt.get("usdc", 0))
    realized = sum(float(f["closedPnl"]) for f in fills) + sum(float(f["closedPnl"]) for f in fills_tail)
    fees = sum(float(f["fee"]) for f in fills) + sum(float(f["fee"]) for f in fills_tail)
    fund = sum(float(x["delta"]["usdc"]) for x in dedup_funding(funding + fund_tail))
    nav_now = dep - wd + realized - fees + fund + upnl_live

    diff = nav_now - equity_live
    pct = abs(diff) / equity_live if equity_live else float("inf")
    print(f"VERIFICATION  rebuilt NAV = ${nav_now:,.4f}  vs  live equity = ${equity_live:,.4f}"
          f"  → gap {diff:+.4f} ({pct:.4%})")
    if pct > VERIFY_TOL:
        print(f"FAILED: gap > {VERIFY_TOL:.0%}")
        sys.exit(1)
    print(f"OK: gap ≤ {VERIFY_TOL:.0%}")


if __name__ == "__main__":
    main()
