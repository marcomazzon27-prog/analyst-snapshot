"""Fondamentali per titolo (Yahoo Finance via yfinance): capitalizzazione, multipli e bilanci.

Quando si aggiorna un titolo (workflow 5, ogni giorno feriale, al massimo --max titoli per giro)
  1. non ha ancora dati
  2. ha pubblicato i conti: il giorno dopo la data di trimestrale (e fino a 5 giorni dopo, se Yahoo è in ritardo)
  3. i dati hanno più di 30 giorni
Output: data/fundamentals/<TICKER>.json
  info       capitalizzazione, azioni, EV, P/E, P/E forward, P/B, rendimento dividendo, beta, margini, settore
  annual     ultimi 4 esercizi   · quarterly  ultimi 6 trimestri
             ricavi, utile lordo, EBIT, EBITDA, utile netto, EPS diluito, totale attivo, patrimonio netto,
             debito, cassa, debito netto, flusso di cassa operativo, capex, free cash flow
  earnings   ultime date di trimestrale con EPS stimato, riportato e sorpresa; prossima data
La capitalizzazione giornaliera la calcola build_db.py: azioni in circolazione × ultima chiusura.
"""
import argparse, datetime as dt, json, math, time
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).parent
OUT = ROOT / "data" / "fundamentals"
STALE_DAYS = 30

ROWS = {  # etichetta nostra -> possibili nomi di riga in yfinance (il primo trovato vince)
    "revenue": ["Total Revenue", "Operating Revenue"],
    "gross_profit": ["Gross Profit"],
    "ebit": ["Operating Income", "EBIT"],
    "ebitda": ["EBITDA", "Normalized EBITDA"],
    "net_income": ["Net Income Common Stockholders", "Net Income"],
    "eps": ["Diluted EPS", "Basic EPS"],
    "total_assets": ["Total Assets"],
    "equity": ["Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"],
    "debt": ["Total Debt"],
    "cash": ["Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"],
    "net_debt": ["Net Debt"],
    "cfo": ["Operating Cash Flow"],
    "capex": ["Capital Expenditure"],
    "fcf": ["Free Cash Flow"],
}
INFO = ["marketCap", "sharesOutstanding", "enterpriseValue", "trailingPE", "forwardPE", "priceToBook",
        "dividendYield", "beta", "enterpriseToEbitda", "profitMargins", "operatingMargins", "returnOnEquity",
        "revenueGrowth", "earningsGrowth", "debtToEquity", "currency", "financialCurrency", "sector", "industry",
        "fullTimeEmployees", "trailingEps", "forwardEps", "bookValue", "payoutRatio"]


def clean(v):
    if v is None: return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and not math.isfinite(v)) else (round(v, 6) if isinstance(v, float) else v)
    if isinstance(v, (pd.Timestamp, dt.date)): return str(v)[:10]
    return str(v)


def statements(frames, n):
    """frames: lista di DataFrame yfinance (righe = voci, colonne = date). -> {periods, <voce>: [valori]}"""
    cols = sorted({c for f in frames if f is not None and not f.empty for c in f.columns}, reverse=True)[:n]
    if not cols: return None
    out = {"periods": [str(pd.Timestamp(c).date()) for c in cols]}
    for k, names in ROWS.items():
        vals = None
        for f in frames:
            if f is None or f.empty: continue
            nm = next((x for x in names if x in f.index), None)
            if nm is None: continue
            s = f.loc[nm]
            vals = [clean(float(s[c])) if c in s.index and pd.notna(s[c]) else None for c in cols]
            break
        if vals and any(v is not None for v in vals): out[k] = vals
    # debito netto e FCF calcolati se Yahoo non li dà
    if "net_debt" not in out and "debt" in out and "cash" in out:
        out["net_debt"] = [None if d is None or c is None else d - c for d, c in zip(out["debt"], out["cash"])]
    if "fcf" not in out and "cfo" in out and "capex" in out:
        out["fcf"] = [None if a is None or b is None else a + b for a, b in zip(out["cfo"], out["capex"])]
    return out


def earnings(tk):
    out = {"history": [], "next": None}
    try:
        ed = tk.get_earnings_dates(limit=12)
        if ed is not None and not ed.empty:
            today = pd.Timestamp.now(tz=ed.index.tz).normalize()
            for ts, r in ed.sort_index().iterrows():
                d = str(ts.date())
                rep = r.get("Reported EPS"); est = r.get("EPS Estimate"); sur = r.get("Surprise(%)")
                if ts.normalize() > today:
                    if out["next"] is None: out["next"] = d
                elif pd.notna(rep) or pd.notna(est):
                    out["history"].append([d, clean(est if pd.notna(est) else None), clean(rep if pd.notna(rep) else None),
                                           clean(sur if pd.notna(sur) else None)])
            out["history"] = out["history"][-8:]
    except Exception as e:
        print("  date trimestrali n.d.:", str(e)[:80])
    if out["next"] is None:
        try:
            cal = tk.calendar or {}
            nd = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if nd: out["next"] = str(nd[0])[:10]
        except Exception:
            pass
    return out


def fetch(t):
    import yfinance as yf
    tk = yf.Ticker(t)
    info = {}
    try:
        raw = tk.info or {}
        info = {k: clean(raw.get(k)) for k in INFO if raw.get(k) is not None}
    except Exception as e:
        print("  info n.d.:", str(e)[:80])
    if "sharesOutstanding" not in info:
        try:
            fi = tk.fast_info
            if fi.get("shares"): info["sharesOutstanding"] = int(fi["shares"])
            if fi.get("market_cap"): info.setdefault("marketCap", clean(float(fi["market_cap"])))
        except Exception:
            pass
    g = lambda a: getattr(tk, a, None)
    rec = {"ticker": t, "fetched": dt.date.today().isoformat(), "info": info,
           "annual": statements([g("income_stmt"), g("balance_sheet"), g("cashflow")], 4),
           "quarterly": statements([g("quarterly_income_stmt"), g("quarterly_balance_sheet"), g("quarterly_cashflow")], 6),
           "earnings": earnings(tk)}
    return rec


def due(rec, today):
    """Motivo per aggiornare il titolo oggi (None = non serve)."""
    if rec is None: return "nuovo"
    f = rec.get("fetched", "2000-01-01")
    ern = rec.get("earnings") or {}
    dates = [h[0] for h in ern.get("history", [])] + ([ern["next"]] if ern.get("next") else [])
    for d in dates:                  # conti pubblicati: si aggiorna dal giorno dopo (per 5 giorni se Yahoo tarda)
        try: d1 = dt.date.fromisoformat(d) + dt.timedelta(days=1)
        except ValueError: continue
        if d1 <= today <= d1 + dt.timedelta(days=5) and f < d1.isoformat(): return "trimestrale"
    if (today - dt.date.fromisoformat(f)).days >= STALE_DAYS: return "mensile"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="universe.csv")
    ap.add_argument("--max", type=int, default=350)
    ap.add_argument("--sleep", type=float, default=0.4)
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    u = pd.read_csv(ROOT / a.universe, dtype=str)
    today = dt.date.today()
    todo = []
    for t in (a.only or u["ticker"].tolist()):
        p = OUT / f"{t}.json"
        rec = json.loads(p.read_text()) if p.exists() else None
        why = due(rec, today)
        if why: todo.append((0 if why == "trimestrale" else 1 if why == "nuovo" else 2, rec.get("fetched", "") if rec else "", t, why))
    todo.sort()
    print(f"da aggiornare: {len(todo)} (trimestrale {sum(x[3] == 'trimestrale' for x in todo)}, "
          f"nuovi {sum(x[3] == 'nuovo' for x in todo)}, mensili {sum(x[3] == 'mensile' for x in todo)}); in questo giro max {a.max}")
    ok = fail = 0
    for _, _, t, why in todo[:a.max]:
        try:
            rec = fetch(t)
            if not rec["info"] and not rec["annual"] and not rec["quarterly"]:
                raise ValueError("nessun dato")
            (OUT / f"{t}.json").write_text(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
            ok += 1
        except Exception as e:
            fail += 1; print(f"{t}: errore ({why}) {str(e)[:100]}")
        time.sleep(a.sleep)
    print(f"aggiornati {ok}, falliti {fail}")


if __name__ == "__main__":
    main()
