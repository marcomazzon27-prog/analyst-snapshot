"""Investitori istituzionali – da CUSIP a ticker e prezzi settimanali.

1. CUSIP -> ticker
   a) dall'universo del terminale: per i titoli USA l'ISIN è "US" + CUSIP + cifra di controllo
   b) OpenFIGI (gratuito; più veloce con il secret OPENFIGI_KEY) per il resto, dai CUSIP più pesanti
   cache: data/13f/cusip_map.csv (cusip, ticker, name, sec_type, src); i non trovati restano con ticker vuoto
2. Prezzi settimanali rettificati (dividendi e split, yfinance) per i ticker mappati + SPY:
   data/13f/prices_weekly.csv.gz (ticker, date, close); il primo giro scarica dal 2013, poi solo l'ultimo trimestre.
"""
import argparse, os, time
from pathlib import Path
import pandas as pd
import requests

ROOT = Path(__file__).parent
D13 = ROOT / "data" / "13f"
MAP = D13 / "cusip_map.csv"
PX = D13 / "prices_weekly.csv.gz"
BENCH = "SPY"


def all_holdings(cols=("cik", "period", "cusip", "issuer", "value", "putcall", "sh_type")):
    fr = []
    for p in sorted((D13 / "holdings").glob("*/*.csv.gz")):
        fr.append(pd.read_csv(p, dtype={"cik": str, "cusip": str}, usecols=lambda c: c in cols))
    return pd.concat(fr) if fr else pd.DataFrame(columns=list(cols))


def load_map():
    if MAP.exists(): return pd.read_csv(MAP, dtype=str)
    return pd.DataFrame(columns=["cusip", "ticker", "name", "sec_type", "src"])


def from_universe():
    u = pd.read_csv(ROOT / "universe.csv", dtype=str)
    rows = []
    us = u[(u["panel"] == "US") & u["isin"].fillna("").str.match(r"^US[0-9A-Z]{9}\d$")]
    for r in us.itertuples():
        rows.append({"cusip": r.isin[2:11], "ticker": r.ticker, "name": r.name, "sec_type": "Common Stock",
                     "src": "universe"})
    return pd.DataFrame(rows)


def openfigi(cusips, key=None):
    url = "https://api.openfigi.com/v3/mapping"
    hdr = {"Content-Type": "application/json"}
    if key: hdr["X-OPENFIGI-APIKEY"] = key
    batch, pause = (100, 6.5 / 25) if key else (10, 60 / 24)
    out = []
    for k in range(0, len(cusips), batch):
        part = cusips[k:k + batch]
        jobs = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in part]
        for attempt in range(5):
            r = requests.post(url, json=jobs, headers=hdr, timeout=60)
            if r.status_code == 429: time.sleep(10 * (attempt + 1)); continue
            break
        if r.status_code != 200:
            print("openfigi errore", r.status_code, r.text[:200]); time.sleep(pause); continue
        for c, res in zip(part, r.json()):
            d = (res.get("data") or [None])[0]
            if d and d.get("ticker"):
                out.append({"cusip": c, "ticker": d["ticker"].replace("/", "-").replace(" ", "-"),
                            "name": d.get("name"), "sec_type": d.get("securityType2") or d.get("securityType"),
                            "src": "openfigi"})
            else:
                out.append({"cusip": c, "ticker": "", "name": None, "sec_type": None, "src": "openfigi-none"})
        time.sleep(pause)
    return pd.DataFrame(out)


def build_map(max_new):
    h = all_holdings()
    m = load_map()
    uni = from_universe()
    m = pd.concat([m, uni[~uni["cusip"].isin(m["cusip"])]]) if len(uni) else m
    # CUSIP da mappare, in ordine di importanza (valore totale detenuto, solo azioni/ETF senza opzioni)
    eq = h[(h["sh_type"].fillna("SH") == "SH")]
    w = eq.groupby("cusip")["value"].sum().sort_values(ascending=False)
    todo = [c for c in w.index if c not in set(m["cusip"])][:max_new]
    print(f"CUSIP distinti: {len(w)}, già mappati: {w.index.isin(m['cusip']).sum()}, da cercare ora: {len(todo)}")
    if todo:
        m = pd.concat([m, openfigi(todo, os.environ.get("OPENFIGI_KEY"))])
    m = m.drop_duplicates("cusip", keep="first")
    MAP.parent.mkdir(parents=True, exist_ok=True)
    m.to_csv(MAP, index=False)
    cov = w[w.index.isin(m.loc[m["ticker"].fillna("") != "", "cusip"])].sum() / w.sum() if w.sum() else 0
    print(f"quota di valore con ticker: {cov:.1%}")
    return m


def prices(m):
    import yfinance as yf
    tick = sorted(set(m.loc[m["ticker"].fillna("") != "", "ticker"]) | {BENCH})
    old = pd.read_csv(PX) if PX.exists() else pd.DataFrame(columns=["ticker", "date", "close"])
    have = set(old["ticker"])
    fresh = [t for t in tick if t not in have]
    frames = []
    def dl(ts, start):
        for k in range(0, len(ts), 150):
            ch = ts[k:k + 150]
            try:
                d = yf.download(ch, start=start, interval="1wk", auto_adjust=True, progress=False,
                                threads=True, group_by="column")
                c = d["Close"]
                if isinstance(c, pd.Series): c = c.to_frame(ch[0])
                c = c.stack().reset_index(); c.columns = ["date", "ticker", "close"]
                frames.append(c)
            except Exception as e:
                print("prezzi: blocco fallito", e)
    if fresh: dl(fresh, "2013-01-01")
    upd = [t for t in tick if t in have]
    # con auto_adjust i prezzi passati cambiano a ogni dividendo: una volta al mese si riscarica tutto
    if upd:
        full = pd.Timestamp.today().day <= 7
        dl(upd, "2013-01-01" if full else (pd.Timestamp.today() - pd.Timedelta(days=120)).date().isoformat())
    if frames:
        p = pd.concat(frames).dropna()
        p["date"] = pd.to_datetime(p["date"]).dt.date.astype(str); p["close"] = p["close"].astype(float).round(4)
        allp = pd.concat([old, p[["ticker", "date", "close"]]]).drop_duplicates(["ticker", "date"], keep="last")
        allp.sort_values(["ticker", "date"]).to_csv(PX, index=False, compression="gzip")
        print(f"prezzi: {allp['ticker'].nunique()} ticker, {len(allp)} righe")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new", type=int, default=3000)
    ap.add_argument("--no-prices", action="store_true")
    a = ap.parse_args()
    m = build_map(a.max_new)
    if not a.no_prices: prices(m)
