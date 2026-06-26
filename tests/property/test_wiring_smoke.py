"""Proves Hypothesis runs under the project's pytest config from tests/property/."""
from hypothesis import given
from hypothesis import strategies as st

from agent.exit_ladder import _round_half_up_min1


@given(x=st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False))
def test_round_half_up_min1_is_at_least_one(x):
    assert _round_half_up_min1(x) >= 1
