from __future__ import annotations

import pytest
from pydantic import ValidationError

from lda.modules.citations.config import CitationsSettings


def test_defaults_are_safe_and_bounded() -> None:
    s = CitationsSettings()
    assert (s.max_sources, s.max_markers) == (50, 200)


@pytest.mark.parametrize(
    "bad", [{"max_sources": 0}, {"max_sources": 501}, {"max_markers": 0}, {"x": 1}]
)
def test_out_of_range_or_unknown_settings_are_refused(bad: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        CitationsSettings(**bad)
