import os, json, time
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

# ===================== AYARLAR =====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

OKX = "https://www.okx.com"
UA = {"User-Agent": "whale-4h-screener/1.0"}

# Evren
BAR = "4H"
CANDLE_LIMIT = 120

# Top 50 hacim
TOP_N = 50

# Hacim spike ayarları
VOL_SPIKE_LOOKBACK = 3      # son 3 mum
VOL_BASELINE = 24           # önceki 24 mum
VOL_RATIO_MIN = 2.0
LAST_CANDLE_RATIO_MIN = 1.6

# Trend & momentum
EMA_FAST = 20
EMA_SLOW = 50
RETURN_LOOKBACK = 5
MIN_4H_RETURN_PCT = 1.0

# Whale benzeri trade
ENABLE_WHALE = True
WHALE_NOTIONAL_USDT = 50_000
TRADES_LIMIT = 100

# Spam kontrol
DAILY_ALERT_LIMIT = 3
COOLDOWN_HOURS = 18
STATE_PATH = ".cache/whale_4h_state.json"
REQUEST_SLEEP = 0.08

# ===================== YARDIMCI =====================
def utc_now():
    return datetime.now(timezone.utc)

def safe_float(x, default=0.0):
    try:
        return float(x)
    except:
        return default

def ensure_state():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    if not os.path.exists(STATE_PATH):
        return {"date": "", "sent": 0, "cooldown": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"date": "", "sent": 0, "cooldown": {}}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }, timeout=20)
    r.raise_for_status()

def okx_get(path, params=None):
    r = requests.get(OKX + path, params=params or {}, headers=UA, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != "0":
        raise RuntimeError(j)
    return j["data"]

# ===================== OKX DATA =====================
def list_top50_usdt_spot():
    data = okx_get("/api/v5/market/tickers", {"instType": "SPOT"})
    rows = []
    for r in data:
        instId = r.get("instId", "")
        if not instId.endswith("-USDT"):
            continue
        if instId.startswith(("USDT-", "USDC-", "DAI-", "FDUSD-", "TUSD-")):
            continue
        volq = safe_float(r.get("volCcyQuote"), 0)
        if volq <= 0:
            continue
        rows.append({"instId": instId, "vol": volq})
    rows.sort(key=lambda x: x["vol"], reverse=True)
    return [x["instId"] for x in rows[:TOP_N]]

def get_candles(instId):
    data = okx_get("/api/v5/market/candles", {
        "instId": instId, "bar": BAR, "limit": str(CANDLE_LIMIT)
    })
    rows = []
    for c in data:
        rows.append({
            "ts": int(c[0]),
            "close": safe_float(c[4]),
            "volQuote": safe_float(c[7])
        })
    df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    return df

def get_whale_flow(instId):
    data = okx_get("/api/v5/market/trades", {
        "instId": instId, "limit": str(TRADES_LIMIT)
    })
    buy = sell = 0.0
    for t in data:
        px = safe_float(t.get("px"))
        sz = safe_float(t.get("sz"))
        notional = px * sz
        if notional >= WHALE_NOTIONAL_USDT:
            if t.get("side") == "buy":
                buy += notional
            elif t.get("side") == "sell":
                sell += notional
    return buy, sell

# ===================== ANALİZ =====================
def ema(series, n):
    return series.ewm(span=n, adjust=False).mean()

def analyze_inst(instId):
    df = get_candles(instId)
    if len(df) < (EMA_SLOW + VOL_BASELINE + VOL_SPIKE_LOOKBACK + 5):
        return None

    close = df["close"]
    volq = df["volQuote"]

    df["ema_fast"] = ema(close, EMA_FAST)
    df["ema_slow"] = ema(close, EMA_SLOW)

    # Trend UP
    if not (close.iloc[-1] > df["ema_fast"].iloc[-1] > df["ema_slow"].iloc[-1]):
        return None

    # Momentum
    ret = (close.iloc[-1] / close.iloc[-(RETURN_LOOKBACK+1)] - 1) * 100
    if ret < MIN_4H_RETURN_PCT:
        return None

    # Volume spike
    recent_avg = volq.iloc[-VOL_SPIKE_LOOKBACK:].mean()
    base_avg = volq.iloc[-(VOL_BASELINE+VOL_SPIKE_LOOKBACK):-VOL_SPIKE_LOOKBACK].mean()
    if base_avg <= 0:
        return None

    vol_ratio = recent_avg / base_avg
    last_ratio = volq.iloc[-1] / base_avg

    if vol_ratio < VOL_RATIO_MIN or last_ratio < LAST_CANDLE_RATIO_MIN:
        return None

    whale_buy = whale_sell = 0.0
    if ENABLE_WHALE:
        try:
            whale_buy, whale_sell = get_whale_flow(instId)
        except:
            pass

    return {
        "instId": instId,
        "close": float(close.iloc[-1]),
        "ret": float(ret),
        "vol_ratio": float(vol_ratio),
        "last_ratio": float(last_ratio),
        "whale_net": whale_buy - whale_sell
    }

# ===================== MAIN =====================
def main():
    state = ensure_state()
    today = utc_now().strftime("%Y-%m-%d")
    if state.get("date") != today:
        state["date"] = today
        state["sent"] = 0
        state["cooldown"] = {}

    if state["sent"] >= DAILY_ALERT_LIMIT:
        return

    insts = list_top50_usdt_spot()
    results = []

    for instId in insts:
        cd = state["cooldown"].get(instId)
        if cd:
            try:
                if utc_now() < datetime.fromisoformat(cd.replace("Z","+00:00")):
                    continue
            except:
                pass

        try:
            r = analyze_inst(instId)
            if r:
                results.append(r)
        except:
            pass

        time.sleep(REQUEST_SLEEP)

    if not results:
        send_telegram("🕵️ 4H Whale/Hacim Tarama: Top50 içinde şartlara uyan coin yok.")
        return

    results.sort(key=lambda x: (x["vol_ratio"], x["ret"], x["whale_net"]), reverse=True)
    top = results[:7]

    now = utc_now().strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"🐳 4H HACİM + UP TREND (TOP50) | {now}",
        f"Filtre: EMA{EMA_FAST}>{EMA_SLOW} | VolSpike≥{VOL_RATIO_MIN}x | Ret≥{MIN_4H_RETURN_PCT}%",
        ""
    ]

    for i, x in enumerate(top, 1):
        wn = x["whale_net"]
        whale_txt = f" | WhaleNet:{wn/1000:.0f}k" if ENABLE_WHALE else ""
        lines.append(
            f"{i}) {x['instId']} | close:{x['close']:.6g} | "
            f"ret{RETURN_LOOKBACK}:{x['ret']:.2f}% | "
            f"vol:{x['vol_ratio']:.2f}x (last:{x['last_ratio']:.2f}x){whale_txt}"
        )
        state["cooldown"][x["instId"]] = (utc_now() + timedelta(hours=COOLDOWN_HOURS)).isoformat().replace("+00:00","Z")

    state["sent"] += 1
    save_state(state)
    send_telegram("\n".join(lines))

if __name__ == "__main__":
    main()
