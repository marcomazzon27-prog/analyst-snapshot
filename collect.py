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
    "in-line","in line","peer perform","perform","market weight","sector weight"],
 4:["underperform","underweight","reduce","sector underperform","market underperform"],
 5:["sell","strong sell","conviction sell"]}
LOOK = {g:n for n,gs in GRADES.items() for g in gs}

def grade(x):
    if not isinstance(x,str) or not x.strip(): return None
    s = " ".join(x.strip().lower().split())
    if s in LOOK: return LOOK[s]
    for k,v in LOOK.items():
        if len(k)>4 and k in s: return v
    return None                      # mai default a 3: creerebbe upgrade/downgrade finti

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="universe.csv")
    ap.add_argument("--out", default="data")
    ap.add_argument("--sleep", type=float, default=0.35)
    a = ap.parse_args()

    import yfinance as yf
    u = pd.read_csv(a.universe)
    tickers = u["ticker"].dropna().astype(str).tolist()
    today = dt.date.today().isoformat()
    rat, rev, unmapped, failed = [], [], set(), []

    for i, t in enumerate(tickers, 1):
        try:
            tk = yf.Ticker(t)
            ud = tk.upgrades_downgrades
            if ud is not None and len(ud):
                d = ud.reset_index().head(40)
                dc = next((c for c in d.columns if "date" in str(c).lower()), d.columns[0])
                for _, r in d.iterrows():
                    fg, tg = r.get("FromGrade"), r.get("ToGrade")
                    unmapped.update(x for x in (fg,tg) if isinstance(x,str) and x.strip() and grade(x) is None)
                    rat.append({"ticker":t,"event_date":pd.to_datetime(r[dc]).date().isoformat(),
                                "broker":r.get("Firm"),"action":str(r.get("Action","")).lower(),
                                "rating_from":fg,"rating_to":tg,
                                "rating_from_num":grade(fg),"rating_to_num":grade(tg)})
            er = tk.eps_revisions
            if er is not None and len(er):
                e = er.reset_index()
                pc = e.columns[0]
                for _, r in e.iterrows():
                    per = str(r[pc]).strip().lower()
                    if per not in ("0y","+1y"): continue
                    rev.append({"ticker":t,"as_of":today,"fy":1 if per=="0y" else 2,
                                "n_up_30d":r.get("upLast30days"),"n_down_30d":r.get("downLast30days")})
        except Exception:
            failed.append(t)
        if i % 50 == 0:
            print(f"{i}/{len(tickers)}", flush=True)
        time.sleep(a.sleep)

    out = Path(a.out); (out/"ratings").mkdir(parents=True, exist_ok=True); (out/"revisions").mkdir(exist_ok=True)
    pd.DataFrame(rat).to_csv(out/"ratings"/f"{today}.csv", index=False)
    pd.DataFrame(rev).to_csv(out/"revisions"/f"{today}.csv", index=False)
    pd.DataFrame({"run_date":[today],"tickers":[len(tickers)],"failed":[len(failed)],
                  "rating_rows":[len(rat)],"revision_rows":[len(rev)],
                  "unmapped_grades":["; ".join(sorted(unmapped))]}).to_csv(out/"latest_health.csv", index=False)
    print(f"ok: {len(rat)} rating, {len(rev)} revisioni, {len(failed)} ticker falliti")

if __name__ == "__main__":
    main()
