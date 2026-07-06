---
title: Nuclear Renaissance Index
description: A single-file research dashboard for nuclear-energy equities — extended into a multi-vertical watchlist and alerts platform.
---

# Nuclear Renaissance Index

A single-file, browser-based research dashboard for nuclear-energy equities
— plus a multi-vertical watchlist and alerts platform that runs on your own
GitHub Actions.

<p align="center">
  <a href="./Nuclear-Renaissance-Index.html" style="display:inline-block;padding:14px 28px;background:#5dc88b;color:#0f1115;font-weight:600;border-radius:6px;text-decoration:none;font-size:18px;">Open the site →</a>
</p>

---

## What you can do

**📊 Track the index** — a composite of ~20 nuclear-energy equities, tiered
equal-weight, with live intraday updates every 5 minutes while the tab is
open.

**📋 Build watchlists** — create named baskets for any vertical.
One-click preset clones for Semiconductors, Defense, AI Infrastructure,
Uranium & Fuel, Biotech, Clean Energy, Fintech, Cybersecurity, Rare
Earths, Data Centers. Add or remove tickers freely.

**📈 Chart with indicators** — SMA 20/50/200, EMA 20, Bollinger 20/2,
RSI 14, MACD 12/26/9, volume bars. Toggle per watchlist.

**🚨 Set rule-based alerts** — price thresholds, % change from anchor
dates, RSI overbought/oversold, SMA golden/death cross, MACD signal
crosses, volume spikes. Fires within ~10 minutes during market hours.
Delivered to email — with optional SMS via free carrier gateways.

**⚡ Optimize a portfolio** — Sharpe / min-vol / equal-weight allocations
at your chosen max position size, computed from real historical returns.

**📅 Get scheduled digests** — daily / weekly / monthly / quarterly /
yearly catalyst summaries with upcoming milestones and pre-IPO watches.

**🔬 Explore pre-IPO names** — X-Energy, TerraPower, Kairos Power, Last
Energy, Commonwealth Fusion, Helion, TAE, Seaborg, General Fusion.
Status tracking through S-1 / SPAC / imminent.

---

## Under the hood

- **One HTML file.** No signup, no backend, works offline. Airdrop it to
  your phone and open from Files app — it still works.
- **GitHub Actions runs the schedule.** Cron every 15 min during US
  market hours evaluates your alerts. Cron at 08:00 UTC daily sends the
  digest if today matches your cadence.
- **Everything you configure lives in this repo.** Watchlists →
  `data/watchlists.json`. Alerts → `data/alerts.json`. Digest cadence →
  `data/email_settings.json`. All pushed from the site via GitHub API.
- **No API keys required.** Prices via Stooq CSV + Yahoo Finance JSON, no
  signup on either.

---

## Getting started

1. **[Open the site](./Nuclear-Renaissance-Index.html)**
2. Go to **Methodology → Email digest schedule**
3. Follow the 5-step first-time setup checklist inside the card
4. Add alert rules on the **Alerts** tab, click **Push to GitHub**

Full instructions in [README.md](./README.md).

---

## What NRI is not

Not tick-level real-time (~8–10 min latency floor without a paid feed).
Not a market-wide screener. Not a broker. Not multi-user. Honest scope
notes in [WHATS_NEXT.md](./WHATS_NEXT.md).

---

<p style="color:#888;font-size:13px;text-align:center;">
Research and analytics only. Nothing here is investment advice. Prices are
sourced from public feeds and can be delayed, missing, or incorrect.
</p>
