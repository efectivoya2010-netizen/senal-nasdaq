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
MIN_FLUSH_RATIO = 1.0  # descarta señales con SL "pegado" (mecha chica vs rango normal)
LOOKBACK = 100
HORIZON = 200  # cuantas velas hacia adelante espera a que toque TP o SL
MIN_TP_PCT_BY_SYMBOL = {"QQQ": 0.0, "BTC/USD": 0.006}  # piso minimo de TP como % del precio, por instrumento

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
            "v": float(v.get("volume") or 0),
            "t": v.get("datetime", "")
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

def evaluate_at(bars, i, min_tp_pct):
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
        if min_tp_pct > 0:
            min_tp = price * (1 + min_tp_pct)
            if tp < min_tp:
                tp = min_tp
    else:
        sl = flush_high + buf
        tp = poc if poc < price else val
        if min_tp_pct > 0:
            min_tp = price * (1 - min_tp_pct)
            if tp > min_tp:
                tp = min_tp
    flush_dist_pct = abs(price - sl) / price * 100
    avg_range_pct = (avg_range / price) * 100
    flush_ratio = flush_dist_pct / avg_range_pct if avg_range_pct > 0 else 0
    if flush_ratio < MIN_FLUSH_RATIO:
        return None
    return {"side": side, "conf": conf, "entry": price, "sl": sl, "tp": tp, "idx": i,
            "flush_ratio": flush_ratio}

def simulate_outcome(bars, signal):
    """Devuelve (resultado, % de distancia entre entrada y el nivel que toco)."""
    side, sl, tp, entry = signal["side"], signal["sl"], signal["tp"], signal["entry"]
    start = signal["idx"] + 1
    end = min(len(bars), start + HORIZON)
    win_pct = abs(tp - entry) / entry * 100
    loss_pct = abs(entry - sl) / entry * 100
    for k in range(start, end):
        b = bars[k]
        if side == "buy":
            if b["l"] <= sl: return "loss", loss_pct
            if b["h"] >= tp: return "win", win_pct
        else:
            if b["h"] >= sl: return "loss", loss_pct
            if b["l"] <= tp: return "win", win_pct
    return "sin_definir", 0  # no toco ninguno de los dos dentro del horizonte

BE_TRIGGER_FRACS = [0.85, 0.90]  # umbrales de break-even a probar (85% y 90% del camino a TP)

def simulate_outcome_be(bars, signal, be_frac):
    """Igual que simulate_outcome, pero mueve el SL a break-even (precio de entrada)
    apenas el precio recorrio be_frac del camino hacia el TP.
    Nota: asume que dentro de una misma vela el precio primero toca el nivel de
    break-even antes que el stop, ya que no sabemos el orden exacto intra-vela con
    datos de velas (solo O/H/L/C) -- es una simplificacion optimista a tener en cuenta."""
    side, sl, tp, entry = signal["side"], signal["sl"], signal["tp"], signal["entry"]
    start = signal["idx"] + 1
    end = min(len(bars), start + HORIZON)
    win_pct = abs(tp - entry) / entry * 100
    loss_pct = abs(entry - sl) / entry * 100
    be_trigger = entry + be_frac * (tp - entry) if side == "buy" else entry - be_frac * (entry - tp)
    current_stop = sl
    be_active = False
    for k in range(start, end):
        b = bars[k]
        if side == "buy":
            if not be_active and b["h"] >= be_trigger:
                be_active = True
                current_stop = entry
            if b["l"] <= current_stop:
                return ("be", 0.0) if be_active else ("loss", loss_pct)
            if b["h"] >= tp: return "win", win_pct
        else:
            if not be_active and b["l"] <= be_trigger:
                be_active = True
                current_stop = entry
            if b["h"] >= current_stop:
                return ("be", 0.0) if be_active else ("loss", loss_pct)
            if b["l"] <= tp: return "win", win_pct
    return "sin_definir", 0

def main():
    report_lines = ["# Backtest — resultados por nivel de confluencia\n"]
    for symbol, interval in INSTRUMENTS:
        report_lines.append(f"\n## {symbol}\n")
        try:
            bars = fetch_bars(symbol, interval, OUTPUTSIZE)
        except Exception as e:
            report_lines.append(f"Error trayendo datos: {e}\n")
            continue

        min_tp_pct = MIN_TP_PCT_BY_SYMBOL.get(symbol, 0.0)
        stats = {c: {"win":0, "loss":0, "sum_win_pct":0.0, "sum_loss_pct":0.0, "count":0} for c in [1,2,3,4]}
        stats_be = {frac: {c: {"win":0, "loss":0, "be":0, "sum_win_pct":0.0, "sum_loss_pct":0.0} for c in [1,2,3,4]} for frac in BE_TRIGGER_FRACS}
        total_signals = 0
        # buckets para estudiar si el tamaño de la mecha (SL) predice el resultado
        ratio_bins = [(0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, float('inf'))]
        ratio_stats = {b: {"win":0, "loss":0, "sum_win_pct":0.0, "sum_loss_pct":0.0} for b in ratio_bins}

        i = LOOKBACK
        while i < len(bars) - 1:
            sig = evaluate_at(bars, i, min_tp_pct)
            if sig:
                total_signals += 1
                stats[sig["conf"]]["count"] += 1
                outcome, pct = simulate_outcome(bars, sig)
                if outcome in ("win","loss"):
                    stats[sig["conf"]][outcome] += 1
                    if outcome == "win": stats[sig["conf"]]["sum_win_pct"] += pct
                    else: stats[sig["conf"]]["sum_loss_pct"] += pct
                    if sig["conf"] >= 2:
                        for lo, hi in ratio_bins:
                            if lo <= sig["flush_ratio"] < hi:
                                ratio_stats[(lo,hi)][outcome] += 1
                                if outcome == "win": ratio_stats[(lo,hi)]["sum_win_pct"] += pct
                                else: ratio_stats[(lo,hi)]["sum_loss_pct"] += pct
                                break
                for frac in BE_TRIGGER_FRACS:
                    outcome_be, pct_be = simulate_outcome_be(bars, sig, frac)
                    if outcome_be in ("win","loss","be"):
                        stats_be[frac][sig["conf"]][outcome_be] += 1
                        if outcome_be == "win": stats_be[frac][sig["conf"]]["sum_win_pct"] += pct_be
                        elif outcome_be == "loss": stats_be[frac][sig["conf"]]["sum_loss_pct"] += pct_be
            i += 1

        # calcular cuantos dias abarcan los datos, para saber señales/dia
        fechas = [b["t"][:10] for b in bars if b["t"]]
        dias_distintos = len(set(fechas)) if fechas else 1
        dias_distintos = max(dias_distintos, 1)

        report_lines.append(f"Velas analizadas: {len(bars)} | Señales encontradas: {total_signals} | Período: ~{dias_distintos} días\n")
        report_lines.append("| Confluencia | Señales/día | Ganadas | Perdidas | % Acierto | Ganancia media | Pérdida media | Expectativa* | Factor de ganancia** |")
        report_lines.append("|---|---|---|---|---|---|---|---|---|")
        for conf in [1,2,3,4]:
            s = stats[conf]
            w, l = s["win"], s["loss"]
            total = w + l
            pct = (w/total*100) if total > 0 else 0
            avg_win = (s["sum_win_pct"]/w) if w > 0 else 0
            avg_loss = (s["sum_loss_pct"]/l) if l > 0 else 0
            win_rate = w/total if total > 0 else 0
            loss_rate = l/total if total > 0 else 0
            expectancy = (win_rate*avg_win) - (loss_rate*avg_loss)
            profit_factor = (s["sum_win_pct"]/s["sum_loss_pct"]) if s["sum_loss_pct"] > 0 else (float('inf') if s["sum_win_pct"]>0 else 0)
            pf_str = "∞" if profit_factor == float('inf') else f"{profit_factor:.2f}"
            señales_dia = s["count"] / dias_distintos
            report_lines.append(f"| {conf}/4 | {señales_dia:.2f} | {w} | {l} | {pct:.1f}% ({total} casos) | +{avg_win:.2f}% | -{avg_loss:.2f}% | {expectancy:+.3f}% | {pf_str} |")
        report_lines.append("\n*Expectativa: cuánto se espera ganar o perder en promedio por operación, combinando el % de acierto con el tamaño de cada ganancia/pérdida. Positivo = rentable en promedio, negativo = pierde plata en promedio aunque acierte muchas veces.")
        report_lines.append("\n**Factor de ganancia: total ganado / total perdido. Por encima de 1 = rentable, por debajo de 1 = no, sin importar el % de acierto.\n")

        for frac in BE_TRIGGER_FRACS:
            report_lines.append(f"\n### {symbol} — con Break-Even (mover SL a entrada al {int(frac*100)}% del camino al TP)\n")
            report_lines.append("| Confluencia | Ganadas | Perdidas | Empates (BE) | % Acierto (sin empates) | Expectativa* |")
            report_lines.append("|---|---|---|---|---|---|")
            for conf in [1,2,3,4]:
                s = stats_be[frac][conf]
                w, l, be = s["win"], s["loss"], s["be"]
                total_decisivo = w + l
                pct = (w/total_decisivo*100) if total_decisivo > 0 else 0
                avg_win = (s["sum_win_pct"]/w) if w > 0 else 0
                avg_loss = (s["sum_loss_pct"]/l) if l > 0 else 0
                total_ops = w + l + be
                win_rate = w/total_ops if total_ops > 0 else 0
                loss_rate = l/total_ops if total_ops > 0 else 0
                expectancy = (win_rate*avg_win) - (loss_rate*avg_loss)
                report_lines.append(f"| {conf}/4 | {w} | {l} | {be} | {pct:.1f}% | {expectancy:+.3f}% |")
            report_lines.append(f"\n*Con BE, 'empates' son operaciones que iban ganando, tocaron el {int(frac*100)}% del camino al TP, y despues volvieron a entrada sin llegar al TP -- ni ganan ni pierden, cierran en 0. Nota: esta simulacion asume que dentro de una vela el precio toca primero el nivel de break-even antes que el stop original, lo cual es optimista ya que no tenemos el orden exacto de los movimientos dentro de cada vela de 5 minutos.\n")

        report_lines.append(f"\n### {symbol} — ¿el tamaño de la mecha (SL) predice el resultado? (solo señales 2/4 o mas)\n")
        report_lines.append("Compara la distancia entrada-SL contra el rango promedio de las velas de esa ventana. Ratio bajo = SL pegado (mecha chica); ratio alto = SL amplio (mecha grande).\n")
        report_lines.append("| Ratio mecha/rango promedio | Casos | % Acierto | Expectativa |")
        report_lines.append("|---|---|---|---|")
        for lo, hi in ratio_bins:
            s = ratio_stats[(lo,hi)]
            w, l = s["win"], s["loss"]
            total = w + l
            pct = (w/total*100) if total > 0 else 0
            avg_win = (s["sum_win_pct"]/w) if w > 0 else 0
            avg_loss = (s["sum_loss_pct"]/l) if l > 0 else 0
            win_rate = w/total if total > 0 else 0
            loss_rate = l/total if total > 0 else 0
            expectancy = (win_rate*avg_win) - (loss_rate*avg_loss)
            hi_str = "+" if hi == float('inf') else f"-{hi}"
            report_lines.append(f"| {lo}{hi_str} | {total} | {pct:.1f}% | {expectancy:+.3f}% |")
        report_lines.append("")

    report = "\n".join(report_lines)
    print(report)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(report)

if __name__ == "__main__":
    main()
