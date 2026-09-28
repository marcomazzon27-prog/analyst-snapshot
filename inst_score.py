"""Investitori istituzionali – rating "chi conviene seguire" ed export per il terminale.

Metodo (dettagli in claude/piano-tracker-istituzionali.md)
  Portafoglio "follower": si compra il 13F alla prima chiusura settimanale dopo la data di deposito
  (non a fine trimestre: quello non è replicabile) e lo si tiene fino al deposito successivo (max 200 giorni).
  Solo posizioni lunghe azionarie mappate a un ticker con prezzi; pesi = valore dichiarato.
  Trade (tra due trimestri consecutivi, azioni "rettificate" = valore / prezzo rettificato di fine trimestre):
    nuova, incremento (>1,2x), riduzione (<0,8x), uscita; solo se il peso coinvolto è >= 0,5%.
    Esito = rendimento del titolo meno SPY a 13/26/52 settimane dal deposito (segno invertito per vendite).
  Score 0-100 sugli ultimi 5 anni (minimo 12 trimestri), percentili di:
    alpha follower 35% · alpha dei trade (26 sett.) 25% · hit rate 15% · costanza (trimestri battuti) 15% ·
    drawdown 10%; ogni misura è "ristretta" verso la media con peso n/(n+k) (k=8 trimestri, 30 trade).
  Test fuori campione: a ogni fine anno si calcola lo score solo con i dati disponibili allora e lo si
  confronta con l'alpha follower dei 12 mesi successivi (rho di Spearman, top 20% vs media).
Limiti dichiarati: niente short/liquidità nei 13F; titoli delistati senza prezzo esclusi (bias di sopravvivenza);
  opzioni mostrate ma non usate nello score; ritardo fino a 45 giorni dopo il trimestre.
Output: data/13f/ranking.csv · site/inst.json · site/inv/<cik>.json · site/own/<ticker>.json · site/feed.json
"""
import json, re, datetime as dt
from pathlib import Path
import numpy as np
import pandas as pd

from inst_13f import D13, periods, read_hold, load_feed, quarter_str

ROOT = Path(__file__).parent
SITE = ROOT / "site"
BENCH = "SPY"
MIN_Q, W_MIN, MAX_HOLD = 12, 0.005, 200
LOOKBACK = 5
WEIGHTS = {"follow": .35, "trade": .25, "hit": .15, "consist": .15, "dd": .10}
K_Q, K_T = 8, 30
HZ = (13, 26, 52)


def r4(x, n=4):
    try:
        x = float(x)
        return None if not np.isfinite(x) else round(x, n)
    except Exception:
        return None


def dump(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False))


# ---------------------------------------------------------------- caricamento
def load():
    per = periods()
    if not per: raise SystemExit("nessun dato 13F: esegui prima il workflow 4 (backfill)")
    H = pd.concat([read_hold(p) for p in per], ignore_index=True)
    H["putcall"] = H["putcall"].fillna("").astype(str).str.upper()
    H["sh_type"] = H["sh_type"].fillna("SH")
    F = pd.read_csv(D13 / "filings.csv.gz", dtype={"cik": str})
    M = pd.read_csv(D13 / "managers.csv", dtype=str)
    mp = D13 / "cusip_map.csv"
    C = pd.read_csv(mp, dtype=str) if mp.exists() else pd.DataFrame(columns=["cusip", "ticker"])
    C = C[C["ticker"].fillna("") != ""].drop_duplicates("cusip")
    H = H.merge(C[["cusip", "ticker"]], on="cusip", how="left")
    pp = D13 / "prices_weekly.csv.gz"
    px = pd.read_csv(pp) if pp.exists() else pd.DataFrame(columns=["ticker", "date", "close"])
    P = px.pivot_table(index="date", columns="ticker", values="close", aggfunc="last").sort_index()
    P.index = pd.to_datetime(P.index)
    P = P.ffill()                  # titolo senza più prezzi (delisting): resta fermo all'ultimo valore
    return H, F, M, P


def names_of(M, F):
    nm = dict(zip(M["cik"], M["name"]))
    last = F.sort_values("filing_date").drop_duplicates("cik", keep="last")
    nm.update({c: n for c, n in zip(last["cik"], last["name"]) if isinstance(n, str) and n})
    return nm


# ---------------------------------------------------------------- portafogli e rendimenti follower
def build_portfolios(H, F):
    L = H[(H["putcall"] == "") & (H["sh_type"] == "SH")]
    tot = L.groupby(["cik", "period"])["value"].sum().rename("tot")
    port = (L[L["ticker"].notna()].groupby(["cik", "period", "ticker"], as_index=False)
            .agg(value=("value", "sum"), issuer=("issuer", "first"), shares=("shares", "sum")))
    port = port[port["value"] > 0]
    av = F.groupby(["cik", "period"])["filing_date"].min().rename("avail")
    return port, tot, av


def follower(port, tot, av, P):
    D = P.index
    cols = {t: i for i, t in enumerate(P.columns)}
    V = P.values
    if BENCH not in cols: raise SystemExit("manca SPY nei prezzi settimanali")
    S = V[:, cols[BENCH]]
    wins, rets = [], []
    for cik, g in port.groupby("cik"):
        pers = []
        for per, gp in g.groupby("period"):
            a = av.get((cik, per))
            if a is None or pd.isna(a): continue
            pers.append((pd.Timestamp(a), per, gp))
        pers.sort(key=lambda x: x[0])
        for k, (a, per, gp) in enumerate(pers):
            end = pers[k + 1][0] if k + 1 < len(pers) else D[-1]
            end = min(end, a + pd.Timedelta(days=MAX_HOLD))
            i0 = D.searchsorted(a); i1 = min(D.searchsorted(end), len(D) - 1)
            if i0 >= len(D) - 1 or i1 <= i0: continue
            idx = np.array([cols.get(t, -1) for t in gp["ticker"]])
            w = gp["value"].values.astype(float)
            ok = (idx >= 0)
            if ok.any():
                ok[ok] = np.isfinite(V[i0, idx[ok]]) & (V[i0, idx[ok]] > 0)
            if not ok.any(): continue
            w_ok = w[ok] / w[ok].sum()
            rel = V[i0:i1 + 1, idx[ok]] / V[i0, idx[ok]]
            rel = np.where(np.isfinite(rel), rel, 1.0)
            v = rel @ w_ok
            b = S[i0:i1 + 1] / S[i0]
            cov = w[ok].sum() / float(tot.get((cik, per), w.sum()) or w.sum())
            wins.append({"cik": cik, "period": per, "start": D[i0], "end": D[i1], "ret": v[-1] - 1,
                         "spy": b[-1] - 1, "cov": min(cov, 1.0), "npos": int(ok.sum())})
            rets.append(pd.DataFrame({"cik": cik, "date": D[i0 + 1:i1 + 1], "r": v[1:] / v[:-1] - 1,
                                      "b": b[1:] / b[:-1] - 1}))
    W = pd.DataFrame(wins)
    R = pd.concat(rets, ignore_index=True) if rets else pd.DataFrame(columns=["cik", "date", "r", "b"])
    R = R.drop_duplicates(["cik", "date"], keep="last")
    return W, R


# ---------------------------------------------------------------- trade ed esiti
def trades(port, av, P):
    D = P.index; cols = {t: i for i, t in enumerate(P.columns)}; V = P.values
    S = V[:, cols[BENCH]]
    out = []
    for cik, g in port.groupby("cik"):
        pers = sorted(g["period"].unique())
        prev = None
        for per in pers:
            cur = g[g["period"] == per].copy()
            ie = D.searchsorted(pd.Timestamp(per), side="right") - 1
            ci = cur["ticker"].map(cols)
            pe = [V[ie, int(c)] if ie >= 0 and pd.notna(c) else np.nan for c in ci]
            cur["adj"] = cur["value"] / np.array(pe, dtype=float)
            cur["w"] = cur["value"] / cur["value"].sum()
            if prev is not None and (pd.Timestamp(per) - pd.Timestamp(prev[0])).days <= 120:
                pv = prev[1]
                m = cur[["ticker", "issuer", "adj", "w"]].merge(pv[["ticker", "issuer", "adj", "w"]], on="ticker",
                                                                   how="outer", suffixes=("", "_p"))
                m["issuer"] = m["issuer"].fillna(m["issuer_p"])
                ratio = m["adj"] / m["adj_p"]
                typ = np.select([m["w_p"].isna(), m["w"].isna(), ratio > 1.2, ratio < 0.8],
                                ["new", "exit", "add", "trim"], "hold")
                m["type"] = typ
                m["wt"] = np.where(np.isin(typ, ["new", "add"]), m["w"], m["w_p"])
                m = m[(m["type"] != "hold") & (m["wt"] >= W_MIN)]
                a = av.get((cik, per))
                if len(m) and a is not None and pd.notna(a):
                    m["cik"] = cik; m["period"] = per; m["date"] = pd.Timestamp(a)
                    out.append(m[["cik", "period", "date", "ticker", "issuer", "type", "wt", "w", "w_p", "adj", "adj_p"]])
            prev = (per, cur)
    T = pd.concat(out, ignore_index=True) if out else pd.DataFrame(
        columns=["cik", "period", "date", "ticker", "issuer", "type", "wt", "w", "w_p", "adj", "adj_p"])
    if len(T):
        i0 = D.searchsorted(T["date"].values)
        ci = T["ticker"].map(cols).fillna(-1).astype(int).values
        sign = np.where(T["type"].isin(["new", "add"]), 1.0, -1.0)
        for h in HZ:
            ih = i0 + h
            ok = (ih < len(D)) & (ci >= 0) & (i0 < len(D))
            ex = np.full(len(T), np.nan)
            j0, jh, cc = i0[ok], ih[ok], ci[ok]
            ex[ok] = V[jh, cc] / V[j0, cc] - S[jh] / S[j0]
            T[f"x{h}"] = ex * sign                          # >0 = il trade ha avuto ragione
            T[f"k{h}"] = [D[j] if j < len(D) else pd.NaT for j in ih]   # data in cui l'esito è noto
    return T


# ---------------------------------------------------------------- score
def mdd(r):
    c = np.cumprod(1 + np.asarray(r, float))
    return float((c / np.maximum.accumulate(c) - 1).min()) if len(c) else np.nan


def pct_rank(s):
    return s.rank(pct=True) if s.notna().sum() > 1 else s * 0 + 0.5


def score_at(W, R, T, cutoff, active=None):
    lo = cutoff - pd.DateOffset(years=LOOKBACK)
    w = W[(W["end"] <= cutoff) & (W["end"] > lo)]
    r = R[(R["date"] <= cutoff) & (R["date"] > lo)]
    t = T[(T["k26"] <= cutoff) & (T["date"] > lo)] if len(T) else T
    nq = w.groupby("cik").size()
    elig = nq[nq >= MIN_Q].index
    if active is not None: elig = elig.intersection(active)
    if not len(elig): return pd.DataFrame()
    g = r[r["cik"].isin(elig)].groupby("cik")
    n = g.size()
    ann = g["r"].apply(lambda x: np.prod(1 + x) ** (52 / len(x)) - 1)
    annb = g["b"].apply(lambda x: np.prod(1 + x) ** (52 / len(x)) - 1)
    d = pd.DataFrame({"weeks": n, "ann": ann, "ann_spy": annb})
    d["follow"] = d["ann"] - d["ann_spy"]
    d["nq"] = nq.reindex(d.index)
    d["consist"] = w[w["cik"].isin(elig)].assign(bt=lambda x: x["ret"] > x["spy"]).groupby("cik")["bt"].mean()
    d["dd"] = g["r"].apply(mdd)
    d["te"] = g.apply(lambda x: (x["r"] - x["b"]).std() * np.sqrt(52))
    d["ir"] = d["follow"] / d["te"]
    if len(t):
        tg = t[t["cik"].isin(elig)].groupby("cik")["x26"]
        d["trade"] = tg.mean(); d["hit"] = tg.apply(lambda x: (x > 0).mean()); d["ntr"] = tg.count()
    else:
        d["trade"] = np.nan; d["hit"] = np.nan; d["ntr"] = 0
    d["ntr"] = d["ntr"].fillna(0)
    sh = {}
    for c, nn, k in (("follow", d["nq"], K_Q), ("consist", d["nq"], K_Q), ("trade", d["ntr"], K_T),
                     ("hit", d["ntr"], K_T)):
        mu = d[c].mean()
        a = nn / (nn + k)
        sh[c] = (a * d[c].fillna(mu) + (1 - a) * mu)
    sh["dd"] = d["dd"]
    d["score"] = sum(WEIGHTS[c] * pct_rank(sh[c]) for c in WEIGHTS) * 100
    for c in WEIGHTS: d[f"p_{c}"] = pct_rank(sh[c]) * 100
    d = d.sort_values("score", ascending=False)
    d["rank"] = np.arange(1, len(d) + 1)
    return d


def oos_test(W, R, T):
    if R.empty: return {"years": [], "note": "dati insufficienti"}
    first, last = R["date"].min(), R["date"].max()
    rows = []
    for y in range(first.year + 3, last.year + 1):
        c = pd.Timestamp(f"{y}-12-31")
        if c + pd.Timedelta(weeks=52) > last: break
        s = score_at(W, R, T, c)
        if len(s) < 15: continue
        f = R[(R["date"] > c) & (R["date"] <= c + pd.Timedelta(weeks=52)) & R["cik"].isin(s.index)]
        fw = f.groupby("cik").apply(lambda x: np.prod(1 + x["r"]) - np.prod(1 + x["b"]))
        j = s[["score"]].join(fw.rename("fwd"), how="inner").dropna()
        if len(j) < 15: continue
        q = j["score"] >= j["score"].quantile(.8)
        rows.append({"year": y + 1, "n": len(j), "rho": r4(j["score"].corr(j["fwd"], method="spearman"), 3),
                     "top20": r4(j.loc[q, "fwd"].mean()), "all": r4(j["fwd"].mean()),
                     "bottom20": r4(j.loc[j["score"] <= j["score"].quantile(.2), "fwd"].mean())})
    if not rows: return {"years": [], "note": "servono almeno 4 anni di storico"}
    df = pd.DataFrame(rows)
    spread = (df["top20"] - df["all"])
    rho = df["rho"].mean()
    return {"years": rows, "rho_mean": r4(rho, 3), "top20_minus_all": r4(spread.mean()),
            "years_top20_beats": int((spread > 0).sum()), "n_years": len(df),
            "verdict": "Utile" if rho >= .08 and (spread > 0).mean() >= .6 else
                       "Debole" if rho > 0 else "Non predittivo"}


# ---------------------------------------------------------------- short europei
STOP = set("LLP LP LLC LTD LIMITED INC CORP CORPORATION SA SAS SE AG GMBH PLC CO THE AND MANAGEMENT MGMT CAPITAL "
           "ADVISORS ADVISERS ADVISER ADVISOR PARTNERS INVESTMENT INVESTMENTS ASSET ASSETS FUND FUNDS GROUP "
           "HOLDINGS LLP. L.P. EUROPE UK INTERNATIONAL GLOBAL MASTER TRUST".split())


def nkey(s):
    toks = [t for t in re.sub(r"[^A-Z0-9 ]", " ", str(s).upper()).split() if t not in STOP]
    k = " ".join(toks[:2])
    return k if len(k.replace(" ", "")) >= 5 else None


def load_shorts():
    p = ROOT / "data" / "shorts" / "positions.csv.gz"
    if not p.exists(): return pd.DataFrame(columns=["source", "holder", "issuer", "isin", "pct", "pos_date", "current"])
    s = pd.read_csv(p, dtype=str)
    s["pct"] = pd.to_numeric(s["pct"], errors="coerce")
    s["current"] = s["current"].astype(str) == "True"
    return s


# ---------------------------------------------------------------- export
def main():
    SITE.mkdir(exist_ok=True)
    feed = load_feed()
    dump(SITE / "feed.json", feed[:200])
    H, F, M, P = load()
    nm = names_of(M, F)
    port, tot, av = build_portfolios(H, F)
    W, R = follower(port, tot, av, P)
    T = trades(port, av, P)
    last_date = P.index[-1]
    last_av = F.groupby("cik")["filing_date"].max()
    active = set(last_av[pd.to_datetime(last_av) >= last_date - pd.Timedelta(days=MAX_HOLD)].index)
    S = score_at(W, R, T, last_date, active=active)
    oos = oos_test(W, R, T)
    print(f"finestre follower {len(W)}, trade {len(T)}, gestori classificati {len(S)}; test OOS: "
          f"rho {oos.get('rho_mean')} ({oos.get('verdict', '-')})")

    u = pd.read_csv(ROOT / "universe.csv", dtype=str)
    db = ROOT / "build" / "analyst.db"
    if db.exists():                  # quotazioni USA già validate da build_db.py (correlazione dei rendimenti)
        import sqlite3
        with sqlite3.connect(db) as con:
            ok = dict(con.execute("SELECT ticker, us_ticker FROM universe").fetchall())
        u["us_ticker"] = u["ticker"].map(ok)
    t2u = {}
    for r in u.itertuples():
        if r.panel == "US": t2u.setdefault(r.ticker, r.ticker)
        elif isinstance(r.us_ticker, str) and r.us_ticker: t2u.setdefault(r.us_ticker, r.ticker)
    isin2u = dict(zip(u["isin"], u["ticker"]))

    # ranking.csv (usato da inst_13f.py per le notifiche importanti)
    rk = S[["rank", "score"]].copy(); rk["name"] = [nm.get(c, c) for c in rk.index]
    rk.rename_axis("cik").reset_index().to_csv(D13 / "ranking.csv", index=False)

    shorts = load_shorts()
    cur_sh = shorts[shorts["current"]].copy()
    cur_sh["key"] = cur_sh["holder"].map(nkey)
    mkey = {}
    for c in set(H["cik"]):
        k = nkey(nm.get(c, ""))
        if k: mkey.setdefault(k, c)
    cur_sh["cik"] = cur_sh["key"].map(mkey)

    latest_per = H.groupby("cik")["period"].max()
    Hg = dict(tuple(H.groupby("cik"))); Pg = dict(tuple(port.groupby("cik")))
    Rg = dict(tuple(R.groupby("cik"))); Tg = dict(tuple(T.groupby("cik"))) if len(T) else {}
    reasons = M.set_index("cik")["reason"].to_dict() if "reason" in M else {}
    ownrows, managers_out = [], []
    idir = SITE / "inv"; idir.mkdir(exist_ok=True)
    for cik, lp in latest_per.items():
        hc = Hg[cik]; pc_ = Pg.get(cik, port.iloc[0:0])
        per_list = sorted(hc["period"].unique())
        cur = hc[hc["period"] == lp]
        eq = cur[(cur["putcall"] == "") & (cur["sh_type"] == "SH")]
        opt = cur[cur["putcall"] != ""]
        prev_p = per_list[-2] if len(per_list) > 1 else None
        pv = hc[(hc["period"] == prev_p) & (hc["putcall"] == "") & (hc["sh_type"] == "SH")] if prev_p else hc.iloc[0:0]
        key = lambda d: d["ticker"].fillna("#" + d["cusip"])
        ea = eq.assign(k=key(eq)).groupby("k").agg(issuer=("issuer", "first"), ticker=("ticker", "first"),
                                                     value=("value", "sum"), shares=("shares", "sum"))
        pa = pv.assign(k=key(pv)).groupby("k").agg(value=("value", "sum"), shares=("shares", "sum"),
                                                     issuer=("issuer", "first"), ticker=("ticker", "first"))
        tv = ea["value"].sum()
        ea["w"] = ea["value"] / tv if tv else 0
        j = ea.join(pa[["shares"]].rename(columns={"shares": "sh_p"}), how="left")
        chg = np.where(j["sh_p"].isna(), "new", np.where(j["shares"] > j["sh_p"] * 1.2, "add",
                       np.where(j["shares"] < j["sh_p"] * 0.8, "trim", "hold")))
        if not prev_p: chg[:] = ""
        j["chg"] = chg
        j["dsh"] = j["shares"] / j["sh_p"] - 1
        j = j.sort_values("value", ascending=False)
        exits = pa[~pa.index.isin(ea.index)].sort_values("value", ascending=False) if prev_p else pa.iloc[0:0]
        sc = S.loc[cik] if cik in S.index else None
        name = nm.get(cik, cik)
        fa = F[F["cik"] == cik]
        lf = fa[fa["period"] == lp]["filing_date"].max()
        # storico trimestrale
        hist = []
        for p_ in per_list[-40:]:
            x = hc[(hc["period"] == p_) & (hc["putcall"] == "")]
            hist.append([p_, r4(x["value"].sum(), 0), int(x["cusip"].nunique())])
        turn = None
        if len(per_list) > 1:
            tos = []
            for a_, b_ in zip(per_list[-9:-1], per_list[-8:]):
                wa = pc_[pc_["period"] == a_].set_index("ticker")["value"]
                wb = pc_[pc_["period"] == b_].set_index("ticker")["value"]
                if wa.sum() and wb.sum():
                    tos.append(((wa / wa.sum()).sub(wb / wb.sum(), fill_value=0).abs().sum()) / 2)
            turn = r4(np.mean(tos), 3) if tos else None
        # curva follower (base 100)
        rc = Rg.get(cik, R.iloc[0:0]).sort_values("date")
        curve = []
        if len(rc):
            cp = np.cumprod(1 + rc["r"].values) * 100; cb = np.cumprod(1 + rc["b"].values) * 100
            curve = [[d.strftime("%Y-%m-%d"), r4(a, 2), r4(b, 2)] for d, a, b in zip(rc["date"], cp, cb)]
        tr = Tg.get(cik, T.iloc[0:0]).sort_values("date", ascending=False)
        ts = {"n": int(tr["x26"].notna().sum()) if len(tr) else 0}
        for h in HZ:
            if len(tr):
                x = tr[f"x{h}"].dropna()
                ts[f"hit{h}"] = r4((x > 0).mean(), 3) if len(x) else None; ts[f"avg{h}"] = r4(x.mean()) if len(x) else None
        sh_m = cur_sh[cur_sh["cik"] == cik].sort_values("pct", ascending=False)
        top = j.head(3)["issuer"].tolist()
        rec = {"cik": cik, "name": name, "period": lp, "q": quarter_str(lp), "filed": lf,
               "aum": r4(tv, 0), "npos": int(len(ea)), "nopt": int(len(opt)),
               "put": r4(opt.loc[opt["putcall"] == "PUT", "value"].sum(), 0),
               "call": r4(opt.loc[opt["putcall"] == "CALL", "value"].sum(), 0),
               "turn": turn, "top": top, "nq": int(len(per_list)), "active": cik in active,
               "nshort": int(len(sh_m)), "reason": reasons.get(cik)}
        if sc is not None:
            rec.update({"rank": int(sc["rank"]), "score": r4(sc["score"], 1), "follow": r4(sc["follow"]),
                        "ann": r4(sc["ann"]), "ann_spy": r4(sc["ann_spy"]), "hit": r4(sc["hit"], 3),
                        "trade": r4(sc["trade"]), "consist": r4(sc["consist"], 3), "dd": r4(sc["dd"]),
                        "ir": r4(sc["ir"], 2), "ntr": int(sc["ntr"]),
                        "pct": {c: r4(sc[f"p_{c}"], 0) for c in WEIGHTS}})
        managers_out.append(rec)
        posl = [[r.ticker if isinstance(r.ticker, str) else None, r.issuer, r4(r.value, 0), r4(r.w, 5),
                 r.chg, r4(r.dsh, 3), t2u.get(r.ticker)] for r in j.head(300).itertuples()]
        optl = (opt.groupby(["cusip", "putcall"], as_index=False)
                .agg(issuer=("issuer", "first"), ticker=("ticker", "first"), value=("value", "sum"), shares=("shares", "sum"))
                .sort_values("value", ascending=False).head(150))
        dump(idir / f"{cik}.json", {
            **rec, "cash": None, "cash_note": "n.d. – i 13F non riportano la liquidità",
            "curve": curve, "hist": hist, "tstats": ts,
            "pos": posl, "more": max(0, len(j) - 300),
            "exits": [[r.ticker if isinstance(r.ticker, str) else None, r.issuer, r4(r.value, 0), t2u.get(r.ticker)]
                      for r in exits.head(60).itertuples()],
            "opts": [[r.ticker if isinstance(r.ticker, str) else None, r.issuer, r.putcall, r4(r.value, 0),
                      r4(r.shares, 0), t2u.get(r.ticker)] for r in optl.itertuples()],
            "shorts": [[r.issuer, r.isin, r4(r.pct, 3), r.pos_date, r.source, isin2u.get(r.isin)]
                       for r in sh_m.itertuples()],
            "trades": [[d.strftime("%Y-%m-%d"), r.ticker, r.issuer, r.type, r4(r.wt, 4), r4(r.x13), r4(r.x26),
                        r4(r.x52), t2u.get(r.ticker)] for d, r in zip(tr["date"].head(150), tr.head(150).itertuples())]
            if len(tr) else []})
        # righe per "chi possiede"
        for r in j.itertuples():
            u_t = t2u.get(r.ticker)
            if u_t: ownrows.append((u_t, cik, name, lp, r.value, r.w, r.shares, r.chg, r.dsh, "", rec.get("rank")))
        for r in optl.itertuples():
            u_t = t2u.get(r.ticker)
            if u_t: ownrows.append((u_t, cik, name, lp, r.value, None, r.shares, "", None, r.putcall, rec.get("rank")))
        for r in exits.itertuples():
            u_t = t2u.get(r.ticker)
            if u_t: ownrows.append((u_t, cik, name, lp, 0.0, 0.0, 0.0, "exit", -1.0, "", rec.get("rank")))

    # ---- per titolo
    O = pd.DataFrame(ownrows, columns=["t", "cik", "name", "period", "value", "w", "shares", "chg", "dsh", "pc", "rank"])
    odir = SITE / "own"; odir.mkdir(exist_ok=True)
    # storico: n. gestori e azioni totali per trimestre (ultimi 12)
    Lh = H[(H["putcall"] == "") & (H["sh_type"] == "SH") & H["ticker"].isin(t2u)]
    hq = Lh.groupby(["ticker", "period"]).agg(n=("cik", "nunique"), sh=("shares", "sum"), v=("value", "sum")).reset_index()
    own_list = []; u_isin = u.set_index("ticker")["isin"].to_dict(); u2t = {v: k for k, v in t2u.items()}
    Og = dict(tuple(O.groupby("t"))) if len(O) else {}
    for ut in sorted(set(O["t"]) | set(isin2u[i] for i in set(shorts["isin"]) if i in isin2u)):
        o = Og.get(ut, O.iloc[0:0])
        us = u2t.get(ut)
        hh = hq[hq["ticker"] == us].sort_values("period").tail(12) if us else hq.iloc[0:0]
        isin = u_isin.get(ut)
        s_all = shorts[shorts["isin"] == isin] if isinstance(isin, str) else shorts.iloc[0:0]
        s_cur = s_all[s_all["current"]].sort_values("pct", ascending=False)
        s_cur = s_cur.assign(cik=s_cur["holder"].map(nkey).map(mkey))
        s_hist = s_all[pd.to_datetime(s_all["pos_date"], errors="coerce") >= last_date - pd.DateOffset(years=3)]
        sh_series = []
        if len(s_hist):
            if (s_hist["source"] == "FCA").all():
                sh_series = s_hist.groupby("pos_date")["pct"].sum().sort_index().tail(300).reset_index().values.tolist()
            else:
                top_h = s_hist.groupby("holder")["pct"].max().sort_values(ascending=False).head(8).index
                sh_series = [[h_, s_hist[s_hist["holder"] == h_].sort_values("pos_date")[["pos_date", "pct"]].values.tolist()]
                             for h_ in top_h]
        hold = o[(o["pc"] == "") & (o["chg"] != "exit")].sort_values("value", ascending=False)
        dump(odir / f"{ut}.json", {
            "t": ut, "us": us, "isin": isin,
            "holders": [[r.cik, r.name, r.period, r4(r.value, 0), r4(r.w, 5), r4(r.shares, 0), r.chg, r4(r.dsh, 3),
                         None if pd.isna(r.rank) else int(r.rank)] for r in hold.head(150).itertuples()],
            "nhold": int(len(hold)),
            "exits": [[r.cik, r.name, r.period, None if pd.isna(r.rank) else int(r.rank)]
                      for r in o[o["chg"] == "exit"].itertuples()],
            "opts": [[r.cik, r.name, r.pc, r4(r.value, 0), r4(r.shares, 0), None if pd.isna(r.rank) else int(r.rank)]
                     for r in o[o["pc"] != ""].sort_values("value", ascending=False).head(60).itertuples()],
            "hist": [[r.period, int(r.n), r4(r.sh, 0), r4(r.v, 0)] for r in hh.itertuples()],
            "shorts": [[r.holder, r4(r.pct, 3), r.pos_date, r.source, r.cik if isinstance(r.cik, str) else None]
                       for r in s_cur.itertuples()],
            "short_tot": r4(s_cur["pct"].sum(), 3) if len(s_cur) else None,
            "short_hist": sh_series,
            "short_note": "Posizioni corte nette >= 0,5% del capitale pubblicate da CONSOB/AMF (per detentore) "
                          "o FCA (aggregato); per i titoli USA non esiste un dato per detentore."})
        own_list.append(ut)

    # ---- pagina Investitori
    top50 = set(S.head(50).index)
    # ultimo trimestre "completo": depositato da almeno metà dei gestori (a inizio stagione 13F ce ne sono pochi)
    npq = H.groupby("period")["cik"].nunique()
    lastq = max(npq[npq >= 0.5 * npq.max()].index) if len(npq) else None
    cons = []
    if len(T) and lastq:
        tq = T[(T["cik"].isin(top50)) & (T["period"] == lastq)]
        for typ, lab in (("new", "buy"), ("exit", "sell")):
            c = tq[tq["type"] == typ].groupby("ticker").agg(n=("cik", "nunique"), issuer=("issuer", "first"))
            for t_, r in c.sort_values("n", ascending=False).head(15).iterrows():
                cons.append([lab, t_, r["issuer"], int(r["n"]), t2u.get(t_)])
    lh = H[(H["period"] == lastq) & (H["putcall"] == "") & (H["sh_type"] == "SH") & H["ticker"].notna()] if lastq else H.iloc[0:0]
    pop = lh.groupby("ticker").agg(n=("cik", "nunique"), issuer=("issuer", "first"), v=("value", "sum")) \
            .sort_values("n", ascending=False).head(20)
    managers_out.sort(key=lambda r: (r.get("rank") or 10 ** 6, -(r.get("aum") or 0)))
    cov = None
    if len(W): cov = r4(W[W["end"] >= last_date - pd.DateOffset(years=1)]["cov"].mean(), 3)
    dump(SITE / "inst.json", {
        "updated": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"), "last_price": last_date.strftime("%Y-%m-%d"),
        "last_q": lastq, "periods": [periods()[0], periods()[-1]], "n_managers": len(managers_out), "n_ranked": len(S),
        "coverage": cov, "weights": WEIGHTS, "min_q": MIN_Q, "lookback": LOOKBACK, "oos": oos,
        "managers": managers_out, "consensus": cons,
        "popular": [[t_, r["issuer"], int(r["n"]), r4(r["v"], 0), t2u.get(t_)] for t_, r in pop.iterrows()],
        "own": own_list, "feed": feed[:100],
        "notes": ["I 13F contengono solo posizioni lunghe in titoli USA: niente short, niente liquidità, niente titoli esteri.",
                  "Le opzioni sono indicate al valore del sottostante (non del premio) e non entrano nello score.",
                  "Gli short europei vengono da CONSOB, AMF e FCA (soglia 0,5%) e sono abbinati ai gestori per nome.",
                  "Titoli delistati senza prezzi sono esclusi dal portafoglio follower (bias di sopravvivenza)."]})
    print(f"export: {len(managers_out)} gestori, {len(own_list)} titoli con proprietari/short")


if __name__ == "__main__":
    main()
