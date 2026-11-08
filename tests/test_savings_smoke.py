"""The offline savings smoke: `--savings all` against `--savings none` on three scripted tasks."""

from pinch import savings_smoke


def test_all_techniques_send_fewer_bytes_than_none_and_still_fix_the_bug(capsys):
    text, pairs = savings_smoke.table()
    with capsys.disabled():
        print("\n" + text + "\n")
    assert len(pairs) == 3
    for name, none, everything in pairs:
        assert none.fixed and everything.fixed, f"{name}: the task must still be solved with the techniques on"
        assert everything.request_bytes < none.request_bytes and everything.calls <= none.calls
    assert any(everything.stopped_on_green for _n, _none, everything in pairs)
    assert "NOT real tokens" in text and "offline" in text.lower()
