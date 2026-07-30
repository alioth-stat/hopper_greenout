# 🐹 hopper_greenout

**A tiny bot that watches US vs. China AI-company market cap and only speaks up when something actually moved.**

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Deps](https://img.shields.io/badge/dependencies-1%20(requests)-brightgreen)
![Schedule](https://img.shields.io/badge/runs-every%2010%20min-orange)
[![Auto Commit](https://github.com/OWNER/REPO/actions/workflows/auto_commit.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/auto_commit.yml)

> Swap `OWNER/REPO` in the badge above once this is pushed to GitHub.

---

## What it does

Every 10 minutes, `hopper_greenout.py`:

1. Pulls live prices for a basket of top **US** and **China** AI-adjacent companies from Yahoo Finance's free quote endpoint.
2. Estimates each company's market cap (`price × shares outstanding`) and totals each region.
3. Compares the new totals to the last logged row to compute % change and a `growth` / `decline` / `flat` trend per region.
4. Appends one row to [`data/log.csv`](data/log.csv) — **but only if the numbers actually changed** from the previous row.
5. Commits and pushes that row with a message summarizing the biggest movers, e.g.:

   ```
   Update AI market cap log: NVDA +1.8%, BABA -0.9% (2026-07-30 09:40)
   ```

No noise, no empty commits, no "nothing happened" spam in your git history.

## Companies tracked

| Region | Tickers |
|---|---|
| 🇺🇸 US | `NVDA` NVIDIA, `MSFT` Microsoft, `GOOGL` Alphabet, `META` Meta, `AMZN` Amazon |
| 🇨🇳 China | `BABA` Alibaba, `BIDU` Baidu, `PDD` PDD Holdings, `0700.HK` Tencent |

Edit `US_COMPANIES` / `CHINA_COMPANIES` at the top of `hopper_greenout.py` to change the basket. Shares-outstanding figures are static approximations (documented inline) — good enough for a trend benchmark, not for financial decisions.

## Quickstart

```bash
git clone <this repo>
cd hopper_greenout
pip install -r requirements.txt      # just `requests`
cp .env.example .env                 # optional, defaults work out of the box
python hopper_greenout.py --once     # single fetch + commit cycle
```

Run it as a background scheduler instead of one-shot:

```bash
python hopper_greenout.py --loop     # fetches every HG_SCHEDULE_MINUTES (default 10), forever
```

Sanity-check the logic without touching the network or git:

```bash
python hopper_greenout.py --selftest
```

## Configuration

All config lives in one block at the top of `hopper_greenout.py` and can be overridden by environment variables or a `.env` file (see `.env.example`):

| Variable | Default | What it controls |
|---|---|---|
| `HG_TARGET_REPO_PATH` | this script's directory | Which git repo gets the commit/push |
| `HG_DATA_SOURCE_URL` | Yahoo Finance chart endpoint | Where price data comes from |
| `HG_DATA_API_KEY` | *(empty)* | Sent as a Bearer token if you swap in a paid data provider |
| `HG_SCHEDULE_MINUTES` | `10` | Interval for `--loop` mode |
| `HG_GIT_REMOTE` | `origin` | Remote to push to |
| `HG_GIT_BRANCH` | *(auto-detect)* | Branch to push to |

## Commit-gating logic

A new row is only written and committed when the rounded US/China totals or % changes differ from the last row already in `data/log.csv` — see `rows_differ()` in the script. Market prices barely move outside trading hours, so this naturally goes quiet on weekends/holidays instead of committing 144 identical rows a day.

## Errors

Network hiccups, a bad ticker, or a git conflict are caught, logged to `logs/error.log` with a timestamp, and the run simply skips that cycle — `--loop` keeps going, and a scheduled GitHub Actions run just does nothing that cycle instead of failing loudly.

## Running on a schedule

**Locally / on a server:** run `python hopper_greenout.py --loop` under `systemd`, `tmux`, `pm2`, or similar so it survives reboots.

**GitHub Actions (recommended for a "self-updating" repo):** [`.github/workflows/auto_commit.yml`](.github/workflows/auto_commit.yml) runs `--once` on a `*/10 * * * *` cron and pushes straight from the runner. Set it up:

1. Add a repo secret `GH_TOKEN` — a personal access token (or fine-grained token) with `contents: write` on this repo.
2. Push this repo to GitHub. The workflow starts firing on its own schedule.
3. Trigger it manually anytime from the **Actions** tab (`workflow_dispatch`).

> GitHub disables scheduled workflows automatically after 60 days of repo inactivity — push a commit or re-enable it from the Actions tab if the log goes stale.

## Disabling the bot

- **Stop a local loop:** `Ctrl+C`, or kill the process/systemd unit running `--loop`.
- **Stop GitHub Actions:** go to *Actions → AI Market Cap Benchmark → ⋯ → Disable workflow*, or delete/comment out the `schedule:` trigger in `auto_commit.yml` and keep `workflow_dispatch` for manual runs only.
- **Stop it from pushing but keep logging locally:** unset `HG_GIT_REMOTE` handling isn't needed — just don't configure a git remote; commits will still happen locally and pushes will fail quietly into `logs/error.log`.

## Caveats

- Market cap is `price × static shares outstanding`, not a live shares count — fine for spotting trends, not a source of truth.
- Tencent (`0700.HK`) trades in HKD and is converted at a static approximate FX rate (`FX_TO_USD` in the script) — update it if it drifts far from reality.
- The Yahoo Finance endpoint is free and unauthenticated but unofficial; it can change without notice. Swap `HG_DATA_SOURCE_URL`/`HG_DATA_API_KEY` for a paid provider if you need an SLA.
