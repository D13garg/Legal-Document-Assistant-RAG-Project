from __future__ import annotations

import pytest

from lda.modules.identity.domain.display_name import MAX_DISPLAY_NAME_LENGTH, DisplayName
from lda.modules.identity.domain.errors import InvalidDisplayName


def test_value_is_trimmed() -> None:
    assert DisplayName("  Ada Lovelace \n").value == "Ada Lovelace"


@pytest.mark.parametrize("bad", ["", "   ", "\t", "line\nbreak", "nul\x00byte", "bell\x07"])
def test_blank_or_unprintable_is_rejected(bad: str) -> None:
    with pytest.raises(InvalidDisplayName):
        DisplayName(bad)


def test_length_bound() -> None:
    DisplayName("x" * MAX_DISPLAY_NAME_LENGTH)
    with pytest.raises(InvalidDisplayName):
        DisplayName("x" * (MAX_DISPLAY_NAME_LENGTH + 1))


def test_unicode_names_are_allowed() -> None:
    assert DisplayName("Zoë Müller 山田").value == "Zoë Müller 山田"


def test_error_and_repr_never_contain_the_value() -> None:
    rejected = "Jane Q.\nConfidential"  # interior newline: invalid
    with pytest.raises(InvalidDisplayName) as info:
        DisplayName(rejected)
    assert "Confidential" not in str(info.value) + info.value.detail + repr(info.value)
    assert "Confidential" not in repr(DisplayName("Jane Q. Confidential"))
    assert "Confidential" not in f"{DisplayName('Jane Q. Confidential')}"
