"""Obs columns as served (`to_obs_values`)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from canopy.data.reader import to_obs_values


def test_nullable_whole_numbers_beyond_float32_stay_exact() -> None:
    """Int64 [16777217, 123456789] came out as [16777216, 123456792] (review full-2 B-L1)."""
    v = to_obs_values(pd.array([16_777_217, None, 123_456_789], dtype="Int64"))
    assert v.values.dtype == np.float64 and v.values[0] == 16_777_217 and np.isnan(v.values[1])
    small = to_obs_values(pd.array([1, None, 3], dtype="Int64"))
    assert small.values.dtype == np.float32
