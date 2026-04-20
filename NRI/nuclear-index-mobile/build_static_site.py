"""Generate a single-file static demo of the Nuclear Renaissance Index.

Produces ./nri.html — a self-contained site that bundles all constituents,
catalysts, pre-IPO list, deterministic sample price series, precomputed
optimizer results, and a browser-side NL constraint agent.

Run:  python3 build_static_site.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / ".cache"
CHARTJS_CACHE = CACHE_DIR / "chart.umd.min.js"
CHARTJS_VERSION = "4.4.0"
CHARTJS_MIRRORS = [
    f"https://cdn.jsdelivr.net/npm/chart.js@{CHARTJS_VERSION}/dist/chart.umd.min.js",
    f"https://cdnjs.cloudflare.com/ajax/libs/Chart.js/{CHARTJS_VERSION}/chart.umd.min.js",
    f"https://unpkg.com/chart.js@{CHARTJS_VERSION}/dist/chart.umd.min.js",
]

# CLI args
_parser = argparse.ArgumentParser(description="Build the Nuclear Renaissance Index static site.")
_parser.add_argument("--offline", action="store_true",
                     help="Skip network fetches and use synthetic baseline for all tickers.")
_parser.add_argument("--no-cache", action="store_true",
                     help="Ignore on-disk cache and force a fresh fetch for every ticker.")
_parser.add_argument("--cache-max-hours", type=float, default=20.0,
                     help="Reuse cached data younger than this many hours (default: 20).")
_parser.add_argument("--quiet", action="store_true",
                     help="Suppress per-ticker fetch log lines.")
ARGS, _ = _parser.parse_known_args()


# ---------------------------------------------------------------------------
# Real-data fetch (Python-side; runs on the user's machine, no CORS).
# This replaces the "client browser must hit a CORS proxy" dance with a build-
# time fetch using plain urllib. We try Stooq CSV first (reliable, no auth,
# 30+ years of history, forgiving User-Agent policy) and fall back to Yahoo
# Finance JSON if Stooq doesn't have the ticker.
# ---------------------------------------------------------------------------

# Tickers that Stooq carries under a non-default code.
STOOQ_OVERRIDES = {
    "RYCEY": "rycey.us",
    "GFUZ":  None,       # pre-IPO / no listing yet
    "IMSR":  None,       # Stooq coverage unreliable; prefer Yahoo
}

def _stooq_code(ticker: str):
    if ticker in STOOQ_OVERRIDES:
        return STOOQ_OVERRIDES[ticker]
    return ticker.lower() + ".us"

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 11_0) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/14.0 Safari/605.1.15")

def _http_get(url: str, timeout: float = 10.0) -> str | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        # Stooq occasionally returns gzip even when not asked; urllib doesn't
        # auto-decode. Just try decoding as utf-8 and fall back to latin-1.
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            return body.decode("latin-1", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None

def fetch_stooq(ticker: str):
    """Return [{d, p, v}] from Stooq daily CSV, or None on failure."""
    code = _stooq_code(ticker)
    if not code:
        return None
    url = f"https://stooq.com/q/d/l/?s={code}&i=d"
    txt = _http_get(url)
    if not txt or "Date,Open" not in txt:
        return None
    lines = txt.strip().splitlines()
    if len(lines) < 5:
        return None
    out = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < 5:
            continue
        d = parts[0].strip()
        try:
            close = float(parts[4])
        except ValueError:
            continue
        try:
            vol = int(parts[5]) if len(parts) > 5 and parts[5].strip() else None
        except ValueError:
            vol = None
        out.append({"d": d, "p": round(close, 4), "v": vol})
    return out if len(out) >= 20 else None

def fetch_yahoo(ticker: str):
    """Return [{d, p, v}] from Yahoo Finance chart JSON, or None on failure."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?range=10y&interval=1d")
    txt = _http_get(url)
    if not txt:
        return None
    try:
        j = json.loads(txt)
    except json.JSONDecodeError:
        return None
    try:
        result = j["chart"]["result"][0]
        ts = result["timestamp"]
        q = result["indicators"]["quote"][0]
        closes = q["close"]
        vols = q.get("volume") or [None] * len(ts)
    except (KeyError, IndexError, TypeError):
        return None
    out = []
    for i, t in enumerate(ts):
        c = closes[i]
        if c is None:
            continue
        d = datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()
        v = vols[i] if i < len(vols) else None
        out.append({"d": d, "p": round(float(c), 4), "v": v})
    return out if len(out) >= 20 else None

def fetch_live(ticker: str, *, quiet: bool = False):
    """Try Stooq, then Yahoo. Returns (series, source) or (None, None)."""
    s = fetch_stooq(ticker)
    if s:
        if not quiet:
            print(f"  ✓ {ticker:6s}  stooq  · {len(s)} daily points "
                  f"· last close ${s[-1]['p']} on {s[-1]['d']}")
        return s, "stooq"
    y = fetch_yahoo(ticker)
    if y:
        if not quiet:
            print(f"  ✓ {ticker:6s}  yahoo  · {len(y)} daily points "
                  f"· last close ${y[-1]['p']} on {y[-1]['d']}")
        return y, "yahoo"
    if not quiet:
        print(f"  ✗ {ticker:6s}  (stooq + yahoo both failed — using synthetic)")
    return None, None

def get_chartjs_source() -> str | None:
    """Return the Chart.js UMD source as a string, cached on disk.

    Tries the on-disk cache first, then a chain of public CDNs. Caches the
    first success so future builds are offline-capable.

    Returns None if every mirror fails AND no cache exists. In that case the
    HTML falls back to loading Chart.js from the CDN <script src=…> tag at
    runtime — which works if the user is online, but not on an iPhone opening
    the HTML from Files app offline.
    """
    if CHARTJS_CACHE.exists():
        try:
            src = CHARTJS_CACHE.read_text(encoding="utf-8")
            if src and "Chart" in src and len(src) > 50_000:
                return src
        except OSError:
            pass
    for url in CHARTJS_MIRRORS:
        txt = _http_get(url, timeout=15.0)
        if txt and "Chart" in txt and len(txt) > 50_000:
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                CHARTJS_CACHE.write_text(txt, encoding="utf-8")
            except OSError:
                pass
            return txt
    return None


def cache_read(ticker: str, max_hours: float):
    """Return cached series + source if fresh, else None."""
    p = CACHE_DIR / f"{ticker}.json"
    if not p.exists():
        return None, None
    try:
        obj = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None, None
    fetched = obj.get("fetched_at", 0)
    if (time.time() - fetched) > max_hours * 3600:
        return None, None
    return obj.get("series"), obj.get("source")

def cache_write(ticker: str, series, source: str):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / f"{ticker}.json"
    p.write_text(json.dumps({
        "ticker": ticker,
        "source": source,
        "fetched_at": time.time(),
        "series": series,
    }))

def get_real_series(ticker: str):
    """Get live/cached series for ticker, or None if everything failed."""
    if ARGS.offline:
        return None, None
    if not ARGS.no_cache:
        cached, src = cache_read(ticker, ARGS.cache_max_hours)
        if cached:
            if not ARGS.quiet:
                print(f"  ↺ {ticker:6s}  cached ({src}) · {len(cached)} daily points")
            return cached, src
    series, src = fetch_live(ticker, quiet=ARGS.quiet)
    if series:
        cache_write(ticker, series, src)
    return series, src


# ---------------------------------------------------------------------------
# Load real data
# ---------------------------------------------------------------------------
with open(ROOT / "data" / "constituents.json") as f:
    CFG = json.load(f)
with open(ROOT / "data" / "catalysts.json") as f:
    CATS = json.load(f)["catalysts"]
with open(ROOT / "data" / "pre_ipo.json") as f:
    PREIPO = json.load(f)["companies"]


# ---------------------------------------------------------------------------
# Synthetic but deterministic price series per ticker
# ---------------------------------------------------------------------------
def _seed(ticker: str) -> int:
    return int(hashlib.md5(ticker.encode()).hexdigest()[:8], 16)


# Volatility / drift by dev stage — roughly reflects reality: operators are
# boring utilities, microreactor / R&D names whip around, fuel cycle is
# commodity-linked.
STAGE_PROFILE = {
    "Operational":     dict(drift=0.18, vol=0.28, start=55),
    "Diversified":     dict(drift=0.12, vol=0.24, start=110),
    "Pre-Commercial":  dict(drift=0.35, vol=0.75, start=32),
    "R&D":             dict(drift=0.45, vol=1.10, start=18),
}
TRADING_DAYS = 1260  # ~5y of trading days
END = date(2026, 4, 18)

# Anchor values so the synthetic baseline (used when live fetch fails) looks
# plausible against real April-2026 market prices. Each value is the approximate
# last-traded close we expect the live feed to hydrate over. When the series is
# generated we rescale so ser[-1].p == LAST_KNOWN_PRICES[ticker].
LAST_KNOWN_PRICES = {
    "CEG":   275.0,
    "VST":   138.0,
    "GEV":   455.0,
    "CCJ":   55.0,
    "LEU":   145.0,
    "ASPI":  7.0,
    "NUCL":  8.0,
    "BWXT":  145.0,
    "OKLO":  60.0,
    "SMR":   28.0,
    "NNE":   30.0,
    "IMSR":  9.0,
    "RYCEY": 18.0,
    "MIR":   19.0,
    "CDRE":  36.0,
}

# Approximate daily share volume (used to support VWAP when offline)
DEFAULT_VOLUMES = {
    "CEG":   3_500_000,
    "VST":   7_500_000,
    "GEV":   2_800_000,
    "CCJ":   9_500_000,
    "LEU":   1_400_000,
    "ASPI":  2_200_000,
    "NUCL":  800_000,
    "BWXT":  900_000,
    "OKLO":  18_000_000,
    "SMR":   12_000_000,
    "NNE":   6_500_000,
    "IMSR":  450_000,
    "RYCEY": 8_000_000,
    "MIR":   3_000_000,
    "CDRE":  550_000,
}


def price_series(ticker: str, stage: str, listed_date: str | None = None):
    prof = STAGE_PROFILE.get(stage, STAGE_PROFILE["Operational"])
    rng = random.Random(_seed(ticker))

    # Extend baseline back ~5 years so charts have pre-2024 context
    start_date = date(END.year - 5, END.month, END.day)
    if listed_date:
        ld = date.fromisoformat(listed_date[:10])
        if ld > start_date:
            start_date = ld

    # skip weekends
    all_days = []
    d = start_date
    while d <= END:
        if d.weekday() < 5:
            all_days.append(d)
        d += timedelta(days=1)

    n = len(all_days)
    if n < 5:
        return []
    # daily drift + vol
    dt = 1 / 252
    mu, sig = prof["drift"], prof["vol"]
    price = prof["start"]
    raw = []
    for i, dt_ in enumerate(all_days):
        shock = rng.gauss(0, 1)
        ret = (mu - 0.5 * sig * sig) * dt + sig * math.sqrt(dt) * shock
        price *= math.exp(ret)
        # occasional event shocks for R&D names
        if prof["vol"] > 0.7 and rng.random() < 0.01:
            price *= 1 + rng.gauss(0, 0.08)
        price = max(price, 0.5)
        raw.append((dt_, price))

    # Anchor the tail of the walk to a realistic April-2026 close so
    # offline viewers don't see absurd prices (e.g. RYCEY showing $140).
    target = LAST_KNOWN_PRICES.get(ticker)
    if target and raw:
        scale = target / raw[-1][1]
        raw = [(d_, p * scale) for (d_, p) in raw]

    base_vol = DEFAULT_VOLUMES.get(ticker, 1_000_000)
    out = []
    for (d_, p) in raw:
        v = int(base_vol * rng.uniform(0.55, 1.65))
        out.append({"d": d_.isoformat(), "p": round(p, 2), "v": v})
    return out


# Attach a series to each constituent. Try real Stooq/Yahoo data first (runs
# here in Python with no CORS restrictions); fall back to the anchored synthetic
# baseline only when network fetch fails or the user passed --offline.
if not ARGS.offline and not ARGS.quiet:
    print(f"Fetching live price data (cache<{ARGS.cache_max_hours:g}h). "
          f"Pass --offline to skip, --no-cache to force fresh.")

SOURCE_COUNTS = {"stooq": 0, "yahoo": 0, "synthetic": 0, "unlisted": 0}
for c in CFG["constituents"]:
    if not c.get("listed"):
        c["series"] = []
        c["data_source"] = "unlisted"
        SOURCE_COUNTS["unlisted"] += 1
        continue
    series, source = get_real_series(c["ticker"])
    if series:
        c["series"] = series
        c["data_source"] = source
        SOURCE_COUNTS[source] = SOURCE_COUNTS.get(source, 0) + 1
    else:
        c["series"] = price_series(c["ticker"], c["dev_stage"], c.get("listed_date"))
        c["data_source"] = "synthetic"
        SOURCE_COUNTS["synthetic"] += 1

if not ARGS.quiet:
    print(f"Data sources: stooq={SOURCE_COUNTS['stooq']}, yahoo={SOURCE_COUNTS['yahoo']}, "
          f"synthetic={SOURCE_COUNTS['synthetic']}, unlisted={SOURCE_COUNTS['unlisted']}")

BAKE_TS = datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Derive returns / vol / sharpe for each constituent from series
# ---------------------------------------------------------------------------
def log_returns(ser):
    if len(ser) < 2:
        return []
    return [math.log(ser[i]["p"] / ser[i - 1]["p"]) for i in range(1, len(ser))]


def perf_metrics(ser):
    if len(ser) < 20:
        return dict(ret_1m=None, ret_ytd=None, ret_1y=None,
                    vol_ann=None, sharpe=None, price=None, change_1d=None,
                    last_date=None, cagr=None, max_dd=None, total_return=None)
    price = ser[-1]["p"]
    last_date = ser[-1]["d"]
    change_1d = (ser[-1]["p"] / ser[-2]["p"] - 1) * 100

    # 21 / 252 / YTD windows
    def pct(n_back):
        i = max(0, len(ser) - 1 - n_back)
        return (ser[-1]["p"] / ser[i]["p"] - 1) * 100

    ret_1m = pct(21)
    ret_1y = pct(252) if len(ser) >= 252 else None

    # YTD
    ytd_start_idx = 0
    for i, pt in enumerate(ser):
        if pt["d"].startswith("2026"):
            ytd_start_idx = i
            break
    ret_ytd = (ser[-1]["p"] / ser[ytd_start_idx]["p"] - 1) * 100

    rets = log_returns(ser)
    vol_ann = statistics.pstdev(rets) * math.sqrt(252) * 100
    mean_ann = statistics.fmean(rets) * 252 * 100
    rf = 4.0
    sharpe = (mean_ann - rf) / vol_ann if vol_ann else 0

    # cumulative
    total_return = (ser[-1]["p"] / ser[0]["p"] - 1) * 100
    years = len(ser) / 252
    cagr = ((ser[-1]["p"] / ser[0]["p"]) ** (1 / years) - 1) * 100 if years else 0

    # max drawdown
    peak = ser[0]["p"]; mdd = 0
    for pt in ser:
        if pt["p"] > peak:
            peak = pt["p"]
        dd = (pt["p"] / peak - 1) * 100
        if dd < mdd:
            mdd = dd

    return dict(price=round(price, 2), change_1d=round(change_1d, 2),
                ret_1m=round(ret_1m, 2), ret_ytd=round(ret_ytd, 2),
                ret_1y=round(ret_1y, 2) if ret_1y is not None else None,
                vol_ann=round(vol_ann, 2), sharpe=round(sharpe, 3),
                last_date=last_date,
                cagr=round(cagr, 2), max_dd=round(mdd, 2),
                total_return=round(total_return, 2))


# Illustrative market caps — broadly plausible for late-April-2026
MARKET_CAP = {
    "CEG": 83e9, "VST": 72e9, "GEV": 120e9,
    "CCJ": 27e9, "LEU": 5.2e9, "ASPI": 2.1e9, "NUCL": 0.45e9,
    "BWXT": 13.5e9, "OKLO": 8.2e9, "SMR": 3.4e9, "NNE": 1.2e9,
    "IMSR": 1.0e9, "RYCEY": 95e9,
    "MIR": 4.0e9, "CDRE": 1.6e9,
    "GFUZ": None,  # not yet public
}


for c in CFG["constituents"]:
    c.update(perf_metrics(c["series"]))
    c["market_cap"] = MARKET_CAP.get(c["ticker"])


# ---------------------------------------------------------------------------
# Composite index series (tiered equal-weight within tier)
# ---------------------------------------------------------------------------
def composite(constituents, tier_weights):
    # group listed tickers by sector, compute equal-weight within, blend by tier.
    listed = [c for c in constituents if c["series"]]
    by_sector: dict[str, list] = {}
    for c in listed:
        by_sector.setdefault(c["sector"], []).append(c)

    weights = {}
    for sector, members in by_sector.items():
        tier_w = tier_weights.get(sector, 0)
        if not members or tier_w == 0:
            continue
        per = tier_w / len(members)
        for m in members:
            weights[m["ticker"]] = per
    # renormalize in case of missing tiers
    total = sum(weights.values())
    if total:
        weights = {k: v / total for k, v in weights.items()}

    # Use the UNION of dates so we don't collapse history to the newest
    # listing's window. On each date include only tickers with data there,
    # renormalize the tier-assigned weights over the active set, and
    # advance the index by that day's active-set weighted return.
    all_dates = set()
    series_by_tkr = {}
    for c in listed:
        m = {p["d"]: p["p"] for p in c["series"]}
        series_by_tkr[c["ticker"]] = m
        all_dates.update(m.keys())
    dates = sorted(all_dates)
    if not dates:
        return [], weights

    prev_prices: dict[str, float] = {}
    idx = 100.0
    values = [{"d": dates[0], "p": 100.0}]
    # seed previous prices from first date's active tickers
    for t, m in series_by_tkr.items():
        if dates[0] in m:
            prev_prices[t] = m[dates[0]]
    for d in dates[1:]:
        active = [t for t in series_by_tkr if d in series_by_tkr[t]
                  and t in prev_prices]
        if active:
            w_total = sum(weights.get(t, 0) for t in active)
            if w_total > 0:
                r = 0.0
                for t in active:
                    p1 = series_by_tkr[t][d]
                    p0 = prev_prices[t]
                    w = weights.get(t, 0) / w_total
                    r += w * (p1 / p0 - 1.0)
                idx *= (1.0 + r)
        values.append({"d": d, "p": round(idx, 2)})
        # refresh previous prices for tickers trading today (incl. new listings)
        for t, m in series_by_tkr.items():
            if d in m:
                prev_prices[t] = m[d]
    return values, weights


INDEX_SERIES, INDEX_WEIGHTS = composite(
    CFG["constituents"], CFG["tier_weights"])


# Benchmarks — synthetic for SPY / URA / NLR
def benchmark(ticker: str, drift: float, vol: float, start: float = 100.0):
    rng = random.Random(_seed(ticker))
    dt = 1 / 252
    p = start
    out = []
    d = date(END.year - 2, END.month, END.day)
    while d <= END:
        if d.weekday() < 5:
            shock = rng.gauss(0, 1)
            p *= math.exp((drift - 0.5 * vol * vol) * dt + vol * math.sqrt(dt) * shock)
            out.append({"d": d.isoformat(), "p": round(p, 2)})
        d += timedelta(days=1)
    return out


BENCHMARKS = {
    "SPY": benchmark("SPY", 0.09, 0.16),
    "URA": benchmark("URA", 0.22, 0.35),
    "NLR": benchmark("NLR", 0.28, 0.32),
}
INDEX_STATS = perf_metrics(INDEX_SERIES)
BENCH_STATS = {k: perf_metrics(v) for k, v in BENCHMARKS.items()}


# ---------------------------------------------------------------------------
# Optimizer — precomputed results for 4 objectives with max_weight in {0.15,0.2,0.25}
# Simple heuristic: build weights from Sharpe / volatility / equal / inverse-vol
# subject to the cap; normalize.
# ---------------------------------------------------------------------------
def capped_softmax_weights(scores, max_weight=0.2):
    # Normalize scores to [0,1], apply water-filling cap.
    listed = [(t, s) for t, s in scores.items() if s is not None]
    if not listed:
        return {}
    lo = min(s for _, s in listed)
    shifted = {t: (s - lo) + 1e-6 for t, s in listed}
    tot = sum(shifted.values())
    w = {t: v / tot for t, v in shifted.items()}
    # iteratively cap
    for _ in range(50):
        over = {t: v for t, v in w.items() if v > max_weight}
        if not over:
            break
        excess = sum(v - max_weight for v in over.values())
        free = {t: v for t, v in w.items() if v <= max_weight}
        if not free:
            break
        free_total = sum(free.values())
        for t in over:
            w[t] = max_weight
        for t, v in free.items():
            w[t] = v + excess * v / free_total
    s = sum(w.values())
    return {t: round(v / s, 4) for t, v in w.items()}


def build_optimizer_result(objective: str, max_weight: float):
    listed = [c for c in CFG["constituents"] if c["series"] and c["sharpe"] is not None]
    if objective == "sharpe":
        scores = {c["ticker"]: c["sharpe"] for c in listed}
    elif objective == "min_vol":
        scores = {c["ticker"]: -(c["vol_ann"] or 100) for c in listed}
    elif objective == "inverse_vol":
        scores = {c["ticker"]: 1 / (c["vol_ann"] or 100) for c in listed}
    else:  # equal
        scores = {c["ticker"]: 1.0 for c in listed}
    w = capped_softmax_weights(scores, max_weight=max_weight)

    # Expected return / vol / sharpe from weights
    ann_return = sum(
        w.get(c["ticker"], 0) * (c["ret_1y"] or c["cagr"] or 0)
        for c in listed)
    ann_vol = math.sqrt(sum(
        (w.get(c["ticker"], 0) * (c["vol_ann"] or 0)) ** 2
        for c in listed))
    sharpe = (ann_return - 4.0) / ann_vol if ann_vol else 0

    breakdown = sorted([
        {"ticker": c["ticker"], "name": c["name"], "weight": w.get(c["ticker"], 0),
         "dollars": round(w.get(c["ticker"], 0) * 100000, 0),
         "exp_return": round((c["ret_1y"] or c["cagr"] or 0) / 100, 4),
         "volatility": round((c["vol_ann"] or 0) / 100, 4)}
        for c in listed if w.get(c["ticker"], 0) > 0
    ], key=lambda r: -r["weight"])

    return {
        "objective": objective,
        "max_weight": max_weight,
        "ann_return": round(ann_return / 100, 4),
        "ann_vol": round(ann_vol / 100, 4),
        "sharpe": round(sharpe, 3),
        "risk_free": 0.04,
        "weights": w,
        "breakdown": breakdown,
        "error": None,
    }


OPTIMIZER_RESULTS = {
    f"{obj}__{int(mw*100)}": build_optimizer_result(obj, mw)
    for obj in ("sharpe", "min_vol", "inverse_vol", "equal")
    for mw in (0.15, 0.20, 0.25)
}


# Efficient frontier — sweep by mixing sharpe-heavy and min-vol weights
def frontier():
    sharpe = OPTIMIZER_RESULTS["sharpe__20"]["weights"]
    minvol = OPTIMIZER_RESULTS["min_vol__20"]["weights"]
    listed = {c["ticker"]: c for c in CFG["constituents"] if c["series"]}
    pts = []
    for alpha in [x / 20 for x in range(21)]:
        mix = {}
        for t in listed:
            mix[t] = alpha * sharpe.get(t, 0) + (1 - alpha) * minvol.get(t, 0)
        s = sum(mix.values())
        if not s:
            continue
        mix = {t: v / s for t, v in mix.items()}
        ret = sum(mix[t] * (listed[t]["ret_1y"] or listed[t]["cagr"] or 0)
                  for t in mix) / 100
        vol = math.sqrt(sum((mix[t] * (listed[t]["vol_ann"] or 0)) ** 2
                            for t in mix)) / 100
        pts.append({"vol": round(vol, 4), "return": round(ret, 4), "alpha": alpha})
    return pts


FRONTIER = frontier()


# ---------------------------------------------------------------------------
# Sample news + alert log — static but realistic
# ---------------------------------------------------------------------------
SAMPLE_NEWS_SECTOR = [
    {"ts": "2026-04-17T14:02:00",
     "title": "US nuclear capacity factor hits record 94.8% in 2025",
     "source": "EIA Today in Energy", "url": "https://www.eia.gov/todayinenergy/"},
    {"ts": "2026-04-16T09:31:00",
     "title": "NRC approves streamlined licensing framework for microreactors",
     "source": "Reuters", "url": "https://www.reuters.com/business/energy/"},
    {"ts": "2026-04-15T18:44:00",
     "title": "Uranium spot price crosses $105/lb as utilities race for supply",
     "source": "Financial Times", "url": "https://www.ft.com/uranium"},
    {"ts": "2026-04-14T11:07:00",
     "title": "DOE announces Phase II HALEU procurement winners",
     "source": "DOE Newswire", "url": "https://www.energy.gov/ne/articles"},
    {"ts": "2026-04-12T08:00:00",
     "title": "Google signs 500 MW PPA with small-modular-reactor developer",
     "source": "WSJ", "url": "https://www.wsj.com/business/energy-oil/"},
]

SAMPLE_NEWS_PER_TICKER = {
    "CEG": ["Constellation Q1 earnings top estimates on hyperscaler PPA pipeline",
            "Three Mile Island Unit 1 restart milestone reached"],
    "VST": ["Vistra hires 400 for Comanche Peak uprate project",
            "ERCOT peak demand forecast raised 8% on AI data-center growth"],
    "GEV": ["OPG Darlington Unit 1 first concrete targeted for Q3",
            "GE Vernova SMR services backlog tops $1B"],
    "CCJ": ["Cameco raises 2026 production guidance on McArthur River ramp",
            "Japan's Kansai Electric signs 10-year uranium supply pact"],
    "LEU": ["Centrus wins $150M DOE HALEU Phase II award",
            "ACP pilot cascade hits 900 kg/yr run-rate"],
    "ASPI": ["ASP Isotopes Pretoria Mo-99 commissioning completes FAT",
            "Quantum Enrichment pilot reaches 5% HALEU assay"],
    "NUCL": ["Eagle Nuclear files PEA for Aurora uranium project",
            "Eagle Nuclear completes $40M bought-deal financing"],
    "BWXT": ["BWXT delivers first TRISO fuel compacts to Idaho National Lab",
            "Project Pele microreactor integration testing begins"],
    "OKLO": ["Oklo submits Aurora COLA to NRC; review target 18 months",
            "DOE fuel allocation for Oklo INL site finalized"],
    "SMR": ["NuScale RoPower Romania EPC proposals under review",
            "NuScale announces strategic review of UK deployment partners"],
    "NNE": ["NANO Nuclear closes $75M follow-on; ZEUS design review in Q4",
            "NANO Nuclear inks MOU with South Korean fuel fabricator"],
    "IMSR": ["Terrestrial Energy CNSC Phase 3 submission expected 2027",
            "IMSR 195 MWe design receives $45M DOE cost-share"],
    "RYCEY": ["Great British Nuclear down-select narrows to Rolls-Royce SMR and AP1000",
            "Rolls-Royce civil aerospace free cashflow guidance raised"],
    "MIR": ["Mirion Q1 detection revenue +14% YoY on fleet restarts",
            "Mirion acquires French dosimetry firm for EUR 85M"],
    "CDRE": ["Cadre Holdings reports +6% hazmat segment growth",
            "Cadre guides 2026 adjusted EBITDA above consensus"],
    "GFUZ": ["Spring Valley III / General Fusion SPAC close expected Q2 2026",
            "LM26 first plasma targeted for late 2026"],
}

SAMPLE_ALERT_LOG = [
    {"ts": "2026-04-18T09:02:14", "rule": "price_move",
     "payload": {"subject": "OKLO moved +11.4% today",
                 "body": "Price $14.82 vs prior close $13.31 — above 8% threshold."}},
    {"ts": "2026-04-17T16:05:51", "rule": "catalyst_approaching",
     "payload": {"subject": "GFUZ SPAC close in 10 days",
                 "body": "2026-Q2 · SPAC close with Spring Valley III expected"}},
    {"ts": "2026-04-15T11:44:02", "rule": "sec_filing",
     "payload": {"subject": "Cameco (CCJ) new 6-K filed",
                 "body": "Filing acc-no 0001125142-26-000247 · monitor EDGAR for details."}},
    {"ts": "2026-04-12T08:14:30", "rule": "price_move",
     "payload": {"subject": "NNE moved -9.1% today",
                 "body": "Price $6.45 vs prior close $7.10 — above 8% threshold."}},
]


# ---------------------------------------------------------------------------
# Final data payload
# ---------------------------------------------------------------------------
PAYLOAD = {
    "meta": {
        "index_name": CFG["index_name"],
        "index_code": CFG["index_code"],
        "base_date": CFG["base_date"],
        "base_value": CFG["base_value"],
        "rebalance_frequency": CFG["rebalance_frequency"],
        "tier_weights": CFG["tier_weights"],
        "generated": END.isoformat(),
        "baked_at": BAKE_TS,
        "source_counts": SOURCE_COUNTS,
    },
    "constituents": CFG["constituents"],
    "index_weights": INDEX_WEIGHTS,
    "index_series": INDEX_SERIES,
    "index_stats": INDEX_STATS,
    "benchmarks": {k: {"name": {"SPY": "S&P 500", "URA": "Uranium ETF",
                                "NLR": "VanEck Uranium+Nuclear ETF"}[k],
                       "series": v, "stats": BENCH_STATS[k]}
                   for k, v in BENCHMARKS.items()},
    "catalysts": CATS,
    "pre_ipo": PREIPO,
    "optimizer": OPTIMIZER_RESULTS,
    "frontier": FRONTIER,
    "news_sector": SAMPLE_NEWS_SECTOR,
    "news_per_ticker": SAMPLE_NEWS_PER_TICKER,
    "alert_log": SAMPLE_ALERT_LOG,
}


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------
HTML_TEMPLATE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover" />
<title>Nuclear Renaissance Index</title>
<!-- Home-screen / PWA hints so Add to Home Screen on iOS & Android looks good. -->
<meta name="theme-color" content="#0f1115" />
<meta name="color-scheme" content="dark light" />
<meta name="apple-mobile-web-app-capable" content="yes" />
<meta name="mobile-web-app-capable" content="yes" />
<meta name="apple-mobile-web-app-title" content="NRI" />
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
<meta name="description" content="Nuclear Renaissance Index — research dashboard for nuclear energy equities." />
<link rel="apple-touch-icon" href="data:image/svg+xml;utf8,__ICON_SVG__" />
<link rel="icon" href="data:image/svg+xml;utf8,__ICON_SVG__" />
__CHARTJS_TAG__
<style>__CSS__</style>
</head>
<body>

<header class="site">
  <div class="wrap">
    <div class="brand">
      <span class="brand-mark">NRI</span>
      <span class="brand-name">Nuclear Renaissance Index</span>
    </div>
    <nav>
      <a href="#dashboard">Dashboard</a>
      <a href="#companies">Companies</a>
      <a href="#optimizer">Optimizer</a>
      <a href="#backtest">Backtest</a>
      <a href="#catalysts">Catalysts</a>
      <a href="#news">News</a>
      <a href="#preipo">Pre-IPO</a>
      <a href="#profile">Profile</a>
      <a href="#alerts">Alerts</a>
      <a href="#methodology">Methodology</a>
    </nav>
  </div>
</header>

<div id="live-bar" class="live-bar">
  <div class="wrap">
    <span class="live-dot" id="live-dot"></span>
    <span id="live-status">Showing baked prices — attempting live refresh…</span>
    <span id="live-updated" class="live-updated" title="Timestamp of the last successful data refresh">Last updated: —</span>
    <span id="baked-info" class="live-updated" title="When this HTML was built and how many tickers got real prices">Baked: —</span>
    <span id="live-meta" class="muted small"></span>
    <button id="live-refresh" class="live-btn" title="Force a fresh fetch (clears cached proxy route)">↻ Refresh</button>
    <button id="live-diagnose" class="live-btn" title="Test every CORS proxy from your browser">Test proxies</button>
    <button id="live-settings" class="live-btn" title="Set your own CORS proxy (Cloudflare Worker, etc)">⚙ Data source</button>
    <label class="live-auto muted small"><input type="checkbox" id="live-auto" checked/> auto 5m</label>
  </div>
  <div id="live-diag-panel" class="live-diag-panel" hidden></div>
  <div id="live-settings-panel" class="live-diag-panel" hidden>
    <div class="row"><strong>Data refresh — no signup required</strong></div>
    <div class="row detail">This file was built with real historical data baked in directly
      (fetched at build time by Python, which has no CORS restrictions). To get fresh data,
      <strong>double-click <code>refresh.command</code></strong> (macOS/Linux) or
      <strong><code>refresh.bat</code></strong> (Windows) in the same folder — it re-runs the build
      and rewrites this HTML with today's prices. Takes ~10 seconds.</div>
    <div class="row detail">The browser-side Refresh button still tries live quotes via public
      CORS proxies when they're reachable — it's a nice-to-have, not the primary mechanism.</div>
    <div class="row"><strong>Optional: use your own proxy</strong></div>
    <div class="row detail">If you run a Cloudflare Worker / nginx / any HTTPS endpoint that
      relays a <code>?url=</code> query, paste it below and live-refresh will use it first.
      Purely optional — leave blank to use the baked-in data + public proxies.</div>
    <div class="row">
      <input id="live-settings-input" type="text" placeholder="https://your-worker.your-name.workers.dev/?url="
             class="live-settings-input" style="flex:1;min-width:280px;padding:5px 8px;background:var(--bg-3);color:var(--fg);border:1px solid var(--line);border-radius:4px;font-family:ui-monospace,Menlo,monospace;font-size:12px;"/>
      <button id="live-settings-save" class="live-btn">Save</button>
      <button id="live-settings-clear" class="live-btn" title="Revert to public proxy chain">Clear</button>
      <button id="live-settings-help" class="live-btn">Deploy guide</button>
    </div>
    <div id="live-settings-msg" class="row detail"></div>
    <div id="live-settings-help-body" class="row detail" hidden>
      <pre style="white-space:pre-wrap;background:var(--bg-3);padding:10px;border-radius:4px;overflow-x:auto;margin:4px 0;">1. Go to <a href="https://workers.cloudflare.com/" target="_blank" style="color:var(--accent)">workers.cloudflare.com</a> and sign in (free).
2. Create → Worker → name it (anything). Click "Edit code".
3. Replace the placeholder code with the contents of cloudflare-worker.js (shipped next to this HTML).
4. Click "Save and Deploy". Copy the *.workers.dev URL.
5. Paste it above, append ?url= at the end (e.g. https://my-worker.example.workers.dev/?url=).
6. Click Save. Refresh will now use your own proxy — no rate limits, no flaky public services.</pre>
    </div>
  </div>
</div>

<main class="wrap">
  <section id="dashboard" class="tab"></section>
  <section id="companies" class="tab"></section>
  <section id="optimizer" class="tab"></section>
  <section id="backtest" class="tab"></section>
  <section id="catalysts" class="tab"></section>
  <section id="news" class="tab"></section>
  <section id="preipo" class="tab"></section>
  <section id="profile" class="tab"></section>
  <section id="alerts" class="tab"></section>
  <section id="methodology" class="tab"></section>
</main>

<footer class="site">
  <div class="wrap">
    <span class="muted small">NRI · Nuclear Renaissance Index · baseline generated __GENERATED__
      · live quotes via Yahoo Finance (client-side, CORS) · falls back to synthetic series if offline.</span>
  </div>
</footer>

<script id="payload" type="application/json">__PAYLOAD__</script>
<script>__JS__</script>
</body>
</html>
'''


CSS = r'''
:root{--bg:#0e1116;--bg-2:#161b22;--bg-3:#1d232c;--fg:#e6ebf2;--fg-dim:#9aa4b2;
  --line:rgba(255,255,255,0.08);--accent:#2d7fbf;--accent-2:#c78f4b;
  --up:#6bb07c;--down:#d25a5a;--radius:10px;}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  font-size:14px;line-height:1.45;}
a{color:var(--accent);text-decoration:none;}
a:hover{text-decoration:underline;}
.wrap{max-width:1280px;margin:0 auto;padding:0 24px;}
header.site{background:var(--bg-2);border-bottom:1px solid var(--line);
  padding:14px 0;position:sticky;top:0;z-index:20;}
header.site .wrap{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;}
.brand{display:flex;align-items:center;gap:10px;color:var(--fg);}
.brand-mark{background:linear-gradient(135deg,var(--accent),var(--accent-2));
  color:#fff;padding:4px 8px;border-radius:6px;font-weight:700;font-size:12px;letter-spacing:1px;}
.brand-name{font-weight:600;}
header.site nav{display:flex;flex-wrap:wrap;gap:4px 18px;}
header.site nav a{color:var(--fg-dim);font-weight:500;padding:4px 2px;}
header.site nav a:hover{color:var(--fg);text-decoration:none;}
header.site nav a.active{color:var(--fg);border-bottom:2px solid var(--accent);}
main.wrap{padding-top:24px;padding-bottom:60px;}
footer.site{border-top:1px solid var(--line);margin-top:40px;padding:16px 0;background:var(--bg-2);}
.tab{display:none;}
.tab.active{display:block;}
.hero{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px;align-items:center;}
.hero-left h1{margin:0 0 6px;font-size:26px;font-weight:700;}
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;}
.stat{background:var(--bg-2);border:1px solid var(--line);padding:12px 14px;border-radius:var(--radius);}
.stat-label{color:var(--fg-dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px;}
.stat-value{font-size:22px;font-weight:700;}
.stat-sub{font-size:12px;margin-top:2px;color:var(--fg-dim);}
.card{background:var(--bg-2);border:1px solid var(--line);padding:18px 20px;
  border-radius:var(--radius);margin-bottom:20px;}
.card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;}
.card h2{margin:0 0 10px;font-size:15px;font-weight:600;letter-spacing:.02em;color:var(--fg);}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:20px;}
.grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;}
table{width:100%;border-collapse:collapse;}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;}
th{font-weight:600;color:var(--fg-dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em;}
.num{text-align:right;font-variant-numeric:tabular-nums;}
table.kv th{width:45%;}
tr.clickable{cursor:pointer;}
tr.clickable:hover td{background:var(--bg-3);}
.up{color:var(--up);}.down{color:var(--down);}
.muted{color:var(--fg-dim);}.small{font-size:12px;}
.badge{display:inline-block;background:var(--accent-2);color:#1a1209;padding:1px 6px;
  border-radius:10px;font-size:10px;margin-left:6px;text-transform:uppercase;letter-spacing:.06em;}
.pill{display:inline-block;padding:2px 10px;border-radius:10px;font-size:11px;
  background:var(--bg-3);color:var(--fg-dim);margin-right:6px;}
.pill.on{background:rgba(107,176,124,0.18);color:var(--up);}
.pill.off{background:rgba(210,90,90,0.18);color:var(--down);}
input[type=text],input[type=number],input[type=email],select,textarea{
  background:var(--bg-3);color:var(--fg);border:1px solid var(--line);
  border-radius:6px;padding:7px 10px;font-family:inherit;font-size:13px;}
input[type=checkbox]{accent-color:var(--accent);margin-right:6px;}
button{background:var(--accent);color:#fff;border:0;border-radius:6px;
  padding:8px 16px;cursor:pointer;font-weight:600;font-family:inherit;}
button:hover{background:#3c93d4;}
button.secondary{background:var(--accent-2);color:#1a1209;}
pre{color:var(--fg);font-size:12px;background:var(--bg-3);padding:10px;border-radius:6px;overflow:auto;}
details{border-bottom:1px solid var(--line);padding:8px 0;}
details>summary{cursor:pointer;padding:8px 4px;color:var(--fg);list-style:none;}
details>summary::-webkit-details-marker{display:none;}
details>summary::before{content:"▸ ";color:var(--fg-dim);margin-right:6px;}
details[open]>summary::before{content:"▾ ";}
.chip{display:inline-block;padding:3px 10px;border-radius:12px;font-size:11px;
  background:var(--bg-3);color:var(--fg);margin-right:6px;margin-bottom:4px;}
.notes-list{list-style:none;padding:0;margin:0;}
.notes-list li{padding:6px 0;border-bottom:1px dashed var(--line);color:var(--fg);font-size:13px;}
.company-card{border:1px solid var(--line);padding:14px;border-radius:var(--radius);
  background:var(--bg-2);margin-bottom:12px;}
.company-card h3{margin:0 0 4px;font-size:16px;}
.company-card .muted{font-size:12px;}
.hist{background:var(--bg-3);padding:8px 12px;border-radius:6px;
  font-size:12px;color:var(--fg-dim);margin-bottom:4px;}
.stage-Operational{color:var(--up);}
.stage-Pre-Commercial{color:var(--accent-2);}
.stage-RD,.stage-R\&D{color:var(--down);}
.stage-Diversified{color:var(--accent);}
.fire-log{list-style:none;padding:0;margin:0;}
.fire-log li{padding:10px 8px;border-bottom:1px solid var(--line);display:grid;
  grid-template-columns:160px 1fr;gap:8px;}
.live-bar{background:var(--bg-3);border-bottom:1px solid var(--line);padding:8px 0;font-size:12px;position:sticky;top:0;z-index:50;}
.live-bar .wrap{display:flex;align-items:center;gap:12px;flex-wrap:wrap;}
.live-dot{width:8px;height:8px;border-radius:50%;background:#c78f4b;box-shadow:0 0 6px #c78f4b;
  display:inline-block;transition:all 0.2s;}
.live-dot.ok{background:var(--up);box-shadow:0 0 6px var(--up);animation:pulse 2.5s infinite;}
.live-dot.warn{background:var(--accent-2);box-shadow:0 0 6px var(--accent-2);}
.live-dot.err{background:var(--down);box-shadow:0 0 6px var(--down);}
@keyframes pulse{0%,100%{opacity:0.9;}50%{opacity:0.45;}}
.live-diag-panel{border-top:1px solid var(--line);padding:10px 20px;font-size:12px;font-family:ui-monospace,Menlo,monospace;background:var(--bg-2);max-width:100%;overflow-x:auto;}
.live-diag-panel .row{display:flex;gap:10px;padding:3px 0;}
.live-diag-panel .ok{color:var(--up);}
.live-diag-panel .fail{color:var(--down);}
.live-diag-panel .pending{color:var(--accent-2);}
.live-diag-panel .proxy{min-width:140px;}
.live-diag-panel .detail{color:var(--muted);}
.live-updated{font-size:12px;color:var(--fg);background:var(--bg-2);border:1px solid var(--line);padding:3px 10px;border-radius:999px;white-space:nowrap;}
.live-updated.stale{background:rgba(199,143,75,0.18);border-color:#c78f4b;color:#c78f4b;}
.live-updated.fresh{background:rgba(93,200,139,0.14);border-color:var(--up);color:var(--up);}
.live-btn{background:transparent;color:var(--accent);border:1px solid var(--line);
  padding:3px 10px;font-size:11px;margin-left:auto;}
.live-btn:hover{background:var(--bg-2);color:var(--fg);}
.live-btn:disabled{opacity:0.5;cursor:default;}
.live-auto{display:flex;align-items:center;gap:4px;}
.range-row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:6px 0 8px;}
.range-seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden;}
.range-seg button{background:var(--bg-3);color:var(--fg-dim);font-size:11px;font-weight:500;
  padding:4px 10px;border:0;border-radius:0;margin:0;}
.range-seg button + button{border-left:1px solid var(--line);}
.range-seg button:hover{color:var(--fg);background:var(--bg-2);}
.range-seg button.active{background:var(--accent);color:#fff;}
.ind-row{display:flex;gap:14px;flex-wrap:wrap;margin:6px 0 10px;font-size:11px;position:relative;}
.ind-toggle{display:inline-flex;align-items:center;gap:4px;color:var(--fg-dim);cursor:pointer;position:relative;}
.ind-toggle input{margin:0;}
.info-icon{display:inline-flex;align-items:center;justify-content:center;width:13px;height:13px;
  border:1px solid var(--line);border-radius:50%;color:var(--fg-dim);font-size:9px;font-weight:700;
  cursor:help;font-style:normal;margin-left:2px;line-height:1;user-select:none;background:var(--bg-3);}
.info-icon:hover{color:var(--accent);border-color:var(--accent);}
.info-icon[data-tip]{position:relative;}
.info-icon[data-tip]:hover::after,
.info-icon[data-tip].open::after{content:attr(data-tip);position:absolute;top:-4px;left:100%;
  transform:translateY(-100%);background:#1a1d24;color:#eaeaea;border:1px solid var(--line);
  padding:7px 9px;border-radius:5px;width:260px;font-size:10.5px;font-weight:400;line-height:1.45;
  z-index:200;pointer-events:none;white-space:normal;font-style:normal;
  box-shadow:0 4px 14px rgba(0,0,0,0.35);}
/* Make tables horizontally scrollable when they exceed the viewport */
.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -4px;}
.table-scroll table{min-width:560px;}

@media (max-width:900px){
  .hero{grid-template-columns:1fr;}
  .grid-2,.grid-3{grid-template-columns:1fr;}
  .stat-grid{grid-template-columns:repeat(2,1fr);}
  header.site .wrap{flex-direction:column;align-items:flex-start;gap:6px;}
  header.site nav{gap:2px 12px;width:100%;overflow-x:auto;flex-wrap:nowrap;
    -webkit-overflow-scrolling:touch;padding-bottom:4px;}
  header.site nav a{white-space:nowrap;padding:8px 2px;}
  .fire-log li{grid-template-columns:1fr;}
  .card{padding:14px 14px;}
  .hero-left h1{font-size:22px;}
  .live-bar .wrap{gap:8px;}
  .live-btn{margin-left:0;}
  /* Force long data tables to scroll horizontally inside their card */
  .card{overflow-x:auto;-webkit-overflow-scrolling:touch;}
  .card table{font-size:12px;min-width:auto;}
  .card table th, .card table td{padding:6px 6px;white-space:nowrap;}
  /* Except key-value tables, which should wrap naturally */
  .card table.kv th, .card table.kv td{white-space:normal;}
  .ind-row{gap:10px 12px;font-size:12px;}
  .ind-toggle{padding:4px 0;}  /* larger tap target */
  /* Tooltip opens below on small screens so it doesn't overflow right-edge */
  .info-icon[data-tip]:hover::after,
  .info-icon[data-tip].open::after{
    left:auto;right:0;top:100%;transform:translateY(6px);
    width:min(280px,calc(100vw - 40px));}
  /* Make buttons / toggles touch-friendly */
  .range-seg button{padding:7px 12px;font-size:12px;}
  button{padding:10px 16px;}
  input[type=checkbox]{width:16px;height:16px;}
  .info-icon{width:16px;height:16px;font-size:11px;}
}

@media (max-width:600px){
  main.wrap, header.site .wrap, .live-bar .wrap{padding-left:14px;padding-right:14px;}
  .stat-grid{grid-template-columns:repeat(2,1fr);gap:8px;}
  .stat{padding:9px 10px;}
  .stat-value{font-size:18px;}
  .stat-label{font-size:10px;}
  .hero-left h1{font-size:20px;line-height:1.2;}
  .card{padding:12px;margin-bottom:14px;}
  .card h2{font-size:14px;}
  .range-row{gap:8px;}
  .range-seg{width:100%;overflow-x:auto;}
  /* Compact live-bar on phones */
  .live-bar{font-size:11px;}
  .live-bar .wrap{flex-wrap:wrap;}
  #live-status{flex:1 1 100%;}  /* status on its own row */
}
/* Ensure canvas charts stay inside the card on all widths */
canvas{max-width:100%!important;height:auto!important;}
'''


JS = r"""
(function(){
"use strict";
const DATA = JSON.parse(document.getElementById('payload').textContent);
const $  = (s,r=document)=>r.querySelector(s);
const $$ = (s,r=document)=>Array.from(r.querySelectorAll(s));
const fmt = {
  num:(x,d=2)=>x==null||isNaN(x)?"–":Number(x).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d}),
  pct:(x,d=2)=>x==null||isNaN(x)?"–":(x>=0?"+":"")+Number(x).toFixed(d)+"%",
  money:(x)=>{
    if(x==null||isNaN(x))return"–";
    const a=Math.abs(x);
    if(a>=1e12)return"$"+(x/1e12).toFixed(2)+"T";
    if(a>=1e9) return"$"+(x/1e9).toFixed(2)+"B";
    if(a>=1e6) return"$"+(x/1e6).toFixed(2)+"M";
    return "$"+x.toLocaleString();
  }
};
const cls = v => v==null?"":(v>=0?"up":"down");
const pretty = s => s?s.replace(/_/g," ").replace(/\b\w/g,c=>c.toUpperCase()):"";

// ================= Profile (persisted in localStorage) =================
const PROFILE_KEY = "nri.profile.v1";
const DEFAULT_PROFILE = {
  capital_usd:100000,max_position_pct:0.20,min_position_pct:0.02,
  min_holdings:6,max_holdings:14,target_vol_pct:0.35,
  max_drawdown_pct:0.45,risk_free_rate:0.04,objective:"sharpe",
  excluded_tickers:[],required_tickers:[],
  alert_opts:{price_move_enabled:true,price_move_threshold_pct:0.08,
    catalyst_enabled:true,catalyst_days_ahead:14,
    sec_filing_enabled:false,email:""},
  history:[{ts:new Date().toISOString(),note:"Created default profile"}]
};
function loadProfile(){
  try{const raw=localStorage.getItem(PROFILE_KEY);
    if(!raw)return structuredClone(DEFAULT_PROFILE);
    return {...structuredClone(DEFAULT_PROFILE),...JSON.parse(raw)};}
  catch(e){return structuredClone(DEFAULT_PROFILE);}
}
function saveProfile(p){localStorage.setItem(PROFILE_KEY,JSON.stringify(p));}
function logChange(p,note){
  if(!p.history)p.history=[];
  p.history.unshift({ts:new Date().toISOString(),note});
  p.history=p.history.slice(0,30);
}
function riskPosture(p){
  const v=p.target_vol_pct*100, d=p.max_drawdown_pct*100, m=p.max_position_pct*100;
  const score=v*0.4+d*0.4+m*0.2;
  if(score<22)return"Conservative";
  if(score<32)return"Balanced";
  if(score<42)return"Growth";
  return "Aggressive";
}

// ================= NL constraint agent (JS port) =================
const TICKER_RE=/\b[A-Z]{1,6}\b/g;
const CLAUSE_SPLIT=/\s*(?:\band\b|\bbut\b|,|;|\bthen\b|\balso\b)\s*|\s*\.(?=\s|$)\s*/gi;
const STOP=new Set(["I","A","AN","ALL","AND","THE","OR","IS","MY","FOR","TO","AT","OF","IN","IF","BE","ON","NO","ANY","SEC"]);
function asPct(v){const x=parseFloat(String(v).replace("%",""));return isNaN(x)?null:(x>1?x/100:x);}
function asMoney(v){
  let s=String(v).toLowerCase().replace(/\$|,/g,"").trim();
  let mult=1;
  if(s.endsWith("k")){mult=1e3;s=s.slice(0,-1);}
  else if(s.endsWith("m")){mult=1e6;s=s.slice(0,-1);}
  else if(s.endsWith("b")){mult=1e9;s=s.slice(0,-1);}
  const x=parseFloat(s);return isNaN(x)?null:x*mult;
}
function findTickers(raw){
  const toks=String(raw).toUpperCase().match(TICKER_RE)||[];
  return toks.filter(t=>!STOP.has(t));
}
function parseClause(clause,p,notes){
  const t=" "+clause.trim().toLowerCase()+" ";
  const raw=clause.trim();
  let m;
  // capital
  m = t.match(/(?:have|invest|capital|budget|portfolio|account|is)[^\d$]*(\$?[\d,.]+\s*[kmb]?)/)
   || t.match(/(\$?[\d,.]+\s*[kmb])\s+(?:in\s+)?(?:capital|budget|portfolio|account|cash|to invest)/);
  if(m){const cap=asMoney(m[1]);if(cap&&cap>=100){p.capital_usd=cap;notes.push("Capital → $"+cap.toLocaleString());}}
  // max position
  m=t.match(/(?:max(?:imum)?\s+(?:positions?|weight|allocation|name|holding)|no name (?:bigger|more|larger|over) than|cap\s+(?:any\s+)?positions?\s+at|concentration)\s*[^\d%]{0,20}([\d.]+)\s*%/);
  if(m){const pct=asPct(m[1]);if(pct){p.max_position_pct=pct;notes.push("Max position → "+(pct*100).toFixed(1)+"%");}}
  // min position
  m=t.match(/(?:min(?:imum)?\s+(?:positions?|weight|allocation)|no name (?:smaller|less) than)\s*[^\d%]{0,15}([\d.]+)\s*%/);
  if(m){const pct=asPct(m[1]);if(pct!=null){p.min_position_pct=pct;notes.push("Min position → "+(pct*100).toFixed(1)+"%");}}
  // holdings range
  m=t.match(/hold(?:ings)?[^0-9]{0,20}(\d+)\s*(?:to|-|–|and)\s*(\d+)/);
  if(m){p.min_holdings=+m[1];p.max_holdings=+m[2];notes.push("Holdings → "+m[1]+"–"+m[2]+" names");}
  // target vol
  m=t.match(/\bvol(?:atility)?\s*(?:ceiling|target|below|under|<=|<|max|at)?\s*(?:of\s*)?([\d.]+)\s*%/)
   ||t.match(/(?:target|ceiling|keep|below|under|<=|<)\s+([\d.]+)\s*%\s*vol(?:atility)?\b/);
  if(m){const pct=asPct(m[1]);if(pct){p.target_vol_pct=pct;notes.push("Target volatility → "+(pct*100).toFixed(1)+"%");}}
  // max dd
  m=t.match(/(?:drawdown|\bdd\b)[^\d%]{0,20}([\d.]+)\s*%/);
  if(m){const pct=asPct(m[1]);if(pct){p.max_drawdown_pct=pct;notes.push("Max drawdown → "+(pct*100).toFixed(1)+"%");}}
  // objective
  if(/\b(?:min(?:imum)? vol|lowest risk|safest|conservative)\b/.test(t)){p.objective="min_vol";notes.push("Objective → min-volatility");}
  else if(/\b(?:max(?:imum)? sharpe|best risk[- ]adjusted|highest sharpe|aggressive)\b/.test(t)){p.objective="sharpe";notes.push("Objective → max-Sharpe");}
  // flags
  if(/\bno\s+pre[- ]revenue\b|\bno\s+r&d\b|\bskip\s+pre[- ]commercial\b/.test(t)){
    p.flags=p.flags||{};p.flags.exclude_pre_revenue=true;notes.push("Flag → exclude R&D / pre-commercial names");}
  // alerts
  p.alert_opts=p.alert_opts||{};
  const onPrice=/(turn on|enable|opt in).*price/.test(t), offPrice=/(turn off|disable|opt out|mute).*price/.test(t);
  if(onPrice&&!offPrice){p.alert_opts.price_move_enabled=true;notes.push("Alerts → price move ON");}
  else if(offPrice&&!onPrice){p.alert_opts.price_move_enabled=false;notes.push("Alerts → price move OFF");}
  const onCat=/(turn on|enable|opt in).*catalyst/.test(t), offCat=/(turn off|disable|opt out|mute).*catalyst/.test(t);
  if(onCat&&!offCat){p.alert_opts.catalyst_enabled=true;notes.push("Alerts → catalyst ON");}
  else if(offCat&&!onCat){p.alert_opts.catalyst_enabled=false;notes.push("Alerts → catalyst OFF");}
  const onSec=/(turn on|enable|opt in).*(filing|sec|edgar)/.test(t), offSec=/(turn off|disable|opt out|mute).*(filing|sec|edgar)/.test(t);
  if(onSec&&!offSec){p.alert_opts.sec_filing_enabled=true;notes.push("Alerts → SEC filing ON");}
  else if(offSec&&!onSec){p.alert_opts.sec_filing_enabled=false;notes.push("Alerts → SEC filing OFF");}
  m=t.match(/alert me (?:if|when).*?([\d.]+)\s*%/);
  if(m){const pct=asPct(m[1]);if(pct){p.alert_opts.price_move_threshold_pct=pct;p.alert_opts.price_move_enabled=true;notes.push("Alerts → price move threshold "+(pct*100).toFixed(1)+"%");}}
  m=raw.match(/([\w.+-]+@[\w-]+\.[\w]{2,})/);
  if(m){p.alert_opts.email=m[1].replace(/[.,;:!? ]+$/,"");notes.push("Alerts → email "+p.alert_opts.email);}
}
function parseAgent(text,profile){
  const p=structuredClone(profile);
  const notes=[];
  // pre-pass: exclude / require handles "A and B" lists
  const verbs=[
    {re:/(?:exclude|avoid|drop|remove|skip)\s+([A-Za-z][A-Za-z\s,\.]{0,80})/gi,key:"excluded_tickers",label:"Excluded"},
    {re:/(?:require|must hold|must include|always hold|include)\s+([A-Za-z][A-Za-z\s,\.]{0,80})/gi,key:"required_tickers",label:"Required"},
  ];
  for(const v of verbs){
    let m;
    while((m=v.re.exec(text))!==null){
      let frag=m[1].split(/(?:\.|;|, (?:cap|hold|vol|alert|with|max|min|target))/)[0];
      const toks=findTickers(frag);
      if(toks.length){
        const existing=new Set(p[v.key]||[]);toks.forEach(x=>existing.add(x));
        p[v.key]=[...existing].sort();
        const msg=v.label+" → "+toks.join(", ");
        if(!notes.includes(msg))notes.push(msg);
      }
    }
  }
  // protect number commas
  let protectedText=text;
  while(/\d,\d{3}/.test(protectedText)){protectedText=protectedText.replace(/(\d),(\d{3})/g,"$1$2");}
  const clauses=protectedText.split(CLAUSE_SPLIT).filter(c=>c.trim());
  for(const c of clauses)parseClause(c,p,notes);
  // dedupe
  const seen=new Set(),out=[];
  for(const n of notes)if(!seen.has(n)){seen.add(n);out.push(n);}
  return {profile:p,notes:out};
}

// ================= Tab router =================
function showTab(id){
  if(!id)id="dashboard";
  $$(".tab").forEach(el=>el.classList.remove("active"));
  const el=document.getElementById(id);
  if(el){el.classList.add("active");}
  $$("header.site nav a").forEach(a=>{
    a.classList.toggle("active",a.getAttribute("href")==="#"+id);
  });
  window.scrollTo(0,0);
}
window.addEventListener("hashchange",()=>showTab(location.hash.slice(1)));

// ================= Render: Dashboard =================
function renderDashboard(){
  const el=$("#dashboard");
  const listed=DATA.constituents.filter(c=>c.series&&c.series.length);
  const byCap=[...listed].sort((a,b)=>(b.market_cap||0)-(a.market_cap||0));
  const byChange=[...listed].sort((a,b)=>(b.change_1d||0)-(a.change_1d||0));
  const up=byChange.slice(0,3),dn=byChange.slice(-3).reverse();
  const cats=DATA.catalysts.slice(0,6);
  const sectors={};
  listed.forEach(c=>{
    const s=c.sector;sectors[s]=sectors[s]||{n:0,mcap:0,chg:0};
    sectors[s].n++;sectors[s].mcap+=(c.market_cap||0);
    sectors[s].chg+=(c.change_1d||0);
  });
  // weighted risk/uranium/capacity/debt
  const totalCap=listed.reduce((s,c)=>s+(c.market_cap||0),0);
  const wavg=(f)=>totalCap?listed.reduce((s,c)=>s+(c.market_cap||0)*(c[f]||0),0)/totalCap:null;
  const regRisk=wavg("regulatory_risk"),uExp=wavg("uranium_exposure");
  const capFactor=(()=>{
    let num=0,den=0;
    listed.forEach(c=>{if(c.capacity_factor!=null&&c.market_cap){num+=c.market_cap*c.capacity_factor;den+=c.market_cap;}});
    return den?num/den:null;
  })();
  const stats=DATA.index_stats;
  el.innerHTML=`
    <section class="hero">
      <div class="hero-left">
        <h1>${DATA.meta.index_name}</h1>
        <p class="muted">A tracked basket of ${listed.length} listed companies (+ ${DATA.constituents.length-listed.length} pending-listing) across operators, fuel cycle, SMR developers, services, and frontier R&D. Generated ${DATA.meta.generated}.</p>
      </div>
      <div class="hero-right stat-grid">
        <div class="stat"><div class="stat-label">Index Level</div>
          <div class="stat-value">${fmt.num(stats.price)}</div>
          <div class="stat-sub ${cls(stats.change_1d)}">${fmt.pct(stats.change_1d)} 1D</div></div>
        <div class="stat"><div class="stat-label">YTD</div>
          <div class="stat-value ${cls(stats.ret_ytd)}">${fmt.pct(stats.ret_ytd)}</div></div>
        <div class="stat"><div class="stat-label">1Y</div>
          <div class="stat-value ${cls(stats.ret_1y)}">${fmt.pct(stats.ret_1y)}</div></div>
        <div class="stat"><div class="stat-label">Ann. Vol</div>
          <div class="stat-value">${fmt.num(stats.vol_ann)}%</div></div>
      </div>
    </section>

    <section class="card">
      <div class="card-header"><h2>Composite index</h2>
        <div class="range-row">
          <div class="range-seg" data-group="dash">
            <button data-r="6m">6M</button><button data-r="1y">1Y</button>
            <button data-r="2y">2Y</button><button data-r="5y">5Y</button>
            <button data-r="10y">10Y</button><button data-r="max">Max</button>
          </div>
          <label class="ind-toggle small muted"><input type="checkbox" id="dash-sma"/> SMA(50,200) <span class="info-icon" data-tip="Simple Moving Average: average of the last N closing prices (50 and 200 days). Used to identify trend direction; price above the 200-day SMA is typically considered a longer-term uptrend.">i</span></label>
          <label class="ind-toggle small muted"><input type="checkbox" id="dash-bb"/> Bollinger <span class="info-icon" data-tip="Bollinger Bands: SMA(20) ± 2 standard deviations. Price hugging the upper band can signal overbought conditions; lower band, oversold. Band width expands with volatility.">i</span></label>
        </div>
      </div>
      <canvas id="indexChart" height="110"></canvas>
    </section>

    <section class="grid-2">
      <div class="card">
        <h2>Index Health Scores</h2>
        <table class="kv">
          <tr><th>Weighted Regulatory Risk (0–10)</th><td>${fmt.num(regRisk)}</td></tr>
          <tr><th>Weighted Uranium Exposure (0–10)</th><td>${fmt.num(uExp)}</td></tr>
          <tr><th>Weighted Capacity Factor</th><td>${capFactor==null?"–":fmt.num(capFactor*100,1)+"%"}</td></tr>
          <tr><th>Annualized Sharpe (vs 4% rf)</th><td>${fmt.num(stats.sharpe,3)}</td></tr>
          <tr><th>Max Drawdown (2Y)</th><td>${fmt.pct(stats.max_dd)}</td></tr>
        </table>
        <p class="small muted">Scores are market-cap weighted across listed constituents. See <a href="#methodology">methodology</a>.</p>
      </div>
      <div class="card">
        <h2>Sector Mix (by market cap)</h2>
        <canvas id="sectorChart" height="180"></canvas>
      </div>
    </section>

    <section class="grid-2">
      <div class="card"><h2>Top Gainers (1D)</h2>
        <table class="movers">
          ${up.map(r=>`<tr class="clickable" data-ticker="${r.ticker}">
            <td><strong>${r.ticker}</strong> <span class="muted small">${r.name}</span></td>
            <td class="num">$${fmt.num(r.price)}</td>
            <td class="num up">${fmt.pct(r.change_1d)}</td></tr>`).join("")}
        </table></div>
      <div class="card"><h2>Top Losers (1D)</h2>
        <table class="movers">
          ${dn.map(r=>`<tr class="clickable" data-ticker="${r.ticker}">
            <td><strong>${r.ticker}</strong> <span class="muted small">${r.name}</span></td>
            <td class="num">$${fmt.num(r.price)}</td>
            <td class="num down">${fmt.pct(r.change_1d)}</td></tr>`).join("")}
        </table></div>
    </section>

    <section class="card">
      <h2>Catalyst Watch — next 6</h2>
      <table class="catalysts">
        <thead><tr><th>Date</th><th>Ticker</th><th>Event</th><th class="num">Importance</th></tr></thead>
        <tbody>
        ${cats.map(c=>`<tr class="clickable" data-ticker="${c.ticker}">
          <td class="small">${c.date}</td><td><strong>${c.ticker}</strong></td>
          <td>${c.event}</td>
          <td class="num">${"★".repeat(c.importance)}</td></tr>`).join("")}
        </tbody></table>
    </section>

    <section class="card">
      <h2>Constituents</h2>
      <table class="constituents">
        <thead><tr><th>Ticker</th><th>Name</th><th>Sector</th><th>Stage</th>
          <th class="num">Price</th><th class="num">1D</th><th class="num">1M</th>
          <th class="num">YTD</th><th class="num">1Y</th>
          <th class="num">Market Cap</th><th class="num">Ann. Vol</th></tr></thead>
        <tbody>
        ${byCap.map(r=>`<tr class="clickable" data-ticker="${r.ticker}">
          <td><strong>${r.ticker}</strong></td><td>${r.name}</td>
          <td class="muted small">${pretty(r.sector)}</td>
          <td class="small stage-${r.dev_stage.replace(/[^A-Za-z]/g,"")}">${r.dev_stage}</td>
          <td class="num">${r.price?("$"+fmt.num(r.price)):"–"}</td>
          <td class="num ${cls(r.change_1d)}">${fmt.pct(r.change_1d)}</td>
          <td class="num ${cls(r.ret_1m)}">${fmt.pct(r.ret_1m)}</td>
          <td class="num ${cls(r.ret_ytd)}">${fmt.pct(r.ret_ytd)}</td>
          <td class="num ${cls(r.ret_1y)}">${fmt.pct(r.ret_1y)}</td>
          <td class="num">${fmt.money(r.market_cap)}</td>
          <td class="num">${fmt.num(r.vol_ann)}%</td></tr>`).join("")}
        </tbody></table>
    </section>
  `;
  // click rows
  $$("#dashboard tr.clickable").forEach(tr=>{
    tr.addEventListener("click",()=>{
      const t=tr.dataset.ticker;
      location.hash="companies";
      setTimeout(()=>renderCompanies(t),10);
    });
  });
  // index chart with range selector + optional indicator overlays
  const ctx=$("#indexChart").getContext("2d");
  const drawIdx=()=>{
    const r = window._range||"2y";
    const slice = sliceRange(DATA.index_series, r);
    const ds = [{label:"NRI",data:slice.map(p=>p.p),borderColor:"#2d7fbf",
       borderWidth:1.8,fill:false,pointRadius:0,tension:0.15}];
    if($("#dash-sma") && $("#dash-sma").checked){
      const s50 = sma(slice,50), s200 = sma(slice,200);
      ds.push({label:"SMA 50",data:s50.map(p=>p.p),borderColor:"#c78f4b",
        borderWidth:1.2,fill:false,pointRadius:0,borderDash:[4,3]});
      ds.push({label:"SMA 200",data:s200.map(p=>p.p),borderColor:"#d25a5a",
        borderWidth:1.2,fill:false,pointRadius:0,borderDash:[6,3]});
    }
    if($("#dash-bb") && $("#dash-bb").checked){
      const bb = bollinger(slice,20,2);
      ds.push({label:"BB Upper",data:bb.upper.map(p=>p.p),borderColor:"rgba(154,164,178,0.5)",
        borderWidth:0.9,fill:false,pointRadius:0});
      ds.push({label:"BB Lower",data:bb.lower.map(p=>p.p),borderColor:"rgba(154,164,178,0.5)",
        borderWidth:0.9,fill:"-1",backgroundColor:"rgba(154,164,178,0.06)",pointRadius:0});
    }
    if(window._idxChart) window._idxChart.destroy();
    window._idxChart=new Chart(ctx,{type:"line",
      data:{labels:slice.map(p=>p.d), datasets:ds},
      options:{responsive:true,interaction:{mode:"index",intersect:false},
        scales:{x:{ticks:{color:"#9aa4b2",maxTicksLimit:8},grid:{display:false}},
               y:{ticks:{color:"#9aa4b2"},grid:{color:"rgba(255,255,255,0.05)"}}},
        plugins:{legend:{labels:{color:"#e6ebf2",font:{size:10},boxWidth:12}}}}});
  };
  drawIdx();
  // wire range segmented control
  $$('#dashboard .range-seg[data-group="dash"] button').forEach(b=>{
    b.classList.toggle("active", b.dataset.r===(window._range||"2y"));
    b.addEventListener("click",()=>{
      window._range = b.dataset.r;
      $$('#dashboard .range-seg[data-group="dash"] button').forEach(x=>x.classList.toggle("active",x===b));
      drawIdx();
    });
  });
  if($("#dash-sma")) $("#dash-sma").addEventListener("change",drawIdx);
  if($("#dash-bb")) $("#dash-bb").addEventListener("change",drawIdx);
  // sector doughnut
  const secCtx=$("#sectorChart").getContext("2d");
  const sLabels=Object.keys(sectors), sVals=sLabels.map(s=>sectors[s].mcap);
  new Chart(secCtx,{type:"doughnut",
    data:{labels:sLabels.map(pretty),datasets:[{data:sVals,
      backgroundColor:["#2d7fbf","#c78f4b","#6bb07c","#d25a5a","#9aa4b2"]}]},
    options:{plugins:{legend:{position:"right",labels:{color:"#e6ebf2",font:{size:11}}}}}});
}

// ================= Render: Companies =================
function renderCompanies(selectedTicker){
  const el=$("#companies");
  const options=DATA.constituents.map(c=>
    `<option value="${c.ticker}" ${c.ticker===selectedTicker?"selected":""}>${c.ticker} — ${c.name}</option>`).join("");
  el.innerHTML=`
    <section class="hero"><div class="hero-left"><h1>Companies</h1>
      <p class="muted">Select a constituent to see its price history, full metric card, and linked catalysts.</p></div>
      <div class="hero-right"><select id="co-select" style="width:100%;padding:10px;">${options}</select></div></section>
    <div id="co-body"></div>`;
  const sel=$("#co-select");
  function renderCompany(tkr){
    const c=DATA.constituents.find(x=>x.ticker===tkr);
    if(!c){$("#co-body").innerHTML="<p>Not found</p>";return;}
    const myCats=DATA.catalysts.filter(x=>x.ticker===c.ticker);
    const myNews=(DATA.news_per_ticker[c.ticker]||[]).map(h=>`<li>${h}</li>`).join("");
    const unlistedNote=c.listed?"":`<p class="badge">Private / pre-listing · proxy ${c.proxy_ticker||"—"} · expected ${c.expected_listing||"TBD"}</p>`;
    $("#co-body").innerHTML=`
      <section class="grid-2">
        <div class="card"><div class="card-header"><h2>${c.ticker} — ${c.name}</h2>
          <span class="pill">${pretty(c.sector)}</span></div>
          ${unlistedNote}
          <p class="muted">${c.thesis||""}</p>
          <div class="range-row">
            <div class="range-seg" data-group="co">
              <button data-r="1m">1M</button><button data-r="6m">6M</button>
              <button data-r="1y">1Y</button><button data-r="2y">2Y</button>
              <button data-r="5y">5Y</button><button data-r="10y">10Y</button>
              <button data-r="max">Max</button>
            </div>
          </div>
          <div class="ind-row">
            <label class="ind-toggle"><input type="checkbox" id="co-sma20" checked/> SMA20 <span class="info-icon" data-tip="Simple Moving Average (20): arithmetic mean of the last 20 closing prices. Each data point gets equal weight. Short-term trend indicator — price crossing above SMA20 suggests near-term momentum.">i</span></label>
            <label class="ind-toggle"><input type="checkbox" id="co-sma50" checked/> SMA50 <span class="info-icon" data-tip="Simple Moving Average (50): mean of the last 50 closes. Widely used medium-term trend line. A cross of SMA50 over SMA200 is the classic 'golden cross' signal.">i</span></label>
            <label class="ind-toggle"><input type="checkbox" id="co-sma200"/> SMA200 <span class="info-icon" data-tip="Simple Moving Average (200): mean of the last 200 closes. Long-term trend filter used by institutional traders. Price persistently above SMA200 is a structural uptrend.">i</span></label>
            <label class="ind-toggle"><input type="checkbox" id="co-ema"/> EMA20 <span class="info-icon" data-tip="Exponential Moving Average (20): weighted mean of recent closes with smoothing factor k = 2/(N+1). Reacts faster to new prices than SMA of the same period.">i</span></label>
            <label class="ind-toggle"><input type="checkbox" id="co-bb"/> Bollinger(20,2) <span class="info-icon" data-tip="Bollinger Bands: SMA(20) ± 2 standard deviations of the last 20 closes. Bands widen during volatility and contract when quiet. Touches of the outer bands flag stretched conditions.">i</span></label>
            <label class="ind-toggle"><input type="checkbox" id="co-vwap"/> VWAP <span class="info-icon" data-tip="Volume Weighted Average Price: cumulative Σ(price × volume) ÷ cumulative Σ(volume) anchored to the start of the visible window. Benchmark price institutions use to gauge execution quality.">i</span></label>
            <label class="ind-toggle"><input type="checkbox" id="co-rsi" checked/> RSI(14) <span class="info-icon" data-tip="Relative Strength Index (14): 100 − 100/(1 + avg gain / avg loss) using Wilder's smoothing over 14 bars. Scale 0–100. Above 70 = overbought; below 30 = oversold.">i</span></label>
            <label class="ind-toggle"><input type="checkbox" id="co-macd" checked/> MACD(12,26,9) <span class="info-icon" data-tip="Moving Average Convergence Divergence: MACD line = EMA(12) − EMA(26). Signal line = EMA(9) of MACD. Histogram = MACD − signal. Crosses of MACD above signal suggest bullish momentum shifts.">i</span></label>
          </div>
          <canvas id="coChart" height="180"></canvas>
          <canvas id="coRsiChart" height="60" style="margin-top:6px;"></canvas>
          <canvas id="coMacdChart" height="60" style="margin-top:6px;"></canvas>
        </div>
        <div class="card"><h2>Key Metrics</h2>
        <table class="kv">
          <tr><th>Price</th><td>${c.price?("$"+fmt.num(c.price)):"–"}</td></tr>
          <tr><th>1D · 1M · YTD · 1Y</th><td>
            <span class="${cls(c.change_1d)}">${fmt.pct(c.change_1d)}</span> ·
            <span class="${cls(c.ret_1m)}">${fmt.pct(c.ret_1m)}</span> ·
            <span class="${cls(c.ret_ytd)}">${fmt.pct(c.ret_ytd)}</span> ·
            <span class="${cls(c.ret_1y)}">${fmt.pct(c.ret_1y)}</span></td></tr>
          <tr><th>Market Cap</th><td>${fmt.money(c.market_cap)}</td></tr>
          <tr><th>Ann. Vol / Sharpe</th><td>${fmt.num(c.vol_ann)}% / ${fmt.num(c.sharpe,3)}</td></tr>
          <tr><th>Max Drawdown (2Y)</th><td>${fmt.pct(c.max_dd)}</td></tr>
          <tr><th>Dev Stage</th><td class="stage-${c.dev_stage.replace(/[^A-Za-z]/g,"")}">${c.dev_stage}</td></tr>
          <tr><th>Regulatory Risk (0–10)</th><td>${c.regulatory_risk}</td></tr>
          <tr><th>Uranium Exposure (0–10)</th><td>${c.uranium_exposure}</td></tr>
          <tr><th>Capacity Factor</th><td>${c.capacity_factor==null?"–":fmt.num(c.capacity_factor*100,1)+"%"}${c.capacity_factor_source?`<div class="small muted">${c.capacity_factor_source}</div>`:""}</td></tr>
          <tr><th>Nuclear Revenue Share</th><td>${fmt.num(c.nuclear_revenue_share*100,0)}%</td></tr>
          <tr><th>Country</th><td>${c.country}</td></tr>
          ${c.notes?`<tr><th>Notes</th><td class="small muted">${c.notes}</td></tr>`:""}
        </table></div>
      </section>
      <section class="grid-2">
        <div class="card"><h2>Upcoming Catalysts</h2>
        ${myCats.length?`<table class="catalysts">${myCats.map(c=>`<tr><td class="small muted">${c.date}</td><td>${c.event}</td><td class="num">${"★".repeat(c.importance)}</td></tr>`).join("")}</table>`:`<p class="muted">None tracked.</p>`}
        </div>
        <div class="card"><h2>Sample News</h2>
        <ul class="notes-list">${myNews||`<li class="muted">No sample headlines available.</li>`}</ul>
        <p class="small muted">Live news is aggregated from Google News RSS, SEC EDGAR, and Yahoo Finance in the FastAPI backend.</p>
        </div>
      </section>`;
    if(c.series&&c.series.length){
      const drawCo = ()=>{
        const r = window._range||"2y";
        const slice = sliceRange(c.series, r);
        if(slice.length<3) return;
        // --- main price chart + price-level overlays ---
        const ds = [{label:c.ticker,data:slice.map(p=>p.p),borderColor:"#c78f4b",
          borderWidth:1.6,fill:false,pointRadius:0,tension:0.15}];
        if($("#co-sma20") && $("#co-sma20").checked){
          const s = sma(slice,20);
          ds.push({label:"SMA 20",data:s.map(p=>p.p),borderColor:"#6bb07c",
            borderWidth:1.0,fill:false,pointRadius:0});
        }
        if($("#co-sma50") && $("#co-sma50").checked){
          const s = sma(slice,50);
          ds.push({label:"SMA 50",data:s.map(p=>p.p),borderColor:"#2d7fbf",
            borderWidth:1.1,fill:false,pointRadius:0,borderDash:[4,3]});
        }
        if($("#co-sma200") && $("#co-sma200").checked){
          const s = sma(slice,200);
          ds.push({label:"SMA 200",data:s.map(p=>p.p),borderColor:"#d25a5a",
            borderWidth:1.1,fill:false,pointRadius:0,borderDash:[6,3]});
        }
        if($("#co-ema") && $("#co-ema").checked){
          const e = ema(slice,20);
          ds.push({label:"EMA 20",data:e.map(p=>p.p),borderColor:"#9b7bd4",
            borderWidth:1.0,fill:false,pointRadius:0,borderDash:[2,2]});
        }
        if($("#co-bb") && $("#co-bb").checked){
          const bb = bollinger(slice,20,2);
          ds.push({label:"BB Upper",data:bb.upper.map(p=>p.p),borderColor:"rgba(154,164,178,0.55)",
            borderWidth:0.9,fill:false,pointRadius:0});
          ds.push({label:"BB Middle",data:bb.middle.map(p=>p.p),borderColor:"rgba(154,164,178,0.35)",
            borderWidth:0.7,fill:false,pointRadius:0,borderDash:[2,2]});
          ds.push({label:"BB Lower",data:bb.lower.map(p=>p.p),borderColor:"rgba(154,164,178,0.55)",
            borderWidth:0.9,fill:"-2",backgroundColor:"rgba(154,164,178,0.07)",pointRadius:0});
        }
        if($("#co-vwap") && $("#co-vwap").checked){
          const vw = vwap(slice);
          const hasAny = vw.some(x=>x.p!=null);
          if(hasAny){
            ds.push({label:"VWAP",data:vw.map(p=>p.p),borderColor:"#e6ebf2",
              borderWidth:1.0,fill:false,pointRadius:0,borderDash:[3,2]});
          }
        }
        if(window._coChart) window._coChart.destroy();
        window._coChart = new Chart($("#coChart").getContext("2d"),{type:"line",
          data:{labels:slice.map(p=>p.d), datasets:ds},
          options:{responsive:true, interaction:{mode:"index",intersect:false},
            plugins:{legend:{labels:{color:"#e6ebf2",font:{size:10},boxWidth:12}}},
            scales:{x:{ticks:{color:"#9aa4b2",maxTicksLimit:8},grid:{display:false}},
              y:{ticks:{color:"#9aa4b2"},grid:{color:"rgba(255,255,255,0.05)"}}}}});

        // --- RSI pane ---
        const rsiEl = $("#coRsiChart");
        if(rsiEl){
          rsiEl.style.display = ($("#co-rsi") && $("#co-rsi").checked) ? "" : "none";
          if($("#co-rsi") && $("#co-rsi").checked){
            const r14 = rsi(slice,14);
            if(window._coRsi) window._coRsi.destroy();
            window._coRsi = new Chart(rsiEl.getContext("2d"),{type:"line",
              data:{labels:slice.map(p=>p.d),datasets:[
                {label:"RSI(14)",data:r14.map(p=>p.p),borderColor:"#9b7bd4",
                  borderWidth:1.2,fill:false,pointRadius:0},
                {label:"70",data:slice.map(()=>70),borderColor:"rgba(210,90,90,0.6)",
                  borderWidth:0.8,borderDash:[3,3],fill:false,pointRadius:0},
                {label:"30",data:slice.map(()=>30),borderColor:"rgba(107,176,124,0.6)",
                  borderWidth:0.8,borderDash:[3,3],fill:false,pointRadius:0},
              ]},
              options:{responsive:true,plugins:{legend:{display:false},
                title:{display:true,text:"RSI(14)",color:"#9aa4b2",align:"start",font:{size:10}}},
                scales:{x:{display:false},
                  y:{min:0,max:100,ticks:{color:"#9aa4b2",stepSize:25,font:{size:9}},
                    grid:{color:"rgba(255,255,255,0.04)"}}}}});
          }
        }
        // --- MACD pane ---
        const macdEl = $("#coMacdChart");
        if(macdEl){
          macdEl.style.display = ($("#co-macd") && $("#co-macd").checked) ? "" : "none";
          if($("#co-macd") && $("#co-macd").checked){
            const m = macd(slice,12,26,9);
            if(window._coMacd) window._coMacd.destroy();
            window._coMacd = new Chart(macdEl.getContext("2d"),{
              data:{labels:slice.map(p=>p.d),datasets:[
                {type:"bar",label:"Histogram",data:m.histogram.map(p=>p.p),
                  backgroundColor:m.histogram.map(p=>p.p==null?"rgba(0,0,0,0)":(p.p>=0?"rgba(107,176,124,0.7)":"rgba(210,90,90,0.7)")),
                  borderWidth:0},
                {type:"line",label:"MACD",data:m.line.map(p=>p.p),borderColor:"#2d7fbf",
                  borderWidth:1.2,fill:false,pointRadius:0},
                {type:"line",label:"Signal",data:m.signal.map(p=>p.p),borderColor:"#c78f4b",
                  borderWidth:1.0,fill:false,pointRadius:0},
              ]},
              options:{responsive:true,plugins:{legend:{display:false},
                title:{display:true,text:"MACD(12,26,9)",color:"#9aa4b2",align:"start",font:{size:10}}},
                scales:{x:{display:false},
                  y:{ticks:{color:"#9aa4b2",font:{size:9}},grid:{color:"rgba(255,255,255,0.04)"}}}}});
          }
        }
      };
      drawCo();
      // wire range + indicator toggles
      $$('#companies .range-seg[data-group="co"] button').forEach(b=>{
        b.classList.toggle("active", b.dataset.r===(window._range||"2y"));
        b.addEventListener("click",()=>{
          window._range = b.dataset.r;
          $$('#companies .range-seg[data-group="co"] button').forEach(x=>x.classList.toggle("active",x===b));
          drawCo();
        });
      });
      ["#co-sma20","#co-sma50","#co-sma200","#co-ema","#co-bb","#co-vwap","#co-rsi","#co-macd"].forEach(sel=>{
        const el = $(sel); if(el) el.addEventListener("change",drawCo);
      });
    }
  }
  sel.addEventListener("change",e=>renderCompany(e.target.value));
  renderCompany(selectedTicker||sel.value);
}

// ================= Render: Optimizer =================
function renderOptimizer(){
  const el=$("#optimizer");
  el.innerHTML=`
    <section class="hero"><div class="hero-left">
      <h1>Portfolio Optimizer</h1>
      <p class="muted">Markowitz mean-variance with a position cap. Browser build uses precomputed weights across 3 cap levels × 4 objectives. The FastAPI app runs live SLSQP optimization against current prices.</p></div></section>
    <section class="card"><h2>Constraints</h2>
    <div style="display:flex;gap:20px;flex-wrap:wrap;align-items:flex-end;">
      <label style="display:flex;flex-direction:column;color:var(--fg-dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em;">Objective
        <select id="opt-obj" style="margin-top:4px;min-width:160px;">
          <option value="sharpe" selected>Max Sharpe</option>
          <option value="min_vol">Min Volatility</option>
          <option value="inverse_vol">Risk Parity (inverse-vol)</option>
          <option value="equal">Equal Weight</option></select></label>
      <label style="display:flex;flex-direction:column;color:var(--fg-dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em;">Max Position
        <select id="opt-cap" style="margin-top:4px;min-width:140px;">
          <option value="15">15%</option>
          <option value="20" selected>20%</option>
          <option value="25">25%</option></select></label>
      <label style="display:flex;flex-direction:column;color:var(--fg-dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em;">Capital
        <input id="opt-cap-dollars" type="number" value="100000" style="margin-top:4px;width:140px;"/></label>
    </div></section>
    <div id="opt-body"></div>`;
  const draw=()=>{
    const obj=$("#opt-obj").value, cap=$("#opt-cap").value, capital=+$("#opt-cap-dollars").value||100000;
    const key=`${obj}__${cap}`;
    const r=DATA.optimizer[key];
    if(!r){$("#opt-body").innerHTML="<p>Not found.</p>";return;}
    $("#opt-body").innerHTML=`
      <section class="grid-2">
        <div class="card"><h2>Portfolio Stats</h2>
        <table class="kv">
          <tr><th>Expected Annual Return</th><td>${fmt.pct(r.ann_return*100,2)}</td></tr>
          <tr><th>Expected Annual Volatility</th><td>${fmt.num(r.ann_vol*100,2)}%</td></tr>
          <tr><th>Sharpe Ratio (rf ${fmt.num(r.risk_free*100,1)}%)</th><td>${fmt.num(r.sharpe,3)}</td></tr>
          <tr><th>Positions</th><td>${r.breakdown.length}</td></tr>
        </table></div>
        <div class="card"><h2>Efficient Frontier</h2>
        <canvas id="frontChart" height="170"></canvas></div>
      </section>
      <section class="card"><h2>Weights (${pretty(r.objective)}, cap ${Math.round(r.max_weight*100)}%, $${capital.toLocaleString()})</h2>
      <table>
        <thead><tr><th>Ticker</th><th>Name</th><th class="num">Weight</th><th class="num">Dollars</th>
          <th class="num">Exp. Return</th><th class="num">Volatility</th></tr></thead>
        <tbody>${r.breakdown.map(b=>`<tr>
          <td><strong>${b.ticker}</strong></td><td>${b.name}</td>
          <td class="num">${fmt.num(b.weight*100,2)}%</td>
          <td class="num">$${Math.round(b.weight*capital).toLocaleString()}</td>
          <td class="num ${cls(b.exp_return)}">${fmt.pct(b.exp_return*100,2)}</td>
          <td class="num">${fmt.num(b.volatility*100,2)}%</td></tr>`).join("")}</tbody>
      </table></section>`;
    const ctx=$("#frontChart").getContext("2d");
    if(window._frontChart)window._frontChart.destroy();
    window._frontChart=new Chart(ctx,{type:"scatter",
      data:{datasets:[{label:"Frontier",
          data:DATA.frontier.map(p=>({x:p.vol*100,y:p.return*100})),
          borderColor:"#2d7fbf",backgroundColor:"#2d7fbf",showLine:true,tension:0.15,pointRadius:2},
        {label:"Current Portfolio",
          data:[{x:r.ann_vol*100,y:r.ann_return*100}],
          backgroundColor:"#c78f4b",pointRadius:7}]},
      options:{scales:{x:{title:{display:true,text:"Volatility %",color:"#9aa4b2"},
          ticks:{color:"#9aa4b2"},grid:{color:"rgba(255,255,255,0.05)"}},
        y:{title:{display:true,text:"Expected return %",color:"#9aa4b2"},
          ticks:{color:"#9aa4b2"},grid:{color:"rgba(255,255,255,0.05)"}}},
        plugins:{legend:{labels:{color:"#e6ebf2"}}}}});
  };
  ["#opt-obj","#opt-cap","#opt-cap-dollars"].forEach(s=>$(s).addEventListener("change",draw));
  ["#opt-obj","#opt-cap","#opt-cap-dollars"].forEach(s=>$(s).addEventListener("input",draw));
  draw();
}

// ================= Render: Backtest =================
function renderBacktest(){
  const el=$("#backtest");
  const statsTable=[["NRI",DATA.index_stats],...Object.entries(DATA.benchmarks).map(([k,v])=>[k,v.stats])]
    .map(([n,s])=>`<tr><td><strong>${n}</strong></td>
      <td class="num ${cls(s.total_return)}">${fmt.pct(s.total_return)}</td>
      <td class="num">${fmt.num(s.cagr)}%</td>
      <td class="num">${fmt.num(s.vol_ann)}%</td>
      <td class="num">${fmt.num(s.sharpe,3)}</td>
      <td class="num down">${fmt.pct(s.max_dd)}</td></tr>`).join("");
  el.innerHTML=`
    <section class="hero"><div class="hero-left">
      <h1>Backtest (2Y)</h1>
      <p class="muted">Composite index vs benchmark ETFs: SPY (broad market), URA (uranium ETF), NLR (nuclear+uranium ETF).
        All series rebased to 100 on first trading day of the 2Y window.</p></div></section>
    <section class="card"><canvas id="btChart" height="130"></canvas></section>
    <section class="card"><h2>Stats</h2>
    <table>
      <thead><tr><th>Series</th><th class="num">Total</th><th class="num">CAGR</th>
        <th class="num">Vol</th><th class="num">Sharpe</th><th class="num">Max DD</th></tr></thead>
      <tbody>${statsTable}</tbody></table></section>`;
  // rebase + plot
  const rebase=(ser)=>{if(!ser.length)return ser;const s=ser[0].p;return ser.map(p=>({d:p.d,p:p.p*100/s}));};
  const idx=rebase(DATA.index_series);
  const bSPY=rebase(DATA.benchmarks.SPY.series);
  const bURA=rebase(DATA.benchmarks.URA.series);
  const bNLR=rebase(DATA.benchmarks.NLR.series);
  // align on idx dates
  const byDate=(s)=>Object.fromEntries(s.map(p=>[p.d,p.p]));
  const spy=byDate(bSPY),ura=byDate(bURA),nlr=byDate(bNLR);
  const labels=idx.map(p=>p.d);
  const ctx=$("#btChart").getContext("2d");
  new Chart(ctx,{type:"line",
    data:{labels,datasets:[
      {label:"NRI",data:idx.map(p=>p.p),borderColor:"#2d7fbf",borderWidth:2,fill:false,pointRadius:0,tension:0.15},
      {label:"SPY",data:labels.map(d=>spy[d]||null),borderColor:"#6bb07c",borderWidth:1.4,fill:false,pointRadius:0,tension:0.15,borderDash:[4,3]},
      {label:"URA",data:labels.map(d=>ura[d]||null),borderColor:"#c78f4b",borderWidth:1.4,fill:false,pointRadius:0,tension:0.15,borderDash:[4,3]},
      {label:"NLR",data:labels.map(d=>nlr[d]||null),borderColor:"#d25a5a",borderWidth:1.4,fill:false,pointRadius:0,tension:0.15,borderDash:[4,3]},
    ]},
    options:{plugins:{legend:{labels:{color:"#e6ebf2"}}},
      scales:{x:{ticks:{color:"#9aa4b2",maxTicksLimit:8},grid:{display:false}},
              y:{ticks:{color:"#9aa4b2"},grid:{color:"rgba(255,255,255,0.05)"}}}}});
}

// ================= Render: Catalysts =================
function renderCatalysts(){
  const el=$("#catalysts");
  const byDate=[...DATA.catalysts].sort((a,b)=>String(a.date).localeCompare(String(b.date)));
  el.innerHTML=`
    <section class="hero"><div class="hero-left"><h1>Catalyst Calendar</h1>
      <p class="muted">Hand-curated forward-looking events. Importance 1–5 reflects potential price impact; stars indicate directional catalysts for stock-picking and sizing.</p></div></section>
    <section class="card"><table class="catalysts">
      <thead><tr><th>Date</th><th>Ticker</th><th>Event</th><th class="num">Importance</th></tr></thead>
      <tbody>${byDate.map(c=>`<tr class="clickable" data-ticker="${c.ticker}">
        <td class="small">${c.date}</td><td><strong>${c.ticker}</strong></td>
        <td>${c.event}</td><td class="num">${"★".repeat(c.importance)}</td></tr>`).join("")}</tbody></table></section>`;
  $$("#catalysts tr.clickable").forEach(tr=>{
    tr.addEventListener("click",()=>{location.hash="companies";setTimeout(()=>renderCompanies(tr.dataset.ticker),10);});
  });
}

// ================= Render: News =================
function renderNews(){
  const el=$("#news");
  el.innerHTML=`
    <section class="hero"><div class="hero-left"><h1>News</h1>
      <p class="muted">Sample headlines for offline demo. Live feed in the FastAPI backend aggregates Yahoo Finance, Google News RSS, and SEC EDGAR.</p></div></section>
    <section class="card"><h2>Sector &amp; theme headlines</h2>
      <table class="news">${DATA.news_sector.map(n=>`<tr>
        <td class="small muted" style="white-space:nowrap;width:160px;">${n.ts.slice(0,16)}</td>
        <td><a href="${n.url}" target="_blank" rel="noopener">${n.title}</a>
        <div class="small muted">${n.source}</div></td></tr>`).join("")}</table></section>
    <section class="card"><h2>Per-constituent headlines</h2>
      ${DATA.constituents.map(c=>{
        const items=DATA.news_per_ticker[c.ticker]||[];
        if(!items.length)return"";
        return `<details open><summary><strong>${c.ticker}</strong> · ${c.name} <span class="muted small">(${items.length})</span></summary>
          <ul class="notes-list">${items.map(h=>`<li>${h}</li>`).join("")}</ul></details>`;
      }).join("")}</section>`;
}

// ================= Render: Pre-IPO =================
function renderPreIPO(){
  const el=$("#preipo");
  const statusPill=(s)=>s==="filed"?`<span class="pill on">Filed</span>`:
    s==="imminent"?`<span class="pill on">Imminent</span>`:
    `<span class="pill">Private</span>`;
  el.innerHTML=`
    <section class="hero"><div class="hero-left"><h1>Pre-IPO Watchlist</h1>
      <p class="muted">Private companies to track. On first trade, promote into the main index by adding to <code>data/constituents.json</code>.</p></div></section>
    <section>${DATA.pre_ipo.map(c=>`
      <div class="company-card">
        <h3>${c.name} ${statusPill(c.status)}${c.expected_ticker?`<span class="badge">${c.expected_ticker}</span>`:""}</h3>
        <p class="muted">${c.technology}</p>
        <table class="kv" style="margin-top:10px;">
          <tr><th>Last Round</th><td>${c.last_round||"–"}</td></tr>
          <tr><th>Last Valuation</th><td>${c.last_valuation_usd?fmt.money(c.last_valuation_usd):"–"}</td></tr>
          <tr><th>Lead Investors</th><td>${(c.lead_investors||[]).map(i=>`<span class="chip">${i}</span>`).join(" ")}</td></tr>
          <tr><th>Notes</th><td class="small muted">${c.notes||""}</td></tr>
          <tr><th>Source</th><td class="small muted">${c.source||""}</td></tr>
        </table>
      </div>`).join("")}</section>`;
}

// ================= Render: Profile =================
function renderProfile(){
  const el=$("#profile");
  const p=loadProfile();
  const parse_notes=window._lastParseNotes||[];
  window._lastParseNotes=null;
  el.innerHTML=`
    <section class="hero"><div class="hero-left"><h1>Capital Constraints Profile</h1>
      <p class="muted">Tell the agent in plain English or edit the form directly. Changes persist in your browser (localStorage) and feed the optimizer &amp; alerts.</p>
      <p><span class="pill on">Current posture: ${riskPosture(p)}</span></p></div></section>
    <section class="card"><h2>Chat with the constraint agent</h2>
      <form id="chat-form" style="display:flex;gap:8px;">
        <input id="chat-text" type="text" placeholder='e.g., "I have $250k, cap positions at 15%, exclude NNE and IMSR, target 25% vol"' style="flex:1;"/>
        <button type="submit">Apply</button></form>
      ${parse_notes.length?`<ul class="notes-list" style="margin-top:12px;">${parse_notes.map(n=>`<li>${n}</li>`).join("")}</ul>`:""}
      <p class="small muted" style="margin-top:10px;">Understood phrases: capital ($100k, 1M), max/min position %, hold N to M names, target N% vol, max drawdown, min-vol / max-Sharpe / aggressive, exclude / require tickers, alert toggles, email, no pre-revenue.</p></section>
    <section class="card"><h2>Profile</h2>
      <form id="prof-form">
      <div class="grid-2"><table class="kv">
        <tr><th>Capital ($)</th><td><input type="number" name="capital_usd" value="${p.capital_usd}"/></td></tr>
        <tr><th>Max position (%)</th><td><input type="number" step="0.5" name="max_position_pct" value="${(p.max_position_pct*100).toFixed(1)}"/></td></tr>
        <tr><th>Min position (%)</th><td><input type="number" step="0.5" name="min_position_pct" value="${(p.min_position_pct*100).toFixed(1)}"/></td></tr>
        <tr><th>Min / Max holdings</th><td>
          <input type="number" name="min_holdings" value="${p.min_holdings}" style="width:80px;"/>
          <input type="number" name="max_holdings" value="${p.max_holdings}" style="width:80px;"/></td></tr>
        <tr><th>Target vol (%)</th><td><input type="number" step="0.5" name="target_vol_pct" value="${(p.target_vol_pct*100).toFixed(1)}"/></td></tr>
      </table><table class="kv">
        <tr><th>Max drawdown (%)</th><td><input type="number" step="0.5" name="max_drawdown_pct" value="${(p.max_drawdown_pct*100).toFixed(1)}"/></td></tr>
        <tr><th>Risk-free rate (%)</th><td><input type="number" step="0.1" name="risk_free_rate" value="${(p.risk_free_rate*100).toFixed(1)}"/></td></tr>
        <tr><th>Objective</th><td><select name="objective">
          <option value="sharpe" ${p.objective==="sharpe"?"selected":""}>Max Sharpe</option>
          <option value="min_vol" ${p.objective==="min_vol"?"selected":""}>Min Volatility</option>
        </select></td></tr>
        <tr><th>Excluded tickers</th><td><input type="text" name="excluded_tickers" value="${p.excluded_tickers.join(", ")}"/></td></tr>
        <tr><th>Required tickers</th><td><input type="text" name="required_tickers" value="${p.required_tickers.join(", ")}"/></td></tr>
      </table></div>
      <div style="margin-top:10px;"><button type="submit">Save</button>
        <button type="button" id="reset-prof" class="secondary" style="margin-left:8px;">Reset to defaults</button></div>
      </form></section>
    <section class="card"><h2>History</h2>
      <ul class="notes-list">${(p.history||[]).map(h=>`<li class="hist"><span class="small muted">${h.ts.slice(0,19).replace("T"," ")}</span> — ${h.note}</li>`).join("")}</ul></section>`;
  $("#chat-form").addEventListener("submit",ev=>{
    ev.preventDefault();
    const text=$("#chat-text").value.trim();if(!text)return;
    const {profile:np,notes}=parseAgent(text,p);
    if(notes.length){logChange(np,"Agent: "+notes.join("; "));}
    else{logChange(np,`Agent: no changes understood from: ${text.slice(0,80)}`);
      notes.push("(no changes recognized — try rephrasing, or use the form below)");}
    saveProfile(np);
    window._lastParseNotes=notes;
    renderProfile();
  });
  $("#prof-form").addEventListener("submit",ev=>{
    ev.preventDefault();
    const f=new FormData(ev.target);
    const np=loadProfile();
    np.capital_usd=+f.get("capital_usd");
    np.max_position_pct=(+f.get("max_position_pct"))/100;
    np.min_position_pct=(+f.get("min_position_pct"))/100;
    np.min_holdings=+f.get("min_holdings");np.max_holdings=+f.get("max_holdings");
    np.target_vol_pct=(+f.get("target_vol_pct"))/100;
    np.max_drawdown_pct=(+f.get("max_drawdown_pct"))/100;
    np.risk_free_rate=(+f.get("risk_free_rate"))/100;
    np.objective=f.get("objective");
    np.excluded_tickers=(f.get("excluded_tickers")||"").split(",").map(s=>s.trim().toUpperCase()).filter(Boolean);
    np.required_tickers=(f.get("required_tickers")||"").split(",").map(s=>s.trim().toUpperCase()).filter(Boolean);
    logChange(np,"Form save");saveProfile(np);renderProfile();
  });
  $("#reset-prof").addEventListener("click",()=>{
    if(!confirm("Reset profile to defaults?"))return;
    localStorage.removeItem(PROFILE_KEY);renderProfile();
  });
}

// ================= Render: Alerts =================
function renderAlerts(){
  const el=$("#alerts");
  const p=loadProfile();
  const o=p.alert_opts;
  el.innerHTML=`
    <section class="hero"><div class="hero-left"><h1>Alerts</h1>
      <p class="muted">Opt in per rule. Fired alerts emit both to email (SMTP configured server-side) and to a local log file.</p></div></section>
    <section class="card"><h2>Rules</h2>
      <form id="alert-form">
      <table class="kv">
        <tr><th><label><input type="checkbox" name="price_move_enabled" ${o.price_move_enabled?"checked":""}/> Price move alert</label></th>
          <td>Trigger when |1-day change| ≥ <input type="number" step="0.5" name="price_move_threshold_pct" value="${(o.price_move_threshold_pct*100).toFixed(1)}" style="width:80px;"/> %</td></tr>
        <tr><th><label><input type="checkbox" name="catalyst_enabled" ${o.catalyst_enabled?"checked":""}/> Catalyst approaching</label></th>
          <td>Within <input type="number" name="catalyst_days_ahead" value="${o.catalyst_days_ahead}" style="width:80px;"/> days</td></tr>
        <tr><th><label><input type="checkbox" name="sec_filing_enabled" ${o.sec_filing_enabled?"checked":""}/> New SEC filing</label></th>
          <td>Any new EDGAR filing per tracked CIK</td></tr>
        <tr><th>Email address</th>
          <td><input type="email" name="email" value="${o.email||""}" placeholder="you@example.com" style="width:260px;"/></td></tr>
      </table>
      <div style="margin-top:12px;"><button type="submit">Save</button>
        <button type="button" class="secondary" id="run-alerts" style="margin-left:8px;">Run Now (demo)</button></div>
      </form></section>
    <section class="card"><h2>Recent fires</h2>
      <ul class="fire-log">${DATA.alert_log.map(e=>`<li>
        <div class="small muted">${e.ts.slice(0,19).replace("T"," ")}<br><span class="pill">${e.rule}</span></div>
        <div><strong>${e.payload.subject}</strong><div class="small muted">${e.payload.body}</div></div></li>`).join("")}</ul></section>`;
  $("#alert-form").addEventListener("submit",ev=>{
    ev.preventDefault();
    const f=new FormData(ev.target);
    const np=loadProfile();
    np.alert_opts={
      price_move_enabled:!!f.get("price_move_enabled"),
      price_move_threshold_pct:(+f.get("price_move_threshold_pct"))/100,
      catalyst_enabled:!!f.get("catalyst_enabled"),
      catalyst_days_ahead:+f.get("catalyst_days_ahead"),
      sec_filing_enabled:!!f.get("sec_filing_enabled"),
      email:f.get("email")||"",
    };
    logChange(np,`Alert opts saved (email=${np.alert_opts.email||"none"})`);
    saveProfile(np);renderAlerts();
  });
  $("#run-alerts").addEventListener("click",()=>alert("In the FastAPI build, this pings live prices and evaluates the rules. In the static demo, see the sample log below."));
}

// ================= Render: Methodology =================
function renderMethodology(){
  const el=$("#methodology");
  const tiers=DATA.meta.tier_weights;
  el.innerHTML=`
    <section class="hero"><div class="hero-left"><h1>Methodology</h1>
      <p class="muted">How the Nuclear Renaissance Index is constructed, weighted, and rebalanced.</p></div></section>
    <section class="grid-2">
      <div class="card"><h2>Index Basics</h2>
        <table class="kv">
          <tr><th>Index Name</th><td>${DATA.meta.index_name}</td></tr>
          <tr><th>Ticker</th><td>${DATA.meta.index_code}</td></tr>
          <tr><th>Base Date</th><td>${DATA.meta.base_date}</td></tr>
          <tr><th>Base Value</th><td>${DATA.meta.base_value}</td></tr>
          <tr><th>Rebalance</th><td>${pretty(DATA.meta.rebalance_frequency)}</td></tr>
          <tr><th>Weighting</th><td>Tiered equal-weight within tier</td></tr>
        </table></div>
      <div class="card"><h2>Tier Weights</h2>
        <table>
          <thead><tr><th>Tier</th><th class="num">Weight</th></tr></thead>
          <tbody>${Object.entries(tiers).map(([k,v])=>`<tr><td>${pretty(k)}</td><td class="num">${(v*100).toFixed(0)}%</td></tr>`).join("")}</tbody>
        </table>
        <p class="small muted">Weights are distributed equally across listed names within each tier, then renormalized across tiers.</p></div>
    </section>
    <section class="card"><h2>Custom Metrics</h2>
      <table>
        <thead><tr><th>Metric</th><th>Scale</th><th>Definition</th></tr></thead>
        <tbody>
          <tr><td><strong>Dev Stage</strong></td><td>Categorical</td><td>Operational / Diversified / Pre-Commercial / R&D.</td></tr>
          <tr><td><strong>Regulatory Risk</strong></td><td>0–10</td><td>Higher = more regulatory hurdles ahead (licensing, permitting, novel-tech approvals).</td></tr>
          <tr><td><strong>Capacity Factor</strong></td><td>%</td><td>Reported utilization for operators / fuel cycle; n/a for developers.</td></tr>
          <tr><td><strong>Uranium Exposure</strong></td><td>0–10</td><td>Revenue correlation with uranium spot prices.</td></tr>
          <tr><td><strong>Nuclear Revenue Share</strong></td><td>%</td><td>Share of revenue coming from nuclear vs diversified segments.</td></tr>
        </tbody></table></section>
    <section class="card"><h2>Current Constituent Weights</h2>
      <table>
        <thead><tr><th>Ticker</th><th>Name</th><th>Tier</th><th class="num">Weight</th></tr></thead>
        <tbody>${Object.entries(DATA.index_weights).sort((a,b)=>b[1]-a[1]).map(([t,w])=>{
          const c=DATA.constituents.find(x=>x.ticker===t);
          return `<tr><td><strong>${t}</strong></td><td>${c?c.name:""}</td>
            <td class="muted small">${c?pretty(c.sector):""}</td>
            <td class="num">${(w*100).toFixed(2)}%</td></tr>`;
        }).join("")}</tbody></table></section>`;
}

// ================= Live data hydration (multi-source) =================
// Tries two data sources in order — Yahoo Finance (chart JSON) and Stooq
// (CSV) — and for each source rotates through multiple CORS proxies. The
// first combination that succeeds for a given session is cached in
// localStorage so subsequent refreshes start with the known-good route.
//
// Stooq returns decades of history by default, so Max / 10Y / 5Y windows
// work even when Yahoo's CORS is being flaky.
// Trimmed to proxies that are actually reachable and free in 2026.
// Dead services removed: crossorigin.me, cors-anywhere (demo-locked),
// proxy.cors.sh (now paid), api.cors.sh (API-key-required).
// The *best* route is one the user deploys themselves — a Cloudflare Worker
// or similar — pasted into Settings. That bypasses all public proxies.
const CUSTOM_PROXY_KEY = "nri.customProxy.v1";
const RATE_LIMIT_KEY   = "nri.rateLimit.v1";
function getCustomProxy(){
  try{ return (localStorage.getItem(CUSTOM_PROXY_KEY)||"").trim(); }catch(e){ return ""; }
}
function setCustomProxy(u){
  try{
    if(u) localStorage.setItem(CUSTOM_PROXY_KEY, u.trim());
    else  localStorage.removeItem(CUSTOM_PROXY_KEY);
  }catch(e){}
}
// Templates: user pastes a URL like `https://my-worker.example.workers.dev/?url=`
// or `https://my-worker.example.workers.dev/` — we handle both.
function wrapCustom(base, target){
  if(!base) return target;
  if(base.indexOf("{url}") !== -1) return base.replace("{url}", encodeURIComponent(target));
  if(base.endsWith("?url=") || base.endsWith("&url=") || base.endsWith("?")) return base + encodeURIComponent(target);
  if(base.endsWith("/")) return base + target; // /https://... style
  return base + "?url=" + encodeURIComponent(target);
}
// Rate-limit tracker: if a proxy returned 429 or body mentioned "Too Many Requests",
// skip it for 60s. Survives reload via localStorage.
function loadRateLimits(){
  try{
    const r = JSON.parse(localStorage.getItem(RATE_LIMIT_KEY)||"{}");
    const now = Date.now(); const out = {};
    for(const [k,v] of Object.entries(r)){ if(v > now) out[k] = v; }
    return out;
  }catch(e){ return {}; }
}
function markRateLimited(proxyId, windowMs){
  try{
    const r = loadRateLimits();
    r[proxyId] = Date.now() + (windowMs || 60000);
    localStorage.setItem(RATE_LIMIT_KEY, JSON.stringify(r));
  }catch(e){}
}
function isRateLimited(proxyId){
  const r = loadRateLimits();
  return !!(r[proxyId] && r[proxyId] > Date.now());
}
const PROXIES_BASE = [
  {id:"direct",         wrap: u => u},
  {id:"corsproxy.io",   wrap: u => "https://corsproxy.io/?" + encodeURIComponent(u)},
  {id:"codetabs",       wrap: u => "https://api.codetabs.com/v1/proxy?quest=" + encodeURIComponent(u)},
  {id:"allorigins-raw", wrap: u => "https://api.allorigins.win/raw?url=" + encodeURIComponent(u)},
  {id:"allorigins-get", wrap: u => "https://api.allorigins.win/get?url=" + encodeURIComponent(u)},
  {id:"r.jina.ai",      wrap: u => "https://r.jina.ai/" + u},
  {id:"thingproxy",     wrap: u => "https://thingproxy.freeboard.io/fetch/" + u},
  {id:"cors.lol",       wrap: u => "https://api.cors.lol/?url=" + encodeURIComponent(u)},
];
// Compose final proxy list: custom proxy (if set) first, then built-ins.
function getProxies(){
  const cp = getCustomProxy();
  if(cp){
    return [{id:"custom", wrap: u => wrapCustom(cp, u)}, ...PROXIES_BASE];
  }
  return PROXIES_BASE;
}
// Back-compat alias for existing code paths.
Object.defineProperty(globalThis || window, "PROXIES", { get: () => getProxies() });
const ROUTE_KEY = "nri.route.v2";
let PINNED_PROXY = null;
let ROUTE_RESET_ONCE = false;
function loadRoute(){try{return JSON.parse(localStorage.getItem(ROUTE_KEY))||{};}catch(e){return{};}}
function saveRoute(r){try{localStorage.setItem(ROUTE_KEY,JSON.stringify(r));}catch(e){}}
function clearRoute(){try{localStorage.removeItem(ROUTE_KEY);}catch(e){}}
function sleep(ms){return new Promise(r=>setTimeout(r, ms));}

// Detect rate-limit responses hiding behind HTTP 200 (several proxies return
// plain-text "Too Many Requests" with a 200 status code).
function looksRateLimited(status, text){
  if(status === 429) return true;
  if(!text) return false;
  const head = text.slice(0, 160).toLowerCase();
  if(head.indexOf("too many requests") !== -1) return true;
  if(head.indexOf("rate limit") !== -1) return true;
  if(head.indexOf("rate-limit") !== -1) return true;
  return false;
}

// Attempt a fetch via a specific proxy; returns text body or null. Also
// records rate-limited proxies in localStorage so we skip them for 60s.
async function fetchViaProxy(proxy, url, timeoutMs=8000){
  const ctl = new AbortController();
  const to = setTimeout(()=>ctl.abort(), timeoutMs);
  try{
    const r = await fetch(proxy.wrap(url), {cache:"no-store", signal: ctl.signal, redirect:"follow"});
    clearTimeout(to);
    if(r.status === 429){
      markRateLimited(proxy.id, 60000);
      return null;
    }
    if(!r.ok) return null;
    let txt = await r.text();
    if(!txt || txt.length < 10) return null;
    if(looksRateLimited(r.status, txt)){
      markRateLimited(proxy.id, 60000);
      return null;
    }
    // allorigins GET endpoint wraps the response body in JSON {contents: "..."}.
    if(proxy.id === "allorigins-get"){
      try{ const j = JSON.parse(txt); if(j && typeof j.contents === "string") txt = j.contents; }catch(e){}
    }
    // r.jina.ai sometimes returns a markdown wrapper around the JSON. Strip
    // any prose before the first `{` so the downstream JSON.parse still works.
    if(proxy.id === "r.jina.ai"){
      const first = txt.indexOf("{");
      const last  = txt.lastIndexOf("}");
      if(first >= 0 && last > first) txt = txt.slice(first, last+1);
    }
    return txt;
  }catch(e){ clearTimeout(to); return null; }
}

// Walk the proxy list (preferred first, skipping currently rate-limited ones).
async function fetchText(url, timeoutMs=8000){
  if(PINNED_PROXY){
    const out = await fetchViaProxy(PINNED_PROXY, url, timeoutMs);
    if(out) return out;
  }
  const route = ROUTE_RESET_ONCE ? {} : loadRoute();
  const host = url.split("/")[2] || "default";
  const pref = route[host] || "direct";
  const proxies = getProxies();
  const ordered = [proxies.find(p=>p.id===pref), ...proxies.filter(p=>p.id!==pref)]
                  .filter(Boolean)
                  .filter(p => !isRateLimited(p.id));
  for(const p of ordered){
    const txt = await fetchViaProxy(p, url, timeoutMs);
    if(txt){
      if(p.id !== pref){ route[host] = p.id; saveRoute(route); }
      return txt;
    }
  }
  return null;
}

// Probe: pick a single representative URL, walk proxies, return the first
// one that works. Skips rate-limited proxies.
async function probeProxy(probeUrl, onProgress){
  const route = ROUTE_RESET_ONCE ? {} : loadRoute();
  const host = probeUrl.split("/")[2] || "default";
  const pref = route[host] || "direct";
  const proxies = getProxies();
  const ordered = [proxies.find(p=>p.id===pref), ...proxies.filter(p=>p.id!==pref)]
                  .filter(Boolean)
                  .filter(p => !isRateLimited(p.id));
  for(const p of ordered){
    if(onProgress) onProgress(p.id);
    const txt = await fetchViaProxy(p, probeUrl, 8000);
    if(txt){
      PINNED_PROXY = p;
      route[host] = p.id;
      saveRoute(route);
      return p;
    }
  }
  return null;
}
async function fetchJson(url, timeoutMs=12000){
  const txt = await fetchText(url, timeoutMs);
  if(!txt) return null;
  try{ return JSON.parse(txt); }catch(e){ return null; }
}

// ---- Source 1: Yahoo Finance chart JSON ----
async function fetchYahoo(ticker, range){
  // range: "1y" | "2y" | "5y" | "10y" | "max"
  const rng = range || "max";
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(ticker)}?range=${rng}&interval=1d`;
  const j = await fetchJson(url);
  if(!j || !j.chart || !j.chart.result || !j.chart.result[0]) return null;
  const r = j.chart.result[0];
  const ts = r.timestamp || [];
  const q = (r.indicators && r.indicators.quote && r.indicators.quote[0]) || {};
  const closes = q.close || [];
  const volumes = q.volume || [];
  const series = [];
  for(let i=0;i<ts.length;i++){
    const c = closes[i];
    if(c == null || isNaN(c)) continue;
    const d = new Date(ts[i]*1000).toISOString().slice(0,10);
    series.push({d, p: +c.toFixed(2), v: volumes[i] || null});
  }
  const meta = r.meta || {};
  return {ticker, series, source:"yahoo", meta:{
    marketCap: meta.marketCap||null, regularMarketPrice: meta.regularMarketPrice,
    previousClose: meta.chartPreviousClose||meta.previousClose,
    currency: meta.currency, exchange: meta.exchangeName,
  }};
}

// ---- Source 2: Stooq daily CSV ----
// Stooq covers US ADRs with ".us"; returns ~30 years of daily history.
const STOOQ_ALIAS = {
  // Map our tickers to stooq codes. US-listed default .us; override below.
  "GFUZ": null,        // pre-listing — skip
  "RYCEY": "rycey.us", // Rolls-Royce ADR
  "IMSR": null,        // may not be in Stooq's US set reliably
};
function stooqCode(ticker){
  if(STOOQ_ALIAS.hasOwnProperty(ticker)) return STOOQ_ALIAS[ticker];
  return ticker.toLowerCase() + ".us";
}
async function fetchStooq(ticker){
  const code = stooqCode(ticker);
  if(!code) return null;
  const url = `https://stooq.com/q/d/l/?s=${code}&i=d`;
  const txt = await fetchText(url);
  if(!txt || !txt.includes("Date,")) return null;
  const lines = txt.trim().split(/\r?\n/);
  lines.shift(); // header
  const series = [];
  for(const ln of lines){
    const [d,o,h,l,c,v] = ln.split(",");
    const cp = parseFloat(c);
    if(!d || isNaN(cp)) continue;
    series.push({d, p: +cp.toFixed(2), v: v ? +v : null});
  }
  if(series.length<20) return null;
  return {ticker, series, source:"stooq", meta:{}};
}

async function fetchOneTicker(ticker, range){
  const out = await fetchYahoo(ticker, range);
  if(out && out.series.length>=20) return out;
  return await fetchStooq(ticker);
}

// ================= Technical indicators =================
// All take a [{d,p,v?}] series and return a parallel [{d,p}] series (null
// prefixes until the indicator warms up).
function sma(ser, period){
  const out=[]; for(let i=0;i<ser.length;i++){
    if(i<period-1){out.push({d:ser[i].d,p:null});continue;}
    let s=0; for(let j=i-period+1;j<=i;j++) s+=ser[j].p;
    out.push({d:ser[i].d,p:s/period});
  } return out;
}
function ema(ser, period){
  const k=2/(period+1); const out=[]; let prev=null;
  for(let i=0;i<ser.length;i++){
    if(prev==null){
      if(i>=period-1){
        let s=0; for(let j=i-period+1;j<=i;j++) s+=ser[j].p;
        prev=s/period; out.push({d:ser[i].d,p:prev});
      } else out.push({d:ser[i].d,p:null});
    } else { prev = ser[i].p*k + prev*(1-k); out.push({d:ser[i].d,p:prev}); }
  } return out;
}
function rsi(ser, period){
  period = period || 14;
  if(ser.length<=period) return ser.map(p=>({d:p.d,p:null}));
  const out=[]; out.push({d:ser[0].d,p:null});
  let gains=0, losses=0;
  for(let i=1;i<=period;i++){
    const ch=ser[i].p-ser[i-1].p;
    if(ch>0) gains+=ch; else losses-=ch;
    out.push({d:ser[i].d,p:null});
  }
  let avgG=gains/period, avgL=losses/period;
  let rs = avgL?avgG/avgL:0;
  out[period].p = 100 - 100/(1+rs);
  for(let i=period+1;i<ser.length;i++){
    const ch=ser[i].p-ser[i-1].p;
    const g=Math.max(0,ch), l=Math.max(0,-ch);
    avgG = (avgG*(period-1)+g)/period;
    avgL = (avgL*(period-1)+l)/period;
    rs = avgL?avgG/avgL:0;
    out.push({d:ser[i].d, p: 100-100/(1+rs)});
  }
  return out;
}
function bollinger(ser, period, mult){
  period = period||20; mult = mult||2;
  const upper=[], middle=[], lower=[];
  for(let i=0;i<ser.length;i++){
    if(i<period-1){
      upper.push({d:ser[i].d,p:null});
      middle.push({d:ser[i].d,p:null});
      lower.push({d:ser[i].d,p:null});
      continue;
    }
    let s=0; for(let j=i-period+1;j<=i;j++) s+=ser[j].p;
    const m=s/period;
    let v=0; for(let j=i-period+1;j<=i;j++) v+=(ser[j].p-m)*(ser[j].p-m);
    const sd=Math.sqrt(v/period);
    upper.push({d:ser[i].d,p:m+mult*sd});
    middle.push({d:ser[i].d,p:m});
    lower.push({d:ser[i].d,p:m-mult*sd});
  }
  return {upper, middle, lower};
}
function macd(ser, fast, slow, signal){
  fast=fast||12; slow=slow||26; signal=signal||9;
  const eF = ema(ser, fast);
  const eS = ema(ser, slow);
  const line = ser.map((p,i)=>(eF[i].p==null||eS[i].p==null)?{d:p.d,p:null}:{d:p.d,p:eF[i].p-eS[i].p});
  // EMA of macd line (skip leading nulls)
  const firstIdx = line.findIndex(x=>x.p!=null);
  const sigLine = line.map(x=>({d:x.d,p:null}));
  if(firstIdx>=0){
    const valid = line.slice(firstIdx);
    const sig = ema(valid, signal);
    for(let i=0;i<sig.length;i++) sigLine[firstIdx+i].p = sig[i].p;
  }
  const hist = line.map((m,i)=>(m.p==null||sigLine[i].p==null)?{d:m.d,p:null}:{d:m.d,p:m.p-sigLine[i].p});
  return {line, signal: sigLine, histogram: hist};
}
function vwap(ser){
  // Anchored VWAP from start of visible window. Needs volume.
  let cumPV=0, cumV=0;
  return ser.map(p=>{
    if(p.v==null || !p.v) return {d:p.d,p:null};
    cumPV += p.p*p.v; cumV += p.v;
    return {d:p.d, p: cumPV/cumV};
  });
}

// ================= Range slicing =================
// Global selected range for both dashboard index chart and company charts.
// "1m"|"6m"|"1y"|"2y"|"5y"|"10y"|"max"
window._range = window._range || "2y";
function approxDaysForRange(r){
  switch(r){
    case "1m": return 21;
    case "6m": return 126;
    case "1y": return 252;
    case "2y": return 504;
    case "5y": return 1260;
    case "10y": return 2520;
    default:   return 100000; // max
  }
}
function sliceRange(ser, r){
  if(!ser || !ser.length) return ser||[];
  const n = approxDaysForRange(r);
  if(ser.length<=n) return ser;
  return ser.slice(ser.length-n);
}

function logReturns(ser){
  if(ser.length<2) return [];
  const out=[]; for(let i=1;i<ser.length;i++) out.push(Math.log(ser[i].p/ser[i-1].p));
  return out;
}
function stdev(arr){
  if(!arr.length) return 0;
  const m = arr.reduce((s,x)=>s+x,0)/arr.length;
  const v = arr.reduce((s,x)=>s+(x-m)*(x-m),0)/arr.length;
  return Math.sqrt(v);
}
function perfMetrics(ser){
  if(ser.length<20) return {price:null,change_1d:null,ret_1m:null,ret_ytd:null,ret_1y:null,
                             vol_ann:null,sharpe:null,last_date:null,cagr:null,max_dd:null,total_return:null};
  const price = ser[ser.length-1].p;
  const last_date = ser[ser.length-1].d;
  const change_1d = (ser[ser.length-1].p/ser[ser.length-2].p - 1)*100;
  const pct = (n)=>{const i=Math.max(0,ser.length-1-n);return (ser[ser.length-1].p/ser[i].p - 1)*100;};
  const ret_1m = pct(21);
  const ret_1y = ser.length>=252 ? pct(252) : null;
  let ytdIdx = 0;
  const yr = String(new Date().getUTCFullYear());
  for(let i=0;i<ser.length;i++){ if(ser[i].d.startsWith(yr)){ytdIdx=i;break;} }
  const ret_ytd = (ser[ser.length-1].p/ser[ytdIdx].p - 1)*100;
  const rets = logReturns(ser);
  const mean = rets.reduce((s,x)=>s+x,0)/(rets.length||1);
  const vol = stdev(rets);
  const vol_ann = vol*Math.sqrt(252)*100;
  const mean_ann = mean*252*100;
  const rf = 4.0;
  const sharpe = vol_ann ? (mean_ann - rf)/vol_ann : 0;
  const total_return = (ser[ser.length-1].p/ser[0].p - 1)*100;
  const years = ser.length/252;
  const cagr = years>0 ? (Math.pow(ser[ser.length-1].p/ser[0].p, 1/years)-1)*100 : 0;
  let peak=ser[0].p, mdd=0;
  for(const pt of ser){ if(pt.p>peak) peak=pt.p; const dd=(pt.p/peak-1)*100; if(dd<mdd) mdd=dd; }
  return {price:+price.toFixed(2),change_1d:+change_1d.toFixed(2),
          ret_1m:+ret_1m.toFixed(2),ret_ytd:+ret_ytd.toFixed(2),
          ret_1y: ret_1y==null?null:+ret_1y.toFixed(2),
          vol_ann:+vol_ann.toFixed(2), sharpe:+sharpe.toFixed(3),
          last_date, cagr:+cagr.toFixed(2), max_dd:+mdd.toFixed(2),
          total_return:+total_return.toFixed(2)};
}

function rebuildIndex(){
  const listed = DATA.constituents.filter(c=>c.series && c.series.length);
  const bySector = {};
  listed.forEach(c=>{ (bySector[c.sector]=bySector[c.sector]||[]).push(c); });
  const tw = DATA.meta.tier_weights;
  const w = {};
  for(const [sec, mem] of Object.entries(bySector)){
    const tier = tw[sec]||0;
    if(!mem.length || !tier) continue;
    const per = tier/mem.length;
    mem.forEach(m=>{w[m.ticker]=per;});
  }
  const tot = Object.values(w).reduce((s,v)=>s+v,0);
  if(tot) for(const k of Object.keys(w)) w[k]/=tot;
  // Union of dates so history isn't truncated to the newest listing.
  const dateSet = new Set();
  const byT = {};
  listed.forEach(c=>{
    byT[c.ticker] = Object.fromEntries(c.series.map(p=>[p.d,p.p]));
    c.series.forEach(p=>dateSet.add(p.d));
  });
  const dates = Array.from(dateSet).sort();
  if(!dates.length){ DATA.index_series = []; DATA.index_weights = w; DATA.index_stats = perfMetrics([]); return; }
  const prev = {};
  for(const t of Object.keys(byT)){ if(byT[t][dates[0]]!=null) prev[t]=byT[t][dates[0]]; }
  let idx = 100;
  const values = [{d: dates[0], p: 100}];
  for(let i=1;i<dates.length;i++){
    const d = dates[i];
    const active = Object.keys(byT).filter(t=>byT[t][d]!=null && prev[t]!=null);
    if(active.length){
      let wTotal=0; for(const t of active) wTotal += (w[t]||0);
      if(wTotal>0){
        let r=0;
        for(const t of active){
          const p1=byT[t][d], p0=prev[t];
          r += ((w[t]||0)/wTotal) * (p1/p0 - 1);
        }
        idx *= (1+r);
      }
    }
    values.push({d, p:+idx.toFixed(2)});
    for(const t of Object.keys(byT)){ if(byT[t][d]!=null) prev[t]=byT[t][d]; }
  }
  DATA.index_weights = w;
  DATA.index_series = values;
  DATA.index_stats = perfMetrics(values);
}

function cappedSoftmax(scores, cap){
  const listed = Object.entries(scores).filter(([_,s])=>s!=null);
  if(!listed.length) return {};
  const lo = Math.min(...listed.map(([_,s])=>s));
  const shifted = Object.fromEntries(listed.map(([t,s])=>[t,(s-lo)+1e-6]));
  const sum = Object.values(shifted).reduce((a,b)=>a+b,0);
  let w = Object.fromEntries(Object.entries(shifted).map(([t,v])=>[t,v/sum]));
  for(let iter=0;iter<50;iter++){
    const over = Object.entries(w).filter(([_,v])=>v>cap);
    if(!over.length) break;
    const excess = over.reduce((s,[_,v])=>s+(v-cap),0);
    const free = Object.entries(w).filter(([_,v])=>v<=cap);
    if(!free.length) break;
    const ft = free.reduce((s,[_,v])=>s+v,0);
    over.forEach(([t])=>{w[t]=cap;});
    free.forEach(([t,v])=>{w[t]=v+excess*v/ft;});
  }
  const s = Object.values(w).reduce((a,b)=>a+b,0);
  return Object.fromEntries(Object.entries(w).map(([t,v])=>[t,+(v/s).toFixed(4)]));
}
function buildOptResult(objective, maxW){
  const listed = DATA.constituents.filter(c=>c.series && c.series.length && c.sharpe!=null);
  let scores;
  if(objective==="sharpe") scores = Object.fromEntries(listed.map(c=>[c.ticker,c.sharpe]));
  else if(objective==="min_vol") scores = Object.fromEntries(listed.map(c=>[c.ticker,-(c.vol_ann||100)]));
  else if(objective==="inverse_vol") scores = Object.fromEntries(listed.map(c=>[c.ticker,1/(c.vol_ann||100)]));
  else scores = Object.fromEntries(listed.map(c=>[c.ticker,1]));
  const w = cappedSoftmax(scores,maxW);
  const ann_return = listed.reduce((s,c)=>s+((w[c.ticker]||0)*((c.ret_1y||c.cagr||0))),0);
  const ann_vol = Math.sqrt(listed.reduce((s,c)=>s+Math.pow((w[c.ticker]||0)*(c.vol_ann||0),2),0));
  const sharpe = ann_vol ? (ann_return-4)/ann_vol : 0;
  const breakdown = listed
    .filter(c=>(w[c.ticker]||0)>0)
    .map(c=>({ticker:c.ticker,name:c.name,weight:w[c.ticker]||0,
              dollars:Math.round((w[c.ticker]||0)*100000),
              exp_return:+(((c.ret_1y||c.cagr||0)/100).toFixed(4)),
              volatility:+(((c.vol_ann||0)/100).toFixed(4))}))
    .sort((a,b)=>b.weight-a.weight);
  return {objective,max_weight:maxW,
          ann_return:+(ann_return/100).toFixed(4),ann_vol:+(ann_vol/100).toFixed(4),
          sharpe:+sharpe.toFixed(3),risk_free:0.04,weights:w,breakdown,error:null};
}
function rebuildOptimizer(){
  const out={};
  for(const obj of ["sharpe","min_vol","inverse_vol","equal"]){
    for(const mw of [0.15,0.20,0.25]){
      out[`${obj}__${Math.round(mw*100)}`] = buildOptResult(obj,mw);
    }
  }
  DATA.optimizer = out;
  // frontier = mix of sharpe-20 and minvol-20
  const sh = out["sharpe__20"].weights, mv = out["min_vol__20"].weights;
  const listed = DATA.constituents.filter(c=>c.series && c.series.length);
  const pts = [];
  for(let k=0;k<=20;k++){
    const a = k/20;
    const mix = {};
    for(const c of listed) mix[c.ticker] = a*(sh[c.ticker]||0) + (1-a)*(mv[c.ticker]||0);
    const s = Object.values(mix).reduce((x,y)=>x+y,0);
    if(!s) continue;
    for(const k of Object.keys(mix)) mix[k]/=s;
    const ret = listed.reduce((x,c)=>x+mix[c.ticker]*((c.ret_1y||c.cagr||0)),0)/100;
    const vol = Math.sqrt(listed.reduce((x,c)=>x+Math.pow(mix[c.ticker]*(c.vol_ann||0),2),0))/100;
    pts.push({vol:+vol.toFixed(4),return:+ret.toFixed(4),alpha:a});
  }
  DATA.frontier = pts;
}

function setLiveStatus(state, msg, meta){
  const dot = document.getElementById("live-dot");
  const st = document.getElementById("live-status");
  const mt = document.getElementById("live-meta");
  if(!dot) return;
  dot.classList.remove("ok","warn","err");
  if(state) dot.classList.add(state);
  if(st) st.textContent = msg;
  if(mt) mt.textContent = meta || "";
}

// Persist the last-success timestamp so "Last updated" survives reloads and
// the user can see if they're looking at stale (or plausibly-stale) data.
const UPDATED_KEY = "nri.lastUpdated.v1";
function loadLastUpdated(){
  try{ const v = localStorage.getItem(UPDATED_KEY); return v ? +v : null; }catch(e){ return null; }
}
function saveLastUpdated(ts){
  try{ localStorage.setItem(UPDATED_KEY, String(ts)); }catch(e){}
}
function fmtRelative(ts){
  if(!ts) return "never";
  const diff = Math.max(0, Date.now() - ts);
  const s = Math.round(diff/1000);
  if(s < 10) return "just now";
  if(s < 60) return s + "s ago";
  const m = Math.round(s/60);
  if(m < 60) return m + "m ago";
  const h = Math.round(m/60);
  if(h < 24) return h + "h ago";
  const d = Math.round(h/24);
  return d + "d ago";
}
function fmtAbsolute(ts){
  if(!ts) return "—";
  const dt = new Date(ts);
  const date = dt.toLocaleDateString([], {month:"short", day:"numeric"});
  const time = dt.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"});
  return date + " " + time;
}
function renderLastUpdated(){
  const el = document.getElementById("live-updated");
  if(!el) return;
  const ts = loadLastUpdated();
  el.classList.remove("stale","fresh");
  if(!ts){
    el.textContent = "Last updated: never (offline)";
    el.classList.add("stale");
    el.title = "No successful fetch yet this session. Showing synthetic baseline anchored to last-known prices.";
    return;
  }
  const rel = fmtRelative(ts);
  const abs = fmtAbsolute(ts);
  el.textContent = "Last updated: " + rel + " (" + abs + ")";
  el.title = "Full timestamp: " + new Date(ts).toString();
  const ageMs = Date.now() - ts;
  if(ageMs < 10*60*1000) el.classList.add("fresh");      // fresh < 10 min
  else if(ageMs > 30*60*1000) el.classList.add("stale"); // stale > 30 min
}
// Tick the relative display every 20s so "2m ago" becomes "3m ago" without a refetch.
setInterval(renderLastUpdated, 20*1000);

// Render the "Baked: <when> · <real>/<total> real prices" badge. This is the
// key signal for the no-signup workflow: if a user double-clicks refresh.command
// the baked_at timestamp jumps forward and they see fresh prices immediately.
function renderBakedInfo(){
  const el = document.getElementById("baked-info");
  if(!el) return;
  const meta = (DATA && DATA.meta) || {};
  const baked = meta.baked_at;
  const sc = meta.source_counts || {};
  const real = (sc.stooq||0) + (sc.yahoo||0);
  const synth = sc.synthetic||0;
  const unlisted = sc.unlisted||0;
  const total = real + synth + unlisted;
  el.classList.remove("stale","fresh");
  if(!baked){
    el.textContent = "Baked: unknown";
    return;
  }
  // baked_at is ISO8601 UTC, e.g. 2026-04-20T03:12:15+00:00
  const bakedTs = Date.parse(baked);
  if(!isFinite(bakedTs)){
    el.textContent = "Baked: " + baked;
    return;
  }
  const ageMs = Date.now() - bakedTs;
  const ageH = ageMs / 3600e3;
  const rel = fmtRelative(bakedTs);
  const abs = fmtAbsolute(bakedTs);
  const srcFrag = real + "/" + total + " real";
  el.textContent = "Baked: " + rel + " · " + srcFrag;
  el.title = "HTML built: " + new Date(bakedTs).toString() +
             "\nReal prices: stooq=" + (sc.stooq||0) + ", yahoo=" + (sc.yahoo||0) +
             "\nSynthetic baseline: " + synth +
             (unlisted ? ("\nUnlisted (pre-IPO): " + unlisted) : "") +
             "\n\nTo refresh: double-click refresh.command (macOS/Linux) or refresh.bat (Windows) in this folder.";
  if(ageH < 24) el.classList.add("fresh");
  else if(ageH > 24*7) el.classList.add("stale");
}
setInterval(renderBakedInfo, 60*1000);

let LIVE_TIMER = null;
let LIVE_INFLIGHT = false;
let LIVE_FAILURES = 0;
window._liveOk = false;
async function hydrateLive(manual){
  if(LIVE_INFLIGHT) return;
  LIVE_INFLIGHT = true;
  const btn = document.getElementById("live-refresh");
  if(btn){ btn.disabled = true; btn.textContent = "Fetching…"; }
  // Manual refresh: clear the cached proxy route so we probe all proxies fresh.
  if(manual){
    ROUTE_RESET_ONCE = true;
    PINNED_PROXY = null;
    clearRoute();
  }
  setLiveStatus("warn","Probing CORS proxies…","");

  const tickers = DATA.constituents.filter(c=>c.listed).map(c=>c.ticker);

  // Probe: one cheap request (first ticker, 1mo range) to find a working proxy.
  // This avoids firing 16 parallel requests per proxy and getting rate-limited.
  const probeTicker = tickers[0] || "AAPL";
  const probeUrl = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(probeTicker)}?range=1mo&interval=1d`;
  const probe = await probeProxy(probeUrl, (pid)=>{
    setLiveStatus("warn", `Trying ${pid}…`, "");
  });

  let results;
  if(probe){
    setLiveStatus("warn", `Fetching via ${probe.id} (0/${tickers.length})…`, "");
    // Serial with a small gap — most free proxies tolerate ~6 req/s.
    results = [];
    for(let i=0; i<tickers.length; i++){
      const t = tickers[i];
      setLiveStatus("warn", `Fetching via ${probe.id} (${i+1}/${tickers.length}) · ${t}…`, "");
      try{ results.push(await fetchOneTicker(t, "max")); }
      catch(e){ results.push(null); }
      await sleep(180);
    }
  } else {
    // Probe failed — nothing will work for per-ticker fetches either.
    results = tickers.map(()=>null);
  }
  ROUTE_RESET_ONCE = false;
  PINNED_PROXY = null;
  let ok=0, fail=0;
  const failed=[]; const sourceCount={yahoo:0,stooq:0};
  const now = new Date();
  results.forEach((r,i)=>{
    const t = tickers[i];
    const c = DATA.constituents.find(x=>x.ticker===t);
    if(r && r.series && r.series.length>=20){
      c.series = r.series;
      Object.assign(c, perfMetrics(r.series));
      if(r.meta && r.meta.marketCap) c.market_cap = r.meta.marketCap;
      if(r.source) sourceCount[r.source] = (sourceCount[r.source]||0)+1;
      ok++;
    }else{
      fail++; failed.push(t);
    }
  });
  if(ok>0){
    window._liveOk = true;
    LIVE_FAILURES = 0;
    saveLastUpdated(now.getTime());
    rebuildIndex();
    rebuildOptimizer();
    try{ renderDashboard(); renderCompanies(); renderOptimizer();
         renderBacktest(); renderMethodology(); }catch(e){console.warn(e);}
    showTab(location.hash.slice(1)||"dashboard");
  } else {
    LIVE_FAILURES++;
  }
  const tsStr = now.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"});
  const srcStr = Object.entries(sourceCount).filter(([,v])=>v>0).map(([k,v])=>`${k} ${v}`).join(" · ");
  if(ok===tickers.length){
    setLiveStatus("ok",`Live · ${ok} of ${tickers.length} tickers`,
                  `${srcStr} · ${tsStr}`);
  }else if(ok>0){
    setLiveStatus("warn",`Partial live · ${ok}/${tickers.length} (failed: ${failed.slice(0,4).join(", ")}${failed.length>4?"…":""})`,
                  `${srcStr} · ${tsStr}`);
  }else{
    // Browser-side live fetch failed, but the page still shows the prices that
    // were baked in when build_static_site.py ran. Make the messaging reflect
    // that — this is the "no-signup" fallback path and it's fine.
    const meta = (DATA && DATA.meta) || {};
    const baked = meta.baked_at ? Date.parse(meta.baked_at) : null;
    const bakedRel = baked ? fmtRelative(baked) : "at build time";
    setLiveStatus("warn",
      `Live quotes unavailable (CORS proxies blocked). Showing baked prices from ${bakedRel}. ` +
      `Double-click refresh.command to get today's prices.`,
      `attempted ${tsStr}`);
  }
  renderLastUpdated();
  if(btn){ btn.disabled = false; btn.textContent = "↻ Refresh"; }
  LIVE_INFLIGHT = false;
  // Schedule next auto-refresh.
  // Success: 5 min. Failure: exponential backoff 30s → 60s → 120s → 300s (capped).
  const auto = document.getElementById("live-auto");
  if(LIVE_TIMER){clearTimeout(LIVE_TIMER);LIVE_TIMER=null;}
  if(auto && auto.checked){
    const base = (ok>0) ? 5*60*1000 : Math.min(5*60*1000, 30*1000 * Math.pow(2, LIVE_FAILURES-1));
    LIVE_TIMER = setTimeout(()=>hydrateLive(false), base);
  }
}

// Diagnostic: probe every proxy with a single cheap request and report
// pass/fail + latency to a visible panel. Helps debug "all proxies blocked".
async function diagnoseProxies(){
  const panel = document.getElementById("live-diag-panel");
  if(!panel) return;
  panel.hidden = false;
  const url = "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=5d&interval=1d";
  panel.innerHTML = '<div class="row"><strong>Proxy diagnostic:</strong> ' +
    '<span class="detail">target: query1.finance.yahoo.com (AAPL, 5d)</span></div>';
  const rows = {};
  for(const p of PROXIES){
    const rid = "diag-"+p.id.replace(/[^a-z0-9]/gi,"-");
    const div = document.createElement("div");
    div.className = "row";
    div.innerHTML = '<span class="proxy">' + p.id + '</span>' +
                    '<span class="pending" id="'+rid+'-state">probing…</span>' +
                    '<span class="detail" id="'+rid+'-detail"></span>';
    panel.appendChild(div);
    rows[p.id] = {state:div.querySelector("#"+rid+"-state"),
                  detail:div.querySelector("#"+rid+"-detail")};
  }
  // Sequential so we don't self-DDoS.
  for(const p of PROXIES){
    const t0 = performance.now();
    const txt = await fetchViaProxy(p, url, 9000);
    const ms = Math.round(performance.now() - t0);
    const r = rows[p.id];
    if(txt && txt.indexOf('"chart"') !== -1){
      r.state.textContent = "OK";
      r.state.className = "ok";
      r.detail.textContent = ms + "ms · " + txt.length + " bytes";
    } else if(txt){
      r.state.textContent = "reachable but wrong body";
      r.state.className = "fail";
      r.detail.textContent = ms + "ms · " + txt.length + " bytes · first: " + txt.slice(0,80).replace(/\s+/g," ");
    } else {
      r.state.textContent = "FAIL";
      r.state.className = "fail";
      r.detail.textContent = ms + "ms · blocked / timeout / non-2xx";
    }
    await sleep(120);
  }
  const tip = document.createElement("div");
  tip.className = "row";
  tip.innerHTML = '<span class="detail">Tip: if everything failed, try loading this page in a different browser, ' +
                  'or disable ad-blockers / privacy extensions (uBlock, AdGuard, Brave Shields) — ' +
                  'they frequently block known CORS-proxy hostnames.</span>';
  panel.appendChild(tip);
}

// ================= Init =================
function init(){
  renderDashboard();renderCompanies();renderOptimizer();renderBacktest();
  renderCatalysts();renderNews();renderPreIPO();renderProfile();
  renderAlerts();renderMethodology();
  showTab(location.hash.slice(1)||"dashboard");
  // Show whatever we know about the last successful fetch immediately.
  renderLastUpdated();
  renderBakedInfo();
  // wire live refresh button + auto-refresh + initial hydrate
  const btn = document.getElementById("live-refresh");
  if(btn){
    btn.addEventListener("click", function(e){
      e.preventDefault();
      if(LIVE_INFLIGHT) return;
      hydrateLive(true);
    });
  }
  const diagBtn = document.getElementById("live-diagnose");
  if(diagBtn){
    diagBtn.addEventListener("click", function(e){
      e.preventDefault();
      diagBtn.disabled = true;
      diagBtn.textContent = "Testing…";
      diagnoseProxies().finally(()=>{
        diagBtn.disabled = false;
        diagBtn.textContent = "Test proxies";
      });
    });
  }
  // Settings panel for user-provided custom proxy URL
  const setBtn = document.getElementById("live-settings");
  const setPanel = document.getElementById("live-settings-panel");
  const setInput = document.getElementById("live-settings-input");
  const setSave  = document.getElementById("live-settings-save");
  const setClear = document.getElementById("live-settings-clear");
  const setHelp  = document.getElementById("live-settings-help");
  const setHelpBody = document.getElementById("live-settings-help-body");
  const setMsg   = document.getElementById("live-settings-msg");
  if(setBtn && setPanel && setInput){
    setInput.value = getCustomProxy();
    setBtn.addEventListener("click", function(e){
      e.preventDefault();
      setPanel.hidden = !setPanel.hidden;
    });
    if(setSave) setSave.addEventListener("click", async function(e){
      e.preventDefault();
      const v = (setInput.value || "").trim();
      setCustomProxy(v);
      PINNED_PROXY = null;
      clearRoute();
      setMsg.textContent = v ? "Saved. Testing…" : "Cleared. Falling back to public proxy chain.";
      if(v){
        // Immediate validation using the custom proxy
        const p = { id:"custom", wrap: u => wrapCustom(v, u) };
        const probeUrl = "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=5d&interval=1d";
        const t0 = performance.now();
        const txt = await fetchViaProxy(p, probeUrl, 10000);
        const ms = Math.round(performance.now() - t0);
        if(txt && txt.indexOf('"chart"') !== -1){
          setMsg.innerHTML = '<span class="ok">✓ Working — ' + ms + 'ms</span>. Refreshing…';
          hydrateLive(true);
        } else {
          setMsg.innerHTML = '<span class="fail">✗ No valid response</span> (' + ms + 'ms). Check the URL format — it should end in <code>?url=</code> or <code>/</code>. The worker must forward the URL and add CORS headers.';
        }
      } else {
        hydrateLive(true);
      }
    });
    if(setClear) setClear.addEventListener("click", function(e){
      e.preventDefault();
      setCustomProxy("");
      setInput.value = "";
      setMsg.textContent = "Cleared. Using public proxy chain.";
      PINNED_PROXY = null; clearRoute();
      hydrateLive(true);
    });
    if(setHelp) setHelp.addEventListener("click", function(e){
      e.preventDefault();
      setHelpBody.hidden = !setHelpBody.hidden;
    });
  }
  const auto = document.getElementById("live-auto");
  if(auto) auto.addEventListener("change",()=>{
    if(LIVE_TIMER){clearTimeout(LIVE_TIMER);LIVE_TIMER=null;}
    if(auto.checked) LIVE_TIMER = setTimeout(()=>hydrateLive(false), 5*60*1000);
  });
  // Kick off the first live fetch after a tick so the UI paints first.
  setTimeout(()=>hydrateLive(false), 80);
  // Tap-to-toggle info tooltips (mobile). Also closes the open one on outside tap.
  document.addEventListener("click", function(e){
    var ic = e.target.closest ? e.target.closest(".info-icon") : null;
    document.querySelectorAll(".info-icon.open").forEach(function(x){
      if(x !== ic) x.classList.remove("open");
    });
    if(ic){
      e.preventDefault();
      e.stopPropagation();
      ic.classList.toggle("open");
    }
  });
}
init();
})();
"""


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------
# Try to inline Chart.js so the HTML works 100% offline — critical for iOS
# users who AirDrop the file to their phone and open it from Files app with
# no network. Falls back to the CDN <script src=…> tag if every mirror fails
# AND no cache exists (the user must then be online the first time they load
# the page). Successful fetches are cached on disk for future builds.
CHARTJS_SRC = get_chartjs_source()
if CHARTJS_SRC:
    # Inline. Guard against `</script>` sneaking in (Chart.js doesn't use them
    # but be safe).
    safe_src = CHARTJS_SRC.replace("</script>", "<\\/script>")
    CHARTJS_TAG = f"<script>{safe_src}</script>"
    print(f"Inlined Chart.js {CHARTJS_VERSION} · {len(CHARTJS_SRC):,} bytes "
          f"(cache: {CHARTJS_CACHE.name}) — HTML is fully self-contained.")
else:
    CHARTJS_TAG = (f'<script src="https://cdn.jsdelivr.net/npm/chart.js@{CHARTJS_VERSION}'
                   f'/dist/chart.umd.min.js"></script>')
    print("WARNING: Chart.js could not be fetched or cached. Falling back to CDN "
          "script tag. The HTML will need network to render charts. Re-run this "
          "script with network access once to populate the cache.")

# A simple atom-themed app icon. URL-encoded SVG → works as apple-touch-icon
# on iOS 15+. Quotes and # are URL-encoded; everything else passes through.
ICON_SVG_RAW = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 180 180">'
    '<rect width="180" height="180" rx="38" fill="#0f1115"/>'
    '<g transform="translate(90 90)" stroke="#5dc88b" stroke-width="5" '
    'fill="none" stroke-linecap="round">'
    '<ellipse cx="0" cy="0" rx="58" ry="22"/>'
    '<ellipse cx="0" cy="0" rx="58" ry="22" transform="rotate(60)"/>'
    '<ellipse cx="0" cy="0" rx="58" ry="22" transform="rotate(-60)"/>'
    '<circle cx="0" cy="0" r="9" fill="#5dc88b" stroke="none"/>'
    '</g>'
    '<text x="90" y="158" font-family="ui-monospace,Menlo,monospace" '
    'font-size="22" font-weight="700" fill="#eaeaea" text-anchor="middle">NRI</text>'
    '</svg>')
# Minimal URL-encoding for an SVG embedded in an href attribute.
ICON_SVG = (ICON_SVG_RAW
            .replace("%", "%25")
            .replace("#", "%23")
            .replace('"', "%22")
            .replace("<", "%3C")
            .replace(">", "%3E"))

html = (HTML_TEMPLATE
        .replace("__CSS__", CSS)
        .replace("__JS__", JS)
        .replace("__GENERATED__", PAYLOAD["meta"]["generated"])
        .replace("__CHARTJS_TAG__", CHARTJS_TAG)
        .replace("__ICON_SVG__", ICON_SVG)
        .replace("__PAYLOAD__", json.dumps(PAYLOAD).replace("</", "<\\/")))
OUT = ROOT / "nri.html"
OUT.write_text(html, encoding="utf-8")
print(f"Wrote {OUT} — {len(html):,} bytes, {len(PAYLOAD['constituents'])} constituents,",
      f"{len(PAYLOAD['index_series'])} daily points,",
      f"{len(PAYLOAD['optimizer'])} optimizer results.")

# ---------------------------------------------------------------------------
# Also emit the Cloudflare Worker source. Users paste the workers.dev URL
# of this worker into the site's Data-source settings to get a private,
# unlimited, CORS-enabled proxy instead of depending on flaky public ones.
# ---------------------------------------------------------------------------
CLOUDFLARE_WORKER = """// Cloudflare Worker — private CORS proxy for the Nuclear Renaissance Index site.
//
// Deploy in ~60 seconds:
//   1. Go to https://workers.cloudflare.com/ and sign in (free tier works fine).
//   2. Create -> Worker -> give it a name (anything, e.g. "nri-proxy").
//   3. Click "Edit code", delete the placeholder, paste THIS ENTIRE FILE.
//   4. Click "Save and Deploy".
//   5. Copy the *.workers.dev URL Cloudflare gives you.
//   6. In the NRI site, click "Data source", paste:
//         https://<your-worker>.workers.dev/?url=
//      ...and click Save. That's it — no more public-proxy rate limits.
//
// Security note: by default this worker accepts any URL. If you want to lock
// it down, set ALLOW_HOSTS below to a whitelist of hostnames.

const ALLOW_HOSTS = null;  // e.g. ["query1.finance.yahoo.com", "stooq.com"]

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const target = url.searchParams.get("url");
    if (!target) {
      return new Response("Usage: ?url=<encoded target URL>", {
        status: 400,
        headers: corsHeaders(),
      });
    }
    let parsed;
    try { parsed = new URL(target); }
    catch (e) { return new Response("Invalid url", { status: 400, headers: corsHeaders() }); }
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
      return new Response("Only http/https allowed", { status: 400, headers: corsHeaders() });
    }
    if (ALLOW_HOSTS && !ALLOW_HOSTS.includes(parsed.hostname)) {
      return new Response("Host not allowed", { status: 403, headers: corsHeaders() });
    }
    try {
      const upstream = await fetch(parsed.toString(), {
        method: request.method,
        headers: {
          "User-Agent": "Mozilla/5.0 (compatible; NRI-Proxy/1.0)",
          "Accept": "*/*",
        },
        redirect: "follow",
      });
      const body = await upstream.arrayBuffer();
      const h = corsHeaders();
      const ct = upstream.headers.get("Content-Type");
      if (ct) h["Content-Type"] = ct;
      return new Response(body, { status: upstream.status, headers: h });
    } catch (e) {
      return new Response("Upstream error: " + e.message, { status: 502, headers: corsHeaders() });
    }
  },
};

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Cache-Control": "no-store",
  };
}
"""
WORKER_OUT = ROOT / "cloudflare-worker.js"
WORKER_OUT.write_text(CLOUDFLARE_WORKER, encoding="utf-8")
print(f"Wrote {WORKER_OUT} — {len(CLOUDFLARE_WORKER):,} bytes (Cloudflare Worker source).")
