from __future__ import annotations

import tomllib
from pathlib import Path

import moodle_autotask

ROOT = Path(__file__).parents[1]


def test_public_distribution_and_import_package_use_canonical_spelling() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["name"] == "moodle-autotask"
    assert moodle_autotask.__version__ == project["version"]
    assert (ROOT / "src" / "moodle_autotask").is_dir()
    assert not (ROOT / "src" / "moddle_autotask").exists()
    assert all(
        target.startswith("moodle_autotask.") for target in project["scripts"].values()
    )
