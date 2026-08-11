#!/usr/bin/env python3
"""
Backtest de la estrategia (reclamo de VAH/VAL + delta + absorcion + POC)
contra el historial real de Nasdaq (QQQ) y Bitcoin (BTC/USD).

Recorre cada vela del historial, calcula que hubiera marcado la senal en ese
momento (igual que check_signal.py), y despues mira hacia adelante para ver
si el precio hubiera tocado el TP o el SL primero. Junta los resultados por
nivel de confluencia (1/4, 2/4, 3/4, 4/4) para saber el % de acierto real de
cada uno -- no una sensacion de dos o tres casos sueltos.

Se corre a mano desde GitHub Actions ("Run workflow"), no automaticamente.
El resultado queda escrito en el resumen de la corrida (Job Summary), facil
de leer desde el celular sin bucear en logs.
"""
import json, os, urllib.request, urllib.parse

API_KEY = os.environ["TWELVEDATA_API_KEY"]
INSTRUMENTS = [("QQQ", "5min"), ("BTC/USD", "5min")]
OUTPUTSIZE = 5000  # el maximo que suele permitir el plan gratuito por pedido

BUCKETS = 40
VALUE_AREA_PCT = 0.70
ABS_VOL_MULT = 1.5
ABS_RANGE_MULT = 0.7
NEAR_PCT = 0.0015
LOOKBACK = 100
HORIZON = 200  # cuantas velas hacia adelante espera a que toque TP o SL

def fetch_bars(symbol, interval, outputsize):
    url = ("https://api.twelvedata.com/time_series?symbol=" + urllib.parse.quote(symbol) +
           "&interval=" + interval + "&outputsize=" + str(outputsize) +
           "&apikey=" + API_KEY)
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.load(r)
    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError(data.get("message", "error desconocido de Twelve Data"))
    bars = []
    for v in reversed(data["values"]):
        bars.append({
            "o": float(v["open"]), "h": float(v["high"]),
            "l": float(v["low"]), "c": float(v["close"]),
            "v": float(v.get("volume") or 0)
        })
    return bars

def volume_profile(bars, start, count):
    window = bars[start:start+count]
    hi = max(b["h"] for b in window)
    lo = min(b["l"] for b in window)
    if hi <= lo:
        mid = (hi + lo) / 2
        return mid, hi, lo
    step = (hi - lo) / BUCKETS
    vol = [0.0] * (BUCKETS + 1)
    for b in window:
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

def evaluate_at(bars, i):
    """Replica la logica de check_signal.py pero usando solo datos hasta la vela i (sin mirar el futuro)."""
    start = max(0, i - LOOKBACK + 1)
    window = bars[start:i+1]
    n = len(window)
    if n < LOOKBACK * 0.5:
        return None
    poc, vah, val = volume_profile(bars, start, n)

    cum = 0.0
    cum_arr = []
    for b in window:
        cum += b["v"] if b["c"] > b["o"] else (-b["v"] if b["c"] < b["o"] else 0)
        cum_arr.append(cum)
    last = len(cum_arr) - 1
    delta_up = last >= 3 and cum_arr[last] > cum_arr[last-3]
    delta_down = last >= 3 and cum_arr[last] < cum_arr[last-3]

    avg_vol = sum(b["v"] for b in window) / n
    avg_range = sum(b["h"] - b["l"] for b in window) / n
    last_bar = bars[i]
    high_vol = last_bar["v"] > avg_vol * ABS_VOL_MULT
    small_range = (last_bar["h"] - last_bar["l"]) < avg_range * ABS_RANGE_MULT
    price = last_bar["c"]
    near_val = abs(price - val) <= price * NEAR_PCT
    near_vah = abs(price - vah) <= price * NEAR_PCT
    absorption_buy = high_vol and small_range and near_val
    absorption_sell = high_vol and small_range and near_vah

    back = bars[max(0, i-3):i+1]
    flush_low = min((b["l"] for b in back), default=price)
    flush_high = max((b["h"] for b in back), default=price)
    flushed_val = any(b["l"] < val for b in back)
    flushed_vah = any(b["h"] > vah for b in back)
    reclaim_buy = flushed_val and price > val
    reclaim_sell = flushed_vah and price < vah

    c_buy = sum([reclaim_buy, delta_up, absorption_buy, price <= poc])
    c_sell = sum([reclaim_sell, delta_down, absorption_sell, price >= poc])

    side = "buy" if reclaim_buy else ("sell" if reclaim_sell else "none")
    if side == "none":
        return None
    conf = c_buy if side == "buy" else c_sell
    buf = price * 0.001
    if side == "buy":
        sl = flush_low - buf
        tp = poc if poc > price else vah
    else:
        sl = flush_high + buf
        tp = poc if poc < price else val
    return {"side": side, "conf": conf, "entry": price, "sl": sl, "tp": tp, "idx": i}

def simulate_outcome(bars, signal):
    side, sl, tp = signal["side"], signal["sl"], signal["tp"]
    start = signal["idx"] + 1
    end = min(len(bars), start + HORIZON)
    for k in range(start, end):
        b = bars[k]
        if side == "buy":
            if b["l"] <= sl: return "loss"
            if b["h"] >= tp: return "win"
        else:
            if b["h"] >= sl: return "loss"
            if b["l"] <= tp: return "win"
    return "sin_definir"  # no toco ninguno de los dos dentro del horizonte

def main():
    report_lines = ["# Backtest — resultados por nivel de confluencia\n"]
    for symbol, interval in INSTRUMENTS:
        report_lines.append(f"\n## {symbol}\n")
        try:
            bars = fetch_bars(symbol, interval, OUTPUTSIZE)
        except Exception as e:
            report_lines.append(f"Error trayendo datos: {e}\n")
            continue

        stats = {1: {"win":0,"loss":0}, 2: {"win":0,"loss":0}, 3: {"win":0,"loss":0}, 4: {"win":0,"loss":0}}
        total_signals = 0
        i = LOOKBACK
        while i < len(bars) - 1:
            sig = evaluate_at(bars, i)
            if sig:
                total_signals += 1
                outcome = simulate_outcome(bars, sig)
                if outcome in ("win","loss"):
                    stats[sig["conf"]][outcome] += 1
            i += 1

        report_lines.append(f"Velas analizadas: {len(bars)} | Señales encontradas: {total_signals}\n")
        report_lines.append("| Confluencia | Ganadas | Perdidas | % Acierto |")
        report_lines.append("|---|---|---|---|")
        for conf in [1,2,3,4]:
            w, l = stats[conf]["win"], stats[conf]["loss"]
            total = w + l
            pct = (w/total*100) if total > 0 else 0
            report_lines.append(f"| {conf}/4 | {w} | {l} | {pct:.1f}% ({total} casos) |")

    report = "\n".join(report_lines)
    print(report)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(report)

if __name__ == "__main__":
    main()
