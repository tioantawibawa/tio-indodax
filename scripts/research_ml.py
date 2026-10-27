"""Research only: can a price-prediction model improve the portfolio manager? (docs/ml_experiment.md)

    python -m scripts.research_ml --data data/history

Walk-forward (retrain every 90 days, 7-day purge), out-of-sample 2020-01-01 .. last candle.
Never touches the live agent.
"""

from __future__ import annotations

import argparse
import math

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

PAIRS = ["btc_idr", "eth_idr", "sol_idr", "xrp_idr", "doge_idr", "ada_idr"]
COST = 0.00342 + 0.001
HORIZON = 7
OOS_START = pd.Timestamp("2020-01-01", tz="UTC")


def load(data_dir: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    close, vol = {}, {}
    for p in PAIRS:
        d = pd.read_csv(f"{data_dir}/{p}_1D.csv")
        d.index = pd.to_datetime(d.ts, unit="s", utc=True)
        close[p], vol[p] = d.close.astype(float), d.volume.astype(float)
    return pd.DataFrame(close).sort_index(), pd.DataFrame(vol).sort_index()


def rsi(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def features(close: pd.DataFrame, vol: pd.DataFrame) -> pd.DataFrame:
    rets = close.pct_change()
    mkt = rets.mean(axis=1)
    mkt_c = (1 + mkt.fillna(0)).cumprod()
    rows = []
    for p in PAIRS:
        c, v = close[p], vol[p]
        f = pd.DataFrame(index=close.index)
        for n in (1, 3, 7, 14, 30, 60, 90):
            f[f"ret{n}"] = c / c.shift(n) - 1
        for n in (20, 50, 100):
            f[f"ema{n}"] = c / c.ewm(span=n, adjust=False).mean() - 1
        r = c.pct_change()
        f["vol10"], f["vol30"] = r.rolling(10).std(), r.rolling(30).std()
        f["volratio"] = f.vol10 / f.vol30
        f["rsi14"] = rsi(c)
        lv = np.log(v.replace(0, np.nan))
        f["volz"] = (lv - lv.rolling(30).mean()) / lv.rolling(30).std()
        f["dd90"] = c / c.rolling(90).max() - 1
        f["btc7"] = close.btc_idr / close.btc_idr.shift(7) - 1
        f["btc30"] = close.btc_idr / close.btc_idr.shift(30) - 1
        f["mkt7"] = mkt_c / mkt_c.shift(7) - 1
        f["mkt30"] = mkt_c / mkt_c.shift(30) - 1
        f["fwd"] = c.shift(-HORIZON) / c - 1
        f["pair"] = p
        f = f[c.notna()].iloc[100:]          # warm-up: EMA100 and 90-day windows
        rows.append(f)
    df = pd.concat(rows).reset_index(names="date")
    return df.replace([np.inf, -np.inf], np.nan)


FEATS = ["ret1", "ret3", "ret7", "ret14", "ret30", "ret60", "ret90", "ema20", "ema50", "ema100", "vol10",
         "vol30", "volratio", "rsi14", "volz", "dd90", "btc7", "btc30", "mkt7", "mkt30"]


def walk_forward(df: pd.DataFrame, model_fn) -> pd.Series:
    """Out-of-sample probability per row (NaN before OOS_START)."""
    prob = pd.Series(np.nan, index=df.index)
    end = df.date.max()
    t = OOS_START
    while t <= end:
        nxt = t + pd.Timedelta(days=90)
        cutoff = t - pd.Timedelta(days=HORIZON + 1)          # labels of training rows are known at t
        tr = df[(df.date <= cutoff) & df.fwd.notna()].dropna(subset=FEATS)
        te = df[(df.date >= t) & (df.date < nxt)].dropna(subset=FEATS)
        if len(tr) > 1000 and len(te):
            m = model_fn()
            m.fit(tr[FEATS], (tr.fwd > 0).astype(int))
            prob.loc[te.index] = m.predict_proba(te[FEATS])[:, 1]
        t = nxt
    return prob


def gbm():
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=200, min_samples_leaf=100,
                                          l2_regularization=1.0, random_state=0)


def logit():
    return make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=2000))


def auc_report(df: pd.DataFrame, prob: pd.Series, name: str) -> dict:
    ok = prob.notna() & df.fwd.notna()
    y, p = (df.fwd[ok] > 0).astype(int), prob[ok]
    out = {"all": roc_auc_score(y, p)}
    for yr, g in df[ok].groupby(df[ok].date.dt.year):
        yy = (g.fwd > 0).astype(int)
        if yy.nunique() == 2:
            out[int(yr)] = roc_auc_score(yy, prob[g.index])
    acc = ((p >= 0.5) == y).mean()
    print(f"{name:8s} AUC keseluruhan {out['all']:.3f} · akurasi {acc * 100:.1f}% (basis naik {y.mean() * 100:.1f}%) · "
          + " ".join(f"{k}:{v:.3f}" for k, v in out.items() if k != "all"))
    return out


def simulate(close: pd.DataFrame, eligible: pd.DataFrame, cost: float, start=OOS_START,
             target_vol=0.20, max_w=0.45) -> pd.Series:
    """Daily equity of the portfolio-manager model given a daily eligibility mask (decided at the close)."""
    rets = close.pct_change()
    vol30 = rets.rolling(30).std()
    days = close.index[close.index >= start]
    w = pd.Series(0.0, index=close.columns)
    eq, out, last_rb, last_set = 1.0, [], None, None
    for i, t in enumerate(days):
        if i > 0:
            r = rets.loc[t].fillna(0.0)
            g = 1 + float((w * r).sum())
            w = w * (1 + r) / g if g > 0 else w
            eq *= g
        el = [p for p in close.columns if bool(eligible.loc[t, p])]
        due = last_rb is None or (t - last_rb).days >= 7 or set(el) != last_set
        if due:
            tgt = pd.Series(0.0, index=close.columns)
            if el:
                iv = 1 / vol30.loc[t, el]
                ww = iv / iv.sum()
                cov = rets.loc[:t, el].tail(60).cov().values * 365
                pv = math.sqrt(max(float(ww.values @ cov @ ww.values), 0.0))
                ww = ww * (min(1.0, target_vol / pv) if pv > 0 else 0.0)
                tgt[el] = ww.clip(upper=max_w)
            eq -= eq * float((tgt - w).abs().sum()) * cost
            w, last_rb, last_set = tgt, t, set(el)
        out.append(eq)
    return pd.Series(out, index=days)


def stats(eq: pd.Series) -> dict:
    d = eq.pct_change().dropna()
    yearly = eq.groupby(eq.index.year).last() / eq.groupby(eq.index.year).first().shift(0) - 1
    first = eq.groupby(eq.index.year).first()
    prev_last = eq.groupby(eq.index.year).last().shift(1)
    yearly = eq.groupby(eq.index.year).last() / prev_last.fillna(first) - 1
    return {"total": (eq.iloc[-1] / eq.iloc[0] - 1) * 100, "dd": float(((eq.cummax() - eq) / eq.cummax()).max() * 100),
            "sharpe": float(d.mean() / d.std() * math.sqrt(365)) if d.std() > 0 else 0.0,
            "yearly": {int(k): v * 100 for k, v in yearly.items()}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/history")
    args = ap.parse_args(argv)
    close, vol = load(args.data)
    df = features(close, vol)
    print(f"baris {len(df)} · OOS mulai {OOS_START.date()} · data sampai {df.date.max().date()}\n")
    print("## 1. Kualitas prediksi (out-of-sample, target: naik dalam 7 hari)")
    p_gbm = walk_forward(df, gbm)
    auc_g = auc_report(df, p_gbm, "GBM")
    auc_report(df, walk_forward(df, logit), "Logistik")

    prob = df.assign(p=p_gbm).pivot_table(index="date", columns="pair", values="p").reindex(
        index=close.index, columns=PAIRS)
    above = close > close.ewm(span=100, adjust=False).mean()
    have = close.notna() & close.rolling(100).count().ge(100)
    masks = {"A baseline (EMA100)": above & have,
             "B EMA100 + ML>=0.5": above & have & (prob >= 0.5),
             "C ML saja >=0.55": have & (prob >= 0.55)}
    res = {}
    for cost_mult in (1, 2):
        print(f"\n## 2. Strategi (biaya {cost_mult}×), {OOS_START.date()} s/d akhir")
        for name, m in masks.items():
            st = stats(simulate(close, m.fillna(False), COST * cost_mult))
            res[(name, cost_mult)] = st
            ys = " ".join(f"{y}:{v:+.0f}%" for y, v in st["yearly"].items())
            print(f"{name:22s} total {st['total']:+7.1f}% · DD {st['dd']:5.1f}% · Sharpe {st['sharpe']:.2f} | {ys}")

    print("\n## 3. Kriteria (docs/ml_experiment.md)")
    yrs = [y for y in auc_g if y != "all"]
    c1 = auc_g["all"] >= 0.53 and all(auc_g[y] > 0.5 for y in yrs)
    A1, B1 = res[("A baseline (EMA100)", 1)], res[("B EMA100 + ML>=0.5", 1)]
    A2, B2 = res[("A baseline (EMA100)", 2)], res[("B EMA100 + ML>=0.5", 2)]
    wins = sum(B1["yearly"][y] > A1["yearly"][y] for y in A1["yearly"])
    c2 = wins >= 5 and B1["total"] > A1["total"]
    c3 = B1["sharpe"] > A1["sharpe"] and B1["dd"] <= A1["dd"] + 5
    c4 = B2["total"] > A2["total"] and B2["sharpe"] > A2["sharpe"]
    for i, (ok, txt) in enumerate([(c1, f"AUC {auc_g['all']:.3f} >= 0,53 & tiap tahun > 0,5"),
                                   (c2, f"B > A di {wins}/{len(A1['yearly'])} tahun & total"),
                                   (c3, "Sharpe B > A & DD B <= DD A + 5"),
                                   (c4, "tetap unggul dengan biaya 2×")], 1):
        print(f"{'✅' if ok else '❌'} {i}. {txt}")
    print("\nKESIMPULAN:", "LOLOS — lanjut uji di mesin backtest asli + paper" if all((c1, c2, c3, c4))
          else "TIDAK LOLOS — model prediksi tidak dipakai")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
