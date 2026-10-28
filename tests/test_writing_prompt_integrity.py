from __future__ import annotations

import json
from pathlib import Path

import pytest

from unified_paper_orchestrator.writing_prompt_integrity import (
    TUNED_WRITING_PROMPT_FILES,
    TUNED_WRITING_PROMPT_SHA256,
    WritingPromptIntegrityError,
    writing_prompt_fingerprint,
    writing_prompt_unchanged,
)


def test_code_owned_prompt_manifest_matches_v1_golden_contract() -> None:
    contract_path = Path(__file__).parent / "contracts" / "v1_internal_prompts.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    files = contract["stages"]["writing"]["files"]
    expected = {
        step: str(files[filename]).casefold()
        for step, filename in TUNED_WRITING_PROMPT_FILES.items()
    }

    assert dict(TUNED_WRITING_PROMPT_SHA256) == expected
    assert len(expected) == 10


def _make_prompts(root: Path) -> None:
    prompts = root / "prompts"
    prompts.mkdir(parents=True)
    for step, filename in TUNED_WRITING_PROMPT_FILES.items():
        (prompts / filename).write_bytes(f"exact prompt {step}\r\n".encode())


def test_prompt_fingerprint_preserves_exact_bytes_and_detects_change(tmp_path: Path) -> None:
    _make_prompts(tmp_path)

    fingerprint = writing_prompt_fingerprint(tmp_path, 3)

    assert fingerprint["path"] == "prompts/03_write_draft_tex.md"
    assert fingerprint["size"] == len(b"exact prompt 3\r\n")
    assert writing_prompt_unchanged(tmp_path, fingerprint) is True
    (tmp_path / fingerprint["path"]).write_bytes(b"changed\n")
    assert writing_prompt_unchanged(tmp_path, fingerprint) is False


def test_prompt_fingerprint_rejects_missing_and_empty_files(tmp_path: Path) -> None:
    _make_prompts(tmp_path)
    (tmp_path / "prompts" / TUNED_WRITING_PROMPT_FILES[2]).unlink()
    with pytest.raises(WritingPromptIntegrityError):
        writing_prompt_fingerprint(tmp_path, 2)

    (tmp_path / "prompts" / TUNED_WRITING_PROMPT_FILES[2]).write_bytes(b"")
    with pytest.raises(WritingPromptIntegrityError):
        writing_prompt_fingerprint(tmp_path, 2)


def test_prompt_fingerprint_rejects_symlink(tmp_path: Path) -> None:
    _make_prompts(tmp_path)
    target = tmp_path / "outside.md"
    target.write_text("outside\n", encoding="utf-8")
    prompt = tmp_path / "prompts" / TUNED_WRITING_PROMPT_FILES[1]
    prompt.unlink()
    try:
        prompt.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    with pytest.raises(WritingPromptIntegrityError):
        writing_prompt_fingerprint(tmp_path, 1)


def test_all_ten_real_tuned_prompts_are_fingerprintable() -> None:
    root = Path(r"D:\Documents\项目控制平台\edge-llm-research")

    fingerprints = [writing_prompt_fingerprint(root, step) for step in range(1, 11)]

    assert len({item["sha256"] for item in fingerprints}) == 10
    assert all(item["size"] > 0 for item in fingerprints)


def test_step3_prompt_limits_each_citation_cluster_to_three_sources() -> None:
    root = Path(r"D:\Documents\项目控制平台\edge-llm-research")
    prompt = (root / "prompts" / TUNED_WRITING_PROMPT_FILES[3]).read_text(encoding="utf-8")

    assert "每一个引用位置最多引用 3 篇参考文献" in prompt
    assert r"单个 \cite{} 最多包含 3 个引用 key" in prompt
    assert "禁止把大量文献压缩为连续编号区间" in prompt
    assert "[13--25]" in prompt
    assert "[31,33,35,46,56,67]" in prompt
    assert "按它们实际支撑的具体 claim 分散到相邻句子或段落中" in prompt


def test_step6_prompt_preserves_operator_confirmed_data_rows() -> None:
    root = Path(r"D:\Documents\项目控制平台\edge-llm-research")
    prompt = (root / "prompts" / TUNED_WRITING_PROMPT_FILES[6]).read_text(encoding="utf-8")

    assert "用户确认数据的保留与转换规则" in prompt
    assert "% CONFIRMED_DATA_START: <label>" in prompt
    assert "% DATA_STATUS: USER_CONFIRMED_REAL" in prompt
    assert "每一行 `DATA_ROWS` 必须逐字保留" in prompt
    assert "数值与 Step 5 原先写入的模拟值完全相同" in prompt


def test_steps7_to_10_prompts_keep_operator_confirmed_data_locked() -> None:
    root = Path(r"D:\Documents\项目控制平台\edge-llm-research")

    for step in range(7, 11):
        prompt = (root / "prompts" / TUNED_WRITING_PROMPT_FILES[step]).read_text(encoding="utf-8")
        assert "DATA_MANUALLY_APPLIED" in prompt
        assert "DATA_STATUS: USER_CONFIRMED_REAL" in prompt
        assert "最高优先级" in prompt
        assert "不得" in prompt
