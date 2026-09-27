"""Genera universe.csv: S&P 500, FTSE 100, FTSE 250, FTSE MIB, DAX 40, CAC 40, Euro Stoxx 50.

Fonte: Wikipedia (scaricata con requests e UA esplicito: il default di pandas prende 403).
Se una pagina cambia struttura e restituisce poche righe, per gli indici europei si
usa la lista di riserva qui sotto, così il run non si svuota mai.

Colonne: ticker (formato Yahoo), name, panel (mercato: US UK IT DE FR NL ES BE FI IE PT),
indices (es. "DAX;SX5E"), cap_tier, isin, currency, exchange.
isin/currency/exchange li riempie enrich_universe.py e vengono preservati tra un rebuild e l'altro.
"""
import io, re
from pathlib import Path
import pandas as pd
import requests

H = {"User-Agent": "analyst-snapshot/1.0 (GitHub Actions; contatto via issue del repo)"}
W = "https://en.wikipedia.org/wiki/"
SUFFIX_PANEL = {".L": "UK", ".MI": "IT", ".DE": "DE", ".PA": "FR", ".AS": "NL", ".MC": "ES",
                ".BR": "BE", ".HE": "FI", ".IR": "IE", ".LS": "PT"}
# parole chiave (borsa o paese) -> suffisso Yahoo, per le righe dell'Euro Stoxx 50
PLACE = [(r"xetra|frankfurt|german", ".DE"), (r"paris|france|french", ".PA"),
         (r"milan|borsa italiana|ital", ".MI"), (r"amsterdam|netherlands|dutch", ".AS"),
         (r"madrid|spain|spanish", ".MC"), (r"brussels|belgi", ".BR"),
         (r"helsinki|finland|finnish", ".HE"), (r"dublin|ireland|irish", ".IR"),
         (r"lisbon|portug", ".LS")]

# --- liste di riserva (ticker Yahoo, nome). Usate solo se Wikipedia non risponde bene.
FALLBACK = {
 "FTSEMIB": """A2A.MI A2A|AMP.MI Amplifon|AZM.MI Azimut|BGN.MI Banca Generali|BMED.MI Banca Mediolanum|
BAMI.MI Banco BPM|BMPS.MI Banca Monte dei Paschi|BPE.MI BPER Banca|BC.MI Brunello Cucinelli|BZU.MI Buzzi|
CPR.MI Campari|DIA.MI DiaSorin|ENEL.MI Enel|ENI.MI Eni|RACE.MI Ferrari|FBK.MI FinecoBank|G.MI Generali|
HER.MI Hera|IP.MI Interpump|ISP.MI Intesa Sanpaolo|INW.MI Inwit|IG.MI Italgas|IVG.MI Iveco|LDO.MI Leonardo|
LTMC.MI Lottomatica|MB.MI Mediobanca|MONC.MI Moncler|NEXI.MI Nexi|PIRC.MI Pirelli|PST.MI Poste Italiane|
PRY.MI Prysmian|REC.MI Recordati|SPM.MI Saipem|SRG.MI Snam|STLAM.MI Stellantis|STMMI.MI STMicroelectronics|
TIT.MI Telecom Italia|TEN.MI Tenaris|TRN.MI Terna|UCG.MI UniCredit|UNI.MI Unipol|AVIO.MI Avio""",
 "DAX": """ADS.DE Adidas|AIR.DE Airbus|ALV.DE Allianz|BAS.DE BASF|BAYN.DE Bayer|BEI.DE Beiersdorf|BMW.DE BMW|
BNR.DE Brenntag|CBK.DE Commerzbank|CON.DE Continental|DTG.DE Daimler Truck|DBK.DE Deutsche Bank|
DB1.DE Deutsche Boerse|DHL.DE DHL Group|DTE.DE Deutsche Telekom|EOAN.DE E.ON|FRE.DE Fresenius|
FME.DE Fresenius Medical Care|G1A.DE GEA Group|HNR1.DE Hannover Rueck|HEI.DE Heidelberg Materials|
HEN3.DE Henkel|IFX.DE Infineon|MBG.DE Mercedes-Benz|MRK.DE Merck KGaA|MTX.DE MTU Aero Engines|
MUV2.DE Munich Re|PAH3.DE Porsche SE|P911.DE Porsche AG|QIA.DE Qiagen|RHM.DE Rheinmetall|RWE.DE RWE|
SAP.DE SAP|SRT3.DE Sartorius|SIE.DE Siemens|ENR.DE Siemens Energy|SHL.DE Siemens Healthineers|
SY1.DE Symrise|VOW3.DE Volkswagen|VNA.DE Vonovia|ZAL.DE Zalando|G24.DE Scout24""",
 "CAC": """AC.PA Accor|AI.PA Air Liquide|AIR.PA Airbus|MT.AS ArcelorMittal|CS.PA AXA|BNP.PA BNP Paribas|
EN.PA Bouygues|BVI.PA Bureau Veritas|CAP.PA Capgemini|CA.PA Carrefour|ACA.PA Credit Agricole|BN.PA Danone|
DSY.PA Dassault Systemes|EDEN.PA Edenred|ENGI.PA Engie|EL.PA EssilorLuxottica|ERF.PA Eurofins Scientific|
RMS.PA Hermes|KER.PA Kering|OR.PA L'Oreal|LR.PA Legrand|MC.PA LVMH|ML.PA Michelin|ORA.PA Orange|
RI.PA Pernod Ricard|PUB.PA Publicis|RNO.PA Renault|SAF.PA Safran|SGO.PA Saint-Gobain|SAN.PA Sanofi|
SU.PA Schneider Electric|GLE.PA Societe Generale|STLAP.PA Stellantis|STMPA.PA STMicroelectronics|
HO.PA Thales|TTE.PA TotalEnergies|URW.PA Unibail-Rodamco-Westfield|VIE.PA Veolia|DG.PA Vinci""",
 "SX5E": """ADYEN.AS Adyen|AD.AS Ahold Delhaize|AI.PA Air Liquide|AIR.PA Airbus|ALV.DE Allianz|
ABI.BR Anheuser-Busch InBev|ARGX.BR argenx|ASML.AS ASML|CS.PA AXA|BBVA.MC BBVA|SAN.MC Banco Santander|
BAS.DE BASF|BAYN.DE Bayer|BMW.DE BMW|BNP.PA BNP Paribas|DB1.DE Deutsche Boerse|DBK.DE Deutsche Bank|
DHL.DE DHL Group|DTE.DE Deutsche Telekom|ENEL.MI Enel|ENI.MI Eni|EL.PA EssilorLuxottica|RACE.MI Ferrari|
G.MI Generali|RMS.PA Hermes|IBE.MC Iberdrola|ITX.MC Inditex|IFX.DE Infineon|INGA.AS ING|ISP.MI Intesa Sanpaolo|
KER.PA Kering|OR.PA L'Oreal|MC.PA LVMH|MBG.DE Mercedes-Benz|MUV2.DE Munich Re|NOKIA.HE Nokia|
NDA-FI.HE Nordea|PRX.AS Prosus|RHM.DE Rheinmetall|SAF.PA Safran|SGO.PA Saint-Gobain|SAN.PA Sanofi|
SAP.DE SAP|SU.PA Schneider Electric|SIE.DE Siemens|ENR.DE Siemens Energy|TTE.PA TotalEnergies|
UCG.MI UniCredit|DG.PA Vinci|VOW3.DE Volkswagen|WKL.AS Wolters Kluwer""",
}
DEFAULT_SUFFIX = {"FTSEMIB": ".MI", "DAX": ".DE", "CAC": ".PA", "SX5E": None}

rows = []


def clean_tk(x):
    x = str(x).strip()
    x = re.sub(r"\[.*?\]", "", x)                  # note wiki [1]
    if ":" in x: x = x.split(":")[-1]               # "BIT: ENI", "FWB: SAP"
    x = x.strip().split()[0] if x.strip() else ""
    return x.upper()


def add(ticker, name, panel, index, tier):
    rows.append({"ticker": ticker, "name": (str(name).strip() if isinstance(name, str) else None),
                 "panel": panel, "indices": index, "cap_tier": tier})


def fetch_tables(page):
    r = requests.get(W + page, headers=H, timeout=30)
    r.raise_for_status()
    return pd.read_html(io.StringIO(r.text))


def pick(t, keys):
    cols = {str(c).strip().lower(): c for c in t.columns}
    for k in keys:
        for c in cols:
            if c == k or c.startswith(k): return cols[c]
    return None


def grab(page, index, tier, suffix, minrows, fixed_panel=None):
    """Legge la tabella dei componenti. suffix=None: dedotto riga per riga (Euro Stoxx)."""
    try:
        tabs = fetch_tables(page)
    except Exception as e:
        print(f"{page}: fallito ({e})"); return 0
    for t in tabs:
        if isinstance(t.columns, pd.MultiIndex):
            t.columns = [" ".join(str(x) for x in c if "Unnamed" not in str(x)) for c in t.columns]
        tk = pick(t, ("ticker", "symbol", "epic", "code"))
        nm = pick(t, ("security", "company", "name", "constituent"))
        if tk is None or len(t) < minrows: continue
        n = 0
        for _, r in t.iterrows():
            x = clean_tk(r[tk])
            if not x or x == "NAN" or len(x) > 12: continue
            if fixed_panel == "US":
                y, panel = x.replace(".", "-"), "US"
            elif "." in x and "." + x.split(".")[-1] in SUFFIX_PANEL:
                y, panel = x, SUFFIX_PANEL["." + x.split(".")[-1]]
            else:
                sfx = suffix
                if sfx is None:
                    blob = " ".join(str(v) for v in r.values).lower()
                    sfx = next((s for pat, s in PLACE if re.search(pat, blob)), None)
                if sfx is None: continue
                y, panel = x.replace(".", "-") + sfx, SUFFIX_PANEL[sfx]
            add(y, r[nm] if nm is not None else None, panel, index, tier); n += 1
        print(f"{page}: {n} ticker"); return n
    print(f"{page}: nessuna tabella usabile"); return 0


def fallback(index, tier):
    for item in FALLBACK[index].replace("\n", "").split("|"):
        tk, name = item.strip().split(" ", 1)
        add(tk, name, SUFFIX_PANEL["." + tk.split(".")[-1]], index, tier)
    print(f"{index}: usata la lista di riserva")


grab("List_of_S%26P_500_companies", "SP500", "large", "", 400, fixed_panel="US")
grab("FTSE_100_Index", "UKX", "large", ".L", 80)
grab("FTSE_250_Index", "MCX", "mid", ".L", 200)
for page, idx, minr in (("FTSE_MIB", "FTSEMIB", 30), ("DAX", "DAX", 30),
                        ("CAC_40", "CAC", 30), ("EURO_STOXX_50", "SX5E", 40)):
    before = len(rows)
    if grab(page, idx, "large", DEFAULT_SUFFIX[idx], minr) < minr:
        del rows[before:]; fallback(idx, "large")

df = pd.DataFrame(rows)
if df.empty: raise SystemExit("nessun ticker raccolto - controlla i log qui sopra")
# un titolo in più indici: una riga sola, indici concatenati
df = (df.groupby("ticker", as_index=False)
        .agg(name=("name", "first"), panel=("panel", "first"),
             indices=("indices", lambda s: ";".join(dict.fromkeys(s))), cap_tier=("cap_tier", "first")))

old = Path("universe.csv")
keep = ["isin", "currency", "exchange", "long_name"]
if old.exists():
    o = pd.read_csv(old)
    o = o[[c for c in ["ticker"] + keep if c in o.columns]]
    df = df.merge(o, on="ticker", how="left")
for c in keep:
    if c not in df.columns: df[c] = None
df = df[["ticker", "name", "panel", "indices", "cap_tier"] + keep].sort_values(["panel", "ticker"])
df.to_csv("universe.csv", index=False)
print(f"\n{len(df)} ticker -> universe.csv")
print(df.groupby("panel").size().to_string())
