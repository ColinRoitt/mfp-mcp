"""Deterministic trend statistics for weight series (numpy only).

x is days (float) since the first observation, y is weight in lb. Slopes are
returned per day and per week. P-values use the normal approximation.
"""
from __future__ import annotations

import math

import numpy as np

Z975 = 1.959963984540054


def _p_two_sided(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2))


def newey_west_lag(n: int) -> int:
    return int(math.floor(4 * (n / 100) ** (2 / 9)))


def ols_hac(x: np.ndarray, y: np.ndarray, lag: int | None = None) -> dict:
    """OLS y = a + b·x with Newey–West (Bartlett) HAC standard errors.

    Observations are taken in time order; the n/(n-k) small-sample factor is applied.
    """
    n = len(x)
    if n < 3:
        raise ValueError("need at least 3 observations")
    lag = newey_west_lag(n) if lag is None else lag
    X = np.column_stack([np.ones(n), x])
    xtx_inv = np.linalg.inv(X.T @ X)
    beta = xtx_inv @ X.T @ y
    e = y - X @ beta
    u = X * e[:, None]
    S = u.T @ u
    for l in range(1, min(lag, n - 1) + 1):
        w = 1 - l / (lag + 1)
        g = u[l:].T @ u[:-l]
        S += w * (g + g.T)
    V = xtx_inv @ S @ xtx_inv * n / (n - 2)
    se = float(math.sqrt(V[1, 1]))
    slope = float(beta[1])
    ss_tot = float(((y - y.mean()) ** 2).sum())
    return {
        "intercept": float(beta[0]),
        "slope_per_day": slope,
        "se_per_day": se,
        "ci95_per_day": [slope - Z975 * se, slope + Z975 * se],
        "t": slope / se if se > 0 else math.inf,
        "p_value": _p_two_sided(slope / se) if se > 0 else 0.0,
        "r_squared": 1 - float((e**2).sum()) / ss_tot if ss_tot > 0 else None,
        "hac_lag": lag,
        "residual_sd": float(math.sqrt((e**2).sum() / (n - 2))),
    }


def _mk_variance(y: np.ndarray) -> float:
    n = len(y)
    _, counts = np.unique(y, return_counts=True)
    ties = counts[counts > 1].astype(float)
    return (n * (n - 1) * (2 * n + 5) - float((ties * (ties - 1) * (2 * ties + 5)).sum())) / 18


def mann_kendall(y: np.ndarray) -> dict:
    """Mann–Kendall trend test with tie-corrected variance (no autocorrelation correction)."""
    n = len(y)
    if n < 3:
        raise ValueError("need at least 3 observations")
    i, j = np.triu_indices(n, k=1)
    s = int(np.sign(y[j] - y[i]).sum())
    var = _mk_variance(y)
    z = 0.0 if s == 0 or var == 0 else (s - math.copysign(1, s)) / math.sqrt(var)
    p = _p_two_sided(z)
    return {
        "s": s,
        "var_s": var,
        "z": z,
        "p_value": p,
        "tau": s / (n * (n - 1) / 2),
        "trend": "decreasing" if z < 0 and p < 0.05 else "increasing" if z > 0 and p < 0.05 else "no significant trend",
    }


def theil_sen(x: np.ndarray, y: np.ndarray) -> dict:
    """Theil–Sen slope with Sen's (1968) 95% confidence interval."""
    n = len(x)
    if n < 3:
        raise ValueError("need at least 3 observations")
    i, j = np.triu_indices(n, k=1)
    dx = x[j] - x[i]
    keep = dx != 0
    slopes = np.sort((y[j] - y[i])[keep] / dx[keep])
    m = len(slopes)
    if m == 0:
        raise ValueError("all observations share one x value")
    slope = float(np.median(slopes))
    c = Z975 * math.sqrt(_mk_variance(y))
    lo = int(np.clip(math.floor((m - c) / 2), 0, m - 1))
    hi = int(np.clip(math.ceil((m + c) / 2), 0, m - 1))
    return {
        "slope_per_day": slope,
        "ci95_per_day": [float(slopes[lo]), float(slopes[hi])],
        "intercept": float(np.median(y - slope * x)),
    }


def weekly(d: dict, keys=("slope_per_day", "se_per_day", "ci95_per_day")) -> dict:
    """Add *_per_week twins for per-day quantities."""
    out = dict(d)
    for k in keys:
        if k in d:
            v = d[k]
            out[k.replace("per_day", "per_week")] = [7 * a for a in v] if isinstance(v, list) else 7 * v
    return out
