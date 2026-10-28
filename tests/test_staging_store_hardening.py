from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import unified_paper_orchestrator_v2.staging as staging_module
from unified_paper_orchestrator_v2.staging import StagingError, StagingStore


RUN_ID = f"run_{'a' * 32}"
TASK_ID = f"task_{'b' * 32}"


def _manifest() -> dict:
    return {
        "schema_version": "test_review_receipt_v1",
        "run_id": RUN_ID,
        "task_id": TASK_ID,
        "canonical_write": False,
    }


def test_post_publish_manifest_tamper_is_detected_before_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StagingStore(tmp_path / "staging", acl_hardener=None)
    original_replace = staging_module.os.replace

    def replace_then_tamper(source, destination) -> None:
        original_replace(source, destination)
        (Path(destination) / "result.json").write_text(
            '{"tampered":true}\n',
            encoding="utf-8",
        )

    monkeypatch.setattr(staging_module.os, "replace", replace_then_tamper)

    with pytest.raises(StagingError) as caught:
        store.persist_review(
            run_id=RUN_ID,
            task_id=TASK_ID,
            markdown="# Review Round 1\n",
            manifest=_manifest(),
        )

    assert caught.value.code == "STAGING_VERIFY_FAILED"
    with pytest.raises(StagingError):
        store.load_review(RUN_ID, TASK_ID)


def test_identical_existing_target_is_idempotent_but_different_content_conflicts(
    tmp_path: Path,
) -> None:
    store = StagingStore(tmp_path / "staging", acl_hardener=None)
    first_outputs, first_manifest = store.persist_review(
        run_id=RUN_ID,
        task_id=TASK_ID,
        markdown="# Review Round 1\n",
        manifest=_manifest(),
    )
    second_outputs, second_manifest = store.persist_review(
        run_id=RUN_ID,
        task_id=TASK_ID,
        markdown="# Review Round 1\n",
        manifest=_manifest(),
    )

    assert [item.sha256 for item in second_outputs] == [
        item.sha256 for item in first_outputs
    ]
    assert second_manifest == first_manifest
    assert {item.name for item in store.task_dir(RUN_ID, TASK_ID).iterdir()} == {
        "review_round_1.md",
        "result.json",
    }
    with pytest.raises(StagingError) as conflict:
        store.persist_review(
            run_id=RUN_ID,
            task_id=TASK_ID,
            markdown="# Review Round 1\nchanged\n",
            manifest=_manifest(),
        )
    assert conflict.value.code == "STAGING_TARGET_CONFLICT"


@pytest.mark.parametrize(
    ("run_id", "task_id"),
    [
        (".", TASK_ID),
        ("..", TASK_ID),
        (RUN_ID, "."),
        (f"{RUN_ID}.", TASK_ID),
        (RUN_ID, "CON"),
    ],
)
def test_ambiguous_windows_identifiers_are_rejected(
    tmp_path: Path,
    run_id: str,
    task_id: str,
) -> None:
    store = StagingStore(tmp_path / "staging", acl_hardener=None)

    with pytest.raises(StagingError) as caught:
        store.task_dir(run_id, task_id)

    assert caught.value.code == "STAGING_ID_INVALID"
    assert not store.root.exists()


def test_serialization_failure_leaves_no_transaction_directory(tmp_path: Path) -> None:
    store = StagingStore(tmp_path / "staging", acl_hardener=None)

    with pytest.raises(StagingError) as caught:
        store.persist_review(
            run_id=RUN_ID,
            task_id=TASK_ID,
            markdown="\ud800",
            manifest=_manifest(),
        )

    assert caught.value.code == "STAGING_SERIALIZATION_FAILED"
    assert not store.root.exists()


def test_concurrent_identical_publishers_converge_to_one_verified_pair(
    tmp_path: Path,
) -> None:
    store = StagingStore(tmp_path / "staging", acl_hardener=None)

    def publish(_index: int) -> list[str]:
        outputs, _manifest_value = store.persist_review(
            run_id=RUN_ID,
            task_id=TASK_ID,
            markdown="# Review Round 1\n",
            manifest=_manifest(),
        )
        return [item.sha256 for item in outputs]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(publish, range(16)))

    assert len({tuple(item) for item in results}) == 1
    assert {item.name for item in store.task_dir(RUN_ID, TASK_ID).iterdir()} == {
        "review_round_1.md",
        "result.json",
    }


def test_post_publish_symlink_swap_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StagingStore(tmp_path / "staging", acl_hardener=None)
    outside = tmp_path / "outside.md"
    outside.write_text("# Review Round 1\n", encoding="utf-8")
    original_replace = staging_module.os.replace
    created_link: Path | None = None

    def replace_then_link(source, destination) -> None:
        nonlocal created_link
        original_replace(source, destination)
        review = Path(destination) / "review_round_1.md"
        review.unlink()
        try:
            review.symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"file symlink creation unavailable: {exc}")
        created_link = review

    monkeypatch.setattr(staging_module.os, "replace", replace_then_link)
    try:
        with pytest.raises(StagingError) as caught:
            store.persist_review(
                run_id=RUN_ID,
                task_id=TASK_ID,
                markdown="# Review Round 1\n",
                manifest=_manifest(),
            )
        assert caught.value.code in {"STAGING_REPARSE_REJECTED", "STAGING_TARGET_UNSAFE"}
    finally:
        if created_link is not None and created_link.is_symlink():
            created_link.unlink()

