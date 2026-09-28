"""Completa universe.csv con nome esteso, ISIN, valuta e borsa (da yfinance).

Serve alla ricerca per nome/ISIN nel terminale. Lavora solo sui titoli a cui manca
qualcosa, fino a --max per giro, così può girare anche ogni notte in coda allo snapshot
senza allungarlo troppo: i buchi si chiudono da soli nel giro di qualche notte.
"""
import argparse, re, time
import pandas as pd
import requests

# borse Wikidata (etichetta inglese) accettate per ciascun suffisso Yahoo
EXCH = {"": ("New York Stock Exchange", "Nasdaq"), ".L": ("London Stock Exchange",),
        ".MI": ("Italian Stock Exchange", "Borsa Italiana", "Euronext Milan"), ".DE": ("Frankfurt Stock Exchange", "Xetra"),
        ".PA": ("Euronext Paris",), ".AS": ("Amsterdam Stock Exchange", "Euronext Amsterdam"),
        ".MC": ("Madrid Stock Exchange", "Bolsa de Madrid"), ".BR": ("Euronext Brussels",),
        ".HE": ("Nasdaq Helsinki Ltd",), ".IR": ("Euronext Dublin", "Irish Stock Exchange"),
        ".LS": ("Euronext Lisbon",)}
CC = {"": "US", ".L": "GB", ".MI": "IT", ".DE": "DE", ".PA": "FR", ".AS": "NL", ".MC": "ES",
      ".BR": "BE", ".HE": "FI", ".IR": "IE", ".LS": "PT"}
# paesi di domicilio plausibili per titoli quotati su queste borse (yfinance a volte
# restituisce l'ISIN di un omonimo estero: JP..., CA... ecc. vanno scartati)
OK_CC = {"US", "GB", "IT", "DE", "FR", "NL", "ES", "BE", "FI", "IE", "PT", "LU", "JE", "GG", "IM",
         "CH", "BM", "KY", "AT", "DK", "SE", "NO", "PA", "CW"}
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
key = lambda x: re.sub(r"[^A-Z0-9]", "", str(x).upper())


def split(t):
    m = re.match(r"^(.*?)(\.[A-Z]{1,2})?$", t)
    return m.group(1), (m.group(2) or "")


def wikidata_isins():
    """{(ticker_normalizzato, borsa): [isin, ...]} da Wikidata (una sola query, pochi secondi)."""
    q = """SELECT ?isin ?ticker ?exl WHERE { ?item p:P414 ?st . ?st ps:P414 ?ex ; pq:P249 ?ticker .
           ?item wdt:P946 ?isin . ?ex rdfs:label ?exl . FILTER(lang(?exl)="en") }"""
    r = requests.get("https://query.wikidata.org/sparql", params={"query": q, "format": "json"},
                     headers={"User-Agent": "analyst-snapshot/1.0 (GitHub Actions)",
                              "Accept": "application/sparql-results+json"}, timeout=90)
    r.raise_for_status()
    out = {}
    for b in r.json()["results"]["bindings"]:
        out.setdefault((key(b["ticker"]["value"]), b["exl"]["value"]), set()).add(b["isin"]["value"])
    return out


STOP = {"plc", "spa", "sa", "se", "ag", "nv", "n", "v", "p", "a", "s", "group", "holdings", "holding", "inc",
        "corporation", "corp", "company", "co", "ltd", "limited", "the", "kgaa", "and", "gruppo", "groupe", "reit"}


def nname(x):
    import unicodedata
    x = unicodedata.normalize("NFKD", str(x)).encode("ascii", "ignore").decode().lower()
    words = [w for w in re.split(r"[^a-z0-9]+", x.replace("&", " and ")) if w and w not in STOP]
    return " ".join(words)


def wikidata_names():
    """{nome_normalizzato: {isin}} per le società europee/USA con ISIN su Wikidata."""
    q = """SELECT ?isin ?l WHERE { ?item wdt:P946 ?isin . ?item rdfs:label ?l . FILTER(lang(?l)="en") }"""
    r = requests.get("https://query.wikidata.org/sparql", params={"query": q, "format": "json"},
                     headers={"User-Agent": "analyst-snapshot/1.0 (GitHub Actions)",
                              "Accept": "application/sparql-results+json"}, timeout=120)
    r.raise_for_status()
    out = {}
    for b in r.json()["results"]["bindings"]:
        i = b["isin"]["value"]
        if ISIN_RE.match(i) and i[:2] in OK_CC:
            out.setdefault(nname(b["l"]["value"]), set()).add(i)
    return out


def from_wikidata_names(u):
    """Ripiego per i titoli senza ISIN: nome identico (normalizzato) e un solo ISIN plausibile."""
    miss = u.index[u["isin"].isna() | (u["isin"] == "-")]
    if not len(miss): return 0
    try:
        wn = wikidata_names()
    except Exception as e:
        print("wikidata (nomi) non raggiungibile:", e); return 0
    n = 0
    for i in miss:
        sfx = split(str(u.at[i, "ticker"]))[1]
        for col in ("name", "long_name"):
            v = u.at[i, col]
            if not isinstance(v, str): continue
            c = wn.get(nname(v), set())
            pref = [x for x in c if x[:2] == CC.get(sfx)]
            pick = pref if pref else list(c)
            if len(set(pick)) == 1:
                u.at[i, "isin"] = pick[0]; n += 1; break
    print(f"wikidata (nomi): ISIN per altri {n} titoli")
    return n


# quotazioni USA (azioni o ADR su NYSE/Nasdaq) di titoli europei: lì Yahoo registra molti più broker
US_OVERRIDE = {"RACE.MI": "RACE", "STLAM.MI": "STLA", "STLAP.PA": "STLA", "STMMI.MI": "STM", "STMPA.PA": "STM",
               "ENI.MI": "E", "TEN.MI": "TS", "SAP.DE": "SAP", "ASML.AS": "ASML", "INGA.AS": "ING",
               "NOKIA.HE": "NOK", "BBVA.MC": "BBVA", "SAN.MC": "SAN", "ABI.BR": "BUD", "ARGX.BR": "ARGX",
               "MT.AS": "MT", "TTE.PA": "TTE", "SAN.PA": "SNY", "DBK.DE": "DB", "SHEL.L": "SHEL", "BP.L": "BP",
               "AZN.L": "AZN", "ULVR.L": "UL", "GSK.L": "GSK", "HSBA.L": "HSBC", "RIO.L": "RIO", "DGE.L": "DEO",
               "NG.L": "NGG", "VOD.L": "VOD", "REL.L": "RELX", "LLOY.L": "LYG", "NWG.L": "NWG", "BATS.L": "BTI",
               "IHG.L": "IHG", "BARC.L": "BCS", "SN.L": "SNN", "HLN.L": "HLN"}
EU_EXCH = {sfx: ex for sfx, ex in EXCH.items() if sfx}


def us_listings(u):
    """us_ticker per i titoli europei: override noti + Wikidata (stessa società quotata anche a NYSE/Nasdaq)."""
    if "us_ticker" not in u.columns: u["us_ticker"] = None
    wd = {}
    try:
        q = """SELECT ?item ?ticker ?exl WHERE { ?item p:P414 ?st . ?st ps:P414 ?ex ; pq:P249 ?ticker .
               ?item wdt:P946 ?isin . ?ex rdfs:label ?exl . FILTER(lang(?exl)="en") }"""
        r = requests.get("https://query.wikidata.org/sparql", params={"query": q, "format": "json"},
                         headers={"User-Agent": "analyst-snapshot/1.0 (GitHub Actions)",
                                  "Accept": "application/sparql-results+json"}, timeout=90)
        r.raise_for_status()
        items = {}
        for b in r.json()["results"]["bindings"]:
            items.setdefault(b["item"]["value"], set()).add((b["exl"]["value"], b["ticker"]["value"].strip().upper()))
        for lst in items.values():
            us = sorted(tk for ex, tk in lst if ex in ("New York Stock Exchange", "Nasdaq") and re.fullmatch(r"[A-Z.\-]{1,6}", tk))
            if len(us) != 1: continue
            for ex, tk in lst:
                for sfx, names in EU_EXCH.items():
                    if ex in names: wd[(key(tk), sfx)] = us[0].replace(".", "-")
    except Exception as e:
        print("wikidata (quotazioni USA) non raggiungibile:", e)
    n = 0
    for i, t in u["ticker"].items():
        base, sfx = split(str(t))
        if not sfx: continue
        v = US_OVERRIDE.get(t) or wd.get((key(base), sfx))
        if v: u.at[i, "us_ticker"] = v; n += 1
    print(f"quotazioni USA/ADR: {n} titoli europei")
    return n


def from_wikidata(u):
    try:
        wd = wikidata_isins()
    except Exception as e:
        print("wikidata non raggiungibile:", e); return 0
    n = 0
    for i, t in u["ticker"].items():
        base, sfx = split(str(t))
        cands = set()
        for ex in EXCH.get(sfx, ()):
            cands |= wd.get((key(base), ex), set())
        cands = sorted(c for c in cands if ISIN_RE.match(c))
        if not cands: continue
        best = next((c for c in cands if c[:2] == CC.get(sfx)), None) or next((c for c in cands if c[:2] in OK_CC), None)
        if best:
            u.at[i, "isin"] = best; n += 1
    print(f"wikidata: ISIN per {n} titoli")
    return n


ISIN_OVERRIDE = {"NEXI.MI": "IT0005366767"}


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
    for t, i in ISIN_OVERRIDE.items():
        u.loc[u["ticker"] == t, "isin"] = i
    # ISIN USA su un titolo quotato solo in Europa: quasi sempre un abbinamento sbagliato
    u.loc[(u["panel"] != "US") & u["isin"].fillna("").str.startswith("US"), "isin"] = None
    from_wikidata(u)
    us_listings(u)
    for t, i in ISIN_OVERRIDE.items():
        u.loc[u["ticker"] == t, "isin"] = i
    # ISIN arrivati da yfinance con paese implausibile -> scartati
    bad = u["isin"].notna() & (u["isin"] != "-") & ~u["isin"].astype(str).str[:2].isin(OK_CC)
    u.loc[bad, "isin"] = "-"
    from_wikidata_names(u)
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
                    if isinstance(isin, str) and ISIN_RE.match(isin) and isin[:2] in OK_CC:
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
