#!/usr/bin/env python3
"""
export_daily.py — Immutable daily export of the combo_G account (HyperLiquid).

STANDALONE service: reads the public HL info API only (account address,
NO private key, no contact with any trading engine).

For each UTC day D, produces exports/D.json:
  - all fills of the day (price, size, fees, closedPnl, ms timestamps)
  - funding payments of the day
  - ledger updates of the day (deposits / withdrawals / transfers)
And on every run, snapshots/<now>.json: current state (perp+spot equity, positions).

Immutability: an existing exports/D.json file is NEVER rewritten.
Every new file is hashed (SHA256) into SHA256SUMS (append-only),
then committed to this git repo — the commit dates the hash, the hash pins the content.

Usage:
  python export_daily.py                    # export previous UTC day + snapshot + commit
  python export_daily.py --date 2026-06-01  # export a specific day
  python export_daily.py --backfill 2026-05-19 2026-06-09   # backfill a range (inclusive)
  python export_daily.py --no-commit        # no git commit (dry run)
  python export_daily.py --verify           # re-verify every hash in SHA256SUMS
"""
import argparse
import hashlib
import json
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ACCOUNT = "0xD2725bfEC6C67b3ef943550DE1d947959Ea1B05E"  # combo_G main address (funds)
HL_INFO = "https://api.hyperliquid.xyz/info"
ROOT = Path(__file__).resolve().parent
EXPORTS = ROOT / "exports"
SNAPSHOTS = ROOT / "snapshots"
SUMS = ROOT / "SHA256SUMS"
CTX = ssl.create_default_context()

INCEPTION = "2026-05-19"  # date of the first deposit on the account


def hl_post(payload, retries=5):
    req = urllib.request.Request(
        HL_INFO, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    for a in range(retries):
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            if a == retries - 1:
                raise RuntimeError(f"HL info failed after {retries} tries: {payload.get('type')}: {e}")
            time.sleep(2 ** a)


def day_bounds_ms(day_str):
    d = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = int(d.timestamp() * 1000)
    end = int((d + timedelta(days=1)).timestamp() * 1000)
    return start, end


def fetch_fills(start_ms, end_ms):
    """Paginated userFillsByTime (max 2000/response) — advances startTime past the last fill."""
    out, cursor = [], start_ms
    while True:
        batch = hl_post({"type": "userFillsByTime", "user": ACCOUNT,
                         "startTime": cursor, "endTime": end_ms,
                         "aggregateByTime": False})
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 2000:
            break
        cursor = max(f["time"] for f in batch) + 1
    # defensive dedup (key = fill identity) + chronological sort
    seen, dedup = set(), []
    for f in out:
        k = (f.get("hash"), f.get("tid"), f.get("time"), f.get("coin"), f.get("px"), f.get("sz"))
        if k in seen:
            continue
        seen.add(k)
        dedup.append(f)
    return sorted(dedup, key=lambda f: f["time"])


def export_day(day_str, generated_at):
    """Writes exports/<day>.json. Refuses to overwrite an existing file (immutability)."""
    out_path = EXPORTS / f"{day_str}.json"
    if out_path.exists():
        print(f"  = exports/{day_str}.json already exists — left untouched (immutability)")
        return None
    start_ms, end_ms = day_bounds_ms(day_str)
    fills = fetch_fills(start_ms, end_ms)
    # HALF-OPEN window [start, end): a payment at midnight (== end_ms == start of the
    # next day) belongs to ONE day only → no funding/ledger double-count across exports.
    funding = [x for x in (hl_post({"type": "userFunding", "user": ACCOUNT,
                                    "startTime": start_ms, "endTime": end_ms}) or [])
               if start_ms <= x["time"] < end_ms]
    ledger = [x for x in (hl_post({"type": "userNonFundingLedgerUpdates", "user": ACCOUNT,
                                   "startTime": start_ms, "endTime": end_ms}) or [])
              if start_ms <= x["time"] < end_ms]
    doc = {
        "schema": "combo_g_trackrecord.daily_export.v1",
        "account": ACCOUNT,
        "day_utc": day_str,
        "window_ms": [start_ms, end_ms],
        "generated_at": generated_at,
        "source": "api.hyperliquid.xyz/info (public, read-only)",
        "fills": fills,
        "funding": funding,
        "ledger_updates": ledger,
        "counts": {"fills": len(fills), "funding": len(funding), "ledger": len(ledger)},
    }
    out_path.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    print(f"  + exports/{day_str}.json  (fills={len(fills)} funding={len(funding)} ledger={len(ledger)})")
    return out_path


def take_snapshot(generated_at):
    """Current account state: perp+spot equity, positions, HL portfolio (reference)."""
    state = hl_post({"type": "clearinghouseState", "user": ACCOUNT})
    spot = hl_post({"type": "spotClearinghouseState", "user": ACCOUNT})
    portfolio = hl_post({"type": "portfolio", "user": ACCOUNT})
    upnl = sum(float(p["position"].get("unrealizedPnl") or 0)
               for p in state.get("assetPositions", []))
    spot_usdc_total, spot_hold = 0.0, 0.0
    for b in spot.get("balances", []):
        if b.get("coin") == "USDC":
            spot_usdc_total = float(b.get("total", 0))
            spot_hold = float(b.get("hold", 0))
    # Unified account + isolated positions: the isolated margin (hold, already part
    # of the spot total) INCLUDES accrued uPnL (verified empirically 2026-06-10:
    # hold = initial margin + uPnL, and spot total moves with uPnL).
    # Equity = free spot cash + perp account value (margin + uPnL).
    perp_av = float(state["marginSummary"]["accountValue"])
    equity = (spot_usdc_total - spot_hold) + perp_av
    snap = {
        "schema": "combo_g_trackrecord.snapshot.v1",
        "account": ACCOUNT,
        "generated_at": generated_at,
        "equity_usd": equity,
        "spot_usdc_total": spot_usdc_total,
        "spot_usdc_hold": spot_hold,
        "perp_account_value": perp_av,
        "unrealized_pnl": upnl,
        "n_open_positions": len(state.get("assetPositions", [])),
        "clearinghouse_state": state,
        "spot_state": spot,
        "portfolio_alltime_tail": dict(portfolio).get("allTime", {}).get("accountValueHistory", [])[-5:],
    }
    name = generated_at.replace(":", "").replace("-", "")[:15]  # YYYYMMDDTHHMMSS
    out_path = SNAPSHOTS / f"{name}Z.json"
    if out_path.exists():
        return None
    out_path.write_text(json.dumps(snap, indent=1, sort_keys=True), encoding="utf-8")
    print(f"  + snapshots/{out_path.name}  equity=${equity:,.2f} "
          f"(free spot {spot_usdc_total - spot_hold:,.2f} + perp AV {perp_av:,.2f}, incl. uPnL {upnl:+,.2f})")
    return out_path


def sha256_file(path):
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def append_sums(paths):
    """Append-only: only adds files not yet listed."""
    existing = set()
    if SUMS.exists():
        for line in SUMS.read_text(encoding="utf-8").splitlines():
            if line.strip():
                existing.add(line.split(maxsplit=1)[1].strip())
    lines = []
    for p in paths:
        rel = p.relative_to(ROOT).as_posix()
        if rel in existing:
            continue
        lines.append(f"{sha256_file(p)}  {rel}")
    if lines:
        with SUMS.open("a", encoding="utf-8", newline="\n") as f:
            for line in lines:
                f.write(line + "\n")
        print(f"  + SHA256SUMS: {len(lines)} entry(ies)")


def verify_sums():
    if not SUMS.exists():
        print("SHA256SUMS missing")
        return 1
    bad = 0
    n = 0
    for line in SUMS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split(maxsplit=1)
        p = ROOT / rel.strip()
        n += 1
        if not p.exists():
            print(f"MISSING: {rel}")
            bad += 1
        elif sha256_file(p) != digest:
            print(f"HASH MISMATCH: {rel}")
            bad += 1
    print(f"{n - bad}/{n} files OK")
    return 1 if bad else 0


def telegram_alert(text):
    """Best-effort alert. Token read from OUTSIDE the repo — never committed here."""
    try:
        env = {}
        env_path = Path.home() / ".trackrecord_alert.env"
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
        token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
        if not (token and chat):
            return
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat, "text": text}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, context=CTX, timeout=15).read()
    except Exception as e:
        print(f"  ! telegram alert failed: {e}")


def git_commit(msg):
    subprocess.run(["git", "add", "-A"], cwd=ROOT, check=True)
    r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
    if r.returncode == 0:
        print("  git: nothing new to commit")
        return
    subprocess.run(["git", "commit", "-m", msg], cwd=ROOT, check=True)
    # Push: a failure is NOT silent. (A past remote desync went unnoticed for 27 days:
    # push rejected every night, logged but never alerted.)
    r = subprocess.run(["git", "push"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode == 0:
        print("  git: commit OK, push OK")
    else:
        err = (r.stderr or "").strip()[:200]
        print(f"  git: commit OK, PUSH FAILED ({err})")
        telegram_alert("🔴 [trackrecord] GITHUB PUSH REJECTED — the public track record "
                       "is no longer updating (data remains archived/hashed "
                       "on the server). Fix promptly.\n" + err)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="UTC day to export (YYYY-MM-DD); default = yesterday UTC")
    ap.add_argument("--backfill", nargs=2, metavar=("START", "END"),
                    help="export every day from START to END inclusive")
    ap.add_argument("--no-commit", action="store_true")
    ap.add_argument("--no-snapshot", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    if args.verify:
        sys.exit(verify_sums())

    EXPORTS.mkdir(exist_ok=True)
    SNAPSHOTS.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    if args.backfill:
        d = datetime.strptime(args.backfill[0], "%Y-%m-%d")
        end = datetime.strptime(args.backfill[1], "%Y-%m-%d")
        days = []
        while d <= end:
            days.append(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)
    else:
        days = [args.date or (now - timedelta(days=1)).strftime("%Y-%m-%d")]

    today = now.strftime("%Y-%m-%d")
    new_files = []
    for day in days:
        if day >= today:
            print(f"  ! {day}: UTC day not finished — refused (an export must never be partial)")
            continue
        p = export_day(day, generated_at)
        if p:
            new_files.append(p)
        time.sleep(0.3)

    if not args.no_snapshot:
        p = take_snapshot(generated_at)
        if p:
            new_files.append(p)

    if new_files:
        append_sums(new_files)
    if not args.no_commit:
        # rebuild NAV + ±1% verification (best-effort: a verification failure does not
        # block archiving of raw data, but is visible in the log/commit message)
        r = subprocess.run([sys.executable, str(ROOT / "build_nav.py")], cwd=ROOT)
        nav_ok = "nav OK" if r.returncode == 0 else "NAV/VERIFY FAILED"
        label = f"{days[0]}..{days[-1]}" if len(days) > 1 else days[0]
        git_commit(f"export {label} ({len(new_files)} file(s), {nav_ok}) — {generated_at}")


if __name__ == "__main__":
    main()
