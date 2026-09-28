"""Raccolta notturna rating + revisioni. Gira su GitHub Actions, committa CSV.

Perché qui e non nel cloud Anthropic: lì Yahoo/Finnhub/FMP sono bloccati dal proxy
aziendale. Actions ha rete libera, è gratis sui repo pubblici e gira col Mac spento.
I dati sono tutti pubblici, quindi il repo può stare pubblico senza problemi.
"""
import argparse, datetime as dt, sys, time
from pathlib import Path
import pandas as pd

GRADES = {
 1:["strong buy","conviction buy","top pick"],
 2:["buy","outperform","overweight","accumulate","add","positive","sector outperform",
    "market outperform","long-term buy","moderate buy","outperformer"],
 3:["hold","neutral","market perform","sector perform","equal-weight","equal weight",
    "in-line","in line","peer perform","perform","market weight","sector weight","mixed"],
 4:["underperform","underweight","reduce","sector underperform","market underperform",
    "negative","underperformer","cautious","trim"],
 5:["sell","strong sell","conviction sell"]}
LOOK = {g:n for n,gs in GRADES.items() for g in gs}

def grade(x):
    if not isinstance(x,str) or not x.strip(): return None
    s = " ".join(x.strip().lower().split())
    if s in LOOK: return LOOK[s]
    for k,v in LOOK.items():
        if len(k)>4 and k in s: return v
    return None                      # mai default a 3: creerebbe upgrade/downgrade finti

# indici di riferimento (ticker Yahoo) per misurare i rendimenti in eccesso
BENCH = {"US": "SPY", "UKX": "^FTSE", "MCX": "^FTMC", "IT": "FTSEMIB.MI", "DE": "^GDAXI",
         "FR": "^FCHI", "EU": "^STOXX50E"}


def bench_for(panel, indices):
    idx = str(indices or "")
    if panel == "US": return "US"
    if panel == "UK": return "MCX" if "MCX" in idx else "UKX"
    if panel in ("IT", "DE", "FR"): return panel
    return "EU"


def pt(x):
    try:
        v = float(x)
        return round(v, 4) if v > 0 else None
    except (TypeError, ValueError):
        return None


def rating_rows(t, ud, limit, src, unmapped=None):
    """Righe rating da un DataFrame upgrades_downgrades di yfinance.
    src = listing da cui arriva il dato (il titolo stesso o la sua quotazione USA/ADR)."""
    out = []
    if ud is None or not len(ud): return out
    d = ud.reset_index()
    if limit: d = d.head(limit)
    dc = next((c for c in d.columns if "date" in str(c).lower()), d.columns[0])
    for _, r in d.iterrows():
        fg, tg = r.get("FromGrade"), r.get("ToGrade")
        if unmapped is not None:
            unmapped.update(x for x in (fg, tg) if isinstance(x, str) and x.strip() and grade(x) is None)
        out.append({"ticker": t, "event_date": pd.to_datetime(r[dc]).date().isoformat(),
                    "broker": r.get("Firm"), "action": str(r.get("Action", "")).lower(),
                    "rating_from": fg, "rating_to": tg,
                    "rating_from_num": grade(fg), "rating_to_num": grade(tg),
                    # target price dell'analisi (yfinance >= 0.2.44); 0 = non fornito
                    "pt_action": r.get("priceTargetAction"),
                    "pt_to": pt(r.get("currentPriceTarget")), "pt_from": pt(r.get("priorPriceTarget")),
                    "src": src})
    return out


def closes(tickers, out, today):
    """Chiusure giornaliere. Primo giro: 1 anno di storico; poi gli ultimi 5 giorni
    (così una notte saltata si recupera). File append-only data/prices/YYYY-MM-DD.csv."""
    import yfinance as yf
    pdir = out / "prices"; pdir.mkdir(parents=True, exist_ok=True)
    period = "5d" if any(pdir.glob("*.csv")) else "1y"
    frames = []
    for k in range(0, len(tickers), 200):
        chunk = tickers[k:k+200]
        try:
            d = yf.download(chunk, period=period, auto_adjust=False, progress=False,
                            threads=True, group_by="column")
            c = d["Close"] if "Close" in d else None
            if c is None: continue
            if isinstance(c, pd.Series): c = c.to_frame(chunk[0])
            c = c.stack().reset_index()
            c.columns = ["date", "ticker", "close"]
            frames.append(c)
        except Exception as e:
            print("prezzi: blocco fallito", e)
    if not frames: return 0
    p = pd.concat(frames).dropna()
    p["date"] = pd.to_datetime(p["date"]).dt.date.astype(str)
    p["close"] = p["close"].round(4)
    p[["ticker", "date", "close"]].sort_values(["ticker", "date"]).to_csv(pdir / f"{today}.csv", index=False)
    return p["ticker"].nunique()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="universe.csv")
    ap.add_argument("--out", default="data")
    ap.add_argument("--sleep", type=float, default=0.35)
    a = ap.parse_args()

    import yfinance as yf
    u = pd.read_csv(a.universe)
    tickers = u["ticker"].dropna().astype(str).tolist()
    # quotazione USA/ADR dei titoli europei: su Yahoo i broker spesso sono registrati solo lì
    usmap = {r.ticker: r.us_ticker for r in u.itertuples() if isinstance(getattr(r, "us_ticker", None), str) and r.us_ticker}
    today = dt.date.today().isoformat()
    rat, rev, tgt, unmapped, failed = [], [], [], set(), []

    for i, t in enumerate(tickers, 1):
        try:
            tk = yf.Ticker(t)
            for src, sym in ((t, t), (usmap.get(t), usmap.get(t))):
                if not sym: continue
                ud = tk.upgrades_downgrades if sym == t else yf.Ticker(sym).upgrades_downgrades
                rat.extend(rating_rows(t, ud, 40, src, unmapped))
            nest = {}
            try:                                   # numero di stime per esercizio -> breadth corretta
                ee = tk.earnings_estimate
                if ee is not None and len(ee):
                    for per, r in ee.iterrows():
                        nest[str(per).strip().lower()] = r.get("numberOfAnalysts")
            except Exception:
                pass
            try:                                   # target di consenso (media/mediana/min/max)
                apt = tk.analyst_price_targets or {}
                if apt:
                    tgt.append({"ticker":t,"as_of":today,"pt_mean":pt(apt.get("mean")),
                                "pt_median":pt(apt.get("median")),"pt_high":pt(apt.get("high")),
                                "pt_low":pt(apt.get("low")),"price":pt(apt.get("current"))})
            except Exception:
                pass
            er = tk.eps_revisions
            if er is not None and len(er):
                e = er.reset_index()
                pc = e.columns[0]
                for _, r in e.iterrows():
                    per = str(r[pc]).strip().lower()
                    if per not in ("0y","+1y"): continue
                    rev.append({"ticker":t,"as_of":today,"fy":1 if per=="0y" else 2,
                                "n_up_30d":r.get("upLast30days"),"n_down_30d":r.get("downLast30days"),
                                "n_est":nest.get(per)})
        except Exception:
            failed.append(t)
        if i % 50 == 0:
            print(f"{i}/{len(tickers)}", flush=True)
        time.sleep(a.sleep)

    out = Path(a.out); (out/"ratings").mkdir(parents=True, exist_ok=True); (out/"revisions").mkdir(exist_ok=True)
    pd.DataFrame(rat).to_csv(out/"ratings"/f"{today}.csv", index=False)
    pd.DataFrame(rev).to_csv(out/"revisions"/f"{today}.csv", index=False)
    (out/"targets").mkdir(exist_ok=True)
    pd.DataFrame(tgt, columns=["ticker","as_of","pt_mean","pt_median","pt_high","pt_low","price"]
                 ).to_csv(out/"targets"/f"{today}.csv", index=False)
    n_px = closes(tickers + sorted(set(usmap.values())) + list(BENCH.values()), out, today)
    pd.DataFrame({"run_date":[today],"tickers":[len(tickers)],"failed":[len(failed)],
                  "rating_rows":[len(rat)],"revision_rows":[len(rev)],
                  "target_rows":[len(tgt)],"price_tickers":[n_px],
                  "pt_events":[sum(1 for r in rat if r["pt_to"])],
                  "unmapped_grades":["; ".join(sorted(unmapped))]}).to_csv(out/"latest_health.csv", index=False)
    print(f"ok: {len(rat)} rating ({sum(1 for r in rat if r['pt_to'])} con target), {len(rev)} revisioni, "
          f"{len(tgt)} target consenso, prezzi per {n_px} titoli, {len(failed)} ticker falliti")

if __name__ == "__main__":
    main()
