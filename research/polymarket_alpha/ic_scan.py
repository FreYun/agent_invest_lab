import pandas as pd, numpy as np

def spearman_ic(df: pd.DataFrame) -> float:
    if len(df) < 10:
        return float("nan")
    return df["sig"].corr(df["fwd_ret"], method="spearman")

def quantile_monotonicity(df: pd.DataFrame, n_q: int = 5) -> float:
    if df["sig"].nunique() < n_q:
        return float("nan")
    q = pd.qcut(df["sig"].rank(method="first"), n_q, labels=False)
    means = df["fwd_ret"].groupby(q).mean()
    if means.notna().sum() < 3:
        return float("nan")
    return means.reset_index(drop=True).corr(pd.Series(range(len(means)), dtype=float), method="spearman")

def detrend(s: pd.Series, k: int = 5) -> pd.Series:
    return s.diff(k)
