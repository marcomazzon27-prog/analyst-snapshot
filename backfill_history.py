"""Storico completo, scaricato una volta sola, per rendere subito significativa la classifica broker.

1. Rating: tutta la cronologia upgrade/downgrade/target che Yahoo conserva per ogni titolo
   (lo snapshot notturno ne tiene solo le ultime 40) -> data/ratings_backfill.csv.gz
2. Prezzi: 10 anni di chiusure per universo e indici -> data/prices_backfill.csv.gz
   (sostituisce il file da 3 anni se è più corto)

Nota: le analisi storiche non sono "point-in-time" (vengono marcate is_backfill=1) e l'universo
è quello attuale: i titoli usciti dagli indici mancano (distorsione da sopravvivenza).
Uso: python backfill_history.py [--force]
"""
import argparse, time
from pathlib import Path
import pandas as pd
from collect import BENCH, grade, pt

RAT = Path("data/ratings_backfill.csv.gz")
PX = Path("data/prices_backfill.csv.gz")
YEARS = 10


def ratings(tickers, sleep):
    import yfinance as yf
    rows, failed = [], []
    for i, t in enumerate(tickers, 1):
        try:
            ud = yf.Ticker(t).upgrades_downgrades
            if ud is not None and len(ud):
                d = ud.reset_index()
                dc = next((c for c in d.columns if "date" in str(c).lower()), d.columns[0])
                for _, r in d.iterrows():
                    fg, tg = r.get("FromGrade"), r.get("ToGrade")
                    rows.append({"ticker": t, "event_date": pd.to_datetime(r[dc]).date().isoformat(),
                                 "broker": r.get("Firm"), "action": str(r.get("Action", "")).lower(),
                                 "rating_from": fg, "rating_to": tg,
                                 "rating_from_num": grade(fg), "rating_to_num": grade(tg),
                                 "pt_action": r.get("priceTargetAction"),
                                 "pt_to": pt(r.get("currentPriceTarget")), "pt_from": pt(r.get("priorPriceTarget"))})
        except Exception:
            failed.append(t)
        if i % 100 == 0: print(f"rating {i}/{len(tickers)}", flush=True)
        time.sleep(sleep)
    df = pd.DataFrame(rows)
    df.to_csv(RAT, index=False)
    print(f"rating storici: {len(df)} righe, {df['ticker'].nunique()} titoli, dal {df['event_date'].min()}; falliti {len(failed)}")


def prices(tickers):
    import yfinance as yf
    frames = []
    for k in range(0, len(tickers), 100):
        chunk = tickers[k:k + 100]
        try:
            d = yf.download(chunk, period=f"{YEARS}y", auto_adjust=False, progress=False, threads=True, group_by="column")
            c = d["Close"]
            if isinstance(c, pd.Series): c = c.to_frame(chunk[0])
            c = c.stack().reset_index(); c.columns = ["date", "ticker", "close"]
            frames.append(c)
        except Exception as e:
            print("prezzi: blocco fallito", e)
    p = pd.concat(frames).dropna()
    p["date"] = pd.to_datetime(p["date"]).dt.date.astype(str)
    p["close"] = p["close"].astype(float).round(4)
    p[["ticker", "date", "close"]].sort_values(["ticker", "date"]).to_csv(PX, index=False)
    print(f"prezzi storici: {len(p)} righe, {p['ticker'].nunique()} titoli, dal {p['date'].min()}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.3)
    a = ap.parse_args()
    tickers = pd.read_csv("universe.csv")["ticker"].dropna().astype(str).tolist()
    if a.force or not RAT.exists():
        ratings(tickers, a.sleep)
    need_px = a.force or not PX.exists()
    if not need_px:
        mn = pd.read_csv(PX, usecols=["date"])["date"].min()
        need_px = pd.Timestamp(mn) > pd.Timestamp.today() - pd.DateOffset(years=YEARS - 1)
    if need_px:
        prices(tickers + list(BENCH.values()))


if __name__ == "__main__":
    main()
