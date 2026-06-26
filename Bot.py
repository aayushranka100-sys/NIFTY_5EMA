# ============================================================
#   NIFTY 5-EMA ALERT BOT - GITHUB ACTIONS VERSION
#   Runs ONCE every 5 min via GitHub cron
#   State saved in signal_log.json between runs
# ============================================================

import os
import json
import requests
import pandas as pd
from datetime import datetime, time as dtime
import pytz
from urllib.parse import quote

# ==================== CONFIGURATION ====================
# Tokens from GitHub Secrets (NOT hardcoded)
ACCESS_TOKEN       = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"
EMA_LENGTH     = 5
RISK_REWARD    = 3.0

MARKET_START = dtime(9, 15)
MARKET_END   = dtime(15, 30)
TIMEZONE     = pytz.timezone("Asia/Kolkata")
STATE_FILE   = "signal_log.json"
# =======================================================


def now_ist():
    return datetime.now(TIMEZONE)

def in_market_hours():
    t = now_ist().time()
    return MARKET_START <= t <= MARKET_END

def fmt(x):
    return f"{float(x):.2f}"


# ---------- STATE (Memory between GitHub runs) ----------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                return set(data.get("processed_bars", [])), data.get("active_setup", None)
        except Exception as e:
            print(f"[STATE] Load error: {e}")
    return set(), None

def save_state(processed_bars, active_setup):
    data = {
        "processed_bars": list(processed_bars)[-100:],  # last 100 bars only
        "active_setup"  : active_setup,
        "last_run"      : now_ist().strftime('%Y-%m-%d %H:%M:%S IST')
    }
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[STATE] Saved. Processed bars: {len(processed_bars)}")


# ---------- TELEGRAM ----------

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        r = requests.post(url, data=payload, timeout=15)
        if r.status_code == 200:
            print(f"[TG SENT] {message[:80]}")
        else:
            print(f"[TG ERROR] {r.status_code} | {r.text[:200]}")
    except Exception as e:
        print(f"[TG EXCEPTION] {e}")


# ---------- DATA FETCH ----------

def parse_candles_to_df(candles):
    if not candles:
        return None
    rows = []
    for c in candles:
        if len(c) >= 5:
            rows.append({
                "timestamp": c[0],
                "open"     : float(c[1]),
                "high"     : float(c[2]),
                "low"      : float(c[3]),
                "close"    : float(c[4])
            })
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()
    df.index = df.index.tz_convert(TIMEZONE)
    return df

def resample_to_5m(df_1m):
    if df_1m is None or df_1m.empty:
        return None
    df_5m = df_1m.resample("5min", label="right", closed="right").agg({
        "open" : "first",
        "high" : "max",
        "low"  : "min",
        "close": "last"
    }).dropna()
    if df_5m.empty:
        return None
    df_5m["ema5"] = df_5m["close"].ewm(span=EMA_LENGTH, adjust=False).mean()
    return df_5m

def fetch_upstox_data():
    encoded_key = quote(INSTRUMENT_KEY, safe="")
    url = (
        f"https://api.upstox.com/v2/historical-candle/"
        f"intraday/{encoded_key}/1minute"
    )
    headers = {
        "Accept"       : "application/json",
        "Api-Version"  : "2.0",
        "Authorization": f"Bearer {ACCESS_TOKEN}"
    }
    try:
        r = requests.get(url, headers=headers, timeout=20)
        print(f"[UPSTOX] HTTP {r.status_code}")
        try:
            data = r.json()
        except Exception:
            print(f"[UPSTOX] Non-JSON: {r.text[:400]}")
            return None
        if not isinstance(data, dict):
            print(f"[UPSTOX] Unexpected type: {type(data)}")
            return None
        if data.get("status") != "success":
            print(f"[UPSTOX] Not success: {str(data)[:400]}")
            return None
        candles = data.get("data", {}).get("candles", [])
        if not candles:
            print("[UPSTOX] Candles empty")
            return None
        print(f"[UPSTOX] {len(candles)} candles received")
        df_1m = parse_candles_to_df(candles)
        if df_1m is None or df_1m.empty:
            return None
        df_5m = resample_to_5m(df_1m)
        if df_5m is None or df_5m.empty:
            return None
        now_floor = pd.Timestamp(now_ist()).floor("5min")
        df_5m = df_5m[df_5m.index <= now_floor]
        if df_5m.empty:
            print("[UPSTOX] No completed 5min bars yet")
            return None
        return df_5m
    except Exception as e:
        print(f"[UPSTOX EXCEPTION] {e}")
        return None


# ---------- MAIN (Runs ONCE per GitHub Action trigger) ----------

def main():
    print("=" * 50)
    print("  NIFTY 5-EMA BOT — GITHUB ACTIONS RUN")
    print(f"  Time : {now_ist().strftime('%d-%b-%Y %H:%M:%S IST')}")
    print("=" * 50)

    # Check market hours
    if not in_market_hours():
        print("[INFO] Outside market hours. Nothing to do.")
        return

    # Check token
    if not ACCESS_TOKEN:
        print("[ERROR] UPSTOX_ACCESS_TOKEN not set in GitHub Secrets!")
        send_telegram("⚠️ Bot Error: UPSTOX_ACCESS_TOKEN missing in GitHub Secrets!")
        return

    # Load previous state from signal_log.json
    processed_bars, active_setup = load_state()
    print(f"[STATE] {len(processed_bars)} bars already processed")
    if active_setup:
        print(f"[STATE] Active setup: {active_setup}")

    # Fetch latest 5min data
    df_5m = fetch_upstox_data()
    if df_5m is None or df_5m.empty:
        print("[INFO] No data. Exiting.")
        return

    recent_bars = df_5m.tail(5)

    for ts, row in recent_bars.iterrows():
        bar_key = ts.strftime('%Y-%m-%d %H:%M')

        # Skip already processed bars
        if bar_key in processed_bars:
            continue

        print(f"\n[BAR] {ts.strftime('%H:%M')} | "
              f"O:{fmt(row['open'])} H:{fmt(row['high'])} "
              f"L:{fmt(row['low'])} C:{fmt(row['close'])} "
              f"EMA5:{fmt(row['ema5'])}")

        # ---- Check active setup for trigger ----
        if active_setup is not None:
            if row["low"] < active_setup["entry_low"]:
                risk   = active_setup["sl_high"] - active_setup["entry_low"]
                target = active_setup["entry_low"] - (risk * RISK_REWARD)

                msg = (
                    f"🔴 SELL SIGNAL TRIGGERED\n"
                    f"──────────────────\n"
                    f"Entry  : {fmt(active_setup['entry_low'])}\n"
                    f"SL     : {fmt(active_setup['sl_high'])}\n"
                    f"Target : {fmt(target)}\n"
                    f"R:R    : 1:{RISK_REWARD}\n"
                    f"Time   : {now_ist().strftime('%H:%M IST')}"
                )
                send_telegram(msg)
                print("[TRIGGER] Sell signal sent!")
                active_setup = None

            else:
                active_setup["bars_checked"] = active_setup.get("bars_checked", 0) + 1
                print(f"[SETUP] No trigger yet. Bars checked: {active_setup['bars_checked']}/2")
                if active_setup["bars_checked"] >= 2:
                    print("[SETUP] Setup expired after 2 bars.")
                    active_setup = None

        # ---- Check for new Alert Candle ----
        if row["low"] > row["ema5"]:
            active_setup = {
                "alert_time"  : bar_key,
                "sl_high"     : float(row["high"]),
                "entry_low"   : float(row["low"]),
                "bars_checked": 0
            }
            msg = (
                f"🔔 ALERT CANDLE IDENTIFIED\n"
                f"──────────────────\n"
                f"Time   : {ts.strftime('%H:%M IST')}\n"
                f"High   : {fmt(row['high'])}\n"
                f"Low    : {fmt(row['low'])}\n"
                f"EMA5   : {fmt(row['ema5'])}\n"
                f"Watching next 2 candles for SELL trigger..."
            )
            send_telegram(msg)
            print("[ALERT CANDLE] New setup identified!")

        processed_bars.add(bar_key)

    # Save state for next run
    save_state(processed_bars, active_setup)
    print("\n[DONE] Run complete. Exiting cleanly.")


# ---- ENTRY POINT ----
main()
