from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from unified_paper_orchestrator_v2.canonical import (
    CanonicalCommitAuthorization,
    CanonicalCommitError,
    CanonicalCommitter,
    DestinationCAS,
)
from unified_paper_orchestrator_v2.contracts import ArtifactRef, canonical_json


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _authorized(
    root: Path,
    control: Path,
    *,
    fault_injector=None,
    stale_seconds: int = 300,
    staging_root: Path | None = None,
) -> CanonicalCommitter:
    staging_root = staging_root or root.parent / "staging"
    return CanonicalCommitter(
        canonical_roots={"papers": root},
        staging_roots=(staging_root,),
        control_root=control,
        authorization=CanonicalCommitAuthorization(
            actor="canonical-test",
            reason="isolated canonical transaction verification",
        ),
        acl_hardener=None,
        fault_injector=fault_injector,
        lock_timeout_seconds=5,
        lock_stale_seconds=stale_seconds,
    )


def _staged(path: Path, payload: bytes) -> ArtifactRef:
    path.write_bytes(payload)
    return ArtifactRef.from_file(path, media_type="text/markdown")


def _error_code(exc_info: pytest.ExceptionInfo[CanonicalCommitError]) -> str:
    assert exc_info.value.fallback_allowed is False
    return exc_info.value.code


def test_gate_is_disabled_by_default_and_requires_explicit_authorization(tmp_path: Path) -> None:
    committer = CanonicalCommitter()

    with pytest.raises(CanonicalCommitError) as caught:
        committer.commit(
            commit_id="disabled-1",
            staged=ArtifactRef(uri=str(tmp_path / "missing"), sha256="a" * 64),
            root_name="papers",
            destination=DestinationCAS("paper.md", expected_exists=False),
        )

    assert _error_code(caught) == "CANONICAL_COMMIT_DISABLED"
    assert committer.enabled is False
    assert list(tmp_path.iterdir()) == []


def test_commit_backs_up_existing_file_audits_hashes_only_and_is_idempotent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "canonical"
    control = tmp_path / "control"
    staging = tmp_path / "staging"
    root.mkdir()
    staging.mkdir()
    original = b"ORIGINAL PRIVATE BODY"
    replacement = b"REPLACEMENT TOP SECRET BODY"
    target = root / "paper.md"
    target.write_bytes(original)
    staged = _staged(staging / "paper.md", replacement)
    committer = _authorized(root, control)

    first = committer.commit(
        commit_id="commit-existing-1",
        staged=staged,
        root_name="papers",
        destination=DestinationCAS(
            "paper.md",
            expected_exists=True,
            expected_sha256=_sha256(original),
        ),
    )
    second = committer.commit(
        commit_id="commit-existing-1",
        staged=staged,
        root_name="papers",
        destination=DestinationCAS(
            "paper.md",
            expected_exists=True,
            expected_sha256=_sha256(original),
        ),
    )

    assert first.status == "committed"
    assert first.idempotent is False
    assert second.status == "already_committed"
    assert second.idempotent is True
    assert target.read_bytes() == replacement
    backups = list((control / "backups").rglob("preimage.bin"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    audit = (control / "canonical_audit.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in audit.splitlines()]
    assert len(records) == 1
    assert records[0]["destination_pre_sha256"] == _sha256(original)
    assert records[0]["destination_post_sha256"] == _sha256(replacement)
    assert original.decode() not in audit
    assert replacement.decode() not in audit
    assert not (control / "active_commit.json").exists()
    assert list(root.glob(".canonical-*.tmp")) == []


def test_same_commit_id_cannot_be_rebound_to_another_artifact(tmp_path: Path) -> None:
    root = tmp_path / "canonical"
    control = tmp_path / "control"
    staging = tmp_path / "staging"
    root.mkdir()
    staging.mkdir()
    first = _staged(staging / "first.md", b"first")
    second = _staged(staging / "second.md", b"second")
    committer = _authorized(root, control)
    destination = DestinationCAS("paper.md", expected_exists=False)
    committer.commit(
        commit_id="immutable-commit-id",
        staged=first,
        root_name="papers",
        destination=destination,
    )

    with pytest.raises(CanonicalCommitError) as caught:
        committer.commit(
            commit_id="immutable-commit-id",
            staged=second,
            root_name="papers",
            destination=destination,
        )

    assert _error_code(caught) == "CANONICAL_COMMIT_ID_CONFLICT"
    assert (root / "paper.md").read_bytes() == b"first"
    assert len((control / "canonical_audit.jsonl").read_text().splitlines()) == 1


def test_malformed_audit_record_is_rejected_before_idempotent_replay(tmp_path: Path) -> None:
    root = tmp_path / "canonical"
    staging = tmp_path / "staging"
    control = tmp_path / "control"
    root.mkdir()
    staging.mkdir()
    staged = _staged(staging / "paper.md", b"audited payload")
    committer = _authorized(root, control)
    destination = DestinationCAS("paper.md", expected_exists=False)
    committer.commit(
        commit_id="audit-corruption",
        staged=staged,
        root_name="papers",
        destination=destination,
    )
    audit_path = control / "canonical_audit.jsonl"
    record = json.loads(audit_path.read_text(encoding="utf-8"))
    record.pop("destination_post_sha256")
    audit_path.write_text(canonical_json(record) + "\n", encoding="utf-8")

    with pytest.raises(CanonicalCommitError) as caught:
        committer.commit(
            commit_id="audit-corruption",
            staged=staged,
            root_name="papers",
            destination=destination,
        )

    assert _error_code(caught) == "CANONICAL_AUDIT_CORRUPT"
    assert (root / "paper.md").read_bytes() == b"audited payload"


def test_staged_artifact_hash_is_rechecked_before_any_canonical_write(tmp_path: Path) -> None:
    root = tmp_path / "canonical"
    staging = tmp_path / "staging"
    root.mkdir()
    staging.mkdir()
    source = staging / "paper.md"
    staged = _staged(source, b"verified")
    source.write_bytes(b"drifted")

    with pytest.raises(CanonicalCommitError) as caught:
        _authorized(root, tmp_path / "control").commit(
            commit_id="hash-drift-1",
            staged=staged,
            root_name="papers",
            destination=DestinationCAS("paper.md", expected_exists=False),
        )

    assert _error_code(caught) == "CANONICAL_SOURCE_HASH_MISMATCH"
    assert not (root / "paper.md").exists()


def test_staged_artifact_must_belong_to_an_explicit_disjoint_staging_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "canonical"
    staging = tmp_path / "staging"
    outside = tmp_path / "outside"
    root.mkdir()
    staging.mkdir()
    outside.mkdir()
    staged = _staged(outside / "paper.md", b"outside staging")

    with pytest.raises(CanonicalCommitError) as caught:
        _authorized(root, tmp_path / "control", staging_root=staging).commit(
            commit_id="outside-staging",
            staged=staged,
            root_name="papers",
            destination=DestinationCAS("paper.md", expected_exists=False),
        )

    assert _error_code(caught) == "CANONICAL_SOURCE_OUTSIDE_STAGING"
    assert not (root / "paper.md").exists()


def test_canonical_staging_and_control_roots_must_be_disjoint(tmp_path: Path) -> None:
    root = tmp_path / "canonical"
    staging = root / "nested-staging"
    root.mkdir()
    staging.mkdir()
    staged = _staged(staging / "paper.md", b"overlapping roots")

    with pytest.raises(CanonicalCommitError) as caught:
        _authorized(root, tmp_path / "control", staging_root=staging).commit(
            commit_id="overlapping-roots",
            staged=staged,
            root_name="papers",
            destination=DestinationCAS("paper.md", expected_exists=False),
        )

    assert _error_code(caught) == "CANONICAL_CONFIGURATION_INVALID"
    assert not (root / "paper.md").exists()


@pytest.mark.parametrize(
    ("expectation", "expected_code"),
    [
        (DestinationCAS("paper.md", expected_exists=False), "CANONICAL_CAS_MISMATCH"),
        (
            DestinationCAS("paper.md", expected_exists=True, expected_sha256="0" * 64),
            "CANONICAL_CAS_MISMATCH",
        ),
        (
            DestinationCAS("paper.md", expected_exists=None),  # type: ignore[arg-type]
            "CANONICAL_CAS_INVALID",
        ),
    ],
)
def test_destination_cas_is_explicit_and_fail_closed(
    tmp_path: Path,
    expectation: DestinationCAS,
    expected_code: str,
) -> None:
    root = tmp_path / "canonical"
    staging = tmp_path / "staging"
    root.mkdir()
    staging.mkdir()
    target = root / "paper.md"
    target.write_bytes(b"current")
    staged = _staged(staging / "paper.md", b"replacement")

    with pytest.raises(CanonicalCommitError) as caught:
        _authorized(root, tmp_path / "control").commit(
            commit_id=f"cas-{expected_code.lower()}",
            staged=staged,
            root_name="papers",
            destination=expectation,
        )

    assert _error_code(caught) == expected_code
    assert target.read_bytes() == b"current"


def test_only_configured_roots_and_contained_relative_destinations_are_allowed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "canonical"
    staging = tmp_path / "staging"
    root.mkdir()
    staging.mkdir()
    staged = _staged(staging / "paper.md", b"replacement")
    committer = _authorized(root, tmp_path / "control")

    with pytest.raises(CanonicalCommitError) as unknown:
        committer.commit(
            commit_id="unknown-root",
            staged=staged,
            root_name="not-configured",
            destination=DestinationCAS("paper.md", expected_exists=False),
        )
    with pytest.raises(CanonicalCommitError) as escaped:
        committer.commit(
            commit_id="escaped-path",
            staged=staged,
            root_name="papers",
            destination=DestinationCAS("../outside.md", expected_exists=False),
        )

    assert _error_code(unknown) == "CANONICAL_ROOT_UNKNOWN"
    assert _error_code(escaped) == "CANONICAL_PATH_ESCAPE"
    assert not (tmp_path / "outside.md").exists()


def test_destination_symlinks_are_rejected_without_following_them(tmp_path: Path) -> None:
    root = tmp_path / "canonical"
    outside = tmp_path / "outside"
    staging = tmp_path / "staging"
    root.mkdir()
    outside.mkdir()
    staging.mkdir()
    staged = _staged(staging / "paper.md", b"replacement")
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    with pytest.raises(CanonicalCommitError) as caught:
        _authorized(root, tmp_path / "control").commit(
            commit_id="symlink-path",
            staged=staged,
            root_name="papers",
            destination=DestinationCAS("linked/paper.md", expected_exists=False),
        )

    assert _error_code(caught) == "CANONICAL_SYMLINK_REJECTED"
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_destination_junctions_are_rejected_without_writing_outside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "canonical"
    outside = tmp_path / "outside"
    staging = tmp_path / "staging"
    root.mkdir()
    outside.mkdir()
    staging.mkdir()
    staged = _staged(staging / "paper.md", b"must remain contained")
    junction = root / "jump"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        check=False,
    )
    if created.returncode != 0 or not junction.exists():
        pytest.skip(f"junction creation unavailable: {created.stderr or created.stdout}")
    try:
        with pytest.raises(CanonicalCommitError) as caught:
            _authorized(root, tmp_path / "control").commit(
                commit_id="junction-escape",
                staged=staged,
                root_name="papers",
                destination=DestinationCAS("jump/pwn.md", expected_exists=False),
            )

        assert _error_code(caught) == "CANONICAL_SYMLINK_REJECTED"
        assert not (outside / "pwn.md").exists()
    finally:
        if junction.exists():
            junction.rmdir()


@pytest.mark.parametrize(
    ("failure_point", "expected_code"),
    [
        ("after_backup", "CANONICAL_BACKUP_FAILED"),
        ("after_temp_write", "CANONICAL_WRITE_FAILED"),
        ("after_publish", "CANONICAL_PUBLISH_FAILED"),
        ("before_audit_write", "CANONICAL_AUDIT_WRITE_FAILED"),
    ],
)
def test_injected_write_failures_restore_exact_preimage_and_never_fallback(
    tmp_path: Path,
    failure_point: str,
    expected_code: str,
) -> None:
    root = tmp_path / "canonical"
    staging = tmp_path / "staging"
    control = tmp_path / "control"
    root.mkdir()
    staging.mkdir()
    original = b"exact original bytes\x00\xff"
    target = root / "paper.md"
    target.write_bytes(original)
    staged = _staged(staging / "paper.md", b"new bytes")

    def fail(point: str) -> None:
        if point == failure_point:
            raise OSError("injected write failure")

    with pytest.raises(CanonicalCommitError) as caught:
        _authorized(root, control, fault_injector=fail).commit(
            commit_id=f"failure-{failure_point}",
            staged=staged,
            root_name="papers",
            destination=DestinationCAS(
                "paper.md",
                expected_exists=True,
                expected_sha256=_sha256(original),
            ),
        )

    assert _error_code(caught) == expected_code
    assert target.read_bytes() == original
    assert not (control / "canonical_audit.jsonl").exists()
    assert not (control / "active_commit.json").exists()
    assert list(root.glob(".canonical-*.tmp")) == []


def test_interrupted_publish_is_rolled_back_by_next_authorized_recovery(tmp_path: Path) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    root = tmp_path / "canonical"
    staging = tmp_path / "staging"
    control = tmp_path / "control"
    root.mkdir()
    staging.mkdir()
    original = b"pre-crash canonical"
    replacement = b"published before crash"
    target = root / "paper.md"
    target.write_bytes(original)
    staged = _staged(staging / "paper.md", replacement)

    def crash(point: str) -> None:
        if point == "after_publish":
            raise SimulatedProcessCrash

    with pytest.raises(SimulatedProcessCrash):
        _authorized(root, control, fault_injector=crash).commit(
            commit_id="crash-after-publish",
            staged=staged,
            root_name="papers",
            destination=DestinationCAS(
                "paper.md",
                expected_exists=True,
                expected_sha256=_sha256(original),
            ),
        )

    assert target.read_bytes() == replacement
    assert (control / "active_commit.json").is_file()
    assert _authorized(root, control).recover() is True
    assert target.read_bytes() == original
    assert not (control / "active_commit.json").exists()
    assert not (control / "canonical_audit.jsonl").exists()


def test_crash_after_audit_preserves_linearized_commit_during_recovery(tmp_path: Path) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    root = tmp_path / "canonical"
    staging = tmp_path / "staging"
    control = tmp_path / "control"
    root.mkdir()
    staging.mkdir()
    target = root / "paper.md"
    target.write_bytes(b"old")
    staged = _staged(staging / "paper.md", b"new")

    def crash(point: str) -> None:
        if point == "after_audit_write":
            raise SimulatedProcessCrash

    with pytest.raises(SimulatedProcessCrash):
        _authorized(root, control, fault_injector=crash).commit(
            commit_id="crash-after-audit",
            staged=staged,
            root_name="papers",
            destination=DestinationCAS(
                "paper.md",
                expected_exists=True,
                expected_sha256=_sha256(b"old"),
            ),
        )

    assert target.read_bytes() == b"new"
    assert (control / "active_commit.json").exists()
    assert _authorized(root, control).recover() is True
    assert target.read_bytes() == b"new"
    assert not (control / "active_commit.json").exists()
    assert len((control / "canonical_audit.jsonl").read_text().splitlines()) == 1


def test_cleanup_failure_after_audit_reports_committed_write_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    root = tmp_path / "canonical"
    staging = tmp_path / "staging"
    control = tmp_path / "control"
    root.mkdir()
    staging.mkdir()
    target = root / "paper.md"
    target.write_bytes(b"old")
    staged = _staged(staging / "paper.md", b"new")

    def crash(point: str) -> None:
        if point == "after_audit_write":
            raise SimulatedProcessCrash

    with pytest.raises(SimulatedProcessCrash):
        _authorized(root, control, fault_injector=crash).commit(
            commit_id="cleanup-state-committed",
            staged=staged,
            root_name="papers",
            destination=DestinationCAS(
                "paper.md",
                expected_exists=True,
                expected_sha256=_sha256(b"old"),
            ),
        )
    recovery = _authorized(root, control)

    def fail_cleanup(*, preserve_backup: bool) -> None:
        raise CanonicalCommitError(
            "CANONICAL_RECOVERY_FAILED",
            f"injected cleanup failure: {preserve_backup}",
        )

    monkeypatch.setattr(recovery, "_cleanup_active_journal", fail_cleanup)
    with pytest.raises(CanonicalCommitError) as caught:
        recovery.recover()

    assert caught.value.code == "CANONICAL_RECOVERY_FAILED"
    assert caught.value.write_state == "committed"
    assert caught.value.fallback_allowed is False
    assert target.read_bytes() == b"new"


def test_missing_preimage_during_rollback_reports_unknown_write_state(tmp_path: Path) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    root = tmp_path / "canonical"
    staging = tmp_path / "staging"
    control = tmp_path / "control"
    root.mkdir()
    staging.mkdir()
    target = root / "paper.md"
    target.write_bytes(b"old")
    staged = _staged(staging / "paper.md", b"new")

    def crash(point: str) -> None:
        if point == "after_publish":
            raise SimulatedProcessCrash

    with pytest.raises(SimulatedProcessCrash):
        _authorized(root, control, fault_injector=crash).commit(
            commit_id="rollback-state-unknown",
            staged=staged,
            root_name="papers",
            destination=DestinationCAS(
                "paper.md",
                expected_exists=True,
                expected_sha256=_sha256(b"old"),
            ),
        )
    backup = next((control / "backups").rglob("preimage.bin"))
    backup.unlink()

    with pytest.raises(CanonicalCommitError) as caught:
        _authorized(root, control).recover()

    assert caught.value.code == "CANONICAL_ROLLBACK_FAILED"
    assert caught.value.write_state == "unknown"
    assert caught.value.fallback_allowed is False
    assert target.read_bytes() == b"new"


def test_forged_recovery_journal_cannot_escape_backup_namespace(tmp_path: Path) -> None:
    root = tmp_path / "canonical"
    staging = tmp_path / "staging"
    control = tmp_path / "control"
    victim_dir = tmp_path / "victim"
    root.mkdir()
    staging.mkdir()
    control.mkdir()
    victim_dir.mkdir()
    old_payload = b"protected external victim"
    new_payload = b"interrupted canonical value"
    target = root / "paper.md"
    target.write_bytes(new_payload)
    victim = victim_dir / "preimage.bin"
    victim.write_bytes(old_payload)
    commit_id = "forged-journal-escape"
    transaction_key = hashlib.sha256(commit_id.encode("utf-8")).hexdigest()[:24]
    request = {
        "root_name": "papers",
        "relative_path": "paper.md",
        "staged_sha256": _sha256(new_payload),
        "expected_exists": True,
        "expected_sha256": _sha256(old_payload),
    }
    journal = {
        "schema_version": "1",
        "commit_id": commit_id,
        "request_digest": hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest(),
        "root_name": "papers",
        "relative_path": "paper.md",
        "pre_exists": True,
        "pre_sha256": _sha256(old_payload),
        "post_sha256": _sha256(new_payload),
        "backup_path": str(control / "backups" / ".." / ".." / "victim" / "preimage.bin"),
        "temporary_path": str(root / f".canonical-{transaction_key}-0123abcd.tmp"),
    }
    (control / "active_commit.json").write_text(canonical_json(journal) + "\n", encoding="utf-8")

    with pytest.raises(CanonicalCommitError) as caught:
        _authorized(root, control).recover()

    assert _error_code(caught) == "CANONICAL_RECOVERY_FAILED"
    assert victim.read_bytes() == old_payload
    assert target.read_bytes() == new_payload
    assert (control / "active_commit.json").exists()


def test_two_processes_linearize_one_commit_and_one_audit_record(tmp_path: Path) -> None:
    root = tmp_path / "canonical"
    staging = tmp_path / "staging"
    control = tmp_path / "control"
    root.mkdir()
    staging.mkdir()
    source = staging / "paper.md"
    payload = b"one cross-process canonical value"
    source.write_bytes(payload)
    script = textwrap.dedent(
        """
        import json
        import sys
        import time
        from pathlib import Path

        from unified_paper_orchestrator_v2.canonical import (
            CanonicalCommitAuthorization,
            CanonicalCommitter,
            DestinationCAS,
        )
        from unified_paper_orchestrator_v2.contracts import ArtifactRef

        def delay(point):
            if point == "after_journal_write":
                time.sleep(0.25)

        root, control, source = map(Path, sys.argv[1:4])
        committer = CanonicalCommitter(
            canonical_roots={"papers": root},
            staging_roots=(source.parent,),
            control_root=control,
            authorization=CanonicalCommitAuthorization(
                actor="subprocess-test",
                reason="prove one cross-process canonical writer",
            ),
            acl_hardener=None,
            fault_injector=delay,
            lock_timeout_seconds=10,
        )
        result = committer.commit(
            commit_id="cross-process-once",
            staged=ArtifactRef.from_file(source),
            root_name="papers",
            destination=DestinationCAS("paper.md", expected_exists=False),
        )
        print(json.dumps(result.to_dict(), sort_keys=True))
        """
    )
    command = [sys.executable, "-c", script, str(root), str(control), str(source)]
    first = subprocess.Popen(
        command,
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        command,
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_stdout, first_stderr = first.communicate(timeout=30)
    second_stdout, second_stderr = second.communicate(timeout=30)

    assert first.returncode == 0, first_stderr
    assert second.returncode == 0, second_stderr
    results = [json.loads(first_stdout), json.loads(second_stdout)]
    assert {item["status"] for item in results} == {"committed", "already_committed"}
    assert (root / "paper.md").read_bytes() == payload
    audit_lines = (control / "canonical_audit.jsonl").read_text().splitlines()
    assert len(audit_lines) == 1
    assert json.loads(audit_lines[0])["commit_id"] == "cross-process-once"
