#!/usr/bin/env python3
"""
hopper_greenout.py
-------------------
Tracks aggregate market cap of top US vs. China AI companies, logs a
timestamped benchmark row to data/log.csv, and auto-commits/pushes the
change to git -- but only when the data actually moved.

Run modes:
    python hopper_greenout.py --once       # single fetch+commit cycle (used by CI)
    python hopper_greenout.py --loop       # long-running scheduler (every N minutes)
    python hopper_greenout.py --selftest   # offline check of the pure logic

Data source: Yahoo Finance's public /v8/finance/chart endpoint (no API key
required). Market cap = live price * a static shares-outstanding estimate
(see COMPANIES below) -- shares outstanding barely moves day to day for
these companies, so this is a fine approximation for a growth/decline
benchmark. Re-check the numbers every few months if you want more accuracy.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone

import requests

# --------------------------------------------------------------------------
# Config -- override any of these via environment variables or a .env file
# next to this script. No new dependency needed: DOTENV_PATH is parsed by
# hand below.
# --------------------------------------------------------------------------

def _load_dotenv(path):
    """Minimal .env loader: KEY=VALUE per line, '#' comments, no quoting."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

TARGET_REPO_PATH = os.environ.get("HG_TARGET_REPO_PATH", SCRIPT_DIR)
DATA_SOURCE_URL = os.environ.get(
    "HG_DATA_SOURCE_URL", "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
)
DATA_API_KEY = os.environ.get("HG_DATA_API_KEY", "")  # sent as Bearer token if set
SCHEDULE_MINUTES = float(os.environ.get("HG_SCHEDULE_MINUTES", "10"))
GIT_REMOTE = os.environ.get("HG_GIT_REMOTE", "origin")
GIT_BRANCH = os.environ.get("HG_GIT_BRANCH", "")  # empty = auto-detect current branch
REQUEST_TIMEOUT = float(os.environ.get("HG_REQUEST_TIMEOUT", "10"))

LOG_CSV_PATH = os.path.join(TARGET_REPO_PATH, "data", "log.csv")
ERROR_LOG_PATH = os.path.join(TARGET_REPO_PATH, "logs", "error.log")

CSV_FIELDS = [
    "timestamp",
    "us_total_market_cap_usd",
    "china_total_market_cap_usd",
    "us_pct_change",
    "china_pct_change",
    "leader",
    "top_movers",
    "companies_json",
]

# ticker -> (display name, shares outstanding in billions, price currency)
# Figures are approximate (mid-2026 public filings) -- update periodically.
US_COMPANIES = {
    "NVDA": ("NVIDIA", 24.4, "USD"),
    "MSFT": ("Microsoft", 7.43, "USD"),
    "GOOGL": ("Alphabet", 12.1, "USD"),
    "META": ("Meta Platforms", 2.59, "USD"),
    "AMZN": ("Amazon", 10.5, "USD"),
}
CHINA_COMPANIES = {
    "BABA": ("Alibaba", 2.05, "USD"),
    "BIDU": ("Baidu", 0.318, "USD"),
    "PDD": ("PDD Holdings", 1.34, "USD"),
    "0700.HK": ("Tencent", 9.1, "HKD"),
}
COMPANIES = {**US_COMPANIES, **CHINA_COMPANIES}
REGION_OF = {t: "us" for t in US_COMPANIES} | {t: "china" for t in CHINA_COMPANIES}

FX_TO_USD = {"USD": 1.0, "HKD": 0.128}  # static approx rate, update if it drifts

TREND_EPSILON_PCT = 0.05  # below this magnitude, call it "flat" not grow/decline


# --------------------------------------------------------------------------
# Error logging -- never let a bad fetch/commit kill the scheduler
# --------------------------------------------------------------------------

def log_error(message: str) -> None:
    os.makedirs(os.path.dirname(ERROR_LOG_PATH), exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with open(ERROR_LOG_PATH, "a") as f:
        f.write(f"[{ts}] {message}\n")


# --------------------------------------------------------------------------
# Fetch + analyze
# --------------------------------------------------------------------------

def fetch_price(ticker: str) -> float:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; hopper-greenout/1.0)"}
    if DATA_API_KEY:
        headers["Authorization"] = f"Bearer {DATA_API_KEY}"
    url = DATA_SOURCE_URL.format(ticker=ticker)
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    meta = resp.json()["chart"]["result"][0]["meta"]
    price = meta.get("regularMarketPrice")
    if price is None:
        raise ValueError(f"no regularMarketPrice for {ticker}")
    return float(price)


def fetch_all_prices(tickers) -> dict:
    """Best-effort fetch: a single ticker failure is logged and skipped."""
    prices = {}
    for ticker in tickers:
        try:
            prices[ticker] = fetch_price(ticker)
        except Exception as exc:
            log_error(f"fetch failed for {ticker}: {exc}")
    return prices


def compute_market_caps(prices: dict) -> dict:
    caps = {}
    for ticker, price in prices.items():
        _, shares_billions, currency = COMPANIES[ticker]
        fx = FX_TO_USD[currency]
        caps[ticker] = price * shares_billions * 1e9 * fx
    return caps


def region_total(caps: dict, region: str) -> float:
    return sum(v for t, v in caps.items() if REGION_OF.get(t) == region)


def pct_change(new: float, old: float):
    if old in (None, 0):
        return None
    return (new - old) / old * 100.0


def trend_label(pct):
    if pct is None:
        return "n/a"
    if pct > TREND_EPSILON_PCT:
        return "growth"
    if pct < -TREND_EPSILON_PCT:
        return "decline"
    return "flat"


def top_movers_str(caps_now: dict, caps_prev: dict, n=2) -> str:
    if not caps_prev:
        return ""
    moves = []
    for ticker, cap in caps_now.items():
        prev = caps_prev.get(ticker)
        pct = pct_change(cap, prev)
        if pct is not None:
            moves.append((ticker, pct))
    moves.sort(key=lambda tp: abs(tp[1]), reverse=True)
    return ", ".join(f"{t} {p:+.1f}%" for t, p in moves[:n])


def build_row(caps_now: dict, prev_row: dict | None) -> dict:
    prev_caps = json.loads(prev_row["companies_json"]) if prev_row else {}
    us_total = region_total(caps_now, "us")
    china_total = region_total(caps_now, "china")
    prev_us_total = float(prev_row["us_total_market_cap_usd"]) if prev_row else None
    prev_china_total = float(prev_row["china_total_market_cap_usd"]) if prev_row else None

    us_pct = pct_change(us_total, prev_us_total)
    china_pct = pct_change(china_total, prev_china_total)

    if us_pct is None or china_pct is None:
        leader = "n/a"
    elif abs(us_pct - china_pct) < TREND_EPSILON_PCT:
        leader = "tie"
    else:
        leader = "US" if us_pct > china_pct else "China"

    return {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "us_total_market_cap_usd": f"{us_total:.2f}",
        "china_total_market_cap_usd": f"{china_total:.2f}",
        "us_pct_change": "" if us_pct is None else f"{us_pct:.4f}",
        "china_pct_change": "" if china_pct is None else f"{china_pct:.4f}",
        "leader": leader,
        "top_movers": top_movers_str(caps_now, prev_caps),
        "companies_json": json.dumps(caps_now, separators=(",", ":")),
    }


def rows_differ(new_row: dict, prev_row: dict | None) -> bool:
    """Compare rounded totals/pct-changes so FX-noise-level moves don't
    trigger a commit, but genuine market moves do."""
    if prev_row is None:
        return True
    keys = [
        "us_total_market_cap_usd",
        "china_total_market_cap_usd",
        "us_pct_change",
        "china_pct_change",
    ]
    for key in keys:
        a = round(float(new_row[key] or 0), 2)
        b = round(float(prev_row[key] or 0), 2)
        if a != b:
            return True
    return False


# --------------------------------------------------------------------------
# CSV read/write
# --------------------------------------------------------------------------

def read_last_row(csv_path: str):
    if not os.path.exists(csv_path):
        return None
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[-1] if rows else None


def append_csv_row(csv_path: str, row: dict) -> None:
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    write_header = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


# --------------------------------------------------------------------------
# Git (plain subprocess -- no GitPython needed)
# --------------------------------------------------------------------------

def _git(repo_path, *args):
    result = subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def current_branch(repo_path: str) -> str:
    return GIT_BRANCH or _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")


def build_commit_message(row: dict) -> str:
    movers = row["top_movers"] or "no per-company deltas yet"
    return f"Update AI market cap log: {movers} ({row['timestamp']})"


def git_commit_and_push(repo_path: str, files, message: str) -> None:
    _git(repo_path, "add", *files)
    _git(repo_path, "commit", "-m", message)
    branch = current_branch(repo_path)
    _git(repo_path, "push", GIT_REMOTE, branch)


# --------------------------------------------------------------------------
# One run cycle
# --------------------------------------------------------------------------

def run_once() -> None:
    try:
        prices = fetch_all_prices(COMPANIES.keys())
        if not prices:
            log_error("run_once: all price fetches failed, skipping this cycle")
            return

        caps_now = compute_market_caps(prices)
        prev_row = read_last_row(LOG_CSV_PATH)
        row = build_row(caps_now, prev_row)

        if not rows_differ(row, prev_row):
            return  # no material change -> no log entry, no commit

        append_csv_row(LOG_CSV_PATH, row)

        message = build_commit_message(row)
        rel_csv = os.path.relpath(LOG_CSV_PATH, TARGET_REPO_PATH)
        try:
            git_commit_and_push(TARGET_REPO_PATH, [rel_csv], message)
        except Exception as exc:
            log_error(f"git commit/push failed: {exc}")

    except Exception:
        log_error("run_once: unhandled error:\n" + traceback.format_exc())


def run_loop() -> None:
    interval_seconds = max(SCHEDULE_MINUTES, 0.1) * 60
    while True:
        run_once()
        time.sleep(interval_seconds)


# --------------------------------------------------------------------------
# Self-test (offline, no network/git) -- run with --selftest
# --------------------------------------------------------------------------

def _selftest():
    caps_prev = {"NVDA": 1000.0, "BABA": 500.0}
    caps_now = {"NVDA": 1050.0, "BABA": 480.0}
    prev_row = {
        "us_total_market_cap_usd": "1000.00",
        "china_total_market_cap_usd": "500.00",
        "us_pct_change": "",
        "china_pct_change": "",
        "companies_json": json.dumps(caps_prev),
    }

    row = build_row(caps_now, prev_row)
    assert row["leader"] == "US", row
    assert "NVDA" in row["top_movers"] and "BABA" in row["top_movers"], row
    assert rows_differ(row, prev_row) is True

    same_row = build_row(caps_prev, prev_row)  # no-op fetch, identical data
    same_row["companies_json"] = prev_row["companies_json"]
    assert rows_differ(same_row, prev_row) is False, same_row

    assert trend_label(1.0) == "growth"
    assert trend_label(-1.0) == "decline"
    assert trend_label(0.001) == "flat"

    msg = build_commit_message(row)
    assert msg.startswith("Update AI market cap log:"), msg

    print("selftest OK")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="run a single fetch+commit cycle")
    group.add_argument("--loop", action="store_true", help="run forever on HG_SCHEDULE_MINUTES")
    group.add_argument("--selftest", action="store_true", help="offline logic check")
    args = parser.parse_args()

    if args.selftest:
        _selftest()
    elif args.loop:
        run_loop()
    else:
        run_once()  # default: single run (also what --once does)


if __name__ == "__main__":
    main()
