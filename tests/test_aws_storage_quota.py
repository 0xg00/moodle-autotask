from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from moddle_autotask.adapters.aws.storage_quota import (
    StorageCapacityError,
    StorageDemand,
    StorageEnvelopeError,
    StorageLimit,
    admit_owner_write,
    measure_tree_no_follow,
    storage_admission_lock,
    storage_demand_for_files,
)


def test_measure_tree_counts_nodes_and_allocated_blocks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    payload = root / "payload"
    payload.write_bytes(b"x")
    demand = measure_tree_no_follow(root)
    root_blocks = getattr(root.lstat(), "st_blocks", None)
    expected = getattr(payload.lstat(), "st_blocks", None)
    expected_root_bytes = root_blocks * 512 if isinstance(root_blocks, int) else root.stat().st_size
    expected_bytes = expected * 512 if isinstance(expected, int) else payload.stat().st_size
    assert demand == StorageDemand(expected_root_bytes + expected_bytes, 2)


def test_admission_rejects_exact_plus_one_node_limit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    current = measure_tree_no_follow(root)
    with pytest.raises(StorageCapacityError):
        admit_owner_write(
            root,
            StorageDemand(0, 1),
            StorageLimit(current.allocated_bytes, current.nodes),
        )


def test_measure_tree_rejects_hardlink_and_special_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    first = root / "first"
    first.write_bytes(b"x")
    try:
        os.link(first, root / "second")
    except OSError:
        pytest.skip("hard links are unavailable")
    with pytest.raises(StorageEnvelopeError):
        measure_tree_no_follow(root)


def test_write_demand_rounds_each_pending_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    demand = storage_demand_for_files(root, (1, 1), 2)
    if os.name != "nt":
        filesystem = getattr(os, "stat" + "vfs")(root)
        unit = filesystem.f_frsize or filesystem.f_bsize
        assert demand == StorageDemand(2 * unit, 2)
    else:
        assert demand == StorageDemand(2, 2)


@pytest.mark.skipif(os.name == "nt", reason="POSIX allocation-unit semantics")
def test_allocation_rounding_preserves_exact_and_plus_one_boundaries(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    unit = getattr(os, "stat" + "vfs")(root).f_frsize or getattr(os, "stat" + "vfs")(root).f_bsize
    current = measure_tree_no_follow(root)
    exact = storage_demand_for_files(root, (1, unit), 2)
    limit = StorageLimit(current.allocated_bytes + exact.allocated_bytes, current.nodes + 2)
    admit_owner_write(root, exact, limit, root_headroom=False)
    with pytest.raises(StorageCapacityError):
        admit_owner_write(
            root,
            storage_demand_for_files(root, (1, unit, 1), 3),
            limit,
            root_headroom=False,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor identity semantics")
def test_headroom_rejects_root_path_replacement_after_descriptor_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import moddle_autotask.adapters.aws.storage_quota as quota

    root = tmp_path / "root"
    root.mkdir()
    replacement = tmp_path / "replacement"
    real_stats = quota._filesystem_stats

    def replace_root(descriptor: int) -> object:
        root.rename(replacement)
        root.mkdir()
        return SimpleNamespace(f_frsize=4096, f_bsize=4096, f_bavail=10**12, f_favail=10**12)

    monkeypatch.setattr(quota, "_filesystem_stats", replace_root)
    with pytest.raises(StorageEnvelopeError, match="changed"):
        admit_owner_write(root, StorageDemand(0, 0), StorageLimit(1 << 30, 10_000))
    monkeypatch.setattr(quota, "_filesystem_stats", real_stats)


@pytest.mark.skipif(os.name == "nt", reason="POSIX kernel admission lock")
def test_admission_lock_allows_only_one_boundary_publisher(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    current = measure_tree_no_follow(root)
    limit = StorageLimit(current.allocated_bytes, current.nodes + 1)
    outcomes: list[str] = []

    def publish(name: str) -> None:
        try:
            with storage_admission_lock(root):
                admit_owner_write(root, StorageDemand(0, 1), limit, root_headroom=False)
                (root / name).write_bytes(b"")
        except StorageCapacityError:
            outcomes.append("refused")
        else:
            outcomes.append("published")

    first = threading.Thread(target=publish, args=("first",))
    second = threading.Thread(target=publish, args=("second",))
    first.start()
    second.start()
    first.join()
    second.join()
    assert sorted(outcomes) == ["published", "refused"]
    assert len(list(root.iterdir())) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO semantics")
def test_measure_tree_rejects_fifo(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    getattr(os, "mk" + "fifo")(root / "fifo")
    with pytest.raises(StorageEnvelopeError):
        measure_tree_no_follow(root)
