#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
vigia.py — Vigía de crisis. Corre en GitHub Actions, no en el PC de Nicolás.

Qué hace
--------
Mide indicadores objetivos de estrés financiero, los clasifica en semáforo
(verde / amarillo / rojo) y avisa SOLO cuando alguno cambia de color respecto
de la corrida anterior. El estado se guarda en estado.json, versionado en el
repo, así que queda historia.

Por qué así
-----------
No intenta predecir caídas: eso no se puede. Y en la AFP el cambio de fondo
demora 4 días hábiles, más el tope normativo que limita los cambios de un mismo
día al 5% del patrimonio del fondo (se procesan por antigüedad). O sea, no se
puede esquivar un crash. Lo que sí sirve es tener reglas escritas en frío y que
algo te avise cuando se cruzan, para no decidir leyendo titulares.

Fuentes (sin API key propia)
----------------------------
  1. Yahoo Finance chart API — cierres casi en tiempo real.
  2. FRED vía el Cloudflare Worker de Nicolás (la key FRED vive en el Worker).
     Sirve de respaldo si Yahoo bloquea la IP del runner, cosa que pasa.
  Si existe la variable de entorno FRED_API_KEY, se usa FRED directo.

Uso
---
    python vigia.py                      # imprime el reporte
    python vigia.py --md reporte.md      # además escribe markdown
    python vigia.py --github-output      # emite variables para GitHub Actions
"""

import os
import sys
import json
import argparse
import datetime as dt
from datetime import timezone
from urllib.parse import quote

import requests

AQUI = os.path.dirname(os.path.abspath(__file__))
ESTADO_PATH = os.path.join(AQUI, "estado.json")
PROXY = "https://market-radar-proxy.joseanrique.workers.dev"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
TIMEOUT = 30

VERDE, AMARILLO, ROJO, SINDATO = "verde", "amarillo", "rojo", "sindato"
ORDEN = {SINDATO: -1, VERDE: 0, AMARILLO: 1, ROJO: 2}
EMOJI = {VERDE: "🟢", AMARILLO: "🟡", ROJO: "🔴", SINDATO: "⚪"}
ETIQ = {VERDE: "NORMAL", AMARILLO: "ALERTA", ROJO: "PELIGRO", SINDATO: "s/d"}


# ──────────────────────────── capa de datos ────────────────────────────

def fred_serie(series_id, limit=400):
    """Observaciones de una serie FRED, más recientes primero: [(fecha, valor)]."""
    key = os.environ.get("FRED_API_KEY", "").strip()
    if key:
        url = ("https://api.stlouisfed.org/fred/series/observations"
               f"?series_id={series_id}&api_key={key}&file_type=json"
               f"&sort_order=desc&limit={limit}")
    else:
        url = f"{PROXY}/fred?series_id={series_id}&limit={limit}"
    r = requests.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    obs = [(o["date"], float(o["value"]))
           for o in r.json()["observations"] if o["value"] not in (".", "")]
    obs.sort(reverse=True)
    return obs


def fred_ultimo(series_id):
    obs = fred_serie(series_id, limit=10)
    return (obs[0][1], obs[0][0]) if obs else (None, None)


def yahoo_serie(symbol, rng="1y"):
    """Cierres diarios desde Yahoo, en orden cronológico."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}"
           f"?range={rng}&interval=1d")
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    ch = r.json()["chart"]
    if ch.get("error"):
        raise RuntimeError(str(ch["error"]))
    res = ch["result"][0]
    cierres = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
    if not cierres:
        raise RuntimeError("sin cierres")
    fecha = dt.datetime.fromtimestamp(
        res["meta"]["regularMarketTime"], timezone.utc).strftime("%Y-%m-%d")
    return cierres, fecha


def indice(yahoo_sym, fred_sym, dias=252):
    """Serie de un índice con respaldo. Devuelve (metricas, fuente, fecha)."""
    try:
        cierres, fecha = yahoo_serie(yahoo_sym)
        fuente = "Yahoo"
    except Exception:
        obs = fred_serie(fred_sym, limit=dias + 60)
        if not obs:
            return None, None, None
        cierres = [v for _, v in reversed(obs)]
        fecha = obs[0][0]
        fuente = "FRED"
    ventana = cierres[-dias:]
    ma = cierres[-200:] if len(cierres) >= 200 else cierres
    return {"ultimo": cierres[-1], "max52": max(ventana),
            "ma200": sum(ma) / len(ma)}, fuente, fecha


def nivel_simple(yahoo_sym, fred_sym):
    """Último valor de un indicador escalar con respaldo."""
    try:
        cierres, fecha = yahoo_serie(yahoo_sym, rng="1mo")
        return cierres[-1], "Yahoo", fecha
    except Exception:
        v, f = fred_ultimo(fred_sym)
        return v, "FRED", f


# ──────────────────────── reglas del semáforo ────────────────────────
# Los umbrales de VIX, spread, curva y Sahm vienen del Market Radar original.
# Drawdown, media 200d y estructura de volatilidad son nuevos.

def r_vix(v):
    if v is None: return SINDATO, "sin dato"
    if v < 20: return VERDE, "volatilidad normal"
    if v < 30: return AMARILLO, "nerviosismo"
    if v < 40: return ROJO, "estrés real"
    return ROJO, "pánico"


def r_spread(bp):
    if bp is None: return SINDATO, "sin dato"
    if bp < 350: return VERDE, "crédito fluido"
    if bp < 500: return AMARILLO, "crédito tensionándose"
    return ROJO, "crédito trancado"


def r_curva(v):
    if v is None: return SINDATO, "sin dato"
    if v >= 0: return VERDE, "pendiente normal"
    if v >= -0.3: return AMARILLO, "aplanada o invertida leve"
    return ROJO, "invertida"


def r_sahm(v):
    if v is None: return SINDATO, "sin dato"
    if v < 0.3: return VERDE, "sin señal de recesión"
    if v < 0.5: return AMARILLO, "deterioro del empleo"
    return ROJO, "regla de Sahm gatillada"


def r_drawdown(pct):
    if pct is None: return SINDATO, "sin dato"
    if pct > -5: return VERDE, "cerca de máximos"
    if pct > -10: return AMARILLO, "retroceso"
    if pct > -20: return ROJO, "corrección sobre 10%"
    return ROJO, "mercado bajista, sobre 20%"


def r_ma200(pct):
    if pct is None: return SINDATO, "sin dato"
    if pct > 0: return VERDE, "sobre la media 200d"
    if pct > -5: return AMARILLO, "bajo la media 200d"
    return ROJO, "bien bajo la media 200d"


def r_term(s):
    """VIX3M menos VIX. Negativo (backwardation) es señal de estrés agudo."""
    if s is None: return SINDATO, "sin dato"
    if s > 0: return VERDE, "curva de volatilidad normal"
    if s > -1.5: return AMARILLO, "curva plana o levemente invertida"
    return ROJO, "backwardation: estrés agudo"


# ──────────────────────────── medición ────────────────────────────

def medir():
    ind, fallas, ctx = [], [], {}

    def add(clave, nombre, valor, fmt, regla, fuente, nota=""):
        color, lectura = regla(valor)
        ind.append({"clave": clave, "nombre": nombre, "valor": valor,
                    "texto": fmt(valor) if valor is not None else "s/d",
                    "color": color, "lectura": lectura,
                    "fuente": fuente or "?", "nota": nota})

    # S&P 500
    sp, f_sp, d_sp = None, None, None
    try:
        sp, f_sp, d_sp = indice("^GSPC", "SP500")
    except Exception as e:
        fallas.append(f"S&P 500: {type(e).__name__}")
    add("dd_sp", "S&P 500 vs máximo 52s",
        (sp["ultimo"] / sp["max52"] - 1) * 100 if sp else None,
        lambda v: f"{v:+.2f}%", r_drawdown, f_sp)
    add("ma200_sp", "S&P 500 vs media 200d",
        (sp["ultimo"] / sp["ma200"] - 1) * 100 if sp else None,
        lambda v: f"{v:+.2f}%", r_ma200, f_sp)
    if sp:
        ctx["sp500"] = sp["ultimo"]
        ctx["sp500_fecha"] = d_sp

    # Nasdaq
    try:
        ndx, f_ndx, _ = indice("^IXIC", "NASDAQCOM")
    except Exception as e:
        ndx, f_ndx = None, None
        fallas.append(f"Nasdaq: {type(e).__name__}")
    add("dd_ndx", "Nasdaq vs máximo 52s",
        (ndx["ultimo"] / ndx["max52"] - 1) * 100 if ndx else None,
        lambda v: f"{v:+.2f}%", r_drawdown, f_ndx)

    # VIX y estructura
    try:
        vix, f_vix, d_vix = nivel_simple("^VIX", "VIXCLS")
    except Exception as e:
        vix, f_vix, d_vix = None, None, None
        fallas.append(f"VIX: {type(e).__name__}")
    try:
        vix3m, _, _ = nivel_simple("^VIX3M", "VXVCLS")
    except Exception:
        vix3m = None
    add("vix", "VIX", vix, lambda v: f"{v:.2f}", r_vix, f_vix)
    add("term", "Estructura VIX3M menos VIX",
        (vix3m - vix) if (vix3m is not None and vix is not None) else None,
        lambda v: f"{v:+.2f}", r_term, f_vix,
        "Negativo significa que cubrirse hoy cuesta más que a 3 meses")

    # FRED: crédito, curva, empleo
    try:
        hy, _ = fred_ultimo("BAMLH0A0HYM2")
        spread = hy * 100 if hy is not None else None
    except Exception as e:
        spread = None
        fallas.append(f"Spread HY: {type(e).__name__}")
    add("spread_hy", "Spread high yield", spread, lambda v: f"{v:.0f} pb",
        r_spread, "FRED",
        "El mejor termómetro de crisis sistémica: mide si el crédito se tranca")

    try:
        d10, _ = fred_ultimo("DGS10")
        d2, _ = fred_ultimo("DGS2")
        curva = (d10 - d2) if (d10 is not None and d2 is not None) else None
        if d10 is not None:
            ctx["t10"] = d10
    except Exception as e:
        curva = None
        fallas.append(f"Curva: {type(e).__name__}")
    add("curva", "Curva 10a menos 2a", curva, lambda v: f"{v:+.2f}", r_curva, "FRED")

    try:
        sahm, _ = fred_ultimo("SAHMREALTIME")
    except Exception as e:
        sahm = None
        fallas.append(f"Sahm: {type(e).__name__}")
    add("sahm", "Regla de Sahm", sahm, lambda v: f"{v:.2f}", r_sahm, "FRED",
        "Señal de recesión basada en el alza del desempleo")

    # Chile, informativo
    try:
        clp, _ = yahoo_serie("USDCLP=X", rng="1y")
        ctx["usdclp"] = clp[-1]
        ctx["usdclp_max52"] = max(clp[-252:])
    except Exception:
        pass

    return ind, ctx, fallas


def nivel_global(ind):
    vivos = [i["color"] for i in ind if i["color"] != SINDATO]
    return max(vivos, key=lambda c: ORDEN[c]) if vivos else SINDATO


# ──────────────────────────── estado ────────────────────────────

def leer_estado():
    if os.path.exists(ESTADO_PATH):
        try:
            return json.load(open(ESTADO_PATH, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def guardar_estado(ind, nivel, ctx):
    json.dump({"actualizado": dt.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "nivel": nivel,
               "colores": {i["clave"]: i["color"] for i in ind},
               "valores": {i["clave"]: i["valor"] for i in ind},
               "contexto": ctx},
              open(ESTADO_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    with open(ESTADO_PATH, "a", encoding="utf-8") as f:
        f.write("\n")


def cambios_de_color(ind, previo):
    antes = (previo or {}).get("colores", {})
    out = []
    for i in ind:
        a = antes.get(i["clave"])
        if a and a != i["color"]:
            out.append({"nombre": i["nombre"], "antes": a, "ahora": i["color"],
                        "valor": i["texto"], "lectura": i["lectura"],
                        "empeora": ORDEN[i["color"]] > ORDEN[a]})
    return out


# ──────────────────────────── reportes ────────────────────────────

RECORDATORIOS = [
    "Cambio de fondo en la AFP: **4 días hábiles**. No se puede esquivar una caída.",
    "Tope normativo: los cambios de un mismo día no superan el **5% del patrimonio** "
    "del fondo y se procesan por antigüedad, así que en pánico demora más.",
    "El **fondo obligatorio deja de ser elegible el 1-abr-2027**: pasa a fondo "
    "generacional según año de nacimiento. El ahorro voluntario sí se podrá elegir, "
    "y hay que hacerlo antes de fines de marzo de 2027.",
]


def markdown(ind, ctx, nivel, cambios, fallas):
    L = [f"## {EMOJI[nivel]} Estado general: **{ETIQ[nivel]}**", "",
         f"`{dt.datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC`", ""]

    if cambios:
        L.append("### Cambios desde la última revisión")
        L.append("")
        for c in cambios:
            flecha = "empeora" if c["empeora"] else "mejora"
            L.append(f"- {EMOJI[c['antes']]} → {EMOJI[c['ahora']]} "
                     f"**{c['nombre']}** ({flecha}): {c['valor']} — {c['lectura']}")
        L.append("")

    L.append("| | Indicador | Valor | Lectura | Fuente |")
    L.append("|---|---|---:|---|---|")
    for i in ind:
        L.append(f"| {EMOJI[i['color']]} | {i['nombre']} | `{i['texto']}` | "
                 f"{i['lectura']} | {i['fuente']} |")
    L.append("")

    if ctx:
        piezas = []
        if "sp500" in ctx:
            piezas.append(f"S&P 500 **{ctx['sp500']:,.2f}** ({ctx.get('sp500_fecha','')})")
        if "t10" in ctx:
            piezas.append(f"Tesoro 10a **{ctx['t10']:.2f}%**")
        if "usdclp" in ctx:
            piezas.append(f"Dólar/CLP **{ctx['usdclp']:,.2f}**")
        if piezas:
            L += ["**Contexto:** " + " · ".join(piezas), ""]

    if fallas:
        L += ["> ⚠️ Fuentes que fallaron: " + "; ".join(fallas), ""]

    L.append("### Recordatorios en frío")
    L.append("")
    L += [f"- {r}" for r in RECORDATORIOS]
    L += ["", "---",
          "Datos: Yahoo Finance y FRED. Automático, sin intervención humana. "
          "**No constituye asesoría financiera.**"]
    return "\n".join(L)


def consola(ind, ctx, nivel, cambios, fallas):
    out = [f"VIGIA DE MERCADO  {dt.datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
           f"Estado general: {ETIQ[nivel]}", ""]
    if cambios:
        out.append("CAMBIOS:")
        for c in cambios:
            out.append(f"  {c['nombre']}: {ETIQ[c['antes']]} -> {ETIQ[c['ahora']]}")
        out.append("")
    out.append(f"{'INDICADOR':<32}{'VALOR':>11}  {'ESTADO':<9}FUENTE")
    out.append("-" * 72)
    for i in ind:
        out.append(f"{i['nombre']:<32}{i['texto']:>11}  "
                   f"{ETIQ[i['color']]:<9}{i['fuente']}")
    if fallas:
        out += ["", "FALLAS: " + "; ".join(fallas)]
    return "\n".join(out)


# ──────────────────────────── main ────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", help="ruta donde escribir el reporte markdown")
    ap.add_argument("--github-output", action="store_true",
                    help="emite variables a GITHUB_OUTPUT")
    ap.add_argument("--sin-guardar", action="store_true")
    args = ap.parse_args()

    ind, ctx, fallas = medir()
    nivel = nivel_global(ind)
    previo = leer_estado()
    primera = not previo
    cambios = cambios_de_color(ind, previo)

    print(consola(ind, ctx, nivel, cambios, fallas))

    md = markdown(ind, ctx, nivel, cambios, fallas)
    if args.md:
        with open(args.md, "w", encoding="utf-8") as f:
            f.write(md)

    avisar = bool(cambios) or primera
    peor = any(c["empeora"] for c in cambios)

    if args.github_output and os.environ.get("GITHUB_OUTPUT"):
        if primera:
            titulo = f"{EMOJI[nivel]} Vigía: línea base inicial ({ETIQ[nivel]})"
        elif len(cambios) == 1:
            c = cambios[0]
            titulo = (f"{EMOJI[c['ahora']]} Vigía: {c['nombre']} "
                      f"{'empeora a' if c['empeora'] else 'mejora a'} {ETIQ[c['ahora']]}")
        else:
            titulo = (f"{EMOJI[nivel]} Vigía: {len(cambios)} indicadores cambiaron "
                      f"(general {ETIQ[nivel]})")
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"avisar={'true' if avisar else 'false'}\n")
            f.write(f"nivel={nivel}\n")
            f.write(f"empeora={'true' if peor else 'false'}\n")
            f.write(f"titulo={titulo}\n")

    if not args.sin_guardar:
        guardar_estado(ind, nivel, ctx)

    if fallas and len(fallas) >= 4:
        print("\nDemasiadas fuentes caidas; revisar.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
