"""Costruisce il database storico da tutti gli snapshot CSV del repo.

Principio: i CSV in data/ sono la fonte di verità (append-only, uno per notte).
Il database è DERIVATO: si ricostruisce da zero ogni notte, quindi non va
committato e non si corrompe mai. Unica eccezione: latest_health.csv viene
sovrascritto ogni notte, quindi qui lo si accoda a data/health_history.csv
(append-only anche lui) prima che vada perso.

Output:
  build/analyst.db         SQLite con tabelle, viste e storico point-in-time
  site/terminal.json       dati compatti per il terminale
  site/index.html          il terminale (copiato da terminal/index.html)

Uso:  python build_db.py            (dalla root del repo)
"""
import bisect, datetime as dt, hashlib, json, re, shutil, sqlite3
from pathlib import Path
import pandas as pd

try:
    from collect import grade          # stessa mappa grade->1..5 della raccolta
except Exception:                      # pragma: no cover
    def grade(x): return None

ROOT = Path(__file__).parent
DATA, BUILD, SITE = ROOT / "data", ROOT / "build", ROOT / "site"
EVENTS_DAYS_IN_JSON = 730             # analisi esportate al terminale (lo storico completo resta nel db)
REV_HISTORY_IN_JSON = 60               # snapshot di revisioni esportati al terminale

SCHEMA = """
CREATE TABLE universe(ticker TEXT PRIMARY KEY, name TEXT, long_name TEXT, panel TEXT,
  indices TEXT, cap_tier TEXT, isin TEXT, currency TEXT, exchange TEXT,
  us_ticker TEXT);                   -- quotazione USA/ADR (fonte aggiuntiva di rating per i titoli europei)

CREATE TABLE snapshot(               -- un record per notte di raccolta
  run_date TEXT PRIMARY KEY, tickers INT, failed INT, rating_rows INT,
  revision_rows INT, unmapped_grades TEXT, target_rows INT, price_tickers INT, pt_events INT);

CREATE TABLE rating_event(           -- evento unico, deduplicato tra snapshot
  event_id TEXT PRIMARY KEY, ticker TEXT, event_date TEXT, broker TEXT,
  action TEXT, rating_from TEXT, rating_to TEXT, from_num INT, to_num INT,
  first_seen TEXT,                   -- primo snapshot che lo contiene (point-in-time)
  last_seen TEXT, n_seen INT,
  is_backfill INT,                   -- 1 = arrivato col primo carico, non point-in-time
  regraded INT,                      -- 1 = grade numerico ricalcolato con la mappa attuale
  pt_action TEXT, pt_from REAL, pt_to REAL,   -- target price dell'analisi (se fornito)
  close_at_event REAL,
  src TEXT,                          -- listing da cui arriva il dato (titolo stesso o quotazione USA)
  pt_ccy_from TEXT,                  -- se valorizzato: target riportato dalla quotazione USA e convertito
  pt_to_raw REAL, pt_from_raw REAL);              -- chiusura del giorno dell'analisi (o l'ultima prima)
CREATE INDEX ix_ev_t ON rating_event(ticker, event_date);
CREATE INDEX ix_ev_d ON rating_event(event_date);
CREATE INDEX ix_ev_b ON rating_event(broker);

CREATE TABLE revision(
  ticker TEXT, as_of TEXT, fy INT, n_up_30d REAL, n_down_30d REAL, n_est REAL,
  PRIMARY KEY(ticker, as_of, fy));

CREATE TABLE price(ticker TEXT, date TEXT, close REAL, PRIMARY KEY(ticker, date));

CREATE TABLE target(                 -- target di consenso, uno snapshot per notte
  ticker TEXT, as_of TEXT, pt_mean REAL, pt_median REAL, pt_high REAL, pt_low REAL, price REAL,
  PRIMARY KEY(ticker, as_of));

CREATE TABLE ingest_log(file TEXT PRIMARY KEY, kind TEXT, snapshot_date TEXT,
  rows_in INT, rows_new INT);

-- ultimo rating noto di ciascun broker su ciascun titolo
CREATE VIEW v_broker_current AS
SELECT * FROM (
  SELECT e.*, ROW_NUMBER() OVER (PARTITION BY ticker, broker
         ORDER BY event_date DESC, first_seen DESC) AS rn
  FROM rating_event e WHERE to_num IS NOT NULL) WHERE rn = 1;

-- consenso: media dei rating correnti con evento negli ultimi 365 giorni
CREATE VIEW v_consensus AS
SELECT ticker, COUNT(*) AS n_brokers, ROUND(AVG(to_num), 2) AS mean_rating,
       SUM(to_num <= 2) AS n_buy, SUM(to_num = 3) AS n_hold, SUM(to_num >= 4) AS n_sell,
       MAX(event_date) AS last_event
FROM v_broker_current
WHERE event_date >= date((SELECT MAX(run_date) FROM snapshot), '-365 days')
GROUP BY ticker;

-- breadth delle revisioni e variazione rispetto allo snapshot precedente
CREATE VIEW v_revision_breadth AS
SELECT ticker, as_of, fy, n_up_30d, n_down_30d, n_est,
       n_up_30d - n_down_30d AS net,
       CASE WHEN COALESCE(n_est, n_up_30d + n_down_30d) > 0
            THEN (n_up_30d - n_down_30d) * 1.0 / COALESCE(n_est, n_up_30d + n_down_30d) END AS breadth,
       CASE WHEN n_est IS NULL THEN 'revisori' ELSE 'n_est' END AS breadth_base,
       LAG(as_of) OVER w AS prev_as_of,
       (CASE WHEN COALESCE(n_est, n_up_30d + n_down_30d) > 0
             THEN (n_up_30d - n_down_30d) * 1.0 / COALESCE(n_est, n_up_30d + n_down_30d) END)
       - LAG(CASE WHEN COALESCE(n_est, n_up_30d + n_down_30d) > 0
             THEN (n_up_30d - n_down_30d) * 1.0 / COALESCE(n_est, n_up_30d + n_down_30d) END) OVER w
         AS d_breadth
FROM revision WINDOW w AS (PARTITION BY ticker, fy ORDER BY as_of);

-- ultima chiusura disponibile per titolo
CREATE VIEW v_last_close AS
SELECT ticker, date, close FROM (
  SELECT p.*, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) rn FROM price p) WHERE rn = 1;

-- analisi con target: confronto con la chiusura del giorno e con l'ultima chiusura
CREATE VIEW v_pt_events AS
SELECT e.ticker, u.name, u.isin, e.event_date, e.broker, e.action, e.rating_to, e.pt_action,
       e.pt_from, e.pt_to, e.close_at_event,
       ROUND(100.0 * (e.pt_to / e.close_at_event - 1), 1) AS upside_at_event_pct,
       lc.date AS last_close_date, lc.close AS last_close,
       ROUND(100.0 * (e.pt_to / lc.close - 1), 1) AS upside_now_pct
FROM rating_event e LEFT JOIN universe u USING(ticker) LEFT JOIN v_last_close lc USING(ticker)
WHERE e.pt_to IS NOT NULL;

-- eventi classificati per rilevanza, come richiesto dal brief
CREATE VIEW v_moves AS
SELECT e.*, u.panel,
  CASE
    WHEN from_num IS NOT NULL AND to_num IS NOT NULL AND ABS(to_num - from_num) >= 2
      THEN CASE WHEN to_num < from_num THEN 'DBL_UP' ELSE 'DBL_DN' END
    WHEN from_num <= 2 AND to_num >= 3 THEN 'BH_DN'
    WHEN from_num >= 3 AND to_num <= 2 THEN 'BH_UP'
    WHEN from_num <= 3 AND to_num >= 4 THEN 'HS_DN'
    WHEN from_num >= 4 AND to_num <= 3 THEN 'HS_UP'
    WHEN action = 'up' THEN 'UP'
    WHEN action = 'down' THEN 'DN'
    WHEN action = 'init' THEN 'INIT'
    ELSE 'OTHER' END AS category
FROM rating_event e LEFT JOIN universe u USING(ticker);
"""


def nn(x):
    """NaN/'' -> None, float intero -> int."""
    if x is None or (isinstance(x, float) and x != x): return None
    if isinstance(x, str) and not x.strip(): return None
    if isinstance(x, float) and x.is_integer(): return int(x)
    return x


def snapshot_files(kind):
    out = []
    for p in sorted((DATA / kind).glob("*.csv")):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem):
            out.append((p.stem, p))
    return out


def update_health_history():
    """Accoda latest_health a health_history (append-only, dedup su run_date)."""
    hist, latest = DATA / "health_history.csv", DATA / "latest_health.csv"
    frames = [pd.read_csv(f) for f in (hist, latest) if f.exists()]
    if not frames: return pd.DataFrame()
    h = pd.concat(frames).drop_duplicates("run_date", keep="last").sort_values("run_date")
    h.to_csv(hist, index=False)
    return h


def build():
    BUILD.mkdir(exist_ok=True); SITE.mkdir(exist_ok=True)
    dbp = BUILD / "analyst.db"
    if dbp.exists(): dbp.unlink()
    con = sqlite3.connect(dbp); con.executescript(SCHEMA)

    u = pd.read_csv(ROOT / "universe.csv", dtype=str)
    ucols = ["ticker", "name", "long_name", "panel", "indices", "cap_tier", "isin", "currency", "exchange", "us_ticker"]
    for c in ucols:
        if c not in u.columns: u[c] = None
    u.loc[u["isin"] == "-", "isin"] = None
    con.executemany(f"INSERT OR IGNORE INTO universe VALUES({','.join('?'*len(ucols))})",
                    [tuple(nn(v) for v in r) for r in u[ucols].itertuples(index=False)])

    health = update_health_history()
    for r in health.to_dict("records"):
        con.execute("INSERT OR REPLACE INTO snapshot VALUES(?,?,?,?,?,?,?,?,?)",
                    tuple(nn(r.get(c)) for c in ("run_date", "tickers", "failed", "rating_rows", "revision_rows",
                                                  "unmapped_grades", "target_rows", "price_tickers", "pt_events")))

    # --- rating: dedup tra snapshot, first_seen = prima notte in cui compare
    rfiles = snapshot_files("ratings")
    first_snap = rfiles[0][0] if rfiles else None
    rbf = DATA / "ratings_backfill.csv.gz"          # storico completo scaricato una volta (non point-in-time)
    if rbf.exists(): rfiles = [("backfill", rbf)] + rfiles
    rbu = DATA / "ratings_backfill_us.csv.gz"       # storico dalle quotazioni USA/ADR dei titoli europei
    if rbu.exists(): rfiles = [("backfill", rbu)] + rfiles
    events = {}
    for snap, p in rfiles:
        df = pd.read_csv(p); new = 0
        for c in ("pt_action", "pt_from", "pt_to", "src"):
            if c not in df.columns: df[c] = None
        for r in df.itertuples(index=False):
            key = "|".join(str(nn(x)) for x in (r.ticker, r.event_date, r.broker, r.action, r.rating_from, r.rating_to))
            eid = hashlib.sha1(key.encode()).hexdigest()[:16]
            if eid in events:
                ev = events[eid]; ev["last_seen"] = snap; ev["n_seen"] += 1
                if nn(r.pt_to) is not None:        # i target arrivano dagli snapshot più recenti
                    ev["pt_action"], ev["pt_from"], ev["pt_to"] = nn(r.pt_action), nn(r.pt_from), nn(r.pt_to)
                    if nn(r.src) and nn(r.src) != r.ticker: ev["src"] = nn(r.src)
                continue
            fnum, tnum, reg = nn(r.rating_from_num), nn(r.rating_to_num), 0
            if fnum is None and nn(r.rating_from) and grade(r.rating_from): fnum, reg = grade(r.rating_from), 1
            if tnum is None and nn(r.rating_to) and grade(r.rating_to): tnum, reg = grade(r.rating_to), 1
            events[eid] = dict(event_id=eid, ticker=r.ticker, event_date=r.event_date, broker=nn(r.broker),
                               action=nn(r.action), rating_from=nn(r.rating_from), rating_to=nn(r.rating_to),
                               from_num=fnum, to_num=tnum, first_seen=None if snap == "backfill" else snap, last_seen=snap, n_seen=1,
                               is_backfill=int(snap in (first_snap, "backfill")), regraded=reg,
                               pt_action=nn(r.pt_action), pt_from=nn(r.pt_from), pt_to=nn(r.pt_to),
                               close_at_event=None, src=nn(r.src) or r.ticker, pt_ccy_from=None,
                               pt_to_raw=nn(r.pt_to), pt_from_raw=nn(r.pt_from))
            new += 1
        if snap != "backfill":
            con.execute("INSERT INTO snapshot(run_date) VALUES(?) ON CONFLICT DO NOTHING", (snap,))
        con.execute("INSERT INTO ingest_log VALUES(?,?,?,?,?)", (str(p.relative_to(ROOT)), "ratings", snap, len(df), new))
    # --- prezzi: prima lo storico scaricato una volta, poi i file notturni (più recenti sovrascrivono)
    bf = DATA / "prices_backfill.csv.gz"
    if bf.exists():
        df = pd.read_csv(bf).dropna()
        con.executemany("INSERT OR REPLACE INTO price VALUES(?,?,?)",
                        [(r.ticker, r.date, float(r.close)) for r in df.itertuples(index=False)])
        con.execute("INSERT INTO ingest_log VALUES(?,?,?,?,?)", (str(bf.relative_to(ROOT)), "prices", "backfill", len(df), len(df)))
    bu = DATA / "prices_backfill_us.csv.gz"
    if bu.exists():
        df = pd.read_csv(bu).dropna()
        con.executemany("INSERT OR IGNORE INTO price VALUES(?,?,?)",
                        [(r.ticker, r.date, float(r.close)) for r in df.itertuples(index=False)])
    for snap, p in snapshot_files("prices"):
        df = pd.read_csv(p).dropna()
        con.executemany("INSERT OR REPLACE INTO price VALUES(?,?,?)",
                        [(r.ticker, r.date, float(r.close)) for r in df.itertuples(index=False)])
        con.execute("INSERT INTO ingest_log VALUES(?,?,?,?,?)", (str(p.relative_to(ROOT)), "prices", snap, len(df), len(df)))
    for snap, p in snapshot_files("targets"):
        df = pd.read_csv(p)
        con.executemany("INSERT OR REPLACE INTO target VALUES(?,?,?,?,?,?,?)",
                        [tuple(nn(v) for v in r) for r in df[["ticker", "as_of", "pt_mean", "pt_median", "pt_high",
                                                                "pt_low", "price"]].itertuples(index=False)])
        con.execute("INSERT INTO ingest_log VALUES(?,?,?,?,?)", (str(p.relative_to(ROOT)), "targets", snap, len(df), len(df)))
    px = {}
    for t, d, c in con.execute("SELECT ticker, date, close FROM price ORDER BY ticker, date"):
        px.setdefault(t, ([], []))[0].append(d); px[t][1].append(c)
    usmap = dict(con.execute("SELECT ticker, us_ticker FROM universe WHERE us_ticker IS NOT NULL").fetchall())
    usmap, bad = validate_us(px, usmap)
    if bad:                                    # quotazione USA non coerente col titolo: via i suoi dati
        con.executemany("UPDATE universe SET us_ticker=NULL WHERE ticker=?", [(t,) for t in bad])
        for k in [k for k, e in events.items() if e["ticker"] in bad and e.get("src") == bad[e["ticker"]]]:
            del events[k]
        print("quotazioni USA scartate (prezzi non correlati):", bad)
    conv = normalize_targets(events, px, usmap)
    fix_consensus_targets(con, px, usmap)
    print(f"target convertiti dalla quotazione USA: {conv}")
    for e in events.values():                  # chiusura del giorno dell'analisi (o precedente)
        s_ = px.get(e["ticker"])
        if s_:
            i = bisect.bisect_right(s_[0], str(e["event_date"])) - 1
            if i >= 0 and (dt.date.fromisoformat(str(e["event_date"])) - dt.date.fromisoformat(s_[0][i])).days <= 5:
                e["close_at_event"] = s_[1][i]
    cols = list(next(iter(events.values())).keys()) if events else []
    if events:
        con.executemany(f"INSERT INTO rating_event({','.join(cols)}) VALUES({','.join('?'*len(cols))})",
                        [tuple(e[c] for c in cols) for e in events.values()])

    # --- revisioni: uno snapshot per notte, chiave (ticker, as_of, fy)
    for snap, p in snapshot_files("revisions"):
        df = pd.read_csv(p)
        if "n_est" not in df.columns: df["n_est"] = None
        rows = [(r.ticker, r.as_of, nn(r.fy), nn(r.n_up_30d), nn(r.n_down_30d), nn(r.n_est)) for r in df.itertuples(index=False)]
        before = con.total_changes
        con.executemany("INSERT OR IGNORE INTO revision VALUES(?,?,?,?,?,?)", rows)
        con.execute("INSERT INTO ingest_log VALUES(?,?,?,?,?)",
                    (str(p.relative_to(ROOT)), "revisions", snap, len(df), con.total_changes - before))
    con.commit()
    export(con)
    tpl = ROOT / "terminal" / "index.html"
    if tpl.exists(): shutil.copy(tpl, SITE / "index.html")
    n = con.execute("SELECT COUNT(*) FROM rating_event").fetchone()[0]
    print(f"db ok: {n} eventi unici, {len(rfiles)} snapshot rating -> {dbp}")
    con.close()


def px_at(px, t, d, maxgap=7):
    s_ = px.get(t)
    if not s_: return None
    i = bisect.bisect_right(s_[0], str(d)) - 1
    if i < 0 or (dt.date.fromisoformat(str(d)) - dt.date.fromisoformat(s_[0][i])).days > maxgap: return None
    return s_[1][i]


def validate_us(px, usmap, min_corr=0.5):
    """Tiene la quotazione USA solo se i rendimenti settimanali dell'ultimo anno sono correlati
    con quelli del titolo europeo: protegge da abbinamenti sbagliati (ticker omonimi)."""
    import numpy as np
    ok, bad = {}, {}
    for t, us in usmap.items():
        a, b = px.get(t), px.get(us)
        if not a or not b: bad[t] = us; continue
        sa = pd.Series(a[1], index=pd.to_datetime(a[0])).resample("W").last()
        sb = pd.Series(b[1], index=pd.to_datetime(b[0])).resample("W").last()
        j = pd.concat([sa, sb], axis=1).dropna().tail(60).pct_change().dropna()
        c = j.iloc[:, 0].corr(j.iloc[:, 1]) if len(j) >= 20 else np.nan
        (ok if c == c and c >= min_corr else bad)[t] = us
    return ok, bad


def normalize_targets(events, px, usmap):
    """Titoli europei con quotazione USA/ADR: Yahoo riporta spesso i target nella valuta (e per
    azione ADR) della quotazione USA, anche sul ticker europeo. Per ogni analisi si decide a quale
    prezzo si riferisce il target (quello più vicino in scala logaritmica, o la quotazione USA se il
    dato arriva da lì) e lo si converte nella valuta locale col rapporto dei prezzi dello stesso giorno
    (che incorpora cambio e rapporto ADR)."""
    import math
    n = 0
    for e in events.values():
        us = usmap.get(e["ticker"])
        if not us or not e["pt_to"]: continue
        pl, pu = px_at(px, e["ticker"], e["event_date"]), px_at(px, us, e["event_date"])
        if not pl or not pu: continue
        from_us = e.get("src") == us or abs(math.log(e["pt_to"] / pu)) < abs(math.log(e["pt_to"] / pl))
        if not from_us: continue
        f = pu / pl
        e["pt_to"] = round(e["pt_to"] / f, 4)
        if e["pt_from"]: e["pt_from"] = round(e["pt_from"] / f, 4)
        e["pt_ccy_from"] = us; n += 1
    return n


def fix_consensus_targets(con, px, usmap):
    """Stessa logica per il target di consenso Yahoo dei titoli europei con quotazione USA."""
    import math
    rows = con.execute(f"SELECT ticker, as_of, pt_mean, pt_median, pt_high, pt_low, price FROM target WHERE ticker IN "
                       f"({','.join('?' * len(usmap))})", tuple(usmap)).fetchall() if usmap else []
    for t, a, m, md, h, l, cur in rows:
        pl, pu = px_at(px, t, a), px_at(px, usmap[t], a)
        if not (m and pl and pu): continue
        ref = cur or m                   # Yahoo dà il prezzo corrente nella stessa valuta dei target
        if abs(math.log(ref / pu)) < abs(math.log(ref / pl)):
            f = pu / pl
            con.execute("UPDATE target SET pt_mean=?, pt_median=?, pt_high=?, pt_low=?, price=? WHERE ticker=? AND as_of=?",
                        (m / f, md / f if md else None, h / f if h else None, l / f if l else None, pl, t, a))


def export(con):
    q = lambda s, *a: con.execute(s, a).fetchall()
    brokers = [r[0] for r in q("SELECT DISTINCT broker FROM rating_event WHERE broker IS NOT NULL ORDER BY broker")]
    bi = {b: i for i, b in enumerate(brokers)}
    ev = q("""SELECT ticker, event_date, broker, action, rating_from, rating_to, from_num, to_num,
                     first_seen, is_backfill, pt_from, pt_to, pt_action, close_at_event, pt_ccy_from
              FROM rating_event WHERE event_date >= date((SELECT MAX(run_date) FROM snapshot), ?)
              ORDER BY event_date DESC, ticker""", f"-{EVENTS_DAYS_IN_JSON} days")
    snaps = [r[0] for r in q("SELECT DISTINCT as_of FROM revision ORDER BY as_of DESC LIMIT ?", REV_HISTORY_IN_JSON)]
    rev = q(f"""SELECT ticker, as_of, fy, n_up_30d, n_down_30d, n_est FROM revision
                WHERE as_of IN ({','.join('?'*len(snaps))}) ORDER BY as_of""", *snaps) if snaps else []
    # ultima e penultima chiusura
    last = {}
    for t, d, c, rn in q("""SELECT ticker, date, close, rn FROM (SELECT p.*, ROW_NUMBER() OVER
                             (PARTITION BY ticker ORDER BY date DESC) rn FROM price p) WHERE rn <= 2"""):
        if rn == 1: last[t] = [d, c, None]
        elif t in last: last[t][2] = c
    tg = {t: [m, md, h, l, a] for t, a, m, md, h, l in q(
        """SELECT ticker, as_of, pt_mean, pt_median, pt_high, pt_low FROM (SELECT t.*, ROW_NUMBER() OVER
           (PARTITION BY ticker ORDER BY as_of DESC) rn FROM target t) WHERE rn = 1""")}
    out = {
        "meta": {"built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                 "as_of": q("SELECT MAX(run_date) FROM snapshot")[0][0],
                 "repo": "marcomazzon27-prog/analyst-snapshot"},
        "universe": {r[0]: list(r[1:]) for r in q(
            "SELECT ticker, name, panel, indices, isin, currency, cap_tier, long_name, exchange, us_ticker FROM universe")},
        "brokers": brokers,
        "events": [[t, d, bi.get(b, -1), a, rf, rt, fn, tn, fs, bf, pf, pt, pa, cx, cu]
                   for t, d, b, a, rf, rt, fn, tn, fs, bf, pf, pt, pa, cx, cu in ev],
        "revisions": [list(r) for r in rev],
        "last": last,
        "targets": tg,
        "snapshots": [list(r) for r in q("SELECT * FROM snapshot ORDER BY run_date")],
        "ingest": [list(r) for r in q("SELECT * FROM ingest_log ORDER BY snapshot_date, kind")],
        "tables": {t: q(f"SELECT COUNT(*) FROM {t}")[0][0]
                   for t in ("universe", "snapshot", "rating_event", "revision", "price", "target", "ingest_log")},
    }
    (SITE / "terminal.json").write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    # serie storiche per titolo (ultimi 3 anni), caricate dal terminale solo quando apri la scheda
    tdir = SITE / "t"; tdir.mkdir(exist_ok=True)
    as_of = out["meta"]["as_of"] or dt.date.today().isoformat()
    start = (dt.date.fromisoformat(as_of) - dt.timedelta(days=3 * 366)).isoformat()
    series = {}
    for t, d, c in q("SELECT ticker, date, close FROM price WHERE date >= ? ORDER BY ticker, date", start):
        s_ = series.setdefault(t, {"d": [], "c": [], "tg": [], "apt": []}); s_["d"].append(d); s_["c"].append(c)
    for t, a, m in q("SELECT ticker, as_of, pt_mean FROM target WHERE pt_mean IS NOT NULL ORDER BY ticker, as_of"):
        series.setdefault(t, {"d": [], "c": [], "tg": [], "apt": []})["tg"].append([a, m])
    apt = avg_target_history(con, as_of)
    for t, v in apt.items():
        if t in series: series[t]["apt"] = v
    for t, s_ in series.items():
        (tdir / f"{t}.json").write_text(json.dumps(s_, separators=(",", ":")))
    export_indices(con, series, apt, as_of)


WEEKS = 112                            # ~26 mesi di storico del target medio


def week_ends(as_of):
    d = dt.date.fromisoformat(as_of)
    d -= dt.timedelta(days=(d.weekday() - 4) % 7)      # ultimo venerdì
    return [(d - dt.timedelta(weeks=k)).isoformat() for k in range(WEEKS, -1, -1)]


def avg_target_history(con, as_of):
    """Target medio degli analisti nel tempo: a ogni venerdì, media dell'ultimo target di
    ciascun broker emesso nei 12 mesi precedenti (come il "previous average price target")."""
    wk = week_ends(as_of)
    first = (dt.date.fromisoformat(wk[0]) - dt.timedelta(days=366)).isoformat()
    ev = {}
    for t, d, b, p in con.execute("""SELECT ticker, event_date, broker, pt_to FROM rating_event
                                     WHERE pt_to IS NOT NULL AND broker IS NOT NULL AND event_date >= ?
                                     ORDER BY ticker, event_date""", (first,)):
        ev.setdefault(t, []).append((d, b, p))
    out = {}
    for t, rows in ev.items():
        res, last, k = [], {}, 0
        for w in wk:
            while k < len(rows) and rows[k][0] <= w:
                last[rows[k][1]] = (rows[k][0], rows[k][2]); k += 1
            lo = (dt.date.fromisoformat(w) - dt.timedelta(days=365)).isoformat()
            v = [p for d, p in last.values() if d > lo]
            if v: res.append([w, round(sum(v) / len(v), 4), len(v)])
        if res: out[t] = res
    return out


INDEX_BENCH = {"SP500": "SPY", "UKX": "^FTSE", "MCX": "^FTMC", "FTSEMIB": "FTSEMIB.MI",
               "DAX": "^GDAXI", "CAC": "^FCHI", "SX5E": "^STOXX50E"}


def export_indices(con, series, apt, as_of):
    """Vista aggregata per indice: livello dell'indice e upside implicito mediano dei componenti nel tempo."""
    idir = SITE / "idx"; idir.mkdir(exist_ok=True)
    members = {}
    for t, idx in con.execute("SELECT ticker, indices FROM universe"):
        for k in str(idx or "").split(";"):
            if k: members.setdefault(k, []).append(t)
    wk = week_ends(as_of)
    close_at = {}
    for t, s_ in series.items():
        d, c = s_["d"], s_["c"]
        if d: close_at[t] = (d, c)

    def px(t, w):
        if t not in close_at: return None
        d, c = close_at[t]
        import bisect as _b
        i = _b.bisect_right(d, w) - 1
        return c[i] if i >= 0 else None

    for key, tk in members.items():
        up = []
        for w in wk:
            vals = []
            for t in tk:
                a = next((x[1] for x in reversed(apt.get(t, [])) if x[0] <= w), None) if t in apt else None
                if a is None: continue
                p = px(t, w)
                if p: vals.append(a / p - 1)
            if len(vals) >= 5:
                vals.sort(); m = len(vals)
                up.append([w, round(vals[m // 2], 5), round(vals[m // 4], 5), round(vals[(3 * m) // 4], 5), m])
        b = series.get(INDEX_BENCH.get(key, ""), {})
        (idir / f"{key}.json").write_text(json.dumps(
            {"key": key, "bench": INDEX_BENCH.get(key), "d": b.get("d", []), "c": b.get("c", []),
             "up": up, "members": tk}, separators=(",", ":")))


if __name__ == "__main__":
    build()
