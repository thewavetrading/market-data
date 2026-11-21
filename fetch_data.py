"""
Descarga diaria de datos de mercado para los informes US Close / US Weekly.

Fuentes:
  - Finviz Elite (clave en la variable de entorno FINVIZ_TOKEN):
      * historico diario OHLCV de 2 anos por ticker  -> data/prices/<TICKER>.csv
      * screener tecnico del S&P 500 (503 valores)    -> data/breadth/sp500_<fecha>.csv
      * grupos por industria y por sector             -> data/groups/
  - CBOE (archivos publicos): VIX, VIX9D, VIX3M       -> data/vix/

Al final escribe data/manifest.json con la fecha de los datos y los fallos,
y anade una fila a data/breadth/breadth_history.csv con el breadth del dia.
"""

import csv
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

TOKEN = os.environ.get("FINVIZ_TOKEN", "").strip()
BASE = "https://elite.finviz.com"
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
NY = ZoneInfo("America/New_York")

# Finviz permite 10 peticiones cada 5 segundos: 0.6 s entre peticiones deja margen.
PAUSE = 0.6
session = requests.Session()
session.headers["User-Agent"] = "market-data-script/1.0"


def log(msg):
    print(f"[{datetime.now(NY):%H:%M:%S}] {msg}", flush=True)


def get(url, params=None, tries=3):
    """GET con reintentos; sigue redirecciones. Devuelve texto o None."""
    for i in range(tries):
        try:
            r = session.get(url, params=params, timeout=60, allow_redirects=True)
            if r.status_code == 429:
                time.sleep(6)
                continue
            if r.status_code == 401:
                log("ERROR 401: clave de Finviz invalida o suscripcion caducada.")
                return None
            if r.ok and r.text.strip():
                return r.text
            log(f"  HTTP {r.status_code} en {url}")
        except requests.RequestException as e:
            log(f"  error de red ({e}); reintento {i + 1}")
        time.sleep(2 * (i + 1))
    return None


def finviz(path, **params):
    params["auth"] = TOKEN
    time.sleep(PAUSE)
    return get(f"{BASE}{path}", params)


def read_tickers():
    with open(os.path.join(ROOT, "tickers.txt")) as f:
        return [l.strip().upper() for l in f if l.strip() and not l.startswith("#")]


def pct(series):
    return pd.to_numeric(series.astype(str).str.rstrip("%"), errors="coerce")


def fetch_prices(tickers, manifest):
    out = os.path.join(DATA, "prices")
    os.makedirs(out, exist_ok=True)
    for t in tickers:
        txt = finviz("/export/stock", t=t, p="d", r="y2")
        if not txt or not txt.startswith("Date"):
            manifest["failed_prices"].append(t)
            log(f"  {t}: sin datos")
            continue
        df = pd.read_csv(io.StringIO(txt))
        df["Date"] = pd.to_datetime(df["Date"], format="%m/%d/%Y").dt.strftime("%Y-%m-%d")
        df.to_csv(os.path.join(out, f"{t}.csv"), index=False)
        manifest["last_date"][t] = df["Date"].iloc[-1]
        log(f"  {t}: {len(df)} sesiones, ultima {df['Date'].iloc[-1]}")


def fetch_breadth(session_date, manifest):
    out = os.path.join(DATA, "breadth")
    os.makedirs(out, exist_ok=True)
    txt = finviz("/export/screener", type="stocks", v="171", f="idx_sp500")
    if not txt or "Ticker" not in txt[:200]:
        manifest["errors"].append("breadth: screener S&P 500 no disponible")
        return
    df = pd.read_csv(io.StringIO(txt))
    df.to_csv(os.path.join(out, f"sp500_{session_date}.csv"), index=False)
    df.to_csv(os.path.join(out, "sp500_latest.csv"), index=False)

    ch = pct(df["Change"])
    hi = pct(df["52-Week High"])
    lo = pct(df["52-Week Low"])
    row = {
        "date": session_date,
        "members": len(df),
        "pct_above_sma20": float(round((pct(df["20-Day Simple Moving Average"]) > 0).mean() * 100, 1)),
        "pct_above_sma50": float(round((pct(df["50-Day Simple Moving Average"]) > 0).mean() * 100, 1)),
        "pct_above_sma200": float(round((pct(df["200-Day Simple Moving Average"]) > 0).mean() * 100, 1)),
        "advancers": int((ch > 0).sum()),
        "decliners": int((ch < 0).sum()),
        "unchanged": int((ch == 0).sum()),
        "new_52w_highs": int((hi >= 0).sum()),
        "new_52w_lows": int((lo <= 0).sum()),
        "within_2pct_of_52w_high": int((hi >= -2).sum()),
        "within_2pct_of_52w_low": int((lo <= 2).sum()),
        "pct_rsi14_above_70": float(round((pd.to_numeric(df["Relative Strength Index (14)"], errors="coerce") > 70).mean() * 100, 1)),
        "pct_rsi14_below_30": float(round((pd.to_numeric(df["Relative Strength Index (14)"], errors="coerce") < 30).mean() * 100, 1)),
    }
    hist = os.path.join(out, "breadth_history.csv")
    rows = []
    if os.path.exists(hist):
        with open(hist) as f:
            rows = [r for r in csv.DictReader(f) if r["date"] != session_date]
    rows.append({k: str(v) for k, v in row.items()})
    with open(hist, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        w.writeheader()
        w.writerows(rows)
    manifest["breadth"] = row
    log(f"  breadth: {row}")


def fetch_groups(manifest):
    out = os.path.join(DATA, "groups")
    os.makedirs(out, exist_ok=True)
    for g in ("industry", "sector"):
        # v=140 = vista Performance (1W, 1M, 3M, 6M, 1Y, YTD...)
        txt = finviz("/grp_export.ashx", g=g, v="140", o="name")
        if txt and "Name" in txt[:200]:
            with open(os.path.join(out, f"{g}_performance.csv"), "w") as f:
                f.write(txt)
            log(f"  grupos {g}: ok")
        else:
            manifest["errors"].append(f"groups {g}: no disponible")


def fetch_vix(manifest):
    out = os.path.join(DATA, "vix")
    os.makedirs(out, exist_ok=True)
    for sym in ("VIX", "VIX9D", "VIX3M"):
        txt = get(f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{sym}_History.csv")
        if txt and "DATE" in txt[:50].upper():
            df = pd.read_csv(io.StringIO(txt)).tail(400)
            df.to_csv(os.path.join(out, f"{sym}.csv"), index=False)
            log(f"  {sym}: ultima fila {df.iloc[-1, 0]}")
        else:
            manifest["errors"].append(f"{sym}: CBOE no disponible")


def main():
    if not TOKEN:
        sys.exit("Falta FINVIZ_TOKEN.")
    now = datetime.now(NY)
    force = "--force" in sys.argv

    # Comprobacion rapida: si SPY aun no tiene la sesion de hoy, no hacer nada
    # (permite programar dos horarios por el cambio de hora de EE. UU.).
    probe = finviz("/export/stock", t="SPY", p="d", r="d5")
    if not probe or not probe.startswith("Date"):
        sys.exit("No se pudo leer SPY desde Finviz (revisa la clave).")
    last = pd.read_csv(io.StringIO(probe))["Date"].iloc[-1]
    session_date = datetime.strptime(last, "%m/%d/%Y").strftime("%Y-%m-%d")
    log(f"Ultima sesion en Finviz: {session_date}")

    mpath = os.path.join(DATA, "manifest.json")
    if not force and os.path.exists(mpath):
        prev = json.load(open(mpath))
        if prev.get("session_date") == session_date and prev.get("complete"):
            log("Los datos de esta sesion ya estaban descargados. Nada que hacer.")
            return
    if not force and session_date == now.strftime("%Y-%m-%d") and (now.hour, now.minute) < (16, 10):
        log("El mercado aun no ha cerrado. Se descargara en la siguiente ejecucion.")
        return

    manifest = {
        "session_date": session_date,
        "run_at_ny": now.strftime("%Y-%m-%d %H:%M"),
        "run_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "source_prices": "Finviz Elite export/stock (daily, 2y)",
        "last_date": {},
        "failed_prices": [],
        "errors": [],
    }
    tickers = read_tickers()
    log(f"Descargando {len(tickers)} historicos...")
    fetch_prices(tickers, manifest)
    log("Breadth S&P 500...")
    fetch_breadth(session_date, manifest)
    log("Grupos...")
    fetch_groups(manifest)
    log("VIX (CBOE)...")
    fetch_vix(manifest)

    stale = [t for t, d in manifest["last_date"].items() if d != session_date]
    manifest["stale_prices"] = stale
    manifest["complete"] = len(manifest["failed_prices"]) <= 5
    with open(mpath, "w") as f:
        json.dump(manifest, f, indent=2)
    log(f"Hecho. Fallos: {manifest['failed_prices']} | desfasados: {stale} | errores: {manifest['errors']}")


if __name__ == "__main__":
    main()
