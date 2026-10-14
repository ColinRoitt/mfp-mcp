import math

import numpy as np
import pytest

from mfp_connector import stats

# Reference values cross-checked once against statsmodels OLS(cov_type="HAC",
# use_correction=True) and scipy.stats.theilslopes on this exact series.
RNG = np.random.default_rng(0)
X = np.sort(RNG.choice(np.arange(200), 120, replace=False)).astype(float)
_E = np.zeros(120)
for _i in range(1, 120):
    _E[_i] = 0.6 * _E[_i - 1] + RNG.normal(0, 0.8)
Y = np.round(200 - 0.08 * X + _E, 1)


def test_ols_hac_matches_statsmodels():
    r = stats.ols_hac(X, Y)
    assert r["hac_lag"] == 4
    assert r["slope_per_day"] == pytest.approx(-0.08101843683188291, rel=1e-9)
    assert r["se_per_day"] == pytest.approx(0.0025068967911870276, rel=1e-9)


def test_theil_sen_matches_scipy():
    r = stats.theil_sen(X, Y)
    assert r["slope_per_day"] == pytest.approx(-0.08095238095238097)
    assert r["ci95_per_day"] == pytest.approx([-0.08444444444444428, -0.0775000000000001])


def test_mann_kendall_detects_decrease():
    r = stats.mann_kendall(Y)
    assert r["trend"] == "decreasing" and r["z"] < -10 and r["p_value"] < 1e-20


def test_flat_series_has_no_trend():
    r = stats.mann_kendall(np.full(20, 200.0))
    assert r["s"] == 0 and r["p_value"] == 1.0 and r["trend"] == "no significant trend"


def test_perfect_line():
    x = np.arange(10, dtype=float)
    y = 150 - 0.5 * x
    assert stats.ols_hac(x, y)["slope_per_day"] == pytest.approx(-0.5)
    assert stats.theil_sen(x, y)["slope_per_day"] == pytest.approx(-0.5)


def test_weekly_twins():
    w = stats.weekly({"slope_per_day": -0.1, "ci95_per_day": [-0.2, 0.0]})
    assert w["slope_per_week"] == pytest.approx(-0.7)
    assert w["ci95_per_week"] == pytest.approx([-1.4, 0.0])


def test_too_few_points():
    with pytest.raises(ValueError):
        stats.ols_hac(np.arange(2.0), np.arange(2.0))


def test_lag_rule():
    assert stats.newey_west_lag(100) == 4 and stats.newey_west_lag(1000) == math.floor(4 * 10 ** (2 / 9))
