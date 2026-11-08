"""Run `pinch prove` for real: the four scenarios through Bifrost in Docker. Skipped when Docker or the image is missing."""

import subprocess

import pytest

from pinch import prove


def image_available() -> bool:
    return prove.docker_ok() and subprocess.run(["docker", "image", "inspect", prove.IMAGE], capture_output=True).returncode == 0


@pytest.mark.skipif(not image_available(), reason=f"needs Docker and {prove.IMAGE} (docker pull {prove.IMAGE})")
def test_pinch_prove_passes_all_four_scenarios(capsys):
    code = prove.main([])
    out = capsys.readouterr().out
    with capsys.disabled():
        print("\n" + out)
    assert code == 0 and "4/4 scenarios passed" in out
    assert out.count("PASS ") == 4 and "FAIL" not in out
