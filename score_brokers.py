"""Classifica dei broker per accuratezza delle previsioni (ricalcolata ogni notte).

Per ogni analisi (rating e target price) misura cosa ha fatto davvero il titolo DOPO,
rispetto al suo indice di riferimento, e costruisce un punteggio per broker.

Metodo (in breve)
  1. Ingresso = chiusura del giorno dell'analisi (dopo la reazione del mercato: si misura
     la capacità di previsione, non l'impatto della notizia).
  2. Rendimento in eccesso sull'indice a 1, 3, 6 e 12 mesi (21/63/126/252 sedute).
  3. Segnale dell'analisi: +1 Buy/upgrade, -1 Sell/downgrade, 0 Hold (escluso dal tasso di successo).
  4. Metriche per broker (orizzonte principale 3 mesi):
       - tasso di successo: il titolo ha battuto/perso l'indice nella direzione indicata
       - alfa medio: rendimento in eccesso "firmato" dal segnale
       - IC: correlazione di rango fra upside implicito del target e rendimento in eccesso
       - target: quota di target raggiunti entro 12 mesi ed errore mediano a 12 mesi
     Tassi e medie sono ridotti verso la media generale (empirical Bayes) in proporzione
     al campione: un broker con 5 analisi fortunate non scavalca uno con 500.
  5. Modello ML: regressione ridge (regolarizzata, alfa scelto con cross-validation
     temporale) del rendimento in eccesso a 3 mesi su segnale, upside implicito, momentum,
     volatilità, mercato e l'interazione segnale x broker. Il coefficiente di ogni broker
     è la sua "abilità" al netto di questi fattori comuni.
  6. Punteggio 0-100 = media pesata dei punteggi standardizzati; classifica solo con
     almeno MIN_N analisi valutate.
  7. Test di affidabilità: la classifica calcolata sulle analisi più vecchie predice il
     risultato dei broker su quelle più recenti? (correlazione di rango fuori campione)

Uso: python score_brokers.py   (dopo build_db.py; legge build/analyst.db)
Output: tabelle event_eval e broker_score nel db, site/brokers.json,
        data/broker_ranking_history.csv (una riga per broker per notte, append-only).
"""
import datetime as dt, json, sqlite3, warnings
from pathlib import Path
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
ROOT = Path(__file__).parent
DB, SITE, DATA = ROOT / "build" / "analyst.db", ROOT / "site", ROOT / "data"
H = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}
MAIN = "3m"
ALPHAS = np.logspace(0, 7, 29)
MIN_N = 20            # analisi valutate (con segnale) per entrare in classifica
PRIOR_K = 30          # forza della riduzione verso la media (in "analisi equivalenti")
W = {"ml": .30, "hit": .25, "alpha": .20, "ic": .10, "pt_hit": .075, "pt_err": .075}
try:
    from collect import BENCH, bench_for
except Exception:                                    # pragma: no cover
    BENCH = {}
    def bench_for(panel, indices): return None


def load(con):
    ev = pd.read_sql("""SELECT e.event_id, e.ticker, e.event_date, e.broker, e.action, e.from_num, e.to_num,
                               e.pt_from, e.pt_to, u.panel, u.indices
                        FROM rating_event e LEFT JOIN universe u USING(ticker)
                        WHERE e.broker IS NOT NULL AND e.to_num IS NOT NULL""", con)
    px = pd.read_sql("SELECT ticker, date, close FROM price", con)
    wide = px.pivot(index="date", columns="ticker", values="close").sort_index()
    wide.index = pd.to_datetime(wide.index)
    wide = wide[wide.index.dayofweek < 5].ffill(limit=5)
    return ev, wide


def benchmark_series(wide, uni):
    """Serie indice per mercato; se l'indice manca, media equipesata dei titoli di quel mercato."""
    rets = wide.pct_change(fill_method=None)
    out = {}
    for key, grp in uni.groupby("bkey"):
        t = BENCH.get(key)
        if t and t in wide.columns and wide[t].notna().sum() > 50:
            out[key] = wide[t].ffill()
        else:
            cols = [c for c in grp["ticker"] if c in rets.columns]
            if not cols: continue
            ew = rets[cols].clip(-.5, .5).mean(axis=1).fillna(0)
            out[key] = (1 + ew).cumprod()
    return pd.DataFrame(out)


def signal(r):
    if r.action in ("up", "down") and pd.notna(r.from_num) and pd.notna(r.to_num) and r.from_num != r.to_num:
        return 1 if r.to_num < r.from_num else -1
    return 1 if r.to_num <= 2 else (-1 if r.to_num >= 4 else 0)


def evaluate(ev, wide, bench, uni):
    dates = wide.index
    arr = wide.to_numpy(dtype=float)
    col = {t: i for i, t in enumerate(wide.columns)}
    barr = {k: bench[k].reindex(dates).ffill().to_numpy(dtype=float) for k in bench.columns}
    bkey = dict(zip(uni["ticker"], uni["bkey"]))
    rows = []
    last = len(dates) - 1
    for r in ev.itertuples(index=False):
        j = col.get(r.ticker)
        if j is None: continue
        i = dates.searchsorted(pd.Timestamp(r.event_date))       # prima seduta >= data analisi
        if i > last or i < 21: continue
        p0 = arr[i, j]
        if not np.isfinite(p0) or p0 <= 0: continue
        b = barr.get(bkey.get(r.ticker))
        s = signal(r)
        d = dict(event_id=r.event_id, ticker=r.ticker, broker=r.broker, event_date=r.event_date,
                 panel=r.panel, signal=s, entry=p0, pt=r.pt_to,
                 upside=(r.pt_to / p0 - 1) if pd.notna(r.pt_to) and r.pt_to > 0 else np.nan,
                 pt_chg=(r.pt_to / r.pt_from - 1) if pd.notna(r.pt_to) and pd.notna(r.pt_from) and r.pt_from > 0 else np.nan)
        # fattori noti al momento dell'analisi (nessun dato futuro)
        p21 = arr[i - 21, j]
        d["mom_1m"] = p0 / p21 - 1 if np.isfinite(p21) and p21 > 0 else np.nan
        w = arr[max(0, i - 63):i + 1, j]
        lr = np.diff(np.log(w[np.isfinite(w)])) if np.isfinite(w).sum() > 10 else []
        d["vol_3m"] = float(np.std(lr) * np.sqrt(252)) if len(lr) else np.nan
        for k, h in H.items():
            if i + h > last:
                d[f"ret_{k}"] = d[f"exc_{k}"] = np.nan; continue
            p1 = arr[i + h, j]
            ret = p1 / p0 - 1 if np.isfinite(p1) else np.nan
            bret = (b[i + h] / b[i] - 1) if b is not None and np.isfinite(b[i]) and b[i] > 0 else 0.0
            d[f"ret_{k}"], d[f"exc_{k}"] = ret, ret - bret
        # target: raggiunto entro 12 mesi? errore a 12 mesi
        d["pt_hit"] = d["pt_err"] = np.nan
        if pd.notna(d["upside"]) and abs(d["upside"]) > .005 and i + H["12m"] <= last:
            path = arr[i + 1:i + H["12m"] + 1, j]
            path = path[np.isfinite(path)]
            if len(path):
                d["pt_hit"] = float(path.max() >= r.pt_to) if r.pt_to > p0 else float(path.min() <= r.pt_to)
                d["pt_err"] = abs(path[-1] / r.pt_to - 1)
        rows.append(d)
    e = pd.DataFrame(rows)
    for k in H:
        e[f"exc_{k}"] = e[f"exc_{k}"].clip(-1, 3)
        e[f"hit_{k}"] = np.where((e["signal"] != 0) & e[f"exc_{k}"].notna(),
                                 (np.sign(e[f"exc_{k}"]) == e["signal"]).astype(float), np.nan)
        e[f"alpha_{k}"] = np.where(e["signal"] != 0, e["signal"] * e[f"exc_{k}"], np.nan)
    return e


def shrink(sum_, n, prior, k=PRIOR_K):
    return (sum_ + k * prior) / (n + k)


def ml_skill(e):
    """Ridge: exc_3m ~ controlli + segnale x broker. Ritorna coef per broker e diagnostica."""
    from sklearn.linear_model import RidgeCV
    from sklearn.model_selection import TimeSeriesSplit
    d = e[e[f"exc_{MAIN}"].notna() & (e["signal"] != 0)].sort_values("event_date").copy()
    if len(d) < 200 or d["broker"].nunique() < 3:
        return {}, {"n": int(len(d)), "note": "campione troppo piccolo per il modello"}
    y = d[f"exc_{MAIN}"].clip(-.6, .6).to_numpy()
    up = d["upside"].clip(-.5, 1.0)
    X = pd.DataFrame({"signal": d["signal"],
                      "upside": up.fillna(0), "upside_na": up.isna().astype(float),
                      "mom_1m": d["mom_1m"].clip(-.5, .5).fillna(0),
                      "vol_3m": d["vol_3m"].clip(0, 2).fillna(d["vol_3m"].median()),
                      "sig_x_mom": d["signal"] * d["mom_1m"].clip(-.5, .5).fillna(0)})
    X = pd.concat([X, pd.get_dummies(d["panel"].fillna("NA"), prefix="mkt", dtype=float)], axis=1)
    B = pd.get_dummies(d["broker"], dtype=float).mul(d["signal"].to_numpy(), axis=0)
    B.columns = ["b::" + c for c in B.columns]
    X = pd.concat([X.reset_index(drop=True), B.reset_index(drop=True)], axis=1)
    mu, sd = X.mean(), X.std().replace(0, 1)
    Xs = ((X - mu) / sd).to_numpy()
    cv = TimeSeriesSplit(n_splits=5)
    m = RidgeCV(alphas=ALPHAS, cv=cv).fit(Xs, y)
    coef = pd.Series(m.coef_, index=X.columns) / sd          # effetto per unità originale
    skill = {c[3:]: float(v) for c, v in coef.items() if c.startswith("b::")}
    # R^2 fuori campione sull'ultimo fold (onestà: di solito è molto basso)
    tr, te = list(cv.split(Xs))[-1]
    from sklearn.linear_model import Ridge
    r2 = Ridge(alpha=m.alpha_).fit(Xs[tr], y[tr]).score(Xs[te], y[te])
    informative = m.alpha_ < ALPHAS.max() and r2 > 0
    if not informative:          # la CV ha scelto la massima regolarizzazione: nessun segnale per broker
        skill = {}
    return skill, {"n": int(len(d)), "alpha": float(m.alpha_), "oos_r2": float(r2), "informative": bool(informative),
                   "coef_signal": float(coef["signal"]), "coef_upside": float(coef["upside"])}


def broker_table(e, skill):
    g_hit = np.nanmean(e[f"hit_{MAIN}"]); g_alpha = np.nanmean(e[f"alpha_{MAIN}"])
    g_pth = np.nanmean(e["pt_hit"]) if e["pt_hit"].notna().any() else np.nan
    g_pte = np.nanmedian(e["pt_err"]) if e["pt_err"].notna().any() else np.nan
    out = []
    for b, x in e.groupby("broker"):
        h = x[f"hit_{MAIN}"].dropna(); a = x[f"alpha_{MAIN}"].dropna()
        pth = x["pt_hit"].dropna(); pte = x["pt_err"].dropna()
        ic_d = x[["upside", f"exc_{MAIN}"]].dropna()
        ic = ic_d["upside"].rank().corr(ic_d[f"exc_{MAIN}"].rank()) if len(ic_d) >= 15 else np.nan
        r = {"broker": b, "n_events": int(len(x)), "n_eval": int(len(h)), "n_tickers": int(x["ticker"].nunique()),
             "n_pt_eval": int(len(pth)), "last_event": x["event_date"].max(),
             "hit_raw": h.mean() if len(h) else np.nan, "hit": shrink(h.sum(), len(h), g_hit),
             "alpha_raw": a.mean() if len(a) else np.nan, "alpha": shrink(a.sum(), len(a), g_alpha),
             "ic": ic * len(ic_d) / (len(ic_d) + PRIOR_K) if pd.notna(ic) else np.nan,
             "pt_hit": shrink(pth.sum(), len(pth), g_pth) if len(pth) and pd.notna(g_pth) else np.nan,
             "pt_err": (pte.median() * len(pte) + g_pte * PRIOR_K) / (len(pte) + PRIOR_K) if len(pte) else np.nan,
             "ml": skill.get(b, np.nan)}
        for k in H:
            hh = x[f"hit_{k}"].dropna()
            r[f"hit_{k}"] = hh.mean() if len(hh) >= 5 else np.nan
            r[f"n_{k}"] = int(len(hh))
        out.append(r)
    t = pd.DataFrame(out)
    if t.empty: return t
    elig = t["n_eval"] >= MIN_N
    z = pd.DataFrame(index=t.index)
    for k, sign in (("ml", 1), ("hit", 1), ("alpha", 1), ("ic", 1), ("pt_hit", 1), ("pt_err", -1)):
        v = t.loc[elig, k] * sign
        z[k] = ((t[k] * sign - v.mean()) / (v.std() or 1)).clip(-3, 3) if v.notna().sum() >= 3 else np.nan
    wz = sum(z[k].fillna(0) * w for k, w in W.items())
    wsum = sum(z[k].notna() * w for k, w in W.items()).replace(0, np.nan)
    t["zscore"] = wz / wsum
    t.loc[~elig, "zscore"] = np.nan
    t["score"] = (t["zscore"].rank(pct=True) * 100).round(1)
    t = t.sort_values(["score", "n_eval"], ascending=[False, False]).reset_index(drop=True)
    t["rank"] = np.where(t["score"].notna(), t["score"].rank(ascending=False, method="min"), np.nan)
    return t


def persistence(e):
    """Classifica sulle analisi vecchie vs risultati su quelle recenti (fuori campione)."""
    d = e[e[f"alpha_{MAIN}"].notna()].sort_values("event_date")
    if len(d) < 400: return {"note": "storico ancora troppo corto", "n": int(len(d))}
    cut = d["event_date"].iloc[int(len(d) * .6)]
    old, new = d[d["event_date"] < cut], d[d["event_date"] >= cut]
    a = old.groupby("broker")["alpha_" + MAIN].agg(["sum", "count"])
    b = new.groupby("broker")["alpha_" + MAIN].agg(["mean", "count"])
    j = a.join(b, how="inner", lsuffix="_o")
    j = j[(j["count_o"] >= 10) & (j["count"] >= 10)]
    if len(j) < 8: return {"note": "pochi broker con dati in entrambi i periodi", "n_brokers": int(len(j))}
    past = shrink(j["sum"], j["count_o"], old["alpha_" + MAIN].mean())
    rho = past.rank().corr(j["mean"].rank())
    return {"cutoff": cut, "n_brokers": int(len(j)), "rank_corr": float(rho)}


def backtest(e, horizon_days=92, train_days=730, min_train=10, min_test=5):
    """Test fuori campione ripetuto ogni trimestre.

    A ogni data di taglio c: punteggio di ogni broker calcolato SOLO sulle analisi il cui esito
    a 3 mesi era già noto in c (ultimi 2 anni); poi si guarda come sono andate le sue analisi
    nei 3 mesi successivi. Se la classifica ha valore, i due ordinamenti sono correlati (rho>0)
    e i broker del terzo alto fanno meglio di quelli del terzo basso (spread>0)."""
    d = e[e[f"alpha_{MAIN}"].notna()][["broker", "event_date", f"alpha_{MAIN}"]].copy()
    if d.empty: return {"periods": [], "ranks": {}}
    d["t"] = pd.to_datetime(d["event_date"]); a = f"alpha_{MAIN}"
    start = d["t"].min() + pd.Timedelta(days=365); end = d["t"].max()
    periods, ranks = [], {}
    for c in pd.date_range(start, end, freq="QE"):
        tr = d[(d["t"] > c - pd.Timedelta(days=train_days)) & (d["t"] <= c - pd.Timedelta(days=horizon_days))]
        te = d[(d["t"] > c) & (d["t"] <= c + pd.Timedelta(days=horizon_days))]
        if len(tr) < 200 or len(te) < 100: continue
        g = tr.groupby("broker")[a].agg(["sum", "count"]); g = g[g["count"] >= min_train]
        sc = shrink(g["sum"], g["count"], tr[a].mean())
        for rk, (b, _) in enumerate(sc.sort_values(ascending=False).items(), 1):
            ranks.setdefault(b, []).append([c.date().isoformat(), rk, len(sc)])
        h = te.groupby("broker")[a].agg(["mean", "count"]); h = h[h["count"] >= min_test]
        j = pd.concat([sc.rename("score"), h], axis=1, join="inner")
        if len(j) < 8: continue
        rho = j["score"].rank().corr(j["mean"].rank())
        q = j["score"].rank(pct=True)
        top, bot = j[q > 2 / 3], j[q <= 1 / 3]
        spread = np.average(top["mean"], weights=top["count"]) - np.average(bot["mean"], weights=bot["count"])
        periods.append({"cutoff": c.date().isoformat(), "n_brokers": int(len(j)), "rho": float(rho), "spread": float(spread)})
    out = {"periods": periods, "ranks": ranks}
    if len(periods) >= 4:
        r = np.array([p["rho"] for p in periods]); sp = np.array([p["spread"] for p in periods])
        out.update(n=len(periods), mean_rho=float(r.mean()), pos_share=float((r > 0).mean()),
                   t_stat=float(r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))) if r.std(ddof=1) > 0 else 0.0,
                   mean_spread=float(sp.mean()))
    return out


def main():
    con = sqlite3.connect(DB)
    ev, wide = load(con)
    uni = pd.read_sql("SELECT ticker, panel, indices FROM universe", con)
    uni["bkey"] = [bench_for(p, i) or (p or "NA") for p, i in zip(uni["panel"], uni["indices"])]
    bench = benchmark_series(wide, uni)
    e = evaluate(ev, wide, bench, uni)
    if e.empty:
        print("nessuna analisi valutabile (servono prezzi dopo la data dell'analisi)"); return
    skill, diag = ml_skill(e)
    t = broker_table(e, skill)
    pers = persistence(e)
    bt = backtest(e)
    as_of = con.execute("SELECT MAX(run_date) FROM snapshot").fetchone()[0]
    e.to_sql("event_eval", con, if_exists="replace", index=False)
    t.to_sql("broker_score", con, if_exists="replace", index=False)
    con.commit(); con.close()

    SITE.mkdir(exist_ok=True)
    cols = ["rank", "broker", "score", "n_events", "n_eval", "n_tickers", "hit", "hit_raw", "alpha", "alpha_raw", "ic",
            "pt_hit", "pt_err", "n_pt_eval", "ml", "hit_1m", "hit_3m", "hit_6m", "hit_12m",
            "n_1m", "n_3m", "n_6m", "n_12m", "last_event"]
    clean = lambda v: None if (isinstance(v, float) and not np.isfinite(v)) else (round(v, 5) if isinstance(v, float) else v)
    g = {"hit": float(np.nanmean(e[f"hit_{MAIN}"])), "alpha": float(np.nanmean(e[f"alpha_{MAIN}"])),
         "n_eval": int(e[f"hit_{MAIN}"].notna().sum()),
         "pt_hit": float(np.nanmean(e["pt_hit"])) if e["pt_hit"].notna().any() else None,
         "n_pt": int(e["pt_hit"].notna().sum())}
    out = {"as_of": as_of, "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "horizon": MAIN, "min_n": MIN_N, "weights": W, "global": g, "model": diag, "persistence": pers,
           "backtest": {k: v for k, v in bt.items() if k != "ranks"}, "rank_hist": bt.get("ranks", {}),
           "coverage": {"first_event": e["event_date"].min(), "n_events": int(len(e)),
                        "by_year": {str(y): int(n) for y, n in e[f"alpha_{MAIN}"].notna().groupby(e["event_date"].str[:4]).sum().items()}},
           "benchmarks": {k: (BENCH.get(k) if BENCH.get(k) in wide.columns else "media equipesata") for k in bench.columns},
           "cols": cols, "rows": [[clean(r[c]) for c in cols] for r in t.to_dict("records")]}
    (SITE / "brokers.json").write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False, default=str))

    hist = DATA / "broker_ranking_history.csv"
    prev = {}
    if hist.exists():
        h0 = pd.read_csv(hist); h0 = h0[h0["as_of"] != as_of]
        if len(h0):
            last = h0[h0["as_of"] == h0["as_of"].max()]
            prev = dict(zip(last["broker"], last["rank"]))
    out["prev_rank"] = {b: int(r) for b, r in prev.items()}
    (SITE / "brokers.json").write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False, default=str))
    new = t[t["rank"].notna()][["rank", "broker", "score", "n_eval", "hit", "alpha", "ml"]].copy()
    new.insert(0, "as_of", as_of)
    if hist.exists():
        old = pd.read_csv(hist); old = old[old["as_of"] != as_of]
        new = pd.concat([old, new])
    new.round(5).to_csv(hist, index=False)
    ranked = t[t["rank"].notna()]
    print(f"broker valutati: {len(t)}, in classifica: {len(ranked)}; analisi valutate a {MAIN}: {g['n_eval']}; "
          f"modello: {diag}; persistenza: {pers}; backtest: { {k: v for k, v in bt.items() if k not in ('ranks', 'periods')} }")
    print(ranked.head(10)[["rank", "broker", "score", "n_eval", "hit", "alpha", "ml"]].to_string(index=False))


if __name__ == "__main__":
    main()
