from __future__ import annotations

from chan520_skill.indicators import slope_deg_optional


def test_ma_slope_is_price_scale_invariant() -> None:
    low_price = slope_deg_optional([10.0, 10.2, 10.4, 10.6, 10.8], 5)[-1]
    high_price = slope_deg_optional([100.0, 102.0, 104.0, 106.0, 108.0], 5)[-1]
    assert low_price is not None and high_price is not None
    assert abs(low_price - high_price) < 1e-12
