"""Scarica una volta sola 3 anni di chiusure (universo + indici) in data/prices_backfill.csv.gz.

Serve alla classifica dei broker: per giudicare un'analisi di un anno fa servono i prezzi
di quel giorno e dei 12 mesi successivi. Se il file esiste già non fa nulla.
"""
from pathlib import Path
import pandas as pd

OUT = Path("data/prices_backfill.csv.gz")


def main():
    if OUT.exists():
        print("backfill già presente"); return
    import yfinance as yf
    from collect import BENCH
    tickers = pd.read_csv("universe.csv")["ticker"].dropna().astype(str).tolist() + list(BENCH.values())
    frames = []
    for k in range(0, len(tickers), 150):
        chunk = tickers[k:k + 150]
        try:
            d = yf.download(chunk, period="3y", auto_adjust=False, progress=False, threads=True, group_by="column")
            c = d["Close"]
            if isinstance(c, pd.Series): c = c.to_frame(chunk[0])
            c = c.stack().reset_index(); c.columns = ["date", "ticker", "close"]
            frames.append(c)
        except Exception as e:
            print("blocco fallito", e)
    p = pd.concat(frames).dropna()
    p["date"] = pd.to_datetime(p["date"]).dt.date.astype(str)
    p["close"] = p["close"].astype(float).round(4)
    p[["ticker", "date", "close"]].sort_values(["ticker", "date"]).to_csv(OUT, index=False)
    print(f"backfill: {len(p)} righe, {p['ticker'].nunique()} titoli")


if __name__ == "__main__":
    main()
