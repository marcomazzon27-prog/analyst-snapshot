"""Genera universe.csv da S&P 500 + FTSE 100 + FTSE 250.

Wikipedia risponde 403 allo user-agent di default di pandas/urllib, quindi la
pagina va scaricata con requests e un UA esplicito, e solo dopo passata a read_html.
"""
import io
import pandas as pd
import requests

H = {"User-Agent": "analyst-snapshot/1.0 (GitHub Actions; contatto via issue del repo)"}
W = "https://en.wikipedia.org/wiki/"
rows = []

def grab(page, suffix, panel, tier, minrows=50):
    try:
        r = requests.get(W + page, headers=H, timeout=30)
        r.raise_for_status()
        tabs = pd.read_html(io.StringIO(r.text))
    except Exception as e:
        print(f"{page}: fallito ({e})")
        return
    for t in tabs:
        cols = {str(c).strip().lower(): c for c in t.columns}
        hit = next((cols[c] for c in cols if c in ("ticker", "symbol", "epic", "code")), None)
        if hit is None or len(t) < minrows:
            continue
        n = 0
        for x in t[hit].astype(str).str.strip().str.replace(".", "-", regex=False):
            if x and x.lower() != "nan" and len(x) <= 8:
                rows.append({"ticker": x + suffix, "panel": panel, "cap_tier": tier})
                n += 1
        print(f"{page}: {n} ticker")
        return
    print(f"{page}: nessuna tabella usabile")

grab("List_of_S%26P_500_companies", "", "US", "large")
grab("FTSE_100_Index", ".L", "UK", "large")
grab("FTSE_250_Index", ".L", "UK", "mid")

df = pd.DataFrame(rows).drop_duplicates("ticker")
if df.empty:
    raise SystemExit("nessun ticker raccolto - controlla i log qui sopra")
df.to_csv("universe.csv", index=False)
print(f"\n{len(df)} ticker -> universe.csv")
print(df.groupby(["panel", "cap_tier"]).size().to_string())
