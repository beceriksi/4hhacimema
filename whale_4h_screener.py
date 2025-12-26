import os, json, time, math
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

# ===================== AYARLAR =====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

OKX = "https://www.okx.com"

# Tarama evreni
INST_TYPE = "SPOT"
QUOTE = "USDT"
MAX_SYMBOLS = 150          # istersen 80/100 yap (rate limit rahatlar)
REQUEST_SLEEP = 0.08       # OKX'e nazik ol

# Sinyal koşulları (4H)
BAR = "4H"
CANDLE_LIMIT = 120

VOL_SPIKE_LOOKBACK = 3     # son kaç mum "spike"
VOL_BASELINE = 24          # önceki kaç mum ortalaması
VOL_RATIO_MIN = 2.0        # son3 ort / önceki24 ort en az kaç
LAST_CANDLE_RATIO_MIN = 1.6# son mum / önceki24 ort min

# Trend koşulları
EMA_FAST = 20
EMA_SLOW = 50
MIN_4H_RETURN_PCT = 1.0    # son 5 mumda toplam +% kaç üstü olsun
RETURN_LOOKBACK = 5

# "Whale" benzeri büyük trade filtresi (public trades)
ENABLE_WHALE = True
WHALE_NOTIONAL_USDT = 50_000  # tek işlemde >= 50k USDT ise whale say
TRADES_LIMIT = 100           # OKX public trades max 100

# Spam kontrol
DAILY_ALERT_LIMIT = 3
COOLDOWN_HOURS = 18
STATE_PATH = ".cache/whale_4h_state.json"

UA = {"User-Agent": "whale-4h-screener/1.0"}

# ===================== YARDIMCI =====================
def utc_now():
    return datetime.now(timezone.utc)

def safe_float(x, default=None):
    try:
        return float(x)
    except:
        return default

def ensure_state_dir():
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)

def load_state():
    ensure_state_dir()
    if not os.path.exists(STATE_PATH):
        return {"date": "", "sent": 0, "cooldown": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"date": "", "sent": 0, "cooldown": {}}

def save_state(state):
    ensure_state_dir()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("\n[UYARI] TELEGRAM_TOKEN/CHAT_ID yok. Mesaj konsola basıldı:\n")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }, timeout=20)
    r.raise_for_status()

# ===================== OKX API =====================
def okx_get(path, params=None):
    r = requests.get(OKX + path, params=params or {}, headers=UA, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != "0":
        raise RuntimeError(f"OKX error: {j}")
    return j["data"]

def list_usdt_spot():
    data = okx_get("/api/v5/market/tickers", {"instType": INST_TYPE})
    inst = []
    for row in data:
        instId = row.get("instId", "")
        if not instId.endswith(f"-{QUOTE}"):
            continue
        # çok saçma/stable-stable temizliği
        if instId.startswith(("USDC-", "USDT-", "DAI-", "FDUSD-", "TUSD-")):
            continue
        inst.append(instId)
    return inst[:MAX_SYMBOLS]

def get_candles(instId: str):
    data = okx_get("/api/v5/market/candles", {"instId": instId, "bar": BAR, "limit": str(CANDLE_LIMIT)})
    # OKX candles: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    rows = []
    for c in data:
        rows.append({
            "ts": int(c[0]),
            "open": safe_float(c[1], 0),
            "high": safe_float(c[2], 0),
            "low":  safe_float(c[3], 0),
            "close": safe_float(c[4], 0),
            "vol": safe_float(c[5], 0),
            "volCcy": safe_float(c[6], 0),
            "volQuote": safe_float(c[7], None)  # USDT cinsinden en iyisi bu
        })
    df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    if df.empty:
        return df
    # quote volume yoksa yaklaşıkla
    if df["volQuote"].isna().all():
        df["volQuote"] = df["volCcy"] * df["close"]
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df

def get_whale_flow(instId: str):
    # public trades: [instId, tradeId, px, sz, side, ts]
    data = okx_get("/api/v5/market/trades", {"instId": instId, "limit": str(TRADES_LIMIT)})
    buy = 0.0
    sell = 0.0
    for t in data:
        px = safe_float(t.get("px"), 0)
        sz = safe_float(t.get("sz"), 0)
        side = t.get("side", "")
        notional = px * sz
        if notional >= WHALE_NOTIONAL_USDT:
            if side == "buy":
                buy += notional
            elif side == "sell":
                sell += notional
    return buy, sell

# ===================== ANALİZ =====================
def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()

def analyze_inst(instId: str):
    df = get_candles(instId)
    if len(df) < (EMA_SLOW + VOL_BASELINE + VOL_SPIKE_LOOKBACK + 5):
        return None

    close = df["close"]
    volq = df["volQuote"]

    df["ema_fast"] = ema(close, EMA_FAST)
    df["ema_slow"] = ema(close, EMA_SLOW)

    # Trend UP
    trend_up = (close.iloc[-1] > df["ema_fast"].iloc[-1] > df["ema_slow"].iloc[-1])

    # Momentum: son N mum getirisi
    ret = (close.iloc[-1] / close.iloc[-(RETURN_LOOKBACK+1)] - 1.0) * 100.0

    # Volume spike
    recent_avg = volq.iloc[-VOL_SPIKE_LOOKBACK:].mean()
    base_avg = volq.iloc[-(VOL_BASELINE+VOL_SPIKE_LOOKBACK):-VOL_SPIKE_LOOKBACK].mean()
    if base_avg <= 0:
        return None
    vol_ratio = recent_avg / base_avg
    last_ratio = volq.iloc[-1] / base_avg

    # Filtreler
    if not trend_up:
        return None
    if ret < MIN_4H_RETURN_PCT:
        return None
    if vol_ratio < VOL_RATIO_MIN:
        return None
    if last_ratio < LAST_CANDLE_RATIO_MIN:
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
        "ema_fast": float(df["ema_fast"].iloc[-1]),
        "ema_slow": float(df["ema_slow"].iloc[-1]),
        "whale_buy": float(whale_buy),
        "whale_sell": float(whale_sell),
    }

def main():
    state = load_state()
    today = utc_now().strftime("%Y-%m-%d")
    if state.get("date") != today:
        state["date"] = today
        state["sent"] = 0
        state["cooldown"] = {}

    if state["sent"] >= DAILY_ALERT_LIMIT:
        print("Daily limit reached.")
        return

    insts = list_usdt_spot()
    results = []

    for instId in insts:
        # cooldown
        cd = state["cooldown"].get(instId)
        if cd:
            try:
                cd_dt = datetime.fromisoformat(cd.replace("Z", "+00:00"))
                if utc_now() < cd_dt:
                    continue
            except:
                pass

        try:
            r = analyze_inst(instId)
            if r:
                results.append(r)
        except Exception:
            pass

        time.sleep(REQUEST_SLEEP)

    if not results:
        send_telegram("🕵️ 4H Whale/Hacim Tarama: Şartlara uyan coin yok (trend+hacim spike).")
        return

    # En iyi 7'yi seç: önce hacim oranı, sonra momentum, sonra whale net
    def whale_net(x): return (x["whale_buy"] - x["whale_sell"])
    results.sort(key=lambda x: (x["vol_ratio"], x["ret"], whale_net(x)), reverse=True)
    top = results[:7]

    # mesaj
    now = utc_now().strftime("%Y-%m-%d %H:%M UTC")
    lines = []
    lines.append(f"🐳 4H HACİM + UP TREND TARAMA (OKX Spot) | {now}")
    lines.append(f"Filtre: EMA{EMA_FAST}>{EMA_SLOW} + Ret{RETURN_LOOKBACK}≥{MIN_4H_RETURN_PCT}% + VolSpike≥{VOL_RATIO_MIN}x")
    lines.append("")

    for i, x in enumerate(top, 1):
        wn = whale_net(x)
        whale_txt = ""
        if ENABLE_WHALE:
            if wn > 0:
                whale_txt = f" | WhaleNet:+{wn/1000:.0f}k"
            elif wn < 0:
                whale_txt = f" | WhaleNet:{wn/1000:.0f}k"
            else:
                whale_txt = " | WhaleNet:0"

        lines.append(
            f"{i}) {x['instId']} | close:{x['close']:.6g} | ret{RETURN_LOOKBACK}:{x['ret']:.2f}% | "
            f"vol:{x['vol_ratio']:.2f}x (last:{x['last_ratio']:.2f}x){whale_txt}"
        )

        # cooldown set
        state["cooldown"][x["instId"]] = (utc_now() + timedelta(hours=COOLDOWN_HOURS)).isoformat().replace("+00:00", "Z")

    state["sent"] += 1
    save_state(state)

    send_telegram("\n".join(lines))

if __name__ == "__main__":
    main()
