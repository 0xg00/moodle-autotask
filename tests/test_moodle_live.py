"""Optional full-path verification against the deterministic local Moodle fixture."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from moddle_autotask.adapters.moodle.config import MoodleConnectionConfig
from moddle_autotask.adapters.moodle.downloads import download_attachment
from moddle_autotask.adapters.moodle.service import MoodleService


@pytest.mark.skipif(
    not os.environ.get("MOODLE_LIVE_TOKEN_FILE"),
    reason="MOODLE_LIVE_TOKEN_FILE is required for local Moodle integration verification",
)
def test_fixture_attachment_downloads(tmp_path: Path) -> None:
    token_file = Path(os.environ["MOODLE_LIVE_TOKEN_FILE"])
    service = MoodleService(MoodleConnectionConfig.from_token_file(token_file))
    assignment = next(item for item in service.assignments() if item.title == "AutoTask assignment")
    attachment = next(
        item for item in assignment.attachments if item.filename == "autotask-brief.txt"
    )
    receipt = download_attachment(service.config, assignment, attachment.attachment_key, tmp_path)
    assert receipt.path.read_text(encoding="utf-8").startswith("AutoTask local fixture brief.")
    assert receipt.size_bytes > 0
