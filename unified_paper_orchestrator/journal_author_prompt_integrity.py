from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


JOURNAL_AUTHOR_PROMPT_FILES: Mapping[str, str] = {
    "scheduler": "controller/JOURNAL_SCHEDULER_PROMPT.md",
    "import": "prompts/01_import_submit.md",
    "choose": "prompts/02_choose_journal.md",
    "package": "prompts/03_make_submission_package.md",
    "combined": "prompts/02_03_choose_journal_and_make_package.md",
}

# Production seals for the already tuned V1 prompt files.  V2 reads the live
# files in place and refuses to dispatch if any byte changed unexpectedly.
JOURNAL_AUTHOR_PROMPT_SHA256: Mapping[str, str] = {
    "scheduler": "A57FDC1979E394957394ABC8668E03A2EA3D74EF6A1B90B1AFF3A9A38368C026",
    "import": "94B3AB98F87BC0B264D9861A01681D4B126BB3A2E64B1EE8D5FC816A5D0419CF",
    "choose": "5697EC7CED0F4D6C53C1AD5060098BD200EEEDD07E4D0057C3C847C593FB4473",
    "package": "96D8E66634F3CD27B3F1D2E194056BDA1B50198DD083A80B4EB042DF6CD932F5",
    "combined": "AE1C82479E755C413B5BFD1F13FA54588250E29BE71093C066293B0F42B21607",
}


class JournalAuthorPromptIntegrityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class JournalAuthorPromptFingerprint:
    name: str
    path: Path
    sha256: str
    size: int


def fingerprint_journal_author_prompt(root: str | Path, name: str) -> JournalAuthorPromptFingerprint:
    key = str(name or "").strip()
    relative = JOURNAL_AUTHOR_PROMPT_FILES.get(key)
    if relative is None:
        raise JournalAuthorPromptIntegrityError(
            "JOURNAL_AUTHOR_PROMPT_NAME_INVALID",
            f"unsupported journal-author prompt: {key!r}",
        )
    project_root = Path(root).expanduser().resolve()
    path = (project_root / relative).resolve()
    try:
        path.relative_to(project_root)
        before = path.stat()
        payload = path.read_bytes()
        after = path.stat()
    except (OSError, ValueError) as exc:
        raise JournalAuthorPromptIntegrityError(
            "JOURNAL_AUTHOR_PROMPT_MISSING",
            f"cannot read tuned journal-author prompt: {relative}",
        ) from exc
    if not path.is_file() or not payload or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise JournalAuthorPromptIntegrityError(
            "JOURNAL_AUTHOR_PROMPT_UNSTABLE",
            f"journal-author prompt is not a stable non-empty file: {relative}",
        )
    return JournalAuthorPromptFingerprint(
        name=key,
        path=path,
        sha256=hashlib.sha256(payload).hexdigest().upper(),
        size=len(payload),
    )


def validate_journal_author_prompt(root: str | Path, name: str) -> JournalAuthorPromptFingerprint:
    fingerprint = fingerprint_journal_author_prompt(root, name)
    expected = JOURNAL_AUTHOR_PROMPT_SHA256[fingerprint.name]
    if fingerprint.sha256 != expected:
        raise JournalAuthorPromptIntegrityError(
            "JOURNAL_AUTHOR_PROMPT_HASH_MISMATCH",
            (
                f"journal-author {fingerprint.name} prompt bytes changed; "
                f"expected {expected}, observed {fingerprint.sha256}"
            ),
        )
    return fingerprint


def validate_all_journal_author_prompts(root: str | Path) -> dict[str, JournalAuthorPromptFingerprint]:
    return {name: validate_journal_author_prompt(root, name) for name in JOURNAL_AUTHOR_PROMPT_FILES}


def build_journal_author_combined_prompt(root: str | Path, paper_id: str) -> str:
    """Return the exact V1 worker command with only ``<paper_id>`` substituted."""

    project_root = Path(root).expanduser().resolve()
    combined = validate_journal_author_prompt(project_root, "combined")
    validate_journal_author_prompt(project_root, "scheduler")
    paper_dir = project_root / "papers" / paper_id
    legacy_root = Path(r"D:\Documents\期刊选刊与作者")
    return (
        "UPO_WORKSPACE_BINDING_V1\n"
        f"本回合唯一允许使用的选刊作者项目根目录是：`{project_root}`。\n"
        f"受保护提示词内出现的旧路径 `{legacy_root}` 仅是历史别名，必须解释为 `{project_root}`；"
        "不得读取或写入旧路径。提示词其余业务内容保持不变。\n\n"
        f"进入 `{project_root}` 项目。执行合并 02-03 总控 prompt；不要在线投稿。\n\n"
        "目标论文目录：\n"
        f"{paper_dir}\n\n"
        "请按：\n"
        f"{combined.path}\n\n"
        "并读取：\n"
        f"{paper_dir / 'journal_preferences.md'}\n"
        f"{paper_dir / 'author_info.md'}\n"
        f"{paper_dir / 'submission_metadata_fingerprint.json'}\n\n"
        "先实时核查并选择最稳妥投稿期刊，生成 journal.md。只有当 journal.md 的 Final decision 是 Recommended，"
        "且 author_info.md 为 Status: READY 且无 TODO 时，才继续生成 package/。\n\n"
        "如果条件不满足，停止并写 package_blocker.md。不要编造期刊信息、作者、单位、基金或声明。\n"
        "如果生成或更新 package/，把 submission_metadata_fingerprint.json 原样复制到 "
        "package/package_metadata_fingerprint.json。\n\n"
        "完成后最终回复必须包含：\n"
        f"JOURNAL_PIPELINE_ID={paper_id}\n"
        "JOURNAL_STEP_DONE=<2 或 3>\n"
        "PACKAGE_STATUS=<READY_TO_SUBMIT | NEEDS_MINOR_FIX | BLOCKED>"
    )


def build_journal_author_step_prompt(root: str | Path, paper_id: str, step: int) -> str:
    """Wrap a protected Step 2 or Step 3 prompt without changing its body."""

    project_root = Path(root).expanduser().resolve()
    if int(step) == 2:
        prompt = validate_journal_author_prompt(project_root, "choose")
        action = "核验候选期刊并生成 journal.md；本回合只执行原 Step 2，不生成投稿包"
    elif int(step) == 3:
        prompt = validate_journal_author_prompt(project_root, "package")
        action = "读取已经通过程序门禁的 journal.md，生成并验收 package；本回合只执行原 Step 3"
    else:
        raise JournalAuthorPromptIntegrityError(
            "JOURNAL_AUTHOR_STEP_INVALID",
            f"unsupported journal-author workflow step: {step}",
        )
    paper_dir = project_root / "papers" / paper_id
    legacy_root = Path(r"D:\Documents\期刊选刊与作者")
    return (
        "UPO_WORKSPACE_BINDING_V1\n"
        f"本回合唯一允许使用的选刊作者项目根目录是：`{project_root}`。\n"
        f"受保护提示词内出现的旧路径 `{legacy_root}` 仅是历史别名，必须解释为 `{project_root}`；"
        "不得读取或写入旧路径。提示词其余业务内容保持不变。\n\n"
        f"进入 `{project_root}` 项目。{action}。不要在线投稿。\n\n"
        f"目标论文目录：`{paper_dir}`\n"
        f"必须逐条执行已经调好的受保护提示词：`{prompt.path}`\n\n"
        "只处理上述 paper_id，不得新建或切换到其他论文；不得编造期刊、作者、单位、基金或声明。\n"
        f"JOURNAL_PIPELINE_ID={paper_id}\n"
        f"JOURNAL_STEP_TARGET={int(step)}"
    )


__all__ = [
    "JOURNAL_AUTHOR_PROMPT_FILES",
    "JOURNAL_AUTHOR_PROMPT_SHA256",
    "JournalAuthorPromptFingerprint",
    "JournalAuthorPromptIntegrityError",
    "build_journal_author_combined_prompt",
    "build_journal_author_step_prompt",
    "fingerprint_journal_author_prompt",
    "validate_all_journal_author_prompts",
    "validate_journal_author_prompt",
]
