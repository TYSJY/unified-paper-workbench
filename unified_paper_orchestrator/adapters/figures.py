from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any, Protocol

from ..config import (
    FIGURE_DEFAULT_PARALLEL_WORKERS,
    FIGURE_MAX_PARALLEL_WORKERS,
    FIGURE_PIPELINE_LOCK_PATH,
    FIGURE_SDK_JOURNAL_PATH,
    LEGACY_PROJECTS,
)
from ..figure_output_contract import sha256_file, validate_figure_step
from ..figure_workflow import (
    FigureWorkflowStore,
    figure_workflow_position_for_source,
    figure_workflow_step_count,
)
from ..io_utils import FileLock, append_unique, copy_file, now_iso, read_json, rel_to
from ..models import stage_status
from .base import LegacyAdapter


class FigureSDKRuntime(Protocol):
    def ensure_ready(self) -> dict[str, Any]: ...

    def start_background(
        self,
        *,
        priority_paper_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]: ...

    def request_pause(self) -> dict[str, Any]: ...

    def request_pause_papers(self, paper_ids: list[str] | tuple[str, ...]) -> dict[str, Any]: ...

    def revalidate_completed_output(self, paper_id: str) -> dict[str, Any]: ...


def _default_sdk_runtime() -> FigureSDKRuntime:
    module = import_module("unified_paper_orchestrator.figure_sdk_runtime")
    return module.get_figure_sdk_runtime()  # type: ignore[no-any-return]


class FigureAdapter(LegacyAdapter):
    stage_name = "figures"
    id_key = "paper_id"
    default_start_step = 2
    default_run_until_step = 3

    def __init__(
        self,
        runtime: FigureSDKRuntime | None = None,
        workflow_store: FigureWorkflowStore | None = None,
    ) -> None:
        super().__init__(LEGACY_PROJECTS["figures"])
        self._runtime = runtime
        self.workflow_store = workflow_store or FigureWorkflowStore()

    @property
    def runtime(self) -> FigureSDKRuntime:
        if self._runtime is None:
            self._runtime = _default_sdk_runtime()
        return self._runtime

    def _require_runtime_ready(self) -> dict[str, Any]:
        result = self.runtime.ensure_ready()
        if not isinstance(result, dict) or result.get("ok") is False or result.get("authenticated") is False:
            raise RuntimeError("figure SDK account is not ready")
        return dict(result)

    def _pause_legacy_automation(self) -> dict[str, Any]:
        result = self.automation.set_enabled(self.stage_name, False, self.load_state())
        return dict(result) if isinstance(result, dict) else {"result": result}

    def _start_sdk_background(self, paper_ids: list[str] | None = None) -> dict[str, Any]:
        result = self.runtime.start_background(priority_paper_ids=paper_ids)
        normalized = dict(result) if isinstance(result, dict) else {"started": bool(result)}
        if normalized.get("ok") is False or not (
            normalized.get("started") is True or normalized.get("already_running") is True
        ):
            raise RuntimeError("figure SDK runtime did not acknowledge the background wakeup")
        return normalized

    def all_paper_ids(self) -> set[str]:
        state = self.load_state()
        ids = set(str(item) for item in state.get("papers", {}).keys())
        if self.papers_dir.exists():
            ids.update(child.name for child in self.papers_dir.iterdir() if child.is_dir())
        return ids

    def paper_dir(self, paper_id: str) -> Path:
        return self.papers_dir / paper_id

    def submit_dir(self, paper_id: str) -> Path:
        return self.paper_dir(paper_id) / "submit"

    def input_tex(self, paper_id: str) -> Path:
        return self.paper_dir(paper_id) / "input.tex"

    def compiled_pdf(self, paper_id: str) -> Path:
        return self.submit_dir(paper_id) / f"{paper_id}.pdf"

    def _source_rel(self, state: dict[str, Any], paper_id: str) -> str:
        record = state.get("papers", {}).get(paper_id, {})
        if isinstance(record, dict):
            source = str(record.get("source_tex") or "").strip()
            if source:
                return source
        return rel_to(self.input_tex(paper_id), self.root)

    def stage_for(self, paper_id: str) -> dict[str, Any]:
        state = self.load_state()
        record = state.get("papers", {}).get(paper_id, {})
        if not isinstance(record, dict):
            record = {}
        journal = read_json(FIGURE_SDK_JOURNAL_PATH, {"papers": {}})
        journal_papers = journal.get("papers", {}) if isinstance(journal, dict) else {}
        sdk_record = journal_papers.get(paper_id, {}) if isinstance(journal_papers, dict) else {}
        if not isinstance(sdk_record, dict):
            sdk_record = {}
        queue = [str(item) for item in state.get("paper_queue", [])]
        queue_position = f"{queue.index(paper_id) + 1}/{len(queue)}" if paper_id in queue else ""
        submit_dir = self.submit_dir(paper_id)
        raw_status = str(record.get("status") or "")
        sdk_status = str(sdk_record.get("status") or "")
        contract = validate_figure_step(self.root, paper_id, 3, workflow=record.get("workflow_snapshot") or sdk_record.get("workflow_snapshot"))
        if raw_status == "deleted":
            status = "deleted"
        elif sdk_status in {
            "creating_thread",
            "starting",
            "running",
            "queued",
            "paused",
            "waiting_event",
            "waiting_data",
            "blocked",
        }:
            status = sdk_status
        elif sdk_status == "complete" and contract.ok:
            status = "complete"
        elif contract.status == "waiting_data":
            status = "waiting_data"
        elif contract.ok and not str(record.get("sdk_request_id") or ""):
            status = "complete"
        elif paper_id in queue and not str(record.get("sdk_request_id") or ""):
            status = "migration_review"
        elif paper_id in queue:
            status = "queued"
        elif raw_status == "complete":
            status = "complete" if contract.ok else "incomplete"
        elif raw_status in {"queued", "running", "paused", "blocked", "migration_review"}:
            status = raw_status
        elif self.input_tex(paper_id).exists() or self.paper_dir(paper_id).exists():
            status = "incomplete"
        else:
            status = "missing"
        artifact = submit_dir if submit_dir.exists() else self.input_tex(paper_id)
        detail = str(sdk_record.get("blocker_message") or record.get("evidence") or "")
        if not detail and contract.issues and status not in {"running", "starting", "queued"}:
            detail = "；".join(contract.issues[:3])
        step_done = int(sdk_record.get("completed_step") or record.get("step_done") or 0)
        current_step = int(sdk_record.get("active_step") or record.get("sdk_current_step") or 0)
        projection = stage_status(
            status,
            label=self.label,
            detail=detail,
            artifact=str(artifact if artifact.exists() else ""),
            updated_at=str(sdk_record.get("updated_at") or record.get("updated_at") or ""),
            raw_status=sdk_status or raw_status or status,
            queue_position=queue_position,
            scheduler_mode=self.scheduler_mode(state),
            step_done=step_done,
        )
        projection.update(
            {
                "current_step": current_step,
                "thread_id": str(sdk_record.get("thread_id") or record.get("thread_id") or ""),
                "output_contract": contract.as_dict(),
            }
        )
        from ..undergraduate_word import is_word_workflow, paper_workflow, validate_word_delivery, safe_path
        context = record.get("workflow_snapshot") or sdk_record.get("workflow_snapshot") or paper_workflow(self.root, paper_id)
        if is_word_workflow(context):
            issues, _ = validate_word_delivery(self.root, paper_id, context)
            receipt = read_json(self.paper_dir(paper_id) / "delivery" / "word_delivery.json", {})
            projection.update(word_required=True, word_delivery_ok=not issues, label="图表与 Word 交付")
            for field in ("docx", "pdf"):
                try:
                    projection["word_" + field] = str(safe_path(self.root, receipt[field]["path"]))
                except (KeyError, ValueError, OSError, TypeError):
                    projection["word_" + field] = ""
            if issues and status == "complete":
                projection.update(status="incomplete", detail="；".join(issues))
        return projection

    def queue(self, paper_id: str, upstream_artifact: str = "") -> dict[str, Any]:
        return self._queue_pipeline(paper_id, upstream_artifact, start_step=self.default_start_step, force=False)

    def queue_from_step1(self, paper_id: str, upstream_artifact: str = "") -> dict[str, Any]:
        return self._queue_pipeline(paper_id, upstream_artifact, start_step=1, force=True)

    def queue_from_step3(self, paper_id: str) -> dict[str, Any]:
        """Resume after the operator has supplied previously missing data."""

        return self._queue_pipeline(paper_id, "", start_step=3, force=True)

    def queue_migration(self, paper_id: str) -> dict[str, Any]:
        """Move a pre-V2 historical task into a new journaled V2 request."""

        step2 = validate_figure_step(self.root, paper_id, 2)
        start_step = 3 if step2.ok else 2
        result = self._queue_pipeline(
            paper_id,
            "",
            start_step=start_step,
            force=True,
            migration=True,
        )
        result.update(
            {
                "migration": True,
                "migration_start_source_step": start_step,
                "preserved_step2": bool(step2.ok),
                "step2_contract": step2.as_dict(),
            }
        )
        return result

    def revalidate_output(self, paper_id: str) -> dict[str, Any]:
        """Re-run the deterministic Step 3 contract without starting Codex."""

        return self.runtime.revalidate_completed_output(paper_id)

    def _queue_pipeline(
        self,
        paper_id: str,
        upstream_artifact: str,
        *,
        start_step: int,
        force: bool,
        migration: bool = False,
    ) -> dict[str, Any]:
        workflow = self.workflow_store.active_snapshot()
        from ..writing_prompt_schemes import is_undergraduate, scheme_id
        from ..writing_workflow import normalize_workflow_snapshot
        from ..figure_workflow import normalize_figure_workflow_snapshot
        from ..undergraduate_figures import make_figure_snapshot
        # Paper state, never the globally selected writing scheme, owns this handoff.
        prior = self.load_state().get("papers", {}).get(paper_id, {}).get("workflow_snapshot")
        writing_state_path = LEGACY_PROJECTS.get("writing", {}).get("state_path")
        writing_state = read_json(writing_state_path, {}) if writing_state_path else {}
        writing = writing_state.get("sessions", {}).get(paper_id, {}).get("workflow_snapshot")
        if prior and is_undergraduate(prior):
            workflow = normalize_figure_workflow_snapshot(prior)
        elif writing and is_undergraduate(writing):
            writing = normalize_workflow_snapshot(writing)
            workflow = (normalize_figure_workflow_snapshot(writing["figure_workflow_snapshot"])
                        if writing.get("figure_workflow_snapshot") else make_figure_snapshot(
                            scheme_id(writing), writing["thesis_brief"], workflow["default_execution_profile"]))
            if start_step == 2 and not migration:
                start_step = 1  # Undergraduate papers first check school-specific figure needs.
        if not is_undergraduate(workflow):
            workflow = self.workflow_store.executable_snapshot()
        account = self._require_runtime_ready()
        legacy_pause = self._pause_legacy_automation()
        try:
            workflow_start = figure_workflow_position_for_source(workflow, start_step)
        except Exception:
            if start_step == 1:
                workflow_start = 1
            else:
                raise
        workflow_end = figure_workflow_step_count(workflow)
        source_path = Path(upstream_artifact).expanduser() if upstream_artifact else Path()
        target_input = self.input_tex(paper_id)
        copied_input = False
        source_changed = False
        with FileLock(FIGURE_PIPELINE_LOCK_PATH):
            state = self.load_state()
            settings = self.scheduler_settings(state)
            try:
                configured_parallel = int(
                    settings.get(
                        "max_parallel_workers", FIGURE_DEFAULT_PARALLEL_WORKERS
                    )
                )
            except (TypeError, ValueError):
                configured_parallel = FIGURE_DEFAULT_PARALLEL_WORKERS
            configured_parallel = min(
                max(configured_parallel, 1), FIGURE_MAX_PARALLEL_WORKERS
            )
            settings.update(
                {
                    "scheduling_mode": "auto",
                    "one_action_per_run": False,
                    "max_parallel_workers": configured_parallel,
                    "dispatch_mode": "sdk_event_driven",
                    "sdk_pause_requested": False,
                }
            )
            paused_ids = [
                str(value)
                for value in settings.get("sdk_paused_paper_ids", [])
                if str(value) and str(value) != paper_id
            ]
            settings["sdk_paused_paper_ids"] = list(dict.fromkeys(paused_ids))
            self.set_targeted_run_scope(state, paper_id)
            if source_path.exists() and source_path.is_file():
                source_hash = sha256_file(source_path)
                target_hash = sha256_file(target_input) if target_input.is_file() else ""
                source_changed = source_hash != target_hash
                if source_changed:
                    copy_file(source_path, target_input)
                    copied_input = True
            if not target_input.is_file():
                raise FileNotFoundError(f"Missing figure input TeX: {target_input}")

            papers = state.setdefault("papers", {})
            record = papers.setdefault(paper_id, {})
            if migration:
                legacy_thread_id = str(record.get("thread_id") or "").strip()
                legacy_thread_title = str(record.get("thread_title") or "").strip()
                if legacy_thread_id:
                    record["legacy_thread_id"] = legacy_thread_id
                if legacy_thread_title:
                    record["legacy_thread_title"] = legacy_thread_title
                record["legacy_migration_from_status"] = str(record.get("status") or "")
                record["legacy_migration_at"] = now_iso()
                for field in (
                    "thread_id",
                    "thread_title",
                    "sdk_runtime_event",
                    "last_turn_id",
                ):
                    record.pop(field, None)
            action = "run_pipeline" if start_step <= 1 else "run_pipeline_late"
            source_rel = rel_to(target_input, self.root)
            source_sha256 = sha256_file(target_input)
            params = {
                "start_step": workflow_start,
                "run_until_step": workflow_end,
                "requested_source_step": start_step,
                "source_tex": source_rel,
                "source_sha256": source_sha256,
                "workflow_snapshot": workflow,
                "workflow_version_id": str(workflow.get("version_id") or ""),
                "reuse_valid_artifacts": not force,
                "legacy_migration": migration,
                "reference_policy": "fresh_step1_then_plan" if start_step <= 1 else "reuse_shared_refs",
                "run_scope": self.targeted_run_scope,
                "target_paper_ids": [paper_id],
            }
            if force:
                params["force_start_step"] = True
            command = self.append_command(state, action, paper_id, params)
            record.update(
                {
                    "paper_id": paper_id,
                    "status": "queued",
                    "source_tex": source_rel,
                    "run_from_step": workflow_start,
                    "run_until_step": workflow_end,
                    "requested_source_step": start_step,
                    "workflow_snapshot": workflow,
                    "workflow_version_id": str(workflow.get("version_id") or ""),
                    "reuse_valid_artifacts": not force,
                    "legacy_migration": migration,
                    "sdk_request_id": str(command.get("id") or ""),
                    "step_done": workflow_start - 1,
                    "updated_at": now_iso(),
                    "evidence": f"V2 SDK 已排队，将执行流程 Step {workflow_start}-{workflow_end}",
                }
            )
            for field in ("blocker", "sdk_current_step"):
                record.pop(field, None)
            if force:
                record["force_next_step"] = workflow_start
                record["force_pipeline_until_step"] = workflow_end
                record["force_reason"] = f"queued from unified UI to rerun figure pipeline from source Step {start_step}"
                record.pop("force_step_in_flight", None)
            if copied_input:
                record["import_source"] = str(source_path)
                record["import_source_sha256"] = source_sha256
            queue = [str(item) for item in state.get("paper_queue", [])]
            append_unique(queue, paper_id)
            state["paper_queue"] = queue
            state["active_paper_id"] = paper_id
            self.save_state(state)
            from ..undergraduate_word import write_context
            write_context(self.root, paper_id, workflow)
        runtime = self._start_sdk_background([paper_id])
        return {
            "stage": self.stage_name,
            "action": action,
            "command": command,
            "input_tex": str(target_input),
            "run_scope": self.targeted_run_scope,
            "target_paper_ids": [paper_id],
            "scheduler_started": True,
            "account": account,
            "automation": legacy_pause,
            "runtime": runtime,
            "source_changed": source_changed,
            "message": (
                f"已自动启动 V2 SDK 固定会话，将执行当前画图流程 Step {workflow_start}-{workflow_end}"
                if start_step <= 1
                else (
                    f"已自动启动 V2 SDK 固定会话，将从生成阶段继续到流程 Step {workflow_end}"
                    if start_step == 3
                    else f"已自动启动 V2 SDK 固定会话，将复用参考池并执行流程 Step {workflow_start}-{workflow_end}"
                )
            ),
        }

    def start(self) -> dict[str, Any]:
        account = self._require_runtime_ready()
        legacy_pause = self._pause_legacy_automation()
        runtime = self._start_sdk_background()
        return {
            "ok": True,
            "stage": self.stage_name,
            "enabled": True,
            "account": account,
            "runtime": runtime,
            "automation": legacy_pause,
        }

    def resume_papers(self, paper_ids: list[str] | tuple[str, ...]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        if not ids:
            raise ValueError("至少选择一篇要继续的画图论文")
        account = self._require_runtime_ready()
        legacy_pause = self._pause_legacy_automation()
        runtime = self._start_sdk_background(ids)
        return {
            "ok": True,
            "stage": self.stage_name,
            "scope": "selected_papers",
            "paper_ids": ids,
            "account": account,
            "automation": legacy_pause,
            "runtime": runtime,
        }

    def pause(self) -> dict[str, Any]:
        legacy_pause = self._pause_legacy_automation()
        runtime = self.runtime.request_pause()
        return {
            "ok": True,
            "stage": self.stage_name,
            "enabled": False,
            "paused": True,
            "status": "pause_requested",
            "runtime": runtime,
            "automation": legacy_pause,
        }

    def pause_papers(self, paper_ids: list[str] | tuple[str, ...]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip()))
        if not ids:
            raise ValueError("至少选择一篇要暂停的画图论文")
        runtime = self.runtime.request_pause_papers(ids)
        return {
            "ok": True,
            "stage": self.stage_name,
            "paused": True,
            "scope": "selected_papers",
            "paper_ids": ids,
            "runtime": runtime,
        }

    def queue_image2_revision(self, paper_id: str, figure: str, request: str = "") -> dict[str, Any]:
        from ..writing_prompt_schemes import is_undergraduate
        workflow = self.load_state().get("papers", {}).get(paper_id, {}).get("workflow_snapshot")
        if workflow and is_undergraduate(workflow):
            raise ValueError("本科结构图保留可编辑源文件，不使用期刊 Image2 强制重绘。请修改图源/逐图说明后从画图 Step 3 重新整合。")
        account = self._require_runtime_ready()
        legacy_pause = self._pause_legacy_automation()
        state = self.load_state()
        target_input = self.input_tex(paper_id)
        if not target_input.exists():
            raise FileNotFoundError(f"Missing figure input TeX: {target_input}")

        source_rel = self._source_rel(state, paper_id)
        request = request.strip()
        revision_request = (
            request
            if request
            else "Fix the renderer policy violation for this figure while preserving the paper meaning, labels, caption intent, and all numeric values."
        )
        request = (
            f"Revision target: {figure}\n"
            f"Revision request: {revision_request}\n"
            "Use the target figure's existing Step 2/Step 3 design as the source of truth: read figure_plan.md, figure_prompts.md, and the matching drafts/figure*_brief.md; locate the target figure section in all available artifacts. "
            "If figure_prompts.md is missing because this is an older imported paper, reconstruct it from figure_plan.md, input.tex, the matching brief, and reference-board notes before generating anything. "
            "Reuse its visual thesis, panel layout, required elements, caption intent, full imagegen/image2 prompt, negative prompt, and sampled reference screenshot list. "
            "If the brief only contains a short image2 prompt summary, reconstruct a full figure-specific imagegen/image2 prompt from the target figure section before generating anything, and save the full prompt back into both figure_prompts.md and the brief. "
            "If this is a conceptual_schematic or a hybrid figure with a conceptual panel, regenerate the conceptual visual content as a raster PNG with Codex imagegen/image2. "
            "The imagegen/image2 PNG must be the complete visible figure, including short labels and callouts; do not generate only a background and then overlay text, arrows, module labels, or numeric callouts with matplotlib, TikZ, PIL, Figma/Illustrator, or other code. "
            "Post-processing is limited to crop, resize, compression, color-profile conversion, and PNG-to-PDF wrapping. "
            "Preserve the paper meaning, labels, caption intent, and all numeric values. "
            "Do not invent data. Do not replace image2-required conceptual content with matplotlib, TikZ, PGF, Figma/Illustrator-style, or hand-designed vector diagrams. "
            "Do not use generic stock-style technology art, photorealistic laptops, glowing dashboards, decorative 3D hardware, random chip icons, or unrelated cloud/server illustrations. "
            "Do not use a generic image2 prompt and then patch labels over it. "
            "Do not accept vector-only or PDF-only conceptual output; the raw output must be a paper-specific raster PNG. "
            "Save the raw imagegen/image2 PNG output under drafts/, convert or wrap it to the final PDF in submit/, update drafts/figure*_brief.md, update figure_prompts.md, update submit/submission_manifest.json with renderer=image2 and a non-empty PNG raw_output, update the submit TeX if filenames or captions change, and recompile the submit PDF."
        )
        prompt = (
            f"Enter `paper-figure-codex`, then run prompts/04_single_figure_revision.md for {source_rel}"
            f"\n\nRevision request:\n{request}"
        )
        params = {
            "step": 4,
            "prompt": prompt,
            "source_tex": source_rel,
            "figure": figure,
            "request": request,
            "run_scope": self.targeted_run_scope,
            "target_paper_ids": [paper_id],
        }
        with FileLock(FIGURE_PIPELINE_LOCK_PATH):
            state = self.load_state()
            self.set_targeted_run_scope(state, paper_id)
            command = self.append_command(state, "revise_figure", paper_id, params)
            papers = state.setdefault("papers", {})
            record = papers.setdefault(paper_id, {})
            # A revision is a Step 4 request, not permission to rerun Step 2-3.
            record.pop("sdk_request_id", None)
            record.update(
                {
                    "paper_id": paper_id,
                    "status": "queued",
                    "source_tex": source_rel,
                    "updated_at": now_iso(),
                    "evidence": f"queued image2 revision for {figure}",
                }
            )
            state["active_paper_id"] = paper_id
            self.save_state(state)
        runtime = self._start_sdk_background([paper_id])
        return {
            "stage": self.stage_name,
            "action": "revise_figure",
            "figure": figure,
            "command": command,
            "run_scope": self.targeted_run_scope,
            "target_paper_ids": [paper_id],
            "account": account,
            "automation": legacy_pause,
            "runtime": runtime,
        }
