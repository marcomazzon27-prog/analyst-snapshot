"""Posizioni corte nette pubblicate dalle autorità europee (Reg. UE 236/2012 e regole UK).

I 13F non contengono posizioni corte: per i titoli europei le integriamo da
  CONSOB (Italia)  PncPubbl.xlsx   per detentore, correnti + storiche          soglia 0,5%
  AMF (Francia)    data.gouv.fr     per detentore, storico dal 01/11/2012      soglia 0,5%
  FCA (UK)         CSV aggregati    per emittente (anonimi), correnti + storici
Output: data/shorts/positions.csv.gz (source, holder, issuer, isin, pct, pos_date, current)
        eventi nel feed per i titoli dell'universo (nuove posizioni o variazioni).
"""
import datetime as dt, io, json, os, re
from pathlib import Path
import pandas as pd
import requests

from inst_13f import load_feed, save_feed, notify, H

ROOT = Path(__file__).parent
OUT = ROOT / "data" / "shorts" / "positions.csv.gz"
CONSOB_PAGE = "https://www.consob.it/web/area-pubblica/pnc"
CONSOB_FALLBACK = "https://www.consob.it/documents/11973/395154/PncPubbl.xlsx/fbefe0a2-795b-bad3-9369-beccbeb14f27"
AMF_CSV = "https://www.data.gouv.fr/api/1/datasets/r/c2539d1c-8531-4937-9cba-3bd8e9786cc5"
FCA_CUR = "https://www.fca.org.uk/publication/documents/aggregated-current-net-short-positions.csv"
FCA_HIST = "https://www.fca.org.uk/publication/documents/aggregated-historic-net-short-positions.csv"
BH = {"User-Agent": "Mozilla/5.0 (analyst-snapshot; +https://github.com/marcomazzon27-prog/analyst-snapshot)"}


def col(df, *keys):
    for c in df.columns:
        cl = str(c).lower()
        if all(k in cl for k in keys): return c
    return None


def pdate(x):
    """Date in formato ISO (aaaa-mm-gg) o europeo (gg/mm/aaaa)."""
    x = pd.Series(x) if not isinstance(x, pd.Series) else x
    if pd.api.types.is_datetime64_any_dtype(x): return x
    s = x.astype(str).str.strip().str[:10]
    iso = s.str.match(r"^\d{4}-\d{2}-\d{2}$")
    out = pd.to_datetime(s.where(iso), errors="coerce", format="%Y-%m-%d")
    return out.fillna(pd.to_datetime(s.where(~iso), errors="coerce", dayfirst=True))


def find_header(raw, word="isin"):
    for i in range(min(15, len(raw))):
        if any(isinstance(v, str) and word in v.lower() for v in raw.iloc[i].values): return i
    return 0


def consob():
    url = CONSOB_FALLBACK
    ses = requests.Session()
    ses.headers.update(BH | {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                             "Accept-Language": "it-IT,it;q=0.9,en;q=0.8"})
    try:
        html = ses.get(CONSOB_PAGE, timeout=60).text                # anche per i cookie di sessione del sito
        m = re.search(r'href="([^"]*PncPubbl\.xlsx[^"]*)"', html)
        if m: url = m.group(1) if m.group(1).startswith("http") else "https://www.consob.it" + m.group(1)
    except Exception as e:
        print("pagina CONSOB non leggibile, uso il link noto:", e)
    r = ses.get(url, timeout=90, headers={"Referer": CONSOB_PAGE,
                                         "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*"})
    b = r.content
    if b[:2] != b"PK":
        raise ValueError(f"il sito non ha restituito un file Excel (HTTP {r.status_code}, "
                         f"{r.headers.get('content-type')}, {b[:80]!r})")
    xl = pd.ExcelFile(io.BytesIO(b))
    rows = []
    for sh in xl.sheet_names:
        raw = xl.parse(sh, header=None)
        if raw.empty: continue
        hi = find_header(raw)
        df = xl.parse(sh, header=hi)
        h, e, i, p, d = (col(df, "detentore") or col(df, "holder"), col(df, "emittente") or col(df, "issuer"),
                         col(df, "isin"), col(df, "perc") or col(df, "%"), col(df, "data") or col(df, "date"))
        if not (h and i and p): continue
        cur = not any(k in sh.lower() for k in ("stor", "hist"))
        for _, r in df.iterrows():
            if pd.isna(r[i]): continue
            rows.append({"source": "CONSOB", "holder": str(r[h]).strip(), "issuer": str(r[e]).strip() if e else None,
                         "isin": str(r[i]).strip(), "pct": pd.to_numeric(str(r[p]).replace(",", "."), errors="coerce"),
                         "pos_date": r[d] if d else None,
                         "current": cur})
    out = pd.DataFrame(rows)
    if len(out): out["pos_date"] = pdate(out["pos_date"])
    return out


def amf():
    b = requests.get(AMF_CSV, headers=BH, timeout=120).content
    df = None
    for enc in ("utf-8-sig", "latin-1"):
        for sep in (";", ",", "\t"):
            try:
                x = pd.read_csv(io.BytesIO(b), sep=sep, encoding=enc, dtype=str)
            except Exception:
                continue
            if x.shape[1] >= 5 and col(x, "isin"): df = x; break
        if df is not None: break
    if df is None: raise ValueError("CSV AMF non riconosciuto")
    h = col(df, "tenteur") or col(df, "holder")
    i = col(df, "isin")
    e = col(df, "metteur") or col(df, "issuer")
    p = col(df, "ratio") or col(df, "position")
    d = col(df, "but de position") or col(df, "position date") or col(df, "date")
    endc = col(df, "fin de publication") or col(df, "end")
    out = pd.DataFrame({"source": "AMF", "holder": df[h], "issuer": df[e] if e else None, "isin": df[i],
                        "pct": pd.to_numeric(df[p].str.replace(",", ".").str.replace("%", ""), errors="coerce"),
                        "pos_date": pdate(df[d])})
    out["current"] = df[endc].isna() | (df[endc].fillna("").str.strip() == "") if endc else True
    return out


def fca():
    fr = []
    for url, cur in ((FCA_CUR, True), (FCA_HIST, False)):
        try:
            df = pd.read_csv(io.BytesIO(requests.get(url, headers=BH, timeout=60).content), dtype=str)
        except Exception as e:
            print("FCA non leggibile:", url, e); continue
        i = col(df, "isin"); e = col(df, "issuer") or col(df, "name")
        p = col(df, "%") or col(df, "percent") or col(df, "net short") or col(df, "position")
        d = col(df, "position date") or col(df, "date")
        if not (i and p): print("FCA: colonne non riconosciute", list(df.columns)); continue
        fr.append(pd.DataFrame({"source": "FCA", "holder": "Totale dichiarato (aggregato FCA)", "issuer": df[e] if e else None,
                                "isin": df[i], "pct": pd.to_numeric(df[p].str.replace("%", ""), errors="coerce"),
                                "pos_date": pdate(df[d]) if d else pd.NaT,
                                "current": cur}))
    return pd.concat(fr) if fr else pd.DataFrame()


def main():
    parts = []
    for name, fn in (("CONSOB", consob), ("AMF", amf), ("FCA", fca)):
        try:
            x = fn(); print(f"{name}: {len(x)} righe, correnti {int(x['current'].sum()) if len(x) else 0}"); parts.append(x)
        except Exception as e:
            print(f"{name}: non disponibile ({e})")
    if not parts: return
    new = pd.concat(parts)
    new["isin"] = new["isin"].str.upper().str.strip()
    new = new[new["isin"].str.match(r"^[A-Z]{2}[A-Z0-9]{9}\d$", na=False) & new["pct"].notna()]
    new["pos_date"] = pd.to_datetime(new["pos_date"]).dt.date.astype(str)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    old = pd.read_csv(OUT, dtype=str) if OUT.exists() else pd.DataFrame(columns=new.columns)
    new.to_csv(OUT, index=False, compression="gzip")
    # --- feed: nuove posizioni o variazioni correnti sui titoli dell'universo
    u = pd.read_csv(ROOT / "universe.csv", dtype=str)
    isin2t = dict(zip(u["isin"], u["ticker"])); isin2n = dict(zip(u["isin"], u["name"]))
    cur = new[new["current"].astype(str) == "True"]
    oc = old[old["current"].astype(str) == "True"] if len(old) else old
    prev = {(r.source, r.holder, r.isin): float(r.pct) for r in oc.itertuples()} if len(oc) else {}
    feed = load_feed(); seen = {e["id"] for e in feed}; n = 0
    if prev:                                                   # al primo giro non si notifica lo storico
        for r in cur.itertuples():
            t = isin2t.get(r.isin)
            if not t: continue
            p0 = prev.get((r.source, r.holder, r.isin))
            if p0 is not None and abs(p0 - r.pct) < 0.05: continue
            eid = f"short-{r.source}-{r.isin}-{r.holder}-{r.pos_date}-{r.pct}"
            if eid in seen: continue
            what = "nuova posizione corta" if p0 is None else f"short da {p0:.2f}% a {r.pct:.2f}%"
            ev = {"id": eid, "ts": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "type": "short",
                  "title": f"{isin2n.get(r.isin, r.issuer)}: {r.holder}",
                  "text": f"{what} ({r.pct:.2f}%, {r.source}, {r.pos_date})", "date": r.pos_date,
                  "important": r.pct >= 1.0 or (p0 is not None and abs(p0 - r.pct) >= 0.25), "link": f"#/t/{t}"}
            feed.append(ev); seen.add(eid); n += 1
            if ev["important"]: notify(ev["title"], ev["text"], tags="chart_with_downwards_trend")
    save_feed(feed)
    print(f"posizioni corte: {len(new)} righe ({len(cur)} correnti); eventi nuovi nel feed: {n}")


if __name__ == "__main__":
    main()
