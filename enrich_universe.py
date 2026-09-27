"""Completa universe.csv con nome esteso, ISIN, valuta e borsa (da yfinance).

Serve alla ricerca per nome/ISIN nel terminale. Lavora solo sui titoli a cui manca
qualcosa, fino a --max per giro, così può girare anche ogni notte in coda allo snapshot
senza allungarlo troppo: i buchi si chiudono da soli nel giro di qualche notte.
"""
import argparse, time
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="universe.csv")
    ap.add_argument("--max", type=int, default=2000)
    ap.add_argument("--sleep", type=float, default=0.3)
    a = ap.parse_args()
    import yfinance as yf

    u = pd.read_csv(a.universe, dtype=str)
    for c in ("isin", "currency", "exchange", "long_name", "name"):
        if c not in u.columns: u[c] = None
    todo = u.index[u["isin"].isna() | u["long_name"].isna() | u["currency"].isna()].tolist()[: a.max]
    print(f"da completare: {len(todo)}")
    ok = 0
    for n, i in enumerate(todo, 1):
        t = u.at[i, "ticker"]
        try:
            tk = yf.Ticker(t)
            info = {}
            try: info = tk.info or {}
            except Exception: pass
            ln = info.get("longName") or info.get("shortName")
            if ln: u.at[i, "long_name"] = ln
            if pd.isna(u.at[i, "name"]) and ln: u.at[i, "name"] = ln
            if info.get("currency"): u.at[i, "currency"] = info["currency"]
            if info.get("exchange"): u.at[i, "exchange"] = info["exchange"]
            if pd.isna(u.at[i, "isin"]):
                try:
                    isin = tk.isin
                    if isinstance(isin, str) and len(isin) == 12 and isin[:2].isalpha():
                        u.at[i, "isin"] = isin
                    else:
                        u.at[i, "isin"] = "-"          # non trovato: non ritentare ogni notte
                except Exception:
                    pass
            ok += 1
        except Exception as e:
            print(t, "errore", e)
        if n % 50 == 0:
            print(f"{n}/{len(todo)}", flush=True)
            u.to_csv(a.universe, index=False)
        time.sleep(a.sleep)
    u.to_csv(a.universe, index=False)
    print(f"ok: {ok} titoli aggiornati; ISIN presenti: {(u['isin'].notna() & (u['isin'] != '-')).sum()}/{len(u)}")


if __name__ == "__main__":
    main()
