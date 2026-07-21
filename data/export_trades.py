#!/usr/bin/env python3
"""
export_trades.py — Produces exports/trades.json: a timestamped trade log (round-trips)
derived ONLY from PUBLIC on-chain HyperLiquid fills.

Source = the immutable exports/*.json (already hashed) + tail via the public HL API
`userFillsByTime` / `userFunding` (same as export_daily.py / build_nav.py). The script
accesses NO non-public data: only the executions (fills) and funding payments of the
on-chain address. It only ever sees trades, never the logic that generated them —
you cannot leak what you do not read.

Pairing convention (= build_nav.py:position_track):
  - signed position per coin (+sz if side B / buy, -sz if side A / sell)
  - a trade = the segment where the position leaves 0 and returns to it
  - entry legs = increase |pos| (size-weighted average); exit legs = reduce toward 0
  - a position that never returns to 0 (still open) = NOT emitted (no exit yet)
  - net: pnl = Σ closedPnl(exits) - Σ fee(all legs) + Σ funding(coin, t∈[entry,exit])

Output: STRICT whitelist { id, coin, direction, entry_ts, exit_ts, duration_h,
entry_price, exit_price, return_pct, pnl_usd }. Deterministic (generated_at derived
from the sources, not the clock → same fills = same bytes). COMMITS/PUSHES NOTHING.

TWO OUTPUTS, two roles (immutability distinction):
  1. exports/trades-YYYY-MM-DD.json  = DATED IMMUTABLE SNAPSHOT.
     - Never rewritten (refuses overwrite, like the daily exports).
     - HASHED into SHA256SUMS (append-only) → THIS is where the tamper-evident proof
       lives: the commit dates the hash, the hash pins the content. Check with `sha256sum -c`.
  2. exports/trades.json  = "live" DERIVED VIEW (what the site reads).
     - Plain COPY of the most recent dated snapshot.
     - EXCLUDED from SHA256SUMS ON PURPOSE: regenerable file (changes with every new
       trade); hashing it would break append-only. Its truthfulness can be re-checked
       either against the dated snapshots (hashed), or directly against the public HL
       API. Exclusion documented here.
The site reads trades.json; the enforceable integrity lives in the dated snapshots.

Usage: python export_trades.py
"""
import glob
import hashlib
import shutil
import json
import ssl
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ACCOUNT = "0xD2725bfEC6C67b3ef943550DE1d947959Ea1B05E"
HL_INFO = "https://api.hyperliquid.xyz/info"
ROOT = Path(__file__).resolve().parent
EXPORTS = ROOT / "exports"
SUMS = ROOT / "SHA256SUMS"
OUT = EXPORTS / "trades.json"
CTX = ssl.create_default_context()


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


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 1. Load fills + funding: immutable exports, then public-API tail ────────────
def load_sources():
    fills, funding, gen_max, last_end = [], [], "", 0
    for p in sorted(EXPORTS.glob("*.json")):
        if p.name.startswith("trades"):   # never re-ingest our own outputs
            continue
        doc = json.loads(p.read_text(encoding="utf-8"))
        if doc.get("account", "").lower() != ACCOUNT.lower():
            continue
        fills.extend(doc.get("fills", []))
        funding.extend(doc.get("funding", []))
        gen_max = max(gen_max, doc.get("generated_at", ""))
        last_end = max(last_end, doc.get("window_ms", [0, 0])[1])
    # tail: from the last exported day to now (public API, identical source)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    cursor = last_end or int(datetime(2026, 5, 19, tzinfo=timezone.utc).timestamp() * 1000)
    tail_f = []
    while True:
        batch = hl_post({"type": "userFillsByTime", "user": ACCOUNT,
                         "startTime": cursor, "endTime": now_ms, "aggregateByTime": False})
        if not batch:
            break
        tail_f.extend(batch)
        if len(batch) < 2000:
            break
        cursor = max(f["time"] for f in batch) + 1
    fills.extend(tail_f)
    tail_fund = hl_post({"type": "userFunding", "user": ACCOUNT,
                         "startTime": last_end or 0, "endTime": now_ms}) or []
    funding.extend(tail_fund)
    # funding dedup: a payment (coin, time) can appear in 2 consecutive daily windows
    # (midnight boundary) or in the exports/tail overlap. Without this, funding would
    # double-count → return_pct no longer exactly reproducible on-chain.
    seen, fund_ded = set(), []
    for x in funding:
        k = (x.get("delta", {}).get("coin"), x.get("time"))
        if k in seen:
            continue
        seen.add(k)
        fund_ded.append(x)
    # deterministic generated_at: max(source generated_at, last fill) — not the live clock
    data_max_ms = max([f["time"] for f in fills] + [last_end])
    gen = max(gen_max, iso(data_max_ms))
    return fills, fund_ded, gen


# ── 2. Dedup + sort ─────────────────────────────────────────────────────────────
def dedupe(fills):
    seen, out = set(), []
    for f in fills:
        k = (f.get("hash"), f.get("tid"), f.get("time"), f.get("coin"), f.get("px"), f.get("sz"))
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
    return sorted(out, key=lambda f: f["time"])


# ── 3. Round-trip reconstruction (per-coin position replay) ─────────────────────
def build_trades(fills, funding):
    by_coin = {}
    for f in fills:
        by_coin.setdefault(f["coin"], []).append(f)
    trades = []
    for coin, fs in by_coin.items():
        fs.sort(key=lambda f: f["time"])
        pos = 0.0
        cur = None  # trade in progress
        for f in fs:
            px, sz, fee = float(f["px"]), float(f["sz"]), float(f["fee"])
            delta = sz if f["side"] == "B" else -sz
            prev = pos
            pos = round(pos + delta, 10)
            if cur is None and prev == 0 and pos != 0:
                cur = {"coin": coin, "dir": "SHORT" if pos < 0 else "LONG",
                       "entry_ts": f["time"], "exit_ts": None,
                       "en_ntl": 0.0, "en_sz": 0.0, "ex_ntl": 0.0, "ex_sz": 0.0,
                       "fee": 0.0, "closed": 0.0}
            if cur is not None:
                cur["fee"] += fee
                opening = (abs(pos) > abs(prev))  # leg that increases |position|
                if opening:
                    cur["en_ntl"] += px * sz
                    cur["en_sz"] += sz
                else:
                    cur["ex_ntl"] += px * sz
                    cur["ex_sz"] += sz
                    cur["closed"] += float(f["closedPnl"])
                if pos == 0:  # complete round-trip
                    cur["exit_ts"] = f["time"]
                    trades.append(cur)
                    cur = None
        # cur not None at the end = open position → ignored (no exit)
    # funding attributed per coin over the holding window
    fund_by_coin = {}
    for x in funding:
        d = x.get("delta", {})
        fund_by_coin.setdefault(d.get("coin"), []).append((x["time"], float(d.get("usdc", 0))))
    out = []
    sigfig = lambda x: float(f"{x:.10g}")   # cleans float artifacts, deterministic
    for t in trades:
        en_px = sigfig(t["en_ntl"] / t["en_sz"])
        ex_px = sigfig(t["ex_ntl"] / t["ex_sz"])
        size = t["en_sz"]
        fund = sum(u for tm, u in fund_by_coin.get(t["coin"], [])
                   if t["entry_ts"] <= tm <= t["exit_ts"])
        pnl = t["closed"] - t["fee"] + fund
        notional = en_px * size
        out.append({
            "coin": t["coin"], "direction": t["dir"],
            "entry_ts": iso(t["entry_ts"]), "exit_ts": iso(t["exit_ts"]),
            "duration_h": round((t["exit_ts"] - t["entry_ts"]) / 3_600_000, 2),
            "entry_price": en_px, "exit_price": ex_px,
            "return_pct": round(pnl / notional * 100, 4) if notional else None,
            "pnl_usd": round(pnl, 4),
            "_entry_ms": t["entry_ts"],  # internal sort key, removed before emission
        })
    out.sort(key=lambda r: (r["_entry_ms"], r["coin"]))
    for i, r in enumerate(out, 1):
        r["id"] = i
    return out


# ── 4. Strict whitelist emission + deterministic envelope ───────────────────────
WHITELIST = ["id", "coin", "direction", "entry_ts", "exit_ts", "duration_h",
             "entry_price", "exit_price", "return_pct", "pnl_usd"]


def serialize(trades, generated_at):
    """Deterministic document bytes (sort_keys + strict whitelist)."""
    clean = [{k: t[k] for k in WHITELIST} for t in trades]
    doc = {
        "schema": "combo_g_trackrecord.trades.v1",
        "account": ACCOUNT,
        "generated_at": generated_at,
        "source": "derived from HL public fills (verifiable on-chain)",
        "trades": clean,
    }
    return doc, json.dumps(doc, indent=1, sort_keys=True) + "\n"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def append_sums(path):
    """Append-only: adds the hash of the DATED SNAPSHOT. Never rewritten."""
    rel = path.relative_to(ROOT).as_posix()
    existing = set()
    if SUMS.exists():
        for line in SUMS.read_text(encoding="utf-8").splitlines():
            if line.strip():
                existing.add(line.split(maxsplit=1)[1].strip())
    digest = sha256(path)
    if rel in existing:
        print(f"  SHA256SUMS: {rel} already listed — NOT rewritten (append-only). hash = {digest}")
        return
    with SUMS.open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"{digest}  {rel}\n")
    print(f"  SHA256SUMS: + {rel}  {digest}")


def main():
    fills, funding, gen = load_sources()
    fills = dedupe(fills)
    print(f"on-chain fills (exports + API tail): {len(fills)} | funding: {len(funding)} | generated_at={gen}")
    trades = build_trades(fills, funding)
    doc, blob = serialize(trades, gen)

    # 1) DATED IMMUTABLE SNAPSHOT (hashed append-only) ──────────────────────────
    dated = EXPORTS / f"trades-{gen[:10]}.json"
    if dated.exists():
        if dated.read_text(encoding="utf-8") == blob:
            print(f"\n{dated.name} already exists, identical — untouched (immutable).")
        else:
            print(f"\n⚠️ {dated.name} exists with DIFFERENT content — NOT rewritten (immutability). "
                  f"Today's snapshot is already pinned; same-day new trades will be captured in the next dated one.")
    else:
        dated.write_text(blob, encoding="utf-8", newline="\n")
        print(f"\n+ {dated.name}  ({len(doc['trades'])} round-trips) — dated immutable snapshot")
        append_sums(dated)

    # 2) LIVE DERIVED VIEW = copy of the latest dated snapshot (NOT in SHA256SUMS)
    latest = sorted(EXPORTS.glob("trades-20*.json"))[-1]
    shutil.copyfile(latest, OUT)
    print(f"  exports/trades.json = copy of {latest.name}  (NOT in SHA256SUMS — regenerable derived view)")

    print(f"\n{OUT.name} (what the site reads):")
    print(json.dumps(doc, indent=1))
    print("\nDONE (no commit/push).")


if __name__ == "__main__":
    main()
