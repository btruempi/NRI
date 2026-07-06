# Nuclear Renaissance Index (NRI)

A single-file, browser-based research dashboard for nuclear-energy equities
— extended into a multi-vertical watchlist and alerts platform.

**Live site:** `https://<your-username>.github.io/<your-repo>/Nuclear-Renaissance-Index.html`
**How it stays up to date:** GitHub Actions runs a scheduled Python job in
this repo that pulls fresh prices, evaluates your alerts, and emails you.

---

## What's inside

| Tab | What it does |
|---|---|
| **Dashboard** | Composite index chart + top movers + benchmarks (S&P 500, uranium ETF, nuclear ETF). Live-updates every 5 minutes while the tab is open. |
| **Watchlists** | Named baskets for any vertical. Ships with one-click preset clones: Semiconductors, Defense & Aerospace, AI Infrastructure, Uranium & Fuel, Biotech, Clean Energy, Fintech, Cybersecurity, Rare Earths, Data Centers, plus the seeded Nuclear (NRI) list. Add/remove tickers, toggle indicators, click Chart to expand. |
| **Companies** | The 20-ish index constituents specifically — deeper per-company view with thesis and metrics. Multi-stock compare tool at the top. |
| **Optimizer** | Portfolio weights that maximize Sharpe / minimize vol / equal-weight, at 15%/20%/25% max position sizes. |
| **Backtest** | Composite vs. benchmarks over your chosen range. |
| **Updates** | Merged feed of catalysts (dated milestones) + sector news, sortable and filterable by ticker. |
| **Pre-IPO** | Watch list of private nuclear names — X-Energy, TerraPower, Kairos, Last Energy, Commonwealth Fusion, Helion, TAE, Seaborg, General Fusion. Status tags: private / filed / imminent. |
| **Profile** | Your risk profile — feeds the optimizer and alert defaults. |
| **Alerts** | Rule engine. Add rules like "NVDA price > 200" or "OKLO % change from 2026-01-01 ≥ 25". Pushes to `data/alerts.json` in the repo via the GitHub API. |
| **Methodology** | Index construction, editable constituent weights, add/remove constituents, and the Email digest schedule card. |

---

## First-time setup (about 5 minutes)

Only needed once. Everything after this happens on the site itself.

### 1. Publish the site (Terminal, one command)

If you haven't already published:

```bash
bash ~/Documents/NRI/publish.sh
```

This rebuilds the HTML and pushes to GitHub. GitHub Pages redeploys within
~1 minute.

Make sure Pages is turned on for the repo: **Settings → Pages → Deploy from
branch → main → /(root)**.

### 2. Make a Gmail App Password (5 minutes on a Google page)

Go to <https://myaccount.google.com/apppasswords>. You need 2-Step
Verification turned on first (Google walks you through it if not).

Create an app password called "NRI". Copy the 16-character code — you
won't see it again.

### 3. Add it as a repo secret (30 seconds on a GitHub page)

Go to `github.com/<you>/<repo>/settings/secrets/actions/new`:

- **Name:** `GMAIL_APP_PASSWORD`
- **Secret:** the 16-character code you just copied

### 4. Generate a GitHub Personal Access Token (2 minutes on a GitHub page)

Go to
<https://github.com/settings/tokens/new?description=NRI+site&scopes=repo,workflow>.
The scopes are pre-selected — just click **Generate token** and copy it.

### 5. Enter it in the site (30 seconds)

Open your published site → **Methodology** tab → **Email digest schedule**
card → paste your Gmail address, the token, pick a cadence (daily / weekly
/ monthly / quarterly / yearly), click **Save & enable**.

Done. The site commits the workflow and Python send script into your repo
automatically. The next scheduled run will send you a test digest.

---

## Using it day-to-day

### Watchlists

**Watchlists tab** → dropdown at the top shows your lists.

- **Clone a preset:** pick from Semiconductors / Defense / AI Infrastructure
  / etc, click **+ Clone preset**. It becomes your active list.
- **Start blank:** type a name in the "Blank list name" box, click **+ Blank**.
- **Add a ticker:** type it in the "Add ticker" box, press Enter or click **+ Add**.
- **Toggle indicators:** the checkboxes (SMA 20/50/200, EMA 20, Bollinger,
  RSI, MACD, Volume) apply to charts opened from this list.
- **Chart a ticker:** click the **Chart** button on any row.

Everything persists in your browser via `localStorage`. To make the same
watchlists visible to the alerts engine on the server, click **Push to
GitHub** on the Alerts tab (writes to `data/watchlists.json`).

### Alerts

**Alerts tab** → three sections:

**Notification channels** — Gmail address (primary) and optionally a phone
number + carrier for SMS via free email-to-SMS gateway (Verizon, AT&T,
T-Mobile, US Cellular, Cricket, Metro, Boost, Google Fi).

**Rules** — pick a ticker (any ticker in any of your watchlists shows up
in the dropdown), pick a rule type, fill in the args, click **+ Add rule**.

Rule types:

| Rule | Fires when |
|---|---|
| Price > | Close > value |
| Price < | Close < value |
| % change over N days | \|N-day return\| ≥ threshold |
| % change from anchor | \|return since date\| ≥ threshold |
| RSI > | RSI(14) > value (default 70 — overbought) |
| RSI < | RSI(14) < value (default 30 — oversold) |
| SMA golden cross | Fast SMA crosses ↑ slow SMA (default 50/200) |
| SMA death cross | Fast SMA crosses ↓ slow SMA |
| MACD cross ↑ signal | Bullish momentum shift |
| MACD cross ↓ signal | Bearish momentum shift |
| Volume spike | Today's volume ≥ multiplier × 20-day avg |

Click **Push to GitHub** after any change. Your rules become live on the
next scheduled workflow run.

### Email digest schedule

**Methodology tab → Email digest schedule** — cadence picker for the
catalyst / pre-IPO summary email (daily / weekly / monthly / quarterly /
yearly). Alerts run separately at 15-min intervals during market hours
and don't depend on this setting.

---

## What runs when

Two crons live in `.github/workflows/nri-email.yml`:

- `*/15 13-20 * * 1-5` — every 15 minutes on weekdays during US market
  hours (covers 9:30 AM–4:00 PM ET across both DST windows). Evaluates
  every enabled alert rule against fresh Yahoo 5-minute bars.
- `0 8 * * *` — 08:00 UTC daily. Sends the catalyst digest if today
  matches your cadence.

Both runs write to `data/alerts_state.json` (dedup ledger) and commit it
back to the repo so the same event doesn't re-alert tomorrow.

**Typical alert latency: 8–10 minutes.**

---

## Publishing updates

Every time the build script or data changes, run:

```bash
bash ~/Documents/NRI/publish.sh
```

Optional commit message:

```bash
bash ~/Documents/NRI/publish.sh "Add TerraPower position, tighten weights"
```

That command pulls the latest `build_static_site.py` from the Claude
scratchpad (if newer), rebuilds `Nuclear-Renaissance-Index.html`, commits
everything, and pushes to `origin/main`. Pages redeploys in ~1 minute.

After a push, hard-refresh (⌘-Shift-R) to bypass browser cache.

---

## File map

```
NRI/
├── README.md                              — this file
├── UPDATE_GUIDE.md                        — step-by-step for each update
├── WHATS_NEXT.md                          — honest scope vs. TradeVision
├── DAILY_EMAIL_SETUP.md                   — legacy launchd-based email setup
├── build_static_site.py                   — the build script
├── publish.sh                             — one-shot rebuild + git push
├── refresh.command / refresh.bat          — double-click rebuild helpers
├── setup_email.sh                         — legacy local-cron installer
├── daily_email.py                         — legacy local-cron sender
├── com.nri.daily.plist                    — legacy launchd plist
├── Nuclear-Renaissance-Index.html         — the built site (published)
├── data/
│   ├── constituents.json                  — the index constituents
│   ├── catalysts.json                     — dated catalyst calendar
│   ├── pre_ipo.json                       — private-company watchlist
│   ├── watchlists.json                    — pushed from the site
│   ├── alerts.json                        — pushed from the site
│   ├── alerts_state.json                  — dedup ledger (workflow-managed)
│   └── email_settings.json                — cadence + address
├── .github/workflows/
│   └── nri-email.yml                      — intraday + daily cron
└── scripts/
    └── maybe_send_email.py                — evaluates alerts + sends
```

---

## Troubleshooting

**"Site is not updating."**
Most common cause: browser cache. Hard-refresh (⌘-Shift-R). If still
stale, check the **Actions** tab on GitHub — the "pages build and
deployment" workflow should show a green check within a minute of your
push. If it hasn't run, make sure Pages is enabled on the repo.

**Prices are stale in the header bar.**
Click the **Test proxies** button. If all proxies say FAIL, you're
probably behind an ad-blocker (uBlock, Brave Shields) that blocks known
CORS-proxy hostnames — allowlist `corsproxy.io`, `r.jina.ai`,
`api.allorigins.win`. If some proxies pass, click **Reset state** and
then **↻ Refresh**.

**Alert workflow isn't running.**
Check `github.com/<you>/<repo>/actions`. If the workflow file was recently
added, GitHub sometimes takes a few minutes to register the cron. You can
also click **Run workflow** on the workflow page to trigger a manual test.

**"GMAIL_APP_PASSWORD is missing."**
You skipped step 3 of first-time setup. Add the secret at
`github.com/<you>/<repo>/settings/secrets/actions/new`.

**"Save & enable" fails with a 403.**
Your GitHub PAT is missing the `workflow` scope. Regenerate at
<https://github.com/settings/tokens/new?description=NRI+site&scopes=repo,workflow>.

**SMS isn't landing.**
Free carrier email-to-SMS gateways are unreliable — carriers throttle or
drop them. To fix: upgrade to Twilio (~$2/month + $0.008/message). See
`WHATS_NEXT.md` for the ~20-line code change.

**A ticker isn't showing intraday data.**
Yahoo doesn't publish intraday for every ticker (pre-IPOs, low-volume
ADRs, foreign listings). Those fall back to daily bars automatically —
alerts on them are end-of-day.

---

## Data sources

- **Historical daily bars:** Stooq CSV, falling back to Yahoo Finance JSON.
- **Intraday bars:** Yahoo Finance 5-minute JSON (server-side and
  client-side, no API key).
- **Static reference data:** hand-curated JSON files under `data/`.

None of this requires an API key. If free proxies get throttled, you can
deploy your own Cloudflare Worker (see the auto-generated
`cloudflare-worker.js` — instructions inside).

---

## What NRI is not (yet)

Full breakdown in `WHATS_NEXT.md`. Short version:

- Not tick-level real-time — 8–10 min latency floor without a paid feed
- Not a screener — shows what you put in your watchlists
- Not an order-routing platform — no broker integration
- Not multi-user — settings live in your browser + your repo, single seat

If any of those become blocking, the doc has concrete upgrade paths.

---

## License / attribution

Research and analytics only. Nothing here is investment advice. All prices
are sourced from public feeds and can be delayed, missing, or incorrect.
Verify anything actionable against your broker's data before trading.
