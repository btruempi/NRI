#!/usr/bin/env python3
"""Scheduled NRI sender — runs inside GitHub Actions.

Two responsibilities:

1. **Digest email** — reads data/email_settings.json and, if today matches
   the cadence, sends a HTML+plain summary of upcoming catalysts / pre-IPOs.

2. **Alert rules** — reads data/alerts.json + data/watchlists.json, fetches
   the latest daily bars for each alerted ticker from Stooq (no API key,
   no CORS since we're server-side), evaluates each rule, and — for any
   NEW fires (not seen in data/alerts_state.json since last run) — sends
   an email and an SMS-via-carrier-gateway.

State (last_fired timestamps) is committed back to the repo by the
workflow so we don't re-alert on the same event next day.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import math
import os
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "data" / "email_settings.json"
CATALYSTS = ROOT / "data" / "catalysts.json"
PRE_IPO = ROOT / "data" / "pre_ipo.json"
ALERTS = ROOT / "data" / "alerts.json"
WATCHLISTS = ROOT / "data" / "watchlists.json"
ALERT_STATE = ROOT / "data" / "alerts_state.json"


def log(msg: str) -> None:
    print(msg, flush=True)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"warn: failed to parse {path.name}: {e}")
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Cadence logic
# --------------------------------------------------------------------------- #

def today_matches(cadence: str, today: dt.date) -> bool:
    if cadence == "daily":
        return True
    if cadence == "weekly":
        return today.weekday() == 0
    if cadence == "monthly":
        return today.day == 1
    if cadence == "quarterly":
        return today.day == 1 and today.month in (1, 4, 7, 10)
    if cadence == "yearly":
        return today.day == 1 and today.month == 1
    return False


# --------------------------------------------------------------------------- #
# Stooq CSV price fetch (server-side, no CORS, no API key)
# --------------------------------------------------------------------------- #

_UA = "Mozilla/5.0 (compatible; NRI-Actions/1.0)"


def _http_get(url: str, timeout: float = 12.0):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        log(f"  http error {url}: {e}")
        return None


STOOQ_ALIAS = {
    "RYCEY": "rycey.us",
    "GFUZ": None,
    "IMSR": None,
    "XE": "xe.us",
}


def _stooq_code(ticker: str):
    if ticker in STOOQ_ALIAS:
        return STOOQ_ALIAS[ticker]
    return ticker.lower() + ".us"


def fetch_series(ticker: str):
    """Return a list of {d, p, v} dicts of DAILY bars, or None."""
    code = _stooq_code(ticker)
    if not code:
        return None
    txt = _http_get(f"https://stooq.com/q/d/l/?s={code}&i=d")
    if not txt or "Date,Open" not in txt:
        # Fallback: Yahoo Finance JSON
        y = _http_get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=1y&interval=1d")
        if not y:
            return None
        try:
            j = json.loads(y)
            r = j["chart"]["result"][0]
            ts = r["timestamp"]
            q = r["indicators"]["quote"][0]
            closes = q.get("close", [])
            vols = q.get("volume") or [None] * len(ts)
            out = []
            for i, t in enumerate(ts):
                c = closes[i] if i < len(closes) else None
                if c is None:
                    continue
                d = dt.datetime.utcfromtimestamp(t).date().isoformat()
                out.append({"d": d, "p": round(float(c), 4), "v": vols[i] if i < len(vols) else None})
            return out if len(out) >= 20 else None
        except Exception:
            return None
    lines = txt.strip().splitlines()
    if len(lines) < 21:
        return None
    out = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < 5:
            continue
        try:
            close = float(parts[4])
        except ValueError:
            continue
        try:
            vol = int(parts[5]) if len(parts) > 5 and parts[5].strip() else None
        except ValueError:
            vol = None
        out.append({"d": parts[0].strip(), "p": round(close, 4), "v": vol})
    return out if len(out) >= 20 else None


def fetch_intraday_overlay(ticker: str):
    """Return {'d': YYYY-MM-DD, 'p': last_price, 'v': cumulative_volume, 'ts': unix_seconds} for
    the most recent intraday bar (Yahoo 5-min interval), or None. Used to overlay today's
    developing candle on top of the daily series so price-based alerts fire near-real-time."""
    y = _http_get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=1d&interval=5m",
        timeout=10.0,
    )
    if not y:
        return None
    try:
        j = json.loads(y)
        r = j["chart"]["result"][0]
        ts_list = r.get("timestamp") or []
        q = (r.get("indicators", {}).get("quote") or [{}])[0]
        closes = q.get("close") or []
        vols = q.get("volume") or []
        # Walk from the end; skip bars where close is null (Yahoo pads).
        for i in range(len(ts_list) - 1, -1, -1):
            c = closes[i] if i < len(closes) else None
            if c is None or (isinstance(c, float) and math.isnan(c)):
                continue
            last_ts = ts_list[i]
            last_price = float(c)
            # Sum cumulative volume across the trading day (bars before the last one)
            cum_v = 0
            for j2 in range(0, i + 1):
                v = vols[j2] if j2 < len(vols) else None
                if v is not None and not (isinstance(v, float) and math.isnan(v)):
                    cum_v += int(v)
            return {
                "d": dt.datetime.utcfromtimestamp(last_ts).date().isoformat(),
                "p": round(last_price, 4),
                "v": cum_v or None,
                "ts": int(last_ts),
            }
        return None
    except Exception as e:
        log(f"  intraday parse fail {ticker}: {e}")
        return None


def series_with_intraday(ticker: str):
    """Fetch daily bars + overlay today's intraday latest onto the last bar (or append)."""
    daily = fetch_series(ticker)
    if not daily:
        return None, None
    intraday = fetch_intraday_overlay(ticker)
    if not intraday:
        return daily, None
    # If intraday's date is > daily's last date, append a new candle (developing today's bar).
    # If it equals daily's last date, replace the last bar with the fresher price/volume.
    if intraday["d"] > daily[-1]["d"]:
        daily.append({"d": intraday["d"], "p": intraday["p"], "v": intraday["v"]})
    elif intraday["d"] == daily[-1]["d"]:
        daily[-1] = {"d": intraday["d"], "p": intraday["p"], "v": intraday["v"]}
    return daily, intraday


# --------------------------------------------------------------------------- #
# Indicator helpers
# --------------------------------------------------------------------------- #

def sma(series, period):
    out = [None] * len(series)
    if len(series) < period:
        return out
    s = sum(p["p"] for p in series[:period])
    out[period - 1] = s / period
    for i in range(period, len(series)):
        s += series[i]["p"] - series[i - period]["p"]
        out[i] = s / period
    return out


def rsi(series, period=14):
    out = [None] * len(series)
    if len(series) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        ch = series[i]["p"] - series[i - 1]["p"]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    avg_g = gains / period
    avg_l = losses / period
    rs = (avg_g / avg_l) if avg_l > 0 else float("inf")
    out[period] = 100 - 100 / (1 + rs)
    for i in range(period + 1, len(series)):
        ch = series[i]["p"] - series[i - 1]["p"]
        g = ch if ch > 0 else 0
        l = -ch if ch < 0 else 0
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
        rs = (avg_g / avg_l) if avg_l > 0 else float("inf")
        out[i] = 100 - 100 / (1 + rs)
    return out


def ema(values, period):
    out = [None] * len(values)
    k = 2 / (period + 1)
    prev = None
    for i, v in enumerate(values):
        if v is None:
            continue
        if prev is None:
            if i >= period - 1:
                seed = sum(values[i - period + 1:i + 1]) / period
                prev = seed
                out[i] = seed
        else:
            prev = v * k + prev * (1 - k)
            out[i] = prev
    return out


def macd(series, fast=12, slow=26, signal=9):
    prices = [p["p"] for p in series]
    ef = ema(prices, fast)
    es = ema(prices, slow)
    line = [None if (ef[i] is None or es[i] is None) else (ef[i] - es[i]) for i in range(len(prices))]
    sig = ema([x if x is not None else 0.0 for x in line], signal)
    # Blank out signal wherever line is None
    sig = [None if line[i] is None else sig[i] for i in range(len(prices))]
    hist = [None if (line[i] is None or sig[i] is None) else (line[i] - sig[i]) for i in range(len(prices))]
    return line, sig, hist


# --------------------------------------------------------------------------- #
# Alert rule evaluation
# --------------------------------------------------------------------------- #

def _to_float(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _to_int(x, default=None):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


def eval_rule(rule: dict, series: list) -> tuple[bool, str]:
    """Return (fired, human-readable message)."""
    if not series or len(series) < 2:
        return False, "no data"
    args = rule.get("args") or {}
    typ = rule.get("type")
    last = series[-1]
    px = last["p"]

    if typ == "price_above":
        v = _to_float(args.get("value"))
        if v is None:
            return False, "missing value"
        return (px > v, f"price ${px:.2f} > {v}")
    if typ == "price_below":
        v = _to_float(args.get("value"))
        if v is None:
            return False, "missing value"
        return (px < v, f"price ${px:.2f} < {v}")
    if typ == "pct_change":
        p = _to_float(args.get("pct"))
        w = _to_int(args.get("window"), 20)
        if p is None or w is None or len(series) <= w:
            return False, "missing args or history"
        prior = series[-1 - w]["p"]
        ch = (px - prior) / prior * 100.0
        fired = abs(ch) >= abs(p)
        return (fired, f"{w}d change {ch:+.2f}% (threshold ±{p}%)")
    if typ == "pct_change_from_anchor":
        p = _to_float(args.get("pct"))
        anchor = args.get("anchor_date", "")
        if p is None or not anchor:
            return False, "missing args"
        anchor_pt = next((s for s in series if s["d"] >= anchor), None)
        if not anchor_pt:
            return False, "anchor in future"
        ch = (px - anchor_pt["p"]) / anchor_pt["p"] * 100.0
        fired = abs(ch) >= abs(p)
        return (fired, f"since {anchor}: {ch:+.2f}% (threshold ±{p}%)")
    if typ == "rsi_above":
        v = _to_float(args.get("value"), 70)
        r = rsi(series, 14)
        last_r = r[-1]
        if last_r is None:
            return False, "no rsi"
        return (last_r > v, f"RSI {last_r:.1f} > {v}")
    if typ == "rsi_below":
        v = _to_float(args.get("value"), 30)
        r = rsi(series, 14)
        last_r = r[-1]
        if last_r is None:
            return False, "no rsi"
        return (last_r < v, f"RSI {last_r:.1f} < {v}")
    if typ in ("sma_cross_up", "sma_cross_down"):
        fp = _to_int(args.get("fast"), 50)
        sp = _to_int(args.get("slow"), 200)
        f = sma(series, fp)
        s = sma(series, sp)
        if len(series) < 2 or f[-1] is None or f[-2] is None or s[-1] is None or s[-2] is None:
            return False, "warmup"
        if typ == "sma_cross_up":
            fired = f[-2] <= s[-2] and f[-1] > s[-1]
        else:
            fired = f[-2] >= s[-2] and f[-1] < s[-1]
        arrow = "↑" if typ.endswith("up") else "↓"
        return (fired, f"SMA {fp} {arrow} SMA {sp} at ${px:.2f}")
    if typ in ("macd_cross_up", "macd_cross_down"):
        line, sig, _ = macd(series)
        if line[-1] is None or line[-2] is None or sig[-1] is None or sig[-2] is None:
            return False, "warmup"
        if typ == "macd_cross_up":
            fired = line[-2] <= sig[-2] and line[-1] > sig[-1]
        else:
            fired = line[-2] >= sig[-2] and line[-1] < sig[-1]
        arrow = "↑" if typ.endswith("up") else "↓"
        return (fired, f"MACD {arrow} signal at ${px:.2f}")
    if typ == "volume_spike":
        mult = _to_float(args.get("multiplier"), 3.0)
        if last.get("v") is None:
            return False, "no volume"
        recent = [s.get("v") or 0 for s in series[-21:-1]]
        avg = sum(recent) / len(recent) if recent else 0
        if avg <= 0:
            return False, "no avg volume"
        ratio = (last["v"] or 0) / avg
        return (ratio >= mult, f"vol {last['v']:,} = {ratio:.1f}× 20d avg")
    return False, f"unknown rule type: {typ}"


# --------------------------------------------------------------------------- #
# Digest builders (unchanged from prior version, trimmed)
# --------------------------------------------------------------------------- #

def upcoming_catalysts(today: dt.date, window_days: int = 30):
    data = load_json(CATALYSTS, {})
    items = data.get("catalysts", []) if isinstance(data, dict) else []
    out = []
    for c in items:
        date_str = str(c.get("date", "")).strip()
        try:
            d = dt.date.fromisoformat(date_str[:10])
            if 0 <= (d - today).days <= window_days:
                out.append(c)
                continue
        except Exception:
            pass
        if "-Q" in date_str:
            try:
                yr, q = date_str.split("-Q")
                qmonth = {"1": 1, "2": 4, "3": 7, "4": 10}[q.strip()]
                d = dt.date(int(yr), qmonth, 1)
                if -30 <= (d - today).days <= window_days + 90:
                    out.append(c)
                    continue
            except Exception:
                pass
        if date_str.lower() == "ongoing":
            out.append(c)
    return out


def pre_ipo_summary():
    data = load_json(PRE_IPO, {})
    items = data.get("companies", []) if isinstance(data, dict) else []
    return [c for c in items if str(c.get("status", "")).lower() in ("imminent", "filed")]


def render_digest_plain(today, cadence, cats, pre_ipo):
    lines = [
        f"Nuclear Renaissance Index — {today.isoformat()} ({cadence} digest)",
        "=" * 64, "",
    ]
    if cats:
        lines.append(f"Catalysts ahead ({len(cats)}):")
        for c in cats[:20]:
            imp = c.get("importance", "")
            tag = f" [{'*'*int(imp)}]" if isinstance(imp, int) and imp > 0 else ""
            lines.append(f"  - {c.get('date','?'):<12} {c.get('ticker',''):<6}{tag} {c.get('event','')}")
    else:
        lines.append("No upcoming catalysts in window.")
    lines.append("")
    if pre_ipo:
        lines.append("Pre-IPO watch (imminent / filed):")
        for p in pre_ipo:
            lines.append(f"  - {p.get('name','?')}: {p.get('status','')} — {p.get('notes','')}")
    return "\n".join(lines)


def render_digest_html(today, cadence, cats, pre_ipo):
    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    parts = [
        "<div style='font-family:-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif;"
        "max-width:640px;margin:0 auto;color:#1a1a1a;'>",
        f"<h2 style='margin:0 0 4px 0;'>Nuclear Renaissance Index</h2>",
        f"<p style='margin:0 0 20px 0;color:#666;'>{today.isoformat()} · {esc(cadence)} digest</p>",
    ]
    if cats:
        parts.append(f"<h3>Catalysts ahead ({len(cats)})</h3>"
                     "<table cellspacing='0' cellpadding='6' style='width:100%;border-collapse:collapse;font-size:14px;'>"
                     "<tr style='background:#f3f3f3;text-align:left;'><th>Date</th><th>Ticker</th><th>Event</th><th>Imp.</th></tr>")
        for c in cats[:40]:
            imp = c.get("importance", "")
            star = "★" * int(imp) if isinstance(imp, int) and imp > 0 else ""
            parts.append("<tr style='border-top:1px solid #eee;'>"
                         f"<td>{esc(c.get('date',''))}</td><td><strong>{esc(c.get('ticker',''))}</strong></td>"
                         f"<td>{esc(c.get('event',''))}</td><td style='color:#b8860b;'>{star}</td></tr>")
        parts.append("</table>")
    if pre_ipo:
        parts.append("<h3>Pre-IPO watch</h3><ul>")
        for p in pre_ipo:
            parts.append(f"<li><strong>{esc(p.get('name',''))}</strong> "
                         f"<span style='color:#666;'>({esc(p.get('status',''))})</span> — {esc(p.get('notes',''))}</li>")
        parts.append("</ul>")
    parts.append("<p style='margin-top:24px;color:#888;font-size:12px;'>"
                 "Change cadence, add alerts, or unsubscribe from the NRI site.</p></div>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Alert digest builders
# --------------------------------------------------------------------------- #

def render_alerts_plain(fires):
    if not fires:
        return ""
    lines = [f"NRI alerts fired ({len(fires)}):", "-" * 40]
    for f in fires:
        lines.append(f"[{f['ticker']}] {f['label']} — {f['detail']}")
    return "\n".join(lines)


def render_alerts_html(fires):
    if not fires:
        return ""
    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    parts = ["<div style='font-family:-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif;max-width:640px;color:#1a1a1a;'>",
             f"<h2 style='margin:0 0 8px 0;'>NRI alerts — {len(fires)} fired</h2>",
             "<table cellspacing='0' cellpadding='6' style='width:100%;border-collapse:collapse;font-size:14px;'>",
             "<tr style='background:#f3f3f3;text-align:left;'><th>Ticker</th><th>Rule</th><th>Detail</th></tr>"]
    for f in fires:
        parts.append(f"<tr style='border-top:1px solid #eee;'>"
                     f"<td><strong>{esc(f['ticker'])}</strong></td>"
                     f"<td>{esc(f['label'])}</td><td>{esc(f['detail'])}</td></tr>")
    parts.append("</table></div>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# SMTP send with optional SMS gateway CC
# --------------------------------------------------------------------------- #

def send_mail(sender, app_pw, to_list, subject, plain, html=None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to_list)
    msg.set_content(plain)
    if html:
        msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as smtp:
        smtp.login(sender, app_pw)
        smtp.send_message(msg)


def sms_address(phone: str, carrier_domain: str) -> str | None:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not digits or not carrier_domain:
        return None
    return f"{digits}@{carrier_domain}"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    settings = load_json(SETTINGS, {})
    alerts_cfg = load_json(ALERTS, {"channels": {}, "rules": []})
    state = load_json(ALERT_STATE, {"fired": {}})

    today = dt.datetime.utcnow().date()
    now_iso = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    app_pw = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not app_pw:
        log("Missing GMAIL_APP_PASSWORD secret. Add it at Settings → Secrets and variables → Actions.")
        # Still exit 0 — nothing to do without credentials.
        return 0

    # ---- Alerts pass -------------------------------------------------------
    a_channels = alerts_cfg.get("channels") or {}
    a_email = a_channels.get("email") or settings.get("email")
    a_sms = sms_address(a_channels.get("sms_phone"), a_channels.get("sms_carrier"))
    fires = []
    if alerts_cfg.get("rules"):
        log(f"Evaluating {len(alerts_cfg['rules'])} alert rule(s)…")
        series_cache = {}
        intraday_cache = {}
        for rule in alerts_cfg["rules"]:
            if not rule.get("enabled", True):
                continue
            tk = str(rule.get("ticker", "")).strip().upper()
            if not tk:
                continue
            if tk not in series_cache:
                s, i = series_with_intraday(tk)
                series_cache[tk] = s
                intraday_cache[tk] = i
            series = series_cache[tk]
            intraday = intraday_cache.get(tk)
            if not series:
                log(f"  ✗ {tk}: no series")
                continue
            fired, detail = eval_rule(rule, series)
            if fired:
                # Dedup key: rule id + latest bar timestamp (intraday if available, else date).
                # An intraday timestamp lets the same rule re-arm intraday if the price crosses
                # back and forth across the threshold on separate bars.
                stamp = str(intraday["ts"]) if intraday else series[-1]["d"]
                dedup_key = f"{rule.get('id','?')}@{stamp}"
                prior = state["fired"].get(dedup_key)
                if prior:
                    continue
                # Look up human label for the rule type
                label_map = {
                    "price_above": "price above",
                    "price_below": "price below",
                    "pct_change": "% change over window",
                    "pct_change_from_anchor": "% change from anchor",
                    "rsi_above": "RSI above",
                    "rsi_below": "RSI below",
                    "sma_cross_up": "golden cross",
                    "sma_cross_down": "death cross",
                    "macd_cross_up": "MACD cross up",
                    "macd_cross_down": "MACD cross down",
                    "volume_spike": "volume spike",
                }
                fires.append({
                    "ticker": tk,
                    "label": label_map.get(rule.get("type"), rule.get("type", "?")),
                    "detail": detail,
                    "rule_id": rule.get("id"),
                    "dedup_key": dedup_key,
                })
                rule["last_fired"] = now_iso
                state["fired"][dedup_key] = now_iso
                log(f"  ★ {tk}: {detail}")

    if fires and a_email:
        subject = f"NRI alerts — {len(fires)} fired ({today.isoformat()})"
        to_list = [a_email]
        if a_sms:
            to_list.append(a_sms)
        # For SMS, use a shorter subject; the body is what shows up on some carriers.
        try:
            send_mail(a_email, app_pw, to_list, subject,
                      render_alerts_plain(fires), render_alerts_html(fires))
            log(f"Sent alerts email → {to_list}")
        except Exception as e:
            log(f"Alert send failed: {e}")

        # Persist updated alerts.json (with last_fired) and state.
        write_json(ALERTS, alerts_cfg)
        # Bound state size — keep last 500 keys.
        keys = list(state["fired"].keys())
        if len(keys) > 500:
            for k in keys[:-500]:
                state["fired"].pop(k, None)
        write_json(ALERT_STATE, state)
    elif fires and not a_email:
        log("Alerts fired but no email address configured — skipping send.")

    # ---- Digest pass -------------------------------------------------------
    cadence = str(settings.get("cadence", "off")).lower().strip()
    if cadence in ("", "off", "disabled"):
        log("Digest cadence is off — skipping digest.")
    elif not today_matches(cadence, today):
        log(f"Digest cadence={cadence}; today ({today}) does not match. Skipping.")
    else:
        sender = settings.get("email") or settings.get("smtp_user") or ""
        recipient = settings.get("to") or sender
        if not sender or not recipient:
            log("Missing sender/recipient — skipping digest.")
        else:
            cats = upcoming_catalysts(today)
            pre_ipo = pre_ipo_summary()
            try:
                send_mail(sender, app_pw, [recipient],
                          f"NRI {cadence} digest — {today.isoformat()}",
                          render_digest_plain(today, cadence, cats, pre_ipo),
                          render_digest_html(today, cadence, cats, pre_ipo))
                log(f"Sent {cadence} digest → {recipient}")
            except Exception as e:
                log(f"Digest send failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
