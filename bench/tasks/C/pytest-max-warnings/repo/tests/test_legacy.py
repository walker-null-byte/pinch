import warnings


def old_api():
    warnings.warn("old_api is deprecated", DeprecationWarning)
    return 1


def test_a():
    assert old_api() == 1


def test_b():
    assert old_api() == 1


def test_c():
    assert old_api() == 1
    assert old_api() == 1


def test_d():
    assert 1 + 1 == 2
