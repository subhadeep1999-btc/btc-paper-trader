"""
BTC perpetual TSI(25,13,13) PAPER-TRADING bot for GitHub Actions
- Signal: TSI crossing its signal line on the last CLOSED 1h candle.
- Bullish cross -> close any short, open long.  Bearish cross -> close any long, open short.
- No real orders are placed. Trades are simulated and reported to Telegram.
- Simulated fills use the close price of the signal candle (real fills would differ).
Data (first that works): Delta Exchange India BTCUSD perpetual, then OKX, then Kraken.
Every alert names the source used. Times in IST.
Secrets: TG_TOKEN, TG_CHAT_ID (GitHub Secrets). State is kept in state.json.
"""
import os
import json
import time
from datetime import datetime, timezone, timedelta

import requests

# ---------------- paper-trading settings ----------------
START_EQUITY = 1000.0      # simulated starting balance (USD)
POSITION_FRACTION = 1.0    # fraction of equity used per trade
LEVERAGE = 1.0             # keep 1.0: liquidation is NOT simulated
FEE_RATE = 0.0006          # assumed 0.06% per side (taker fee + slippage allowance)
# --------------------------------------------------------

LABEL = "1h"
BAR_SECONDS = 3600
LONG, SHORT, SIGNAL = 25, 13, 13
STATE_FILE = "state.json"
MAX_TRADES_KEPT = 200

TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

IST = timezone(timedelta(hours=5, minutes=30))
HEADERS = {"User-Agent": "tsi-scanner/1.0"}


# ---------------------------- data ----------------------------
def parse_delta_candles(payload, now):
    """Delta returns candle objects with time (unix seconds, candle START).
    Accepts either a bare list or {"result": [...]}; keeps closed candles only."""
    rows = payload.get("result", []) if isinstance(payload, dict) else payload
    rows = sorted(rows, key=lambda x: int(x["time"]))
    rows = [x for x in rows if int(x["time"]) + BAR_SECONDS <= now]
    closes = [float(x["close"]) for x in rows]
    close_ts = int(rows[-1]["time"]) + BAR_SECONDS
    return closes, close_ts


def from_delta():
    now = int(time.time())
    r = requests.get(
        "https://api.india.delta.exchange/v2/history/candles",
        params={"symbol": "BTCUSD", "resolution": "1h",
                "start": str(now - 300 * BAR_SECONDS), "end": str(now)},
        headers=HEADERS, timeout=20,
    )
    r.raise_for_status()
    closes, close_ts = parse_delta_candles(r.json(), now)
    return "Delta India BTCUSD", closes, close_ts


def from_okx():
    r = requests.get(
        "https://www.okx.com/api/v5/market/candles",
        params={"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": "300"},
        headers=HEADERS, timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise RuntimeError(f"OKX error: {data}")
    rows = [x for x in data["data"] if x[8] == "1"]   # confirmed candles only
    rows.sort(key=lambda x: int(x[0]))
    closes = [float(x[4]) for x in rows]
    close_ts = int(rows[-1][0]) / 1000 + BAR_SECONDS
    return "OKX BTC-USDT-SWAP", closes, close_ts


def from_kraken():
    now = int(time.time())
    r = requests.get(
        "https://futures.kraken.com/api/charts/v1/trade/PF_XBTUSD/1h",
        params={"from": now - 300 * BAR_SECONDS, "to": now},
        headers=HEADERS, timeout=20,
    )
    r.raise_for_status()
    rows = r.json()["candles"]
    rows.sort(key=lambda x: x["time"])
    rows = [x for x in rows if x["time"] / 1000 + BAR_SECONDS <= now]
    closes = [float(x["close"]) for x in rows]
    close_ts = rows[-1]["time"] / 1000 + BAR_SECONDS
    return "Kraken PF_XBTUSD", closes, close_ts


def fetch_closes():
    errors = []
    for fn in (from_delta, from_okx, from_kraken):
        try:
            source, closes, close_ts = fn()
            if len(closes) < 100:
                raise RuntimeError("too few candles")
            t = datetime.fromtimestamp(close_ts, tz=IST).strftime("%Y-%m-%d %H:%M")
            return source, closes, t
        except Exception as e:
            errors.append(f"{fn.__name__}: {str(e)[:80]}")
    raise RuntimeError("All data sources failed: " + " | ".join(errors))


# --------------------------- indicator ---------------------------
def ema(values, n):
    k = 2.0 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def compute_tsi(closes):
    pc = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    apc = [abs(x) for x in pc]
    num = ema(ema(pc, LONG), SHORT)
    den = ema(ema(apc, LONG), SHORT)
    tsi = [100 * a / b if b != 0 else 0.0 for a, b in zip(num, den)]
    sig = ema(tsi, SIGNAL)
    return tsi, sig


def detect_cross(tsi, sig):
    prev_diff = tsi[-2] - sig[-2]
    curr_diff = tsi[-1] - sig[-1]
    if prev_diff <= 0 < curr_diff:
        return "BULLISH"
    if prev_diff >= 0 > curr_diff:
        return "BEARISH"
    return None


# ------------------------- paper trading -------------------------
def new_state():
    return {"last_alert": None, "equity": START_EQUITY, "start_equity": START_EQUITY,
            "position": None, "trades": []}


def normalize_state(state):
    base = new_state()
    for k, v in base.items():
        state.setdefault(k, v)
    return state


def close_position(state, price, when):
    pos = state["position"]
    direction = 1 if pos["side"] == "LONG" else -1
    notional_entry = pos["qty"] * pos["entry"]
    notional_exit = pos["qty"] * price
    gross = direction * pos["qty"] * (price - pos["entry"])
    fees = FEE_RATE * (notional_entry + notional_exit)
    pnl = gross - fees
    state["equity"] += pnl
    trade = {
        "side": pos["side"], "entry": pos["entry"], "exit": price,
        "pnl": round(pnl, 2), "pct": round(100 * pnl / pos["margin"], 2),
        "opened": pos["opened"], "closed": when,
    }
    state["trades"].append(trade)
    state["trades"] = state["trades"][-MAX_TRADES_KEPT:]
    state["position"] = None
    return trade


def open_position(state, side, price, when):
    margin = state["equity"] * POSITION_FRACTION
    notional = margin * LEVERAGE
    state["position"] = {"side": side, "entry": price, "qty": notional / price,
                         "margin": margin, "opened": when}
    return state["position"]


def summary_line(state):
    trades = state["trades"]
    wins = sum(1 for t in trades if t["pnl"] > 0)
    ret = 100 * (state["equity"] / state["start_equity"] - 1)
    wr = f"{100 * wins / len(trades):.0f}%" if trades else "n/a"
    return (f"Paper equity: ${state['equity']:,.2f} ({ret:+.2f}%) | "
            f"closed trades: {len(trades)} | win rate: {wr}")


def apply_signal(state, cross, price, when):
    """Return the Telegram message lines for this signal, and update state."""
    want = "LONG" if cross == "BULLISH" else "SHORT"
    lines = []
    pos = state["position"]
    if pos and pos["side"] == want:
        return [f"Signal {cross}, already {want}: no change."]
    if pos:
        t = close_position(state, price, when)
        icon = "✅" if t["pnl"] > 0 else "❌"
        lines.append(f"{icon} CLOSED {t['side']} @ {t['exit']:,.2f} "
                     f"(entry {t['entry']:,.2f}) PnL ${t['pnl']:+,.2f} ({t['pct']:+.2f}%)")
    p = open_position(state, want, price, when)
    lines.append(f"📌 OPENED {want} @ {price:,.2f} | size {p['qty']:.5f} BTC "
                 f"(${p['qty'] * price:,.0f})")
    return lines


# ----------------------------- plumbing -----------------------------
def send_alert(text):
    print(text)
    if not (TG_TOKEN and TG_CHAT_ID):
        print("Telegram secrets missing")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT_ID, "text": text}, timeout=20,
    )
    if not r.json().get("ok"):
        print("Telegram error:", r.text)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return normalize_state(json.load(f))
    except Exception:
        return new_state()


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def main():
    if os.getenv("TEST_ALERT") == "1":
        source, closes, t = fetch_closes()
        send_alert(f"Test OK - paper-trading bot connected. Data: {source}, last close {t} IST")
        return
    source, closes, candle_time = fetch_closes()
    tsi, sig = compute_tsi(closes)
    cross = detect_cross(tsi, sig)
    state = load_state()
    price = closes[-1]
    if cross and state.get("last_alert") != candle_time:
        arrow = "🟢" if cross == "BULLISH" else "🔴"
        lines = apply_signal(state, cross, price, candle_time)
        msg = (
            f"{arrow} [PAPER] BTC perp {LABEL} TSI({LONG},{SHORT},{SIGNAL}) {cross} crossover\n"
            + "\n".join(lines) + "\n"
            + summary_line(state) + "\n"
            + f"TSI {tsi[-1]:.2f} | Signal {sig[-1]:.2f} | Source: {source}\n"
            + f"Candle close (IST): {candle_time}"
        )
        send_alert(msg)
        state["last_alert"] = candle_time
        save_state(state)
    else:
        pos = state["position"]
        where = f"{pos['side']} from {pos['entry']:,.2f}" if pos else "flat"
        print(f"[{candle_time} IST] {source} | no new crossover | position: {where} | "
              f"TSI {tsi[-1]:.2f} vs signal {sig[-1]:.2f}")


if __name__ == "__main__":
    main()
