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
import datetime as dt, hashlib, json, re, shutil, sqlite3
from pathlib import Path
import pandas as pd

try:
    from collect import grade          # stessa mappa grade->1..5 della raccolta
except Exception:                      # pragma: no cover
    def grade(x): return None

ROOT = Path(__file__).parent
DATA, BUILD, SITE = ROOT / "data", ROOT / "build", ROOT / "site"
REV_HISTORY_IN_JSON = 60               # snapshot di revisioni esportati al terminale

SCHEMA = """
CREATE TABLE universe(ticker TEXT PRIMARY KEY, panel TEXT, cap_tier TEXT);

CREATE TABLE snapshot(               -- un record per notte di raccolta
  run_date TEXT PRIMARY KEY, tickers INT, failed INT, rating_rows INT,
  revision_rows INT, unmapped_grades TEXT);

CREATE TABLE rating_event(           -- evento unico, deduplicato tra snapshot
  event_id TEXT PRIMARY KEY, ticker TEXT, event_date TEXT, broker TEXT,
  action TEXT, rating_from TEXT, rating_to TEXT, from_num INT, to_num INT,
  first_seen TEXT,                   -- primo snapshot che lo contiene (point-in-time)
  last_seen TEXT, n_seen INT,
  is_backfill INT,                   -- 1 = arrivato col primo carico, non point-in-time
  regraded INT);                     -- 1 = grade numerico ricalcolato con la mappa attuale
CREATE INDEX ix_ev_t ON rating_event(ticker, event_date);
CREATE INDEX ix_ev_d ON rating_event(event_date);
CREATE INDEX ix_ev_b ON rating_event(broker);

CREATE TABLE revision(
  ticker TEXT, as_of TEXT, fy INT, n_up_30d REAL, n_down_30d REAL, n_est REAL,
  PRIMARY KEY(ticker, as_of, fy));

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

    u = pd.read_csv(ROOT / "universe.csv")
    con.executemany("INSERT OR IGNORE INTO universe VALUES(?,?,?)",
                    [tuple(nn(v) for v in r) for r in u[["ticker", "panel", "cap_tier"]].itertuples(index=False)])

    health = update_health_history()
    for r in health.itertuples(index=False):
        con.execute("INSERT OR REPLACE INTO snapshot VALUES(?,?,?,?,?,?)",
                    (r.run_date, nn(r.tickers), nn(r.failed), nn(r.rating_rows),
                     nn(r.revision_rows), nn(r.unmapped_grades)))

    # --- rating: dedup tra snapshot, first_seen = prima notte in cui compare
    rfiles = snapshot_files("ratings")
    first_snap = rfiles[0][0] if rfiles else None
    events = {}
    for snap, p in rfiles:
        df = pd.read_csv(p); new = 0
        for r in df.itertuples(index=False):
            key = "|".join(str(nn(x)) for x in (r.ticker, r.event_date, r.broker, r.action, r.rating_from, r.rating_to))
            eid = hashlib.sha1(key.encode()).hexdigest()[:16]
            if eid in events:
                ev = events[eid]; ev["last_seen"] = snap; ev["n_seen"] += 1; continue
            fnum, tnum, reg = nn(r.rating_from_num), nn(r.rating_to_num), 0
            if fnum is None and nn(r.rating_from) and grade(r.rating_from): fnum, reg = grade(r.rating_from), 1
            if tnum is None and nn(r.rating_to) and grade(r.rating_to): tnum, reg = grade(r.rating_to), 1
            events[eid] = dict(event_id=eid, ticker=r.ticker, event_date=r.event_date, broker=nn(r.broker),
                               action=nn(r.action), rating_from=nn(r.rating_from), rating_to=nn(r.rating_to),
                               from_num=fnum, to_num=tnum, first_seen=snap, last_seen=snap, n_seen=1,
                               is_backfill=int(snap == first_snap), regraded=reg)
            new += 1
        con.execute("INSERT INTO snapshot(run_date) VALUES(?) ON CONFLICT DO NOTHING", (snap,))
        con.execute("INSERT INTO ingest_log VALUES(?,?,?,?,?)", (str(p.relative_to(ROOT)), "ratings", snap, len(df), new))
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


def export(con):
    q = lambda s, *a: con.execute(s, a).fetchall()
    brokers = [r[0] for r in q("SELECT DISTINCT broker FROM rating_event WHERE broker IS NOT NULL ORDER BY broker")]
    bi = {b: i for i, b in enumerate(brokers)}
    ev = q("""SELECT ticker, event_date, broker, action, rating_from, rating_to, from_num, to_num,
                     first_seen, is_backfill FROM rating_event ORDER BY event_date DESC, ticker""")
    snaps = [r[0] for r in q("SELECT DISTINCT as_of FROM revision ORDER BY as_of DESC LIMIT ?", REV_HISTORY_IN_JSON)]
    rev = q(f"""SELECT ticker, as_of, fy, n_up_30d, n_down_30d, n_est FROM revision
                WHERE as_of IN ({','.join('?'*len(snaps))}) ORDER BY as_of""", *snaps) if snaps else []
    out = {
        "meta": {"built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                 "as_of": q("SELECT MAX(run_date) FROM snapshot")[0][0],
                 "repo": "marcomazzon27-prog/analyst-snapshot"},
        "universe": {t: [p, c] for t, p, c in q("SELECT * FROM universe")},
        "brokers": brokers,
        "events": [[t, d, bi.get(b, -1), a, rf, rt, fn, tn, fs, bf] for t, d, b, a, rf, rt, fn, tn, fs, bf in ev],
        "revisions": [list(r) for r in rev],
        "snapshots": [list(r) for r in q("SELECT * FROM snapshot ORDER BY run_date")],
        "ingest": [list(r) for r in q("SELECT * FROM ingest_log ORDER BY snapshot_date, kind")],
        "tables": {t: q(f"SELECT COUNT(*) FROM {t}")[0][0]
                   for t in ("universe", "snapshot", "rating_event", "revision", "ingest_log")},
    }
    (SITE / "terminal.json").write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))


if __name__ == "__main__":
    build()
