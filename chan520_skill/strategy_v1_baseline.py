"""Frozen V1 baseline strategy.

The implementation intentionally delegates to the pre-refactor ``analyze``
function.  Keeping this adapter explicit makes later comparisons reproducible
without silently changing the historical baseline.
"""

from .strategy import analyze


STRATEGY_ID = "strategy_v1_baseline"


def analyze_baseline(*args, **kwargs):
    return analyze(*args, **kwargs)

