"""Optional full-path verification against the deterministic local Moodle fixture."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from io import StringIO
from pathlib import Path
from typing import TypeVar

import pytest

from moddle_autotask.adapters.moodle.config import MoodleConnectionConfig
from moddle_autotask.adapters.moodle.downloads import download_attachment
from moddle_autotask.adapters.moodle.models import MoodleAssignmentSnapshot
from moddle_autotask.adapters.moodle.scheduler import LocalJsonSink, once
from moddle_autotask.adapters.moodle.service import MoodleService
from moddle_autotask.adapters.moodle.state import MoodleState

_Result = TypeVar("_Result")

_RICH_TITLES = {
    "Pràctica ISO 1 - Desplegament d'una OVA",
    "Pràctica ISO 2 - Usuaris, grups i permisos Linux",
    "Pràctica GBD - Còpia i restauració de PostgreSQL",
    "Pràctica LMSGI - Validació XML amb XSD",
    "Pràctica IAW - Desplegament web amb contenidors",
    "Pràctica ASGBD - Pla de replicació",
    "Pràctica PAX - VLAN, routing i diagnòstic",
    "Pràctica ASO - Automatització amb Ansible",
    "Pràctica SXI - DNS autoritatiu i resolució",
    "Pràctica SAD - Hardening de Windows Server",
    "Projecte ASIX - Lliurament de la proposta tècnica",
}

_FIXTURE_BYTES = {
    "autotask-brief.txt": (
        b"AutoTask local fixture brief.\nUse the Moodle mobile API attachment metadata."
    ),
    "practica-iso-ova.pdf": (
        b"%PDF-1.4\n% AutoTask deterministic PDF fixture\n"
        b"1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
    ),
    "asix-router-lab.ova": (
        b"AUTOTASK-SIMULATED-OVA\nThis tiny deterministic fixture is metadata-only "
        b"and is not bootable.\n"
    ),
    "inventari.sql": (
        b"CREATE TABLE inventari (id integer PRIMARY KEY, nom text NOT NULL);\n"
        b"INSERT INTO inventari VALUES (1, 'router');\n"
    ),
    "servidors.xml": (
        b'<?xml version="1.0"?><servidors><servidor id="1">web01</servidor>'
        b"</servidors>\n"
    ),
    "servidors.xsd": (
        b'<?xml version="1.0"?><xs:schema xmlns:xs="http://www.w3.org/2001/'
        b'XMLSchema"><xs:element name="servidors" type="xs:string"/></xs:schema>\n'
    ),
    "compose.yml": (
        b"services:\n  web:\n    image: nginx:1.27-alpine\n"
        b"    ports:\n      - '8080:80'\n"
    ),
    "requisits-replicacio.txt": (
        b"RPO objectiu: 5 minuts\nRTO objectiu: 20 minuts\n"
        + "Dades de prova exclusivament fictícies.\n".encode()
    ),
    "topologia.txt": (
        b"VLAN 10 ALUMNES 10.10.10.0/24\nVLAN 20 SERVEIS 10.10.20.0/24\n"
        b"VLAN 30 GESTIO 10.10.30.0/24\n"
    ),
    "site.yml": (
        b"---\n- hosts: all\n  gather_facts: false\n  tasks:\n"
        b"    - debug:\n        msg: autotask fixture\n"
    ),
    "db.example.test": (
        b"$ORIGIN example.test.\n@ 3600 IN SOA ns.example.test. admin.example.test. "
        b"(1 3600 900 604800 300)\n@ IN NS ns.example.test.\nns IN A 192.0.2.53\n"
    ),
    "baseline.ps1": (
        b"Set-StrictMode -Version Latest\n"
        b"Write-Output 'AutoTask deterministic hardening fixture'\n"
    ),
    "plantilla-projecte.md": (
        b"# Proposta t\xc3\xa8cnica\n\n## Arquitectura\n## Riscos\n## Pressupost\n"
        b"## Proves\n## Recuperaci\xc3\xb3\n"
    ),
}

_ADVANCED_BYTES = (
    b"Fixture revision 2: add a second interface and verify the return route.\n"
)


def _live_or_fail(operation: Callable[[], _Result]) -> _Result:
    """Keep connector request internals, including credentials, out of pytest output."""
    try:
        return operation()
    except Exception:
        pytest.fail("local Moodle live connector operation failed", pytrace=False)


def _live_config(token_file: Path) -> MoodleConnectionConfig:
    return replace(MoodleConnectionConfig.from_token_file(token_file), timeout_seconds=120)


@pytest.mark.skipif(
    not os.environ.get("MOODLE_LIVE_TOKEN_FILE"),
    reason="MOODLE_LIVE_TOKEN_FILE is required for local Moodle integration verification",
)
def test_fixture_attachment_downloads(tmp_path: Path) -> None:
    token_file = Path(os.environ["MOODLE_LIVE_TOKEN_FILE"])
    service = MoodleService(_live_config(token_file))
    assignment = next(
        item for item in _live_or_fail(service.assignments) if item.title == "AutoTask assignment"
    )
    attachment = next(
        item for item in assignment.attachments if item.filename == "autotask-brief.txt"
    )
    receipt = _live_or_fail(
        lambda: download_attachment(service.config, assignment, attachment.attachment_key, tmp_path)
    )
    assert receipt.path.read_text(encoding="utf-8").startswith("AutoTask local fixture brief.")
    assert receipt.size_bytes > 0


@pytest.mark.skipif(
    not os.environ.get("MOODLE_LIVE_TOKEN_FILE"),
    reason="MOODLE_LIVE_TOKEN_FILE is required for local Moodle integration verification",
)
def test_fixture_scheduler_once_is_idempotent(tmp_path: Path) -> None:
    token_file = Path(os.environ["MOODLE_LIVE_TOKEN_FILE"])
    live = MoodleService(_live_config(token_file))

    class FixtureService:
        def assignments(self) -> tuple[MoodleAssignmentSnapshot, ...]:
            return tuple(
                item
                for item in _live_or_fail(live.assignments)
                if item.title == "AutoTask assignment"
            )

    stream = StringIO()
    state = MoodleState(tmp_path / "scheduler.sqlite3")
    first = _live_or_fail(lambda: once(state, FixtureService(), LocalJsonSink(stream)))
    assert first.enqueued == first.delivered == 1
    record = json.loads(stream.getvalue())
    assert record["assignment_title"] == "AutoTask assignment"
    assert record["attachments"] == [
        {
            "filename": "autotask-brief.txt",
            "size_bytes": 76,
            "mimetype": "text/plain",
            "is_lab_artifact": False,
        }
    ]
    second = _live_or_fail(lambda: once(state, FixtureService(), LocalJsonSink(StringIO())))
    assert second.enqueued == second.delivered == 0


@pytest.mark.skipif(
    not os.environ.get("MOODLE_LIVE_TOKEN_FILE"),
    reason="MOODLE_LIVE_TOKEN_FILE is required for local Moodle integration verification",
)
def test_rich_fixture_exposes_exact_assignment_matrix() -> None:
    token_file = Path(os.environ["MOODLE_LIVE_TOKEN_FILE"])
    assignments = _live_or_fail(MoodleService(_live_config(token_file)).assignments)
    assert len(assignments) == 12
    assert {item.title for item in assignments} == _RICH_TITLES | {"AutoTask assignment"}
    assert {item.course_shortname for item in assignments} == {
        "ASIX-LAB",
        "ASIX1-0369-ISO",
        "ASIX1-0372-GBD",
        "ASIX1-0373-LMSGI",
        "ASIX1-0376-IAW",
        "ASIX1-0377-ASGBD",
        "ASIX2-0370-PAX",
        "ASIX2-0374-ASO",
        "ASIX2-0375-SXI",
        "ASIX2-0378-SAD",
        "ASIX2-0379-PROJ",
    }
    assert len({item.task_key for item in assignments}) == 12
    assert len({item.revision_digest for item in assignments}) == 12


@pytest.mark.skipif(
    not os.environ.get("MOODLE_LIVE_TOKEN_FILE"),
    reason="MOODLE_LIVE_TOKEN_FILE is required for local Moodle integration verification",
)
def test_rich_fixture_downloads_every_attachment_with_exact_bytes(tmp_path: Path) -> None:
    token_file = Path(os.environ["MOODLE_LIVE_TOKEN_FILE"])
    service = MoodleService(_live_config(token_file))
    assignments = _live_or_fail(service.assignments)
    seen: dict[str, bytes] = {}
    for assignment in assignments:
        for attachment in assignment.attachments:
            receipt = _live_or_fail(
                partial(
                    download_attachment,
                    service.config,
                    assignment,
                    attachment.attachment_key,
                    tmp_path,
                )
            )
            seen[attachment.filename] = receipt.path.read_bytes()
            assert receipt.size_bytes == attachment.size_bytes
            assert len(receipt.sha256) == 64
    expected = dict(_FIXTURE_BYTES)
    if "revision-2.txt" in seen:
        expected["revision-2.txt"] = _ADVANCED_BYTES
    assert seen == expected


@pytest.mark.skipif(
    not os.environ.get("MOODLE_LIVE_TOKEN_FILE"),
    reason="MOODLE_LIVE_TOKEN_FILE is required for local Moodle integration verification",
)
def test_rich_fixture_covers_deadline_attachment_and_lab_scenarios(tmp_path: Path) -> None:
    token_file = Path(os.environ["MOODLE_LIVE_TOKEN_FILE"])
    live = MoodleService(_live_config(token_file))
    assignments = _live_or_fail(live.assignments)
    by_title = {item.title: item for item in assignments}
    now = int(time.time())
    assert by_title["Pràctica ISO 2 - Usuaris, grups i permisos Linux"].due_date < now
    assert by_title["Pràctica IAW - Desplegament web amb contenidors"].allows_submissions_from > now
    assert by_title["Pràctica PAX - VLAN, routing i diagnòstic"].due_date == 0
    assert not by_title["Pràctica ISO 2 - Usuaris, grups i permisos Linux"].attachments

    ova = by_title["Pràctica ISO 1 - Desplegament d'una OVA"]

    class OvaService:
        def assignments(self) -> tuple[MoodleAssignmentSnapshot, ...]:
            return (ova,)

    stream = StringIO()
    result = _live_or_fail(
        lambda: once(MoodleState(tmp_path / "ova.sqlite3"), OvaService(), LocalJsonSink(stream))
    )
    assert result.enqueued == result.delivered == 1
    event = json.loads(stream.getvalue())
    artifacts = {item["filename"]: item["is_lab_artifact"] for item in event["attachments"]}
    assert artifacts["asix-router-lab.ova"] is True
    assert artifacts["practica-iso-ova.pdf"] is False


@pytest.mark.skipif(
    not os.environ.get("MOODLE_LIVE_TOKEN_FILE")
    or not os.environ.get("MOODLE_LIVE_EXPECT_ADVANCED"),
    reason="advanced local Moodle fixture verification was not requested",
)
def test_rich_fixture_advanced_revision_is_visible() -> None:
    token_file = Path(os.environ["MOODLE_LIVE_TOKEN_FILE"])
    assignments = _live_or_fail(MoodleService(_live_config(token_file)).assignments)
    ova = next(
        item
        for item in assignments
        if item.title == "Pràctica ISO 1 - Desplegament d'una OVA"
    )
    assert "Actualització:" in ova.intro
    assert {item.filename for item in ova.attachments} == {
        "asix-router-lab.ova",
        "practica-iso-ova.pdf",
        "revision-2.txt",
    }


def test_live_failure_helper_redacts_exception_text() -> None:
    sentinel = "SENTINEL_LIVE_TOKEN"
    with pytest.raises(pytest.fail.Exception) as failure:
        _live_or_fail(lambda: (_ for _ in ()).throw(RuntimeError(sentinel)))
    assert str(failure.value) == "local Moodle live connector operation failed"
