# Nuclear Renaissance Index (NRI)

A live web app that tracks, analyzes, and optimizes a curated basket of public companies exposed to the nuclear power revival — utilities, fuel cycle, SMR developers, services, and frontier R&D.

## What it does

- **Dashboard**: composite index level, 1D/YTD/1Y returns, annualized vol, index health scores (regulatory risk, uranium exposure, capacity factor, debt/cap), sector mix, top movers, full constituent table, and upcoming catalysts.
- **Company pages**: 5Y price chart, fundamentals from Yahoo Finance, custom metric scorecard, per-company catalyst list.
- **Optimizer**: mean-variance (max-Sharpe or min-vol) over the constituents with configurable per-name cap and lookback. Plots the efficient frontier.
- **Backtest**: composite NRI rebased to 100 against SPY, XLU (utilities), URA (uranium ETF), and NLR (nuclear ETF) with CAGR, vol, Sharpe, and max drawdown.
- **Catalysts**: hand-curated calendar of NRC licensing milestones, SPAC closes, PPAs, and project decisions.
- **Methodology**: how weights and custom scores are built.

## Local run

```bash
cd nuclear-index
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py
# open http://127.0.0.1:8000
```

First page load pulls ~20 tickers from Yahoo and caches to `./.cache/`. Subsequent loads are fast.

## Deploy

### Render (recommended, free tier works)
1. Push this folder to a GitHub repo.
2. On Render, create a new *Web Service* pointing at the repo.
3. It will auto-detect `render.yaml`. Hit **Deploy**.
4. Your site will be at `https://<name>.onrender.com`.

### Railway / Fly.io
The `Procfile` works on Railway. For Fly.io, add a simple `fly.toml` and use the same start command.

### Any VM
```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Put nginx in front for TLS.

## Structure

```
nuclear-index/
├── app/
│   ├── main.py          FastAPI routes + Jinja rendering
│   ├── data.py          yfinance layer + disk cache
│   ├── metrics.py       per-company derived metrics
│   ├── index_calc.py    composite index time series
│   ├── optimizer.py     scipy mean-variance optimizer + frontier
│   ├── backtest.py      NRI vs benchmarks
│   └── templates/       Jinja2 HTML
├── static/style.css
├── data/
│   ├── constituents.json   ← edit to add/remove names, tweak scores
│   └── catalysts.json      ← edit to add upcoming events
├── requirements.txt
├── Procfile
├── render.yaml
└── run.py
```

## Editing the index

To add a company, open `data/constituents.json` and append an object to `constituents`:

```json
{
  "ticker": "ABCD",
  "name": "Example Corp",
  "sector": "smr_developers",
  "sub_sector": "smr",
  "country": "US",
  "thesis": "Why this name.",
  "dev_stage": "Pre-Commercial",
  "regulatory_risk": 6,
  "capacity_factor": null,
  "uranium_exposure": 4,
  "nuclear_revenue_share": 1.0,
  "is_private": false,
  "listed": true
}
```

Restart the server. The new name appears on the dashboard and flows through the optimizer and backtest.

## Custom metrics

| Metric | Range | Meaning |
|---|---|---|
| Development Stage | R&D · Pre-Commercial · Construction · Operational · Diversified | Where the company sits on the commercialization curve |
| Regulatory Risk | 0–10 | Exposure to NRC/CNSC licensing, permitting, policy reversals |
| Capacity Factor | 0–1 | Utilization of nuclear/uranium production assets (operators only) |
| Uranium Exposure | 0–10 | How much revenue/margin is tied to uranium spot/term prices |
| Debt / Capitalization | 0–1 | `totalDebt / (totalDebt + marketCap)`; pulled from Yahoo |

Index-level scores are market-cap-weighted averages across listed constituents.

## Notes

- **GFUZ (General Fusion)** is included but unlisted until mid-2026 SPAC close. The app marks it as *pending* and uses Spring Valley Acquisition Corp. III (`SVACU`) as a proxy ticker in the meantime — adjust in `constituents.json` after the merger closes.
- **RYCEY** (Rolls-Royce ADR) is used instead of `RR.L` for cleaner USD-denominated comparisons.
- **CDRE** (Cadre Holdings) has only tangential nuclear exposure; it carries a low weight.
- Yahoo Finance sometimes returns empty data for thinly traded names (e.g., immediately post-de-SPAC). These show `–` until data is available.
- Not investment advice. Scores are opinion-based.
