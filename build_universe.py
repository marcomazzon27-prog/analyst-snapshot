"""Genera universe.csv da S&P 500 + FTSE 100 + FTSE 250. Da lanciare una volta."""
import pandas as pd
W = "https://en.wikipedia.org/wiki/"
rows = []
def grab(page, col, suffix, panel, tier):
    try: tabs = pd.read_html(W+page)
    except Exception as e: print(page, "fallito", e); return
    for t in tabs:
        cols = {str(c).strip().lower(): c for c in t.columns}
        hit = next((cols[c] for c in cols if c in (col.lower(),"ticker","symbol","epic")), None)
        if hit is None or len(t) < 50: continue
        tk = t[hit].astype(str).str.strip().str.replace(".","-",regex=False)
        rows.extend({"ticker":x+suffix,"panel":panel,"cap_tier":tier} for x in tk if x and x.lower()!="nan")
        print(f"{page}: {len(tk)}"); return
    print(page, "nessuna tabella usabile")

grab("List_of_S%26P_500_companies","Symbol","","US","large")
grab("FTSE_100_Index","Ticker",".L","UK","large")
grab("FTSE_250_Index","Ticker",".L","UK","mid")
df = pd.DataFrame(rows).drop_duplicates("ticker")
df.to_csv("universe.csv", index=False)
print(f"\n{len(df)} ticker -> universe.csv")
print(df.groupby(["panel","cap_tier"]).size().to_string())
