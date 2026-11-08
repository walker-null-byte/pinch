import pytest

LONG_A = "alpha-" * 40
LONG_B = "beta-" * 50


@pytest.mark.parametrize("text", [LONG_A, LONG_B, "short"])
def test_nonempty(text):
    assert text
