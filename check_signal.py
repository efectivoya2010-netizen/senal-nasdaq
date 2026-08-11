#!/usr/bin/env python3
"""
Robot de senal Nasdaq/Bitcoin - corre solo en GitHub Actions (nube, gratis).
Misma logica que la app movil: perfil de volumen, delta, reclamo, absorcion, confluencia.
Cuando hay senal valida, manda notificacion push real via ntfy.sh (llega al celular
como notificacion de sistema, aunque el navegador este cerrado y el telefono bloqueado).
"""
import json, math, os, urllib.request, urllib.parse

API_KEY = os.environ["TWELVEDATA_API_KEY"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
INSTRUMENTS = [("QQQ", "5min", 100), ("BTC/USD", "5min", 100)]
BUCKETS = 40
VALUE_AREA_PCT = 0.70
ABS_VOL_MULT = 1.5
ABS_RANGE_MULT = 0.7
NEAR_PCT = 0.0015
MIN_CONFLUENCE = 2
MIN_TP_PCT_BY_SYMBOL = {"QQQ": 0.0, "BTC/USD": 0.006}  # piso minimo de TP como % del precio, por instrumento
STATE_FILE = "state.json"

def fetch_bars(symbol, interval, outputsize):
    url = ("https://api.twelvedata.com/time_series?symbol=" + urllib.parse.quote(symbol) +
           "&interval=" + interval + "&outputsize=" + str(outputsize) +
           "&apikey=" + API_KEY)
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.load(r)
    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError(data.get("message", "error desconocido de Twelve Data"))
    bars = []
    for v in reversed(data["values"]):
        bars.append({
            "o": float(v["open"]), "h": float(v["high"]),
            "l": float(v["low"]), "c": float(v["close"]),
            "v": float(v.get("volume") or 0),
            "t": v.get("datetime", "")
        })
    return bars

def volume_profile(bars):
    hi = max(b["h"] for b in bars)
    lo = min(b["l"] for b in bars)
    if hi <= lo:
        mid = (hi + lo) / 2
        return mid, hi, lo
    step = (hi - lo) / BUCKETS
    vol = [0.0] * (BUCKETS + 1)
    for b in bars:
        b0 = max(0, int((b["l"] - lo) / step))
        b1 = min(BUCKETS, int((b["h"] - lo) / step))
        n = max(1, b1 - b0 + 1)
        per = b["v"] / n
        for i in range(b0, b1 + 1):
            vol[i] += per
    total = sum(vol)
    poc_b = max(range(len(vol)), key=lambda i: vol[i])
    poc = lo + poc_b * step + step / 2
    target = total * VALUE_AREA_PCT
    covered = vol[poc_b]
    up = down = poc_b
    while covered < target and (up < BUCKETS or down > 0):
        vu = vol[up + 1] if up < BUCKETS else -1
        vd = vol[down - 1] if down > 0 else -1
        if vu >= vd and up < BUCKETS:
            up += 1; covered += vol[up]
        elif down > 0:
            down -= 1; covered += vol[down]
        else:
            break
    vah = lo + (up + 1) * step
    val = lo + down * step
    return poc, vah, val

def evaluate(bars, symbol):
    n = len(bars)
    poc, vah, val = volume_profile(bars)

    cum = 0.0
    cum_arr = []
    for b in bars:
        cum += b["v"] if b["c"] > b["o"] else (-b["v"] if b["c"] < b["o"] else 0)
        cum_arr.append(cum)
    last = len(cum_arr) - 1
    delta_up = last >= 3 and cum_arr[last] > cum_arr[last - 3]
    delta_down = last >= 3 and cum_arr[last] < cum_arr[last - 3]

    avg_vol = sum(b["v"] for b in bars) / n
    avg_range = sum(b["h"] - b["l"] for b in bars) / n
    last_bar = bars[-1]
    high_vol = last_bar["v"] > avg_vol * ABS_VOL_MULT
    small_range = (last_bar["h"] - last_bar["l"]) < avg_range * ABS_RANGE_MULT
    price = last_bar["c"]
    near_val = abs(price - val) <= price * NEAR_PCT
    near_vah = abs(price - vah) <= price * NEAR_PCT
    absorption_buy = high_vol and small_range and near_val
    absorption_sell = high_vol and small_range and near_vah

    back = bars[-4:]
    flush_low = min((b["l"] for b in back), default=price)
    flush_high = max((b["h"] for b in back), default=price)
    flushed_val = any(b["l"] < val for b in back)
    flushed_vah = any(b["h"] > vah for b in back)
    reclaim_buy = flushed_val and price > val
    reclaim_sell = flushed_vah and price < vah

    c_buy = sum([reclaim_buy, delta_up, absorption_buy, price <= poc])
    c_sell = sum([reclaim_sell, delta_down, absorption_sell, price >= poc])

    side = "buy" if reclaim_buy else ("sell" if reclaim_sell else "none")
    conf = c_buy if side == "buy" else (c_sell if side == "sell" else 0)
    valid = conf >= MIN_CONFLUENCE

    result = {"side": side, "conf": conf, "valid": valid, "price": price,
              "val": val, "vah": vah, "poc": poc, "t": last_bar.get("t", "")}
    if valid:
        buf = price * 0.001
        min_tp_pct = MIN_TP_PCT_BY_SYMBOL.get(symbol, 0.0)
        if side == "buy":
            result["sl"] = flush_low - buf
            result["tp"] = poc if poc > price else vah
            if min_tp_pct > 0:
                min_tp = price * (1 + min_tp_pct)
                if result["tp"] < min_tp:
                    result["tp"] = min_tp
        else:
            result["sl"] = flush_high + buf
            result["tp"] = poc if poc < price else val
            if min_tp_pct > 0:
                min_tp = price * (1 - min_tp_pct)
                if result["tp"] > min_tp:
                    result["tp"] = min_tp
    return result

def send_push(title, message):
    url = "https://ntfy.sh/" + NTFY_TOPIC
    req = urllib.request.Request(url, data=message.encode("utf-8"), method="POST")
    req.add_header("Title", title)
    req.add_header("Priority", "high")
    req.add_header("Tags", "chart_with_upwards_trend")
    urllib.request.urlopen(req, timeout=15)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def main():
    state = load_state()
    for symbol, interval, outputsize in INSTRUMENTS:
        try:
            bars = fetch_bars(symbol, interval, outputsize)
            r = evaluate(bars, symbol)
        except Exception as e:
            print(f"{symbol}: error -> {e}")
            continue

        key = symbol
        last_side = state.get(key, {}).get("side", "none")
        print(f"{symbol}: side={r['side']} conf={r['conf']}/4 valid={r['valid']} price={r['price']:.2f}")

        if r["valid"] and r["side"] != last_side:
            label = "COMPRA" if r["side"] == "buy" else "VENTA"
            hora = r["t"][11:16] if r.get("t") else "?"
            msg = (f"{symbol} @ {r['price']:.2f}\n"
                   f"Vela: {hora}\n"
                   f"Confluencia {r['conf']}/4\n"
                   f"SL {r['sl']:.2f} | TP {r['tp']:.2f}\n"
                   f"VAL {r['val']:.2f} | POC {r['poc']:.2f} | VAH {r['vah']:.2f}")
            send_push(f"NQ Flow - Posible {label} en {symbol}", msg)
            print(f"  -> push enviado ({label})")

        state[key] = {"side": r["side"] if r["valid"] else "none"}

    save_state(state)

if __name__ == "__main__":
    main()
