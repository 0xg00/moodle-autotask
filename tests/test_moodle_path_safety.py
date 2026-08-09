from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from moddle_autotask.adapters.moodle.path_safety import assert_no_indirection


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows reparse-point feature")
def test_windows_junction_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    command = ["cmd", "/c", "mklink", "/J", str(junction), str(target)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        pytest.skip(f"junction creation unavailable: {error}")
    if result.returncode != 0:
        pytest.skip(
            f"junction creation unavailable: {result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        with pytest.raises(ValueError, match="reparse"):
            assert_no_indirection(junction / "child")
    finally:
        if junction.exists() or junction.is_symlink():
            assert junction.parent == tmp_path
            assert junction.name == "junction"
            try:
                junction.rmdir()
            except OSError:
                pass
