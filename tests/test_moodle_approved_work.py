from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from moddle_autotask.adapters.moodle.approval_state import ApprovalState, ApprovalStateError
from moddle_autotask.adapters.moodle.models import MoodleAssignmentSnapshot, MoodleAttachment
from moddle_autotask.adapters.moodle.scheduler import draft_from_assignment
from moddle_autotask.adapters.moodle.state import (
    MoodleState,
    NotificationAttachment,
    NotificationDraft,
    NotificationEvent,
)
from moddle_autotask.domain.models import ExecutionMode, LabHandle


def _event(
    tmp_path: Path,
    revision: str,
    *,
    title: str = "Despliegue",
    attachments: tuple[NotificationAttachment, ...] = (),
) -> NotificationEvent:
    event = MoodleState(tmp_path / f"moodle-{revision}.sqlite3").enqueue(
        NotificationDraft(
            "moodle-task-v1:" + "a" * 64,
            "moodle-assignment-v1:" + revision * 64,
            "Administración de sistemas",
            "ASIX-M06",
            title,
            0,
            100,
            0,
            0,
            1,
            attachments,
        ),
        now=1,
    )
    assert event is not None
    return event


def _approve(state: ApprovalState, event: NotificationEvent, now: int = 2) -> None:
    buttons = state.prepare(event, now=1)
    assert state.resolve(buttons.approve, 42, 42, now=now).result == "approved"


def test_approval_atomically_creates_exact_in_guest_work(tmp_path: Path) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    event = _event(
        tmp_path,
        "b",
        attachments=(
            NotificationAttachment("base.ova", 123, "application/octet-stream", True),
        ),
    )

    _approve(state, event)
    item = state.work_status(event.task_key, event.revision_digest)

    assert item is not None
    assert item.event == event
    assert item.selected_mode is ExecutionMode.IN_GUEST
    assert item.status == "pending" and item.lab_handle is None and item.attempts == 0
    assert len(item.provision_key) == 64


def test_mode_selection_is_deterministic_and_never_auto(tmp_path: Path) -> None:
    cases = (
        ("b", "Informe", (), ExecutionMode.CENTRAL),
        ("c", "Práctica de redes", (), ExecutionMode.HYBRID),
        (
            "d",
            "Informe",
            (NotificationAttachment("capture.pcap", 20, None, True),),
            ExecutionMode.HYBRID,
        ),
    )
    state = ApprovalState(tmp_path / "approval.sqlite3")
    for revision, title, attachments, expected in cases:
        event = _event(tmp_path, revision, title=title, attachments=attachments)
        _approve(state, event, now=ord(revision))
        item = state.work_status(event.task_key, event.revision_digest)
        assert item is not None and item.selected_mode is expected


def test_campaign_catalog_drafts_flow_through_approval_with_exact_modes(tmp_path: Path) -> None:
    attachment = MoodleAttachment(
        "moodle-attachment-v1:" + "b" * 64,
        "introattachments",
        "negative.ova",
        "/",
        "https://example.test/pluginfile.php/1/negative.ova",
        3,
        1,
        "application/octet-stream",
    )
    entries = (
        ("a", "Campaign Report", (), ExecutionMode.CENTRAL),
        ("b", "Práctica Windows Server validation", (), ExecutionMode.HYBRID),
        ("c", "Práctica Windows Server command failure", (), ExecutionMode.HYBRID),
        ("d", "OVA import validation", (attachment,), ExecutionMode.IN_GUEST),
    )
    notifications = MoodleState(tmp_path / "notifications.sqlite3")
    approval = ApprovalState(tmp_path / "approval.sqlite3")
    for index, (letter, title, attachments, expected) in enumerate(entries, start=1):
        snapshot = MoodleAssignmentSnapshot(
            "moodle-task-v1:" + letter * 64,
            "moodle-assignment-v1:" + letter * 64,
            "https://example.test",
            index,
            10,
            "ASIX Campaign 01",
            "ASIX-CAMPAIGN-01",
            index + 100,
            title,
            "discarded fixture intro",
            0,
            1,
            0,
            0,
            1,
            attachments,
        )
        event = notifications.enqueue(draft_from_assignment(snapshot), now=index)
        assert event is not None
        _approve(approval, event, now=index + 10)
        item = approval.work_status(event.task_key, event.revision_digest)
        assert item is not None and item.selected_mode is expected


def test_work_lease_has_one_winner_and_recovers_with_same_key(tmp_path: Path) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    event = _event(tmp_path, "b")
    _approve(state, event)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = tuple(
            pool.map(lambda owner: state.claim_work(owner, 10, now=10), ("one", "two"))
        )
    winners = tuple(claim for claim in claims if claim is not None)
    assert len(winners) == 1
    recovered = state.claim_work("recovery", 10, now=21)
    assert recovered is not None
    assert recovered.item.provision_key == winners[0].item.provision_key
    assert recovered.item.attempts == 2


def test_only_one_noncentral_lab_can_be_active(tmp_path: Path) -> None:
    state = ApprovalState(tmp_path / "approval.sqlite3")
    attachment = (NotificationAttachment("base.ova", 123, None, True),)
    first = _event(tmp_path, "b", attachments=attachment)
    second = _event(tmp_path, "c", attachments=attachment)
    _approve(state, first, 2)
    _approve(state, second, 3)
    claim = state.claim_work("one", 10, now=10)
    assert claim is not None
    assert state.record_lab(claim, LabHandle("lab:first"), now=11)

    pending_claim = state.claim_work("two", 10, now=12)

    assert pending_claim is not None
    assert pending_claim.item.event == first
    assert pending_claim.item.status == "lab_pending"
    assert state.mark_ready(pending_claim, now=13)
    assert state.claim_work("two", 10, now=14) is None


def test_v1_database_migrates_and_backfills_prior_approval(tmp_path: Path) -> None:
    path = tmp_path / "approval.sqlite3"
    state = ApprovalState(path)
    event = _event(tmp_path, "b")
    _approve(state, event, now=9)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX submission_outbox_pending_idx")
        connection.execute("DROP TABLE submission_callbacks")
        connection.execute("DROP TABLE submission_outbox")
        connection.execute("DROP TABLE submissions")
        connection.execute("DROP INDEX work_claimable_idx")
        connection.execute("DROP INDEX execution_outbox_pending_idx")
        connection.execute("DROP TABLE execution_outbox")
        connection.execute("DROP TABLE work_items")
        connection.execute("UPDATE metadata SET value = '1' WHERE key = 'schema_version'")

    migrated = ApprovalState(path)

    item = migrated.work_status(event.task_key, event.revision_digest)
    assert item is not None and item.status == "pending"


def test_tampered_mode_is_rejected_before_claim(tmp_path: Path) -> None:
    path = tmp_path / "approval.sqlite3"
    state = ApprovalState(path)
    event = _event(
        tmp_path,
        "b",
        attachments=(NotificationAttachment("base.ova", 123, None, True),),
    )
    _approve(state, event)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE work_items SET selected_mode = 'central'")

    try:
        state.claim_work("worker", 10, now=10)
    except ApprovalStateError as error:
        assert "corrupt" in str(error)
    else:
        raise AssertionError("tampered work must be rejected")
    with sqlite3.connect(path) as connection:
        lease = connection.execute("SELECT lease_token FROM work_items").fetchone()
    assert lease == (None,)
