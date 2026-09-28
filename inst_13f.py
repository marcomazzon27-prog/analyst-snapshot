"""Investitori istituzionali – raccolta dei portafogli 13F (SEC).

Fonte primaria: SEC Form 13F Data Sets (dati strutturati trimestrali, dal 2013).
Integrazione:    EDGAR (data.sec.gov) per i depositi arrivati dopo l'ultimo data set.

Cosa c'è nei 13F e cosa no
  sì  posizioni lunghe in titoli USA "13(f)" (azioni, ADR, ETF, obbligazioni convertibili)
  sì  opzioni: righe con PUTCALL = Put/Call (valore = sottostante, non il premio)
  no  posizioni corte, liquidità, titoli non USA -> per gli short europei vedi inst_shorts.py

Comandi
  python inst_13f.py backfill [--max-sets N]   scarica i data set non ancora elaborati (riprende da dove era)
  python inst_13f.py live                      depositi nuovi via EDGAR per i gestori seguiti
Output (append/upsert, nel repo)
  data/13f/managers.csv             gestori seguiti (selezione automatica + lista curata + manuale)
  data/13f/filings.csv.gz           un record per deposito usato (tutti i gestori seguiti)
  data/13f/holdings/<periodo>/<bb>.csv.gz posizioni per trimestre, gestori divisi in 20 gruppi (cik % 20):
                                    un nuovo deposito riscrive solo un file piccolo (storia git leggera)
  data/13f/ingested.txt             data set già elaborati
  data/feed.json                    novità per il terminale e le notifiche
"""
import argparse, datetime as dt, gzip, io, json, os, re, sys, tempfile, time, zipfile
from pathlib import Path
import pandas as pd
import requests

ROOT = Path(__file__).parent
D13 = ROOT / "data" / "13f"
HOLD = D13 / "holdings"
FEED = ROOT / "data" / "feed.json"
UA = os.environ.get("SEC_UA") or "analyst-snapshot research (github.com/marcomazzon27-prog/analyst-snapshot)"
H = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
DATASETS_PAGE = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"

# gestori seguiti sempre, se presenti (nome come nei 13F, maiuscolo, match per sottostringa)
CURATED = ["BERKSHIRE HATHAWAY", "PERSHING SQUARE", "SCION ASSET", "BAUPOST", "APPALOOSA", "THIRD POINT",
           "GREENLIGHT CAPITAL", "TIGER GLOBAL", "COATUE", "DUQUESNE FAMILY OFFICE", "SOROS FUND", "ICAHN",
           "ELLIOTT INVESTMENT", "VALUEACT", "STARBOARD VALUE", "TRIAN FUND", "LONE PINE", "VIKING GLOBAL",
           "MAVERICK CAPITAL", "HIMALAYA CAPITAL", "PABRAI", "AKRE CAPITAL", "MARKEL", "FAIRFAX FINANCIAL",
           "GATES FOUNDATION", "TWEEDY, BROWNE", "OAKTREE CAPITAL", "GARDNER RUSSO", "RUANE, CUNNIFF",
           "FAIRHOLME", "DAILY JOURNAL", "ARK INVESTMENT", "BRIDGEWATER", "POLEN CAPITAL", "FUNDSMITH",
           "DODGE & COX", "SEQUOIA", "EGERTON", "D1 CAPITAL", "ALTIMETER", "DRAGONEER", "SIR CAPITAL",
           "JANA PARTNERS", "CORVEX", "GLENVIEW", "SACHEM HEAD", "ENGAGED CAPITAL", "LANDSDOWNE", "MARSHALL WACE",
           "AQR CAPITAL", "RENAISSANCE TECHNOLOGIES"]
# gestori enormi ammessi oltre le 1500 righe (i passivi tipo Vanguard/BlackRock sono esclusi di proposito:
# replicano gli indici e non dicono nulla su "chi seguire", ma pesano centinaia di MB)
BIG_OK = {"BRIDGEWATER", "AQR CAPITAL", "MARSHALL WACE", "ARK INVESTMENT", "RENAISSANCE TECHNOLOGIES"}
EXCLUDE_WORDS = re.compile(r"\b(?:BANK|BANCORP|BANCSHARES|INSURANCE|ASSURANCE|PENSION|RETIREMENT|TRUST CO|"
                           r"TRUST COMPANY|CREDIT UNION|WEALTH|FINANCIAL ADVISORS|FINANCIAL PLANNING|"
                           r"PRIVATE CLIENT|FAMILY WEALTH)\b")
N_AUTO = 400                     # gestori scelti automaticamente (per valore, tra gli idonei)


# ---------------------------------------------------------------- utilità
def get(url, stream=False, tries=4):
    for k in range(tries):
        try:
            r = requests.get(url, headers=H, timeout=120, stream=stream)
            if r.status_code == 200: return r
            if r.status_code in (403, 429): time.sleep(5 * (k + 1)); continue
            r.raise_for_status()
        except requests.RequestException as e:
            if k == tries - 1: raise
            time.sleep(3 * (k + 1))
    raise RuntimeError(f"GET fallita: {url}")


def sec_date(s):
    return pd.to_datetime(s, format="%d-%b-%Y", errors="coerce")


def quarter_str(ts):
    ts = pd.Timestamp(ts)
    return f"{ts.year}Q{(ts.month - 1) // 3 + 1}"


NB = 20


def hold_path(period, cik):
    return HOLD / str(period) / f"{int(cik) % NB:02d}.csv.gz"


def read_hold(period, cik=None):
    """Posizioni di un trimestre (tutti i gestori, o solo `cik`)."""
    files = [hold_path(period, cik)] if cik is not None else sorted((HOLD / str(period)).glob("*.csv.gz"))
    fr = [pd.read_csv(p, dtype={"cik": str, "cusip": str}) for p in files if p.exists()]
    df = pd.concat(fr) if fr else pd.DataFrame(columns=["cik", "period", "cusip", "putcall", "sh_type", "kind",
                                                           "issuer", "cls", "value", "shares", "filing_date", "src"])
    return df[df["cik"] == str(cik)] if cik is not None else df


def periods():
    return sorted(p.name for p in HOLD.glob("*") if p.is_dir()) if HOLD.exists() else []


def load_feed():
    try: return json.loads(FEED.read_text())
    except Exception: return []


def save_feed(ev):
    ev = sorted(ev, key=lambda e: e["ts"], reverse=True)[:500]
    FEED.parent.mkdir(parents=True, exist_ok=True)
    FEED.write_text(json.dumps(ev, ensure_ascii=False, indent=0))


def notify(title, body, tags="chart_with_upwards_trend"):
    """Notifica push via ntfy.sh se è configurato il secret NTFY_TOPIC (altrimenti niente)."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic: return
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode(), timeout=15,
                      headers={"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Tags": tags,
                               "Click": "https://marcomazzon27-prog.github.io/analyst-snapshot/#/investors"})
    except Exception as e:
        print("ntfy fallita:", e)


# ---------------------------------------------------------------- data set SEC
def dataset_links():
    try:
        html = get(DATASETS_PAGE).text
        links = re.findall(r'href="([^"]+form13f\.zip)"', html)
        links = [l if l.startswith("http") else "https://www.sec.gov" + l for l in links]
        if links: return list(dict.fromkeys(links))
    except Exception as e:
        print("pagina data set non leggibile:", e)
    base = "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/"
    return [f"{base}{y}q{q}_form13f.zip" for y in range(2023, 2012, -1) for q in (4, 3, 2, 1)
            if (y, q) >= (2013, 2)]


def read_tsv(z, name, usecols, chunks=False):
    fn = next((n for n in z.namelist() if n.upper().endswith(name.upper() + ".TSV")), None)
    if fn is None: raise FileNotFoundError(name)
    kw = dict(sep="\t", dtype=str, usecols=lambda c: c.upper() in usecols, quoting=3,
              on_bad_lines="skip", encoding="utf-8", encoding_errors="replace")
    if chunks: return pd.read_csv(z.open(fn), chunksize=400_000, **kw)
    df = pd.read_csv(z.open(fn), **kw)
    df.columns = [c.upper() for c in df.columns]
    return df


def filings_of(z):
    sub = read_tsv(z, "SUBMISSION", {"ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT"})
    cov = read_tsv(z, "COVERPAGE", {"ACCESSION_NUMBER", "ISAMENDMENT", "AMENDMENTTYPE", "FILINGMANAGER_NAME", "REPORTTYPE"})
    summ = read_tsv(z, "SUMMARYPAGE", {"ACCESSION_NUMBER", "TABLEENTRYTOTAL", "TABLEVALUETOTAL"})
    f = sub.merge(cov, on="ACCESSION_NUMBER", how="left").merge(summ, on="ACCESSION_NUMBER", how="left")
    f["filing_date"] = sec_date(f["FILING_DATE"]); f["period"] = sec_date(f["PERIODOFREPORT"])
    f["entries"] = pd.to_numeric(f["TABLEENTRYTOTAL"], errors="coerce")
    v = pd.to_numeric(f["TABLEVALUETOTAL"], errors="coerce")
    f["value_usd"] = v.where(f["filing_date"] >= "2023-01-03", v * 1000)   # prima del 2023: migliaia
    f["cik"] = f["CIK"].str.lstrip("0")
    f = f.rename(columns={"ACCESSION_NUMBER": "accession", "SUBMISSIONTYPE": "form", "FILINGMANAGER_NAME": "name",
                          "AMENDMENTTYPE": "amend_type", "ISAMENDMENT": "is_amend", "REPORTTYPE": "report_type"})
    return f[["accession", "cik", "name", "form", "is_amend", "amend_type", "report_type", "filing_date", "period",
              "entries", "value_usd"]]


def pick_managers(f):
    """Selezione alla WhaleWisdom: portafogli concentrati (10–750 righe), almeno 200 M$,
    esclusi banche/assicurazioni/pensioni/wealth manager; + lista curata + overrides manuali."""
    last = f[(f["form"] == "13F-HR") & (f["report_type"].fillna("").str.contains("HOLDINGS|COMBINATION", case=False))]
    last = last.sort_values("filing_date").drop_duplicates("cik", keep="last")
    nm = last["name"].fillna("").str.upper()
    elig = last[(last["entries"].between(10, 750)) & (last["value_usd"] >= 2e8) & ~nm.str.contains(EXCLUDE_WORDS)]
    auto = elig.sort_values("value_usd", ascending=False).head(N_AUTO)
    rows = [{"cik": r.cik, "name": r.name, "reason": "auto"} for r in auto.itertuples()]
    for pat in CURATED:
        hit = last[nm.str.contains(re.escape(pat), regex=True)]
        if not len(hit): continue
        if pat not in BIG_OK: hit = hit[hit["entries"] <= 1500]
        for r in hit.sort_values("value_usd", ascending=False).head(2).itertuples():
            rows.append({"cik": r.cik, "name": r.name, "reason": "curata"})
    m = pd.DataFrame(rows).drop_duplicates("cik")
    man = D13 / "managers_manual.csv"                         # aggiunte manuali (cik,name)
    if man.exists():
        extra = pd.read_csv(man, dtype=str); extra["reason"] = "manuale"
        m = pd.concat([m, extra]).drop_duplicates("cik")
    return m


def choose_filings(fs):
    """Per ogni (gestore, trimestre): base = ultimo 13F-HR o ultima rettifica RESTATEMENT;
    si aggiungono gli emendamenti NEW HOLDINGS depositati dopo la base."""
    fs = fs[fs["form"].isin(["13F-HR", "13F-HR/A"])].sort_values("filing_date")
    keep = []
    for (cik, per), g in fs.groupby(["cik", "period"]):
        at = g["amend_type"].fillna("").str.upper()
        base = g[(g["form"] == "13F-HR") | at.str.contains("RESTATE")].tail(1)
        b = base.iloc[0]["filing_date"] if len(base) else pd.Timestamp.min
        if len(base): keep.append(base.iloc[0].to_dict() | {"kind": "base"})
        for _, r in g[at.str.contains("NEW HOLDING") & (g["filing_date"] >= b)].iterrows():
            keep.append(r.to_dict() | {"kind": "add"})
    return pd.DataFrame(keep)


def holdings_from(z, accs):
    cols = {"ACCESSION_NUMBER", "NAMEOFISSUER", "TITLEOFCLASS", "CUSIP", "VALUE", "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL"}
    out = []
    for ch in read_tsv(z, "INFOTABLE", cols, chunks=True):
        ch.columns = [c.upper() for c in ch.columns]
        ch = ch[ch["ACCESSION_NUMBER"].isin(accs)]
        if len(ch): out.append(ch)
    return pd.concat(out) if out else pd.DataFrame(columns=list(cols))


def normalize_holdings(h, fsel):
    h = h.rename(columns={"ACCESSION_NUMBER": "accession", "NAMEOFISSUER": "issuer", "TITLEOFCLASS": "cls",
                          "CUSIP": "cusip", "VALUE": "value", "SSHPRNAMT": "shares", "SSHPRNAMTTYPE": "sh_type",
                          "PUTCALL": "putcall"})
    if "kind" not in fsel: fsel = fsel.assign(kind="base")
    h = h.merge(fsel[["accession", "cik", "period", "filing_date", "kind"]], on="accession")
    h["value"] = pd.to_numeric(h["value"], errors="coerce").fillna(0)
    h.loc[h["filing_date"] < "2023-01-03", "value"] *= 1000
    h["shares"] = pd.to_numeric(h["shares"], errors="coerce").fillna(0)
    h["cusip"] = h["cusip"].str.upper().str.strip().str[:9]
    h["putcall"] = h["putcall"].fillna("").str.upper().str.strip()
    h["sh_type"] = h["sh_type"].fillna("SH").str.upper()
    g = (h.groupby(["cik", "period", "cusip", "putcall", "sh_type", "kind"], as_index=False)
          .agg(issuer=("issuer", "first"), cls=("cls", "first"), value=("value", "sum"), shares=("shares", "sum"),
               filing_date=("filing_date", "max")))
    g["period"] = g["period"].dt.date.astype(str); g["filing_date"] = g["filing_date"].dt.date.astype(str)
    return g


def save_holdings(g, source):
    """Upsert per (gestore, trimestre). Una base sostituisce la base esistente solo se è più recente
    (i data set si elaborano dal più nuovo al più vecchio); le righe 'add' (emendamenti NEW HOLDINGS)
    si aggiungono se non già presenti."""
    g = g.copy(); g["src"] = source
    g["_b"] = g["cik"].astype(int) % NB
    for (per, b), part in g.groupby(["period", "_b"]):
        part = part.drop(columns="_b")
        p = hold_path(per, b)
        p.parent.mkdir(parents=True, exist_ok=True)
        old = pd.read_csv(p, dtype={"cik": str, "cusip": str}) if p.exists() else part.iloc[0:0]
        if "kind" not in old: old["kind"] = "base"
        new_parts = []
        for cik, gp in part.groupby("cik"):
            o = old[old["cik"] == cik]
            ob, nb = o[o["kind"] == "base"], gp[gp["kind"] == "base"]
            if len(nb):
                if len(ob) and ob["filing_date"].max() > nb["filing_date"].max():
                    pass                                              # c'è già una base più recente
                else:
                    old = old[~((old["cik"] == cik) & (old["kind"] == "base"))]
                    new_parts.append(nb)
            na = gp[gp["kind"] == "add"]
            if len(na):
                oa = o[o["kind"] == "add"]
                na = na[~na["filing_date"].isin(set(oa["filing_date"]))]
                new_parts.append(na)
        out = pd.concat([old] + new_parts) if new_parts else old
        out.sort_values(["cik", "value"], ascending=[True, False]).to_csv(p, index=False, compression="gzip")


def upsert_filings(fsel):
    p = D13 / "filings.csv.gz"
    cols = ["accession", "cik", "name", "form", "amend_type", "filing_date", "period", "entries", "value_usd", "src"]
    f = fsel.copy()
    for c in ("filing_date", "period"):
        f[c] = pd.to_datetime(f[c]).dt.date.astype(str)
    f = f.reindex(columns=cols)
    if p.exists(): f = pd.concat([pd.read_csv(p, dtype={"cik": str}), f]).drop_duplicates("accession", keep="last")
    f.sort_values(["cik", "period", "filing_date"]).to_csv(p, index=False, compression="gzip")


def backfill(max_sets):
    D13.mkdir(parents=True, exist_ok=True)
    done_p = D13 / "ingested.txt"
    done = set(done_p.read_text().split()) if done_p.exists() else set()
    links = dataset_links()
    todo = [l for l in links if l.rsplit("/", 1)[-1] not in done]
    print(f"data set: {len(links)} totali, {len(todo)} da elaborare")
    mp = D13 / "managers.csv"
    managers = pd.read_csv(mp, dtype=str) if mp.exists() else None
    n = 0
    for url in todo:                                   # dal più recente al più vecchio
        if n >= max_sets: break
        name = url.rsplit("/", 1)[-1]; t0 = time.time()
        with tempfile.TemporaryFile() as tmp:
            r = get(url, stream=True)
            for chunk in r.iter_content(1 << 20): tmp.write(chunk)
            tmp.seek(0)
            z = zipfile.ZipFile(tmp)
            f = filings_of(z)
            if managers is None:
                managers = pick_managers(f); managers.to_csv(mp, index=False)
                print(f"gestori selezionati: {len(managers)}")
            fs = f[f["cik"].isin(set(managers["cik"]))]
            fsel = choose_filings(fs)
            if len(fsel):
                h = holdings_from(z, set(fsel["accession"]))
                g = normalize_holdings(h, fsel)
                save_holdings(g, "dataset")
                fsel = fsel.assign(src="dataset"); upsert_filings(fsel)
            # nomi aggiornati dei gestori
            nm = f.sort_values("filing_date").drop_duplicates("cik", keep="last").set_index("cik")["name"]
            managers["name"] = managers["cik"].map(nm).fillna(managers["name"]); managers.to_csv(mp, index=False)
        done.add(name); done_p.write_text("\n".join(sorted(done)))
        n += 1
        print(f"{name}: {len(fsel)} depositi, {time.time() - t0:.0f}s", flush=True)
    return n


# ---------------------------------------------------------------- EDGAR live
def parse_infotable_xml(xml):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)
    strip = lambda t: t.split("}", 1)[-1]
    rows = []
    for it in root.iter():
        if strip(it.tag) != "infoTable": continue
        d = {}
        for el in it.iter():
            tg = strip(el.tag)
            if el.text and el.text.strip(): d[tg] = el.text.strip()
        rows.append({"issuer": d.get("nameOfIssuer"), "cls": d.get("titleOfClass"), "cusip": d.get("cusip"),
                     "value": d.get("value"), "shares": d.get("sshPrnamt"), "sh_type": d.get("sshPrnamtType", "SH"),
                     "putcall": d.get("putCall", "")})
    return pd.DataFrame(rows)


def live(sleep=0.15):
    mp = D13 / "managers.csv"
    if not mp.exists(): print("nessun gestore: esegui prima il backfill"); return 0
    managers = pd.read_csv(mp, dtype=str)
    fp = D13 / "filings.csv.gz"
    known = pd.read_csv(fp, dtype={"cik": str}) if fp.exists() else pd.DataFrame(columns=["accession", "cik", "period"])
    have = set(known["accession"])
    rank = {}
    rp = D13 / "ranking.csv"
    if rp.exists():
        rk = pd.read_csv(rp, dtype={"cik": str}); rank = dict(zip(rk["cik"], rk["rank"]))
    feed = load_feed(); seen = {e["id"] for e in feed}
    new_n = 0
    for m in managers.itertuples():
        try:
            j = get(f"https://data.sec.gov/submissions/CIK{int(m.cik):010d}.json").json()
        except Exception as e:
            print(m.cik, "submissions non disponibili:", e); continue
        rec = j.get("filings", {}).get("recent", {})
        for form, acc, fdate, rdate in zip(rec.get("form", []), rec.get("accessionNumber", []),
                                           rec.get("filingDate", []), rec.get("reportDate", [])):
            if form not in ("13F-HR", "13F-HR/A") or acc in have: continue
            if fdate < "2025-01-01": continue                       # lo storico arriva dai data set
            try:
                idx = get(f"https://www.sec.gov/Archives/edgar/data/{int(m.cik)}/{acc.replace('-', '')}/index.json").json()
                base_url = f"https://www.sec.gov/Archives/edgar/data/{int(m.cik)}/{acc.replace('-', '')}/"
                names = [it["name"] for it in idx["directory"]["item"]]
                xmls = [n for n in names if n.lower().endswith(".xml") and "primary_doc" not in n.lower()]
                if not xmls: continue
                h = parse_infotable_xml(get(base_url + xmls[0]).content)
                kind = "base"
                if form.endswith("/A"):
                    prim = [n for n in names if "primary_doc" in n.lower()]
                    at = re.search(r"<(?:\w+:)?amendmentType>([^<]+)<", get(base_url + prim[0]).text) if prim else None
                    kind = "base" if at and "RESTATE" in at.group(1).upper() else "add"
            except Exception as e:
                print(m.cik, acc, "non leggibile:", e); continue
            if h.empty: continue
            per = pd.Timestamp(rdate); fd = pd.Timestamp(fdate)
            fsel = pd.DataFrame([{"accession": acc, "cik": m.cik, "name": m.name, "form": form,
                                  "amend_type": None if kind == "base" else "NEW HOLDINGS", "filing_date": fd,
                                  "period": per, "entries": len(h), "value_usd": None, "kind": kind}])
            h = h.rename(columns=str.upper).rename(columns={"ISSUER": "NAMEOFISSUER", "CLS": "TITLEOFCLASS",
                                                            "SHARES": "SSHPRNAMT", "SH_TYPE": "SSHPRNAMTTYPE"})
            h["ACCESSION_NUMBER"] = acc
            g = normalize_holdings(h, fsel)
            fsel["value_usd"] = g["value"].sum()
            save_holdings(g, "edgar"); upsert_filings(fsel.assign(src="edgar")); have.add(acc)
            new_n += 1
            ev = summarize_changes(m.cik, m.name, str(per.date()), fdate, acc, rank.get(m.cik))
            if ev and ev["id"] not in seen:
                feed.append(ev); seen.add(ev["id"])
                if ev.get("important"): notify(ev["title"], ev["text"])
            time.sleep(sleep)
        time.sleep(sleep)
    save_feed(feed)
    print(f"depositi nuovi da EDGAR: {new_n}")
    return new_n


def summarize_changes(cik, name, period, fdate, acc, rank):
    c = read_hold(period, cik)
    c = c[c["putcall"].isna()]
    if c.empty: return None
    pv = c.iloc[0:0]
    for pp in reversed([p for p in periods() if p < period][-2:]):
        x = read_hold(pp, cik); x = x[x["putcall"].isna()]
        if len(x): pv = x; break
    a, b = set(c["cusip"]), set(pv["cusip"])
    new, out = a - b, b - a
    top = c.sort_values("value", ascending=False)
    nm = dict(zip(pv["cusip"], pv["issuer"])); nm.update(dict(zip(c["cusip"], c["issuer"])))
    txt = []
    pl = lambda n, a, b: f"{n} {a if n == 1 else b}"
    if new: txt.append(pl(len(new), "nuova posizione", "nuove posizioni") + " (" + ", ".join(sorted(str(nm[x]) for x in list(new)[:4])) + ")")
    if out: txt.append(pl(len(out), "uscita", "uscite") + " (" + ", ".join(sorted(str(nm[x]) for x in list(out)[:4])) + ")")
    if len(top): txt.append(f"prima posizione: {top.iloc[0]['issuer']}")
    important = rank is not None and rank <= 50
    return {"id": f"13f-{acc}", "ts": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "type": "13f",
            "cik": cik, "title": f"{name}: 13F {quarter_str(period)}", "text": "; ".join(txt),
            "date": fdate, "important": bool(important), "link": f"#/inv/{cik}"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backfill", "live"])
    ap.add_argument("--max-sets", type=int, default=60)
    a = ap.parse_args()
    if a.cmd == "backfill":
        n = backfill(a.max_sets)
        sys.exit(0 if n else 10)              # 10 = nessun data set rimasto (serve al workflow per fermarsi)
    live()
