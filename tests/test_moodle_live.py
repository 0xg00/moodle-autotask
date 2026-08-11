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

_MANAGED_TITLES_BY_COURSE = {
    "ASIX-LAB": frozenset({"AutoTask assignment"}),
    "ASIX1-0369-ISO": frozenset(
        {
            "Pràctica ISO 1 - Desplegament d'una OVA",
            "Pràctica ISO 2 - Usuaris, grups i permisos Linux",
        }
    ),
    "ASIX1-0371-FM": frozenset(),
    "ASIX1-0372-GBD": frozenset({"Pràctica GBD - Còpia i restauració de PostgreSQL"}),
    "ASIX1-0373-LMSGI": frozenset({"Pràctica LMSGI - Validació XML amb XSD"}),
    "ASIX1-0376-IAW": frozenset({"Pràctica IAW - Desplegament web amb contenidors"}),
    "ASIX1-0377-ASGBD": frozenset({"Pràctica ASGBD - Pla de replicació"}),
    "ASIX2-0370-PAX": frozenset({"Pràctica PAX - VLAN, routing i diagnòstic"}),
    "ASIX2-0374-ASO": frozenset({"Pràctica ASO - Automatització amb Ansible"}),
    "ASIX2-0375-SXI": frozenset({"Pràctica SXI - DNS autoritatiu i resolució"}),
    "ASIX2-0378-SAD": frozenset({"Pràctica SAD - Hardening de Windows Server"}),
    "ASIX2-0379-PROJ": frozenset({"Projecte ASIX - Lliurament de la proposta tècnica"}),
    "ASIX-CAMPAIGN-01": frozenset(
        {
            "Campaign Report",
            "Práctica Windows Server validation",
            "Práctica Windows Server command failure",
            "OVA import validation",
            "AutoTask draft-only submission fixture",
            "AutoTask draft statement fixture",
            "AutoTask statement-only blocked fixture",
        }
    ),
}

_V4_SUBMISSION_POLICIES = {
    "AutoTask draft-only submission fixture": (True, False),
    "AutoTask draft statement fixture": (True, True),
    "AutoTask statement-only blocked fixture": (False, True),
}

_V4_SUBMISSION_STATEMENT = (
    b"<p>Declaro que aquesta entrega \xc3\xa9s meva \xe2\x80\x94 \xe4\xbd\xa0\xe5\xa5\xbd.</p>"
    b"<p>Versi\xc3\xb3 <strong>HTML</strong> distintiva.</p>"
)

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
    "report.txt": b"Write the deterministic central report.\n",
    "windows-lab.txt": b"Validate the Windows lab through SSM.\n",
    "failure.txt": b"Run the deterministic failing Windows command.\n",
    "negative.ova": (
        b"AUTOTASK-METADATA-ONLY-OVA\nThis tiny deterministic fixture is metadata-only "
        b"and is not a real OVA.\n"
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


def _managed_fixture_assignments(
    assignments: tuple[MoodleAssignmentSnapshot, ...],
) -> tuple[MoodleAssignmentSnapshot, ...]:
    return tuple(
        item for item in assignments if item.course_shortname in _MANAGED_TITLES_BY_COURSE
    )


def test_managed_fixture_filter_includes_assignment_empty_course() -> None:
    unexpected = MoodleAssignmentSnapshot(
        task_key="task",
        revision_digest="revision",
        site_url="https://example.test",
        assignment_id=1,
        course_id=1,
        course_name="Fonaments de Maquinari",
        course_shortname="ASIX1-0371-FM",
        course_module_id=1,
        title="Unexpected assignment",
        intro="",
        allows_submissions_from=0,
        due_date=0,
        cutoff_date=0,
        grading_due_date=0,
        time_modified=0,
        attachments=(),
    )

    assert _managed_fixture_assignments((unexpected,)) == (unexpected,)


@pytest.mark.skipif(
    not os.environ.get("MOODLE_LIVE_TOKEN_FILE"),
    reason="MOODLE_LIVE_TOKEN_FILE is required for local Moodle integration verification",
)
def test_fixture_attachment_downloads(tmp_path: Path) -> None:
    token_file = Path(os.environ["MOODLE_LIVE_TOKEN_FILE"])
    service = MoodleService(_live_config(token_file))
    assignment = next(
        item
        for item in _managed_fixture_assignments(_live_or_fail(service.assignments))
        if item.course_shortname == "ASIX-LAB" and item.title == "AutoTask assignment"
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
                for item in _managed_fixture_assignments(_live_or_fail(live.assignments))
                if item.course_shortname == "ASIX-LAB" and item.title == "AutoTask assignment"
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
    assignments = _managed_fixture_assignments(
        _live_or_fail(MoodleService(_live_config(token_file)).assignments)
    )
    expected_matrix = {
        (course_shortname, title)
        for course_shortname, titles in _MANAGED_TITLES_BY_COURSE.items()
        for title in titles
    }
    assert len(assignments) == 19
    assert {
        (item.course_shortname, item.title) for item in assignments
    } == expected_matrix
    assert {item.course_shortname for item in assignments} == {
        course_shortname
        for course_shortname, titles in _MANAGED_TITLES_BY_COURSE.items()
        if titles
    }
    assert len({item.task_key for item in assignments}) == 19
    assert len({item.revision_digest for item in assignments}) == 19


@pytest.mark.skipif(
    not os.environ.get("MOODLE_LIVE_TOKEN_FILE"),
    reason="MOODLE_LIVE_TOKEN_FILE is required for local Moodle integration verification",
)
def test_rich_fixture_downloads_every_attachment_with_exact_bytes(tmp_path: Path) -> None:
    token_file = Path(os.environ["MOODLE_LIVE_TOKEN_FILE"])
    service = MoodleService(_live_config(token_file))
    assignments = _managed_fixture_assignments(_live_or_fail(service.assignments))
    seen: dict[str, bytes] = {}
    for assignment in assignments:
        for attachment in assignment.attachments:
            assert attachment.filename not in seen
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
    expected = {**_FIXTURE_BYTES, "revision-2.txt": _ADVANCED_BYTES}
    assert seen == expected


@pytest.mark.skipif(
    not os.environ.get("MOODLE_LIVE_TOKEN_FILE"),
    reason="MOODLE_LIVE_TOKEN_FILE is required for local Moodle integration verification",
)
def test_rich_fixture_v4_submission_policies_are_exact() -> None:
    token_file = Path(os.environ["MOODLE_LIVE_TOKEN_FILE"])
    assignments = _managed_fixture_assignments(
        _live_or_fail(MoodleService(_live_config(token_file)).assignments)
    )
    policies = {
        item.title: item
        for item in assignments
        if item.course_shortname == "ASIX-CAMPAIGN-01"
        and item.title in _V4_SUBMISSION_POLICIES
    }
    assert set(policies) == set(_V4_SUBMISSION_POLICIES)
    for title, (submission_drafts, requires_statement) in _V4_SUBMISSION_POLICIES.items():
        assignment = policies[title]
        assert (assignment.submission_drafts, assignment.requires_submission_statement) == (
            submission_drafts,
            requires_statement,
        )
        assert (
            assignment.file_submission_enabled,
            assignment.file_submission_max_files,
            assignment.file_submission_max_bytes,
            assignment.file_submission_filetypes,
        ) == (True, 1, 2 * 1024 * 1024, ".md")
        assert not assignment.team_submission
        assert not assignment.no_submissions
        assert not assignment.attachments
        if requires_statement:
            assert assignment.submission_statement.encode("utf-8") == _V4_SUBMISSION_STATEMENT
            assert assignment.submission_statement_format == 1


@pytest.mark.skipif(
    not os.environ.get("MOODLE_LIVE_TOKEN_FILE"),
    reason="MOODLE_LIVE_TOKEN_FILE is required for local Moodle integration verification",
)
def test_rich_fixture_covers_deadline_attachment_and_lab_scenarios(tmp_path: Path) -> None:
    token_file = Path(os.environ["MOODLE_LIVE_TOKEN_FILE"])
    live = MoodleService(_live_config(token_file))
    assignments = _managed_fixture_assignments(_live_or_fail(live.assignments))
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
    assignments = _managed_fixture_assignments(
        _live_or_fail(MoodleService(_live_config(token_file)).assignments)
    )
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
