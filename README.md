# Nuclear Renaissance Index (NRI)

A single-file, browser-based research dashboard for nuclear-energy equities
— extended into a multi-vertical watchlist and alerts platform.

**Live site:** `https://<your-username>.github.io/<your-repo>/`

Emails, alerts, and scheduled digests run in this repo's GitHub Actions.

---

## What's inside

| Tab | What it does |
|---|---|
| **Dashboard** | Composite index chart + top movers + benchmarks (S&P 500, uranium ETF, nuclear ETF). Auto-refreshes intraday every 5 minutes while the tab is open. |
| **Watchlists** | Named baskets for any vertical. One-click presets: Semiconductors, Defense & Aerospace, AI Infrastructure, Uranium & Fuel, Biotech, Clean Energy, Fintech, Cybersecurity, Rare Earths, Data Centers, Nuclear (NRI). Add/remove tickers, toggle indicators, chart on demand. |
| **Companies** | The 20-ish index constituents specifically — per-company view with thesis and metrics. Multi-stock compare tool at the top. |
| **Optimizer** | Portfolio weights that maximize Sharpe, minimize vol, or equal-weight, at 15%/20%/25% max position sizes. |
| **Backtest** | Composite vs. benchmarks over your chosen range. |
| **Updates** | Merged feed of catalysts (dated milestones) + sector news. |
| **Pre-IPO** | X-Energy, TerraPower, Kairos, Last Energy, Commonwealth Fusion, Helion, TAE, Seaborg, General Fusion. |
| **Profile** | Risk profile — feeds the optimizer and alert defaults. |
| **Alerts** | Rule engine. Add rules like "NVDA price > 200" or "OKLO % change from 2026-01-01 ≥ 25". Pushes to `data/alerts.json` in the repo via the GitHub API. |
| **Methodology** | Index construction, editable constituent weights, add/remove constituents, and the Email digest schedule card. |

---

## First-time setup (about 5 minutes, all on websites)

Everything after this happens on the site itself.

### 1. Publish the site (one Terminal command)

```bash
bash ~/Documents/NRI/publish.sh
```

Turn on Pages if you haven't: repo → **Settings → Pages → Deploy from branch → main → /(root)**.

### 2. Make a Gmail App Password

Go to <https://myaccount.google.com/apppasswords>. You need 2-Step
Verification turned on first (Google walks you through it if not). Create
an app password called "NRI" and copy the 16-character code.

### 3. Add it as a repo secret

Open `github.com/<you>/<repo>/settings/secrets/actions/new`:

- **Name:** `GMAIL_APP_PASSWORD`
- **Secret:** the 16-character code

### 4. Generate a GitHub Personal Access Token

<https://github.com/settings/tokens/new?description=NRI+site&scopes=repo,workflow>

Scopes come pre-selected — just click **Generate token** and copy it.

### 5. Enter everything in the site

Open your Pages URL → **Methodology** tab → **Email digest schedule** card.
Paste your Gmail address, the token, pick a cadence, click **Save & enable**.

The site commits the workflow and Python send script into your repo
automatically. The next scheduled run sends you a test digest.

---

## Using it day-to-day

### Watchlists

**Watchlists tab** → dropdown at the top shows your lists.

- **Clone a preset:** pick from the dropdown, click **+ Clone preset**.
- **Start blank:** type a name, click **+ Blank**.
- **Add a ticker:** type it, press Enter or click **+ Add**.
- **Toggle indicators:** SMA 20/50/200, EMA 20, Bollinger, RSI, MACD, Volume — checkboxes apply to charts opened from this list.
- **Chart a ticker:** click the **Chart** button on any row.

Everything persists in your browser via `localStorage`. Click **Push to
GitHub** on the Alerts tab to mirror your watchlists to
`data/watchlists.json` so the server-side alert engine can see them.

### Alerts

**Alerts tab** — three sections.

**Notification channels** — Gmail address (primary) and optionally a phone
number + carrier for SMS via free email-to-SMS gateway.

**Rules** — pick a ticker, pick a rule type, fill in args, **+ Add rule**:

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

Click **Push to GitHub** after any change to make rules live in the workflow.

### Digest schedule

**Methodology tab → Email digest schedule** — cadence picker for the
catalyst / pre-IPO summary email (daily / weekly / monthly / quarterly /
yearly). Independent from alerts, which run at 15-min intervals during
market hours regardless of digest cadence.

---

## What runs when

Two crons live in `.github/workflows/nri-email.yml`:

- `*/15 13-20 * * 1-5` — every 15 minutes on weekdays during US market
  hours. Pulls fresh Yahoo 5-minute bars, evaluates every enabled rule,
  emails you (+ SMS-cc) for any new fires.
- `0 8 * * *` — 08:00 UTC daily digest.

Both runs commit `data/alerts_state.json` (dedup ledger) back to the repo
so the same event doesn't re-alert tomorrow.

**Typical alert latency: 8–10 minutes.**

---

## Publishing updates

```bash
bash ~/Documents/NRI/publish.sh                        # default commit msg
bash ~/Documents/NRI/publish.sh "Custom commit here"   # or a specific one
```

The script rebuilds the HTML, commits changes, and pushes. Pages redeploys
in ~1 minute. Hard-refresh (⌘-Shift-R) to bypass browser cache.

---

## File map

```
NRI/
├── README.md                              — this file
├── UPDATE_GUIDE.md                        — per-update walkthrough
├── WHATS_NEXT.md                          — honest scope vs. TradeVision
├── build_static_site.py                   — the build script
├── publish.sh                             — one-shot rebuild + git push
├── refresh.command / refresh.bat          — double-click rebuild helpers
├── index.html                             — the built site (Pages root)
├── Nuclear-Renaissance-Index.html         — legacy copy of same file
├── .nojekyll                              — tells Pages to skip Jekyll
├── data/
│   ├── constituents.json                  — index constituents
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

Legacy local-cron path (kept for reference, not required):
`setup_email.sh`, `daily_email.py`, `com.nri.daily.plist`,
`DAILY_EMAIL_SETUP.md`.

---

## Troubleshooting

**Site isn't updating.**
Hard-refresh (⌘-Shift-R). Check **Actions** tab on GitHub — the "pages
build and deployment" workflow should have a green check within a minute
of your push. If it didn't run, make sure Pages is enabled.

**Prices are stale in the header bar.**
Click **Test proxies** in the live bar. If all show FAIL, an ad-blocker
(uBlock, Brave Shields) is probably blocking known CORS-proxy hostnames.
Allowlist `corsproxy.io`, `r.jina.ai`, `api.allorigins.win`. If some pass,
click **Reset state** then **↻ Refresh**.

**Alert workflow isn't running.**
Check `github.com/<you>/<repo>/actions`. If the workflow was just added,
GitHub can take a few minutes to register the cron. Click **Run workflow**
on the workflow page for a manual test.

**"GMAIL_APP_PASSWORD is missing" in the workflow log.**
You skipped step 3 of first-time setup — add the secret.

**"Save & enable" fails with 403.**
Your GitHub PAT is missing the `workflow` scope. Regenerate at
<https://github.com/settings/tokens/new?description=NRI+site&scopes=repo,workflow>.

**SMS isn't landing.**
Free carrier email-to-SMS gateways are unreliable. To fix reliably:
Twilio (~$2/month + $0.008/message). See `WHATS_NEXT.md`.

**A ticker isn't showing intraday data.**
Yahoo doesn't publish intraday for every ticker (pre-IPOs, low-volume
ADRs, foreign listings). Those fall back to daily bars.

---

## Data sources

- **Historical daily bars:** Stooq CSV, falling back to Yahoo Finance JSON.
- **Intraday bars:** Yahoo Finance 5-minute JSON (server-side + client-side, no key).
- **Static reference data:** hand-curated JSON files under `data/`.

None of this requires an API key. If free proxies get throttled, you can
deploy your own Cloudflare Worker (see the auto-generated
`cloudflare-worker.js`).

---

## What NRI is not (yet)

Full breakdown in `WHATS_NEXT.md`. Short version:

- Not tick-level real-time — 8–10 min latency floor without a paid feed
- Not a screener — shows what you put in your watchlists
- Not an order-routing platform — no broker integration
- Not multi-user — settings live in your browser + your repo

---

## License / attribution

Research and analytics only. Nothing here is investment advice. Prices are
sourced from public feeds and can be delayed, missing, or incorrect.
Verify anything actionable against your broker's data before trading.
