"""Hypothesis profiles for the property suite.

Loaded before any property test imports (pytest imports conftest first), so a
`@settings(deadline=None, ...)` decorator that omits volume fields inherits
max_examples / stateful_step_count / derandomize from the profile selected here.

Select with HYPOTHESIS_PROFILE=ci (default: dev).
"""
import os

from hypothesis import HealthCheck, settings

settings.register_profile(
    "dev",
    max_examples=50,
    stateful_step_count=24,
    deadline=None,
    derandomize=True,  # reproducible locally
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "ci",
    max_examples=300,
    stateful_step_count=40,
    deadline=None,
    derandomize=False,  # explore more of the space in CI
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "dev"))
