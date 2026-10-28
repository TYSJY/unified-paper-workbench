"""Pure-console entry point for the isolated generic workflow candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping

# This switch must be set before workflow modules can import workspace helpers.
os.environ["UNIFIED_PAPER_WORKSPACE_DISABLE_REGISTRY"] = "1"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from unified_paper_orchestrator.workflow.candidate import (  # noqa: E402
    CANDIDATE_VERSION,
    CANDIDATE_WORKSPACE_MARKER,
    SUPPORTED_LOCALES,
    CandidateApplication,
    localized_message,
    normalize_locale,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Isolated, offline-first generic workflow candidate. Every stateful command requires an explicit workspace."
        )
    )
    parser.add_argument("--workspace", help="Explicit candidate workspace root.")
    parser.add_argument(
        "--locale",
        default="zh-CN",
        choices=SUPPORTED_LOCALES,
        help="Machine-output message locale (default: zh-CN).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"GenericWorkflowCandidate {CANDIDATE_VERSION}",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="Initialize and install all built-in workflow packs.")
    commands.add_parser("list", help="List installed workflows and immutable versions.")

    doctor = commands.add_parser("doctor", help="Verify local seals and workspace isolation.")
    _add_sci_roots(doctor)

    create = commands.add_parser("demo-create", help="Create one deterministic offline demo run.")
    create.add_argument("--request-file", help="UTF-8 JSON object containing the demo request.")
    create.add_argument("--goal", default="Exercise the generic workflow candidate.")
    create.add_argument("--action", action="append", dest="actions", default=[])
    create.add_argument("--constraint", action="append", dest="constraints", default=[])
    create.add_argument("--idempotency-key", default="")

    tick = commands.add_parser("tick", help="Run one bounded scheduler cycle.")
    tick.add_argument("run_id")

    resume = commands.add_parser(
        "resume",
        help="Reopen the durable ledger and advance one demo run until stable.",
    )
    resume.add_argument("run_id")

    timeline = commands.add_parser("timeline", help="Read a sanitized durable run timeline.")
    timeline.add_argument("run_id")

    acceptance = commands.add_parser(
        "accept-offline",
        help="Run offline acceptance and write a non-overwriting evidence report.",
    )
    acceptance.add_argument(
        "--report",
        default="reports/generic-workflow-offline-acceptance.json",
        help="Report path inside the explicit workspace.",
    )
    _add_sci_roots(acceptance)
    return parser


def _add_sci_roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sci-writing-root")
    parser.add_argument("--sci-figures-root")
    parser.add_argument("--sci-journal-author-root")


def _sci_roots(args: argparse.Namespace) -> Mapping[str, Path] | None:
    values = {
        "writing": getattr(args, "sci_writing_root", None),
        "figures": getattr(args, "sci_figures_root", None),
        "journal_author": getattr(args, "sci_journal_author_root", None),
    }
    supplied = [bool(value) for value in values.values()]
    if any(supplied) and not all(supplied):
        raise ValueError("SCI contract simulation requires writing, figures, and journal_author roots together")
    if not any(supplied):
        return None
    return {key: Path(str(value)).expanduser().absolute() for key, value in values.items()}


def _request(args: argparse.Namespace) -> Mapping[str, Any]:
    if args.request_file:
        request = json.loads(Path(args.request_file).read_text(encoding="utf-8-sig"))
        if not isinstance(request, dict):
            raise ValueError("demo request JSON must be an object")
        return request
    return {
        "goal": args.goal,
        "actions": args.actions or ["prepare", "execute", "verify"],
        "constraints": args.constraints or ["offline", "deterministic"],
    }


def _decorate(result: Mapping[str, Any], locale: str, message_key: str) -> dict[str, Any]:
    return {
        **dict(result),
        "locale": locale,
        "message": localized_message(locale, message_key),
    }


def _emit(payload: Mapping[str, Any]) -> None:
    print(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True))


def _interactive_default_workspace() -> Path:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return (base / "GenericWorkflowCandidate" / "workspace").absolute()


def _stdin_is_interactive() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)())


def _interactive_workspace_status(workspace: Path) -> str:
    if not workspace.exists():
        return "未初始化（尚未创建目录）"
    if not workspace.is_dir():
        return "不可用（路径不是目录）"
    if (workspace / CANDIDATE_WORKSPACE_MARKER).is_file():
        return "已有候选 marker（操作时会继续验证 SQLite owner）"
    try:
        empty = next(workspace.iterdir(), None) is None
    except OSError:
        return "不可读取"
    return "可初始化（空目录）" if empty else "非空且未归属（初始化会安全拒绝）"


def _interactive_menu() -> int:
    print("=" * 66)
    print(f"通用工作流候选工具  {CANDIDATE_VERSION}")
    print("本窗口使用独立候选工作区，不会切换或写入现有生产调度入口。")
    print("=" * 66)
    workspace = _interactive_default_workspace()
    while True:
        print(f"\n当前工作区：{workspace}")
        print(f"状态：{_interactive_workspace_status(workspace)}")
        print("\n请选择操作：")
        print("  1. 快速开始（初始化、自检并完成离线演示）")
        print("  2. 初始化或检查工作区")
        print("  3. 查看已安装工作流")
        print("  4. 运行工作区自检（可能补齐候选资源）")
        print("  5. 运行离线核心验收（不调用真实模型或浏览器）")
        print("  6. 本次切换到其他候选工作区")
        print("  7. 查看命令行帮助")
        print("  0. 退出")
        try:
            choice = input("输入编号：").strip()
        except EOFError:
            print("\n交互输入已关闭，候选工具以失败关闭。")
            return 2
        if choice == "0":
            print("候选工具已安全退出。")
            return 0
        try:
            if choice == "1":
                app = CandidateApplication(workspace)
                initialized = app.initialize_defaults()
                result = app.doctor()
                if result["status"] != "PASS":
                    raise RuntimeError("工作区自检未通过，快速开始已停止")
                goal = input("演示目标（直接回车使用默认目标）：").strip()
                created = app.create_demo(
                    {
                        "goal": goal or "验证通用工作流候选可以安全运行",
                        "actions": ["prepare", "execute", "verify"],
                        "constraints": ["offline", "deterministic"],
                    },
                    idempotency_key=f"interactive-{uuid.uuid4().hex}",
                )
                run_id = str(created["run"]["run_id"])
                resumed = app.resume(run_id)
                timeline = app.timeline(run_id)
                print(
                    f"\n快速开始完成：已安装 {initialized['installed_workflows']} 条工作流，"
                    f"自检={result['status']}，演示={resumed['status']}"
                )
                print(f"run ID：{run_id}")
                print(f"进度：{timeline['progress']:.0%}")
            elif choice == "2":
                app = CandidateApplication(workspace)
                initialized = app.initialize_defaults()
                result = app.doctor()
                print(
                    f"\n工作区：{initialized['status']}；"
                    f"工作流={initialized['installed_workflows']}；自检={result['status']}"
                )
                for check in result["checks"]:
                    print(f"- {check['id']}: {check['status']}")
            elif choice == "3":
                app = CandidateApplication(workspace)
                workflows = app.list_workflows()["workflows"]
                print(f"\n共 {len(workflows)} 条工作流：")
                for item in workflows:
                    flags = []
                    if item["is_builtin"]:
                        flags.append("内置")
                    if item["is_readonly"]:
                        flags.append("只读")
                    if item["cloneable"]:
                        flags.append("可克隆")
                    print(f"- {item['name']}  [{item['workflow_id']}]  v{item['latest_version']}  {'/'.join(flags)}")
            elif choice == "4":
                app = CandidateApplication(workspace)
                result = app.doctor()
                print(f"\n自检结果：{result['status']}")
                for check in result["checks"]:
                    print(f"- {check['id']}: {check['status']}")
                print("在线生产验收：NOT_RUN；候选不可自动晋级生产。")
            elif choice == "5":
                app = CandidateApplication(workspace)
                print("\n正在运行离线核心验收，请稍候……")
                report = app.accept_offline()
                report_path = workspace / "reports" / (f"interactive-offline-acceptance-{uuid.uuid4().hex[:12]}.json")
                written = app.write_acceptance_report(report, report_path)
                print(f"离线验收结果：{report['offline_candidate_acceptance']}")
                print(f"报告：{written}")
                print("SCI 在线执行、真实模型、浏览器和投稿动作均为 NOT_RUN。")
            elif choice == "6":
                selected = input("请输入新的候选工作区绝对路径：").strip()
                if not selected:
                    raise ValueError("工作区路径不能为空")
                candidate = Path(selected).expanduser()
                if not candidate.is_absolute():
                    raise ValueError("交互菜单只接受绝对工作区路径")
                next_workspace = candidate.absolute()
                CandidateApplication(next_workspace)
                workspace = next_workspace
                print("已切换本次菜单的工作区；尚未创建或修改该目录。")
            elif choice == "7":
                _parser().print_help()
            else:
                print("未识别的选项，请输入 0–7。")
                continue
        except EOFError:
            print("\n交互输入已关闭，候选工具以失败关闭。")
            return 2
        except Exception as exc:
            print(f"\n操作失败：{type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    if not raw_arguments:
        if not _stdin_is_interactive():
            print("零参数交互菜单需要从资源管理器双击或在交互式终端中运行。")
            print("自动化调用请使用 --help 查看显式命令。")
            return 2
        try:
            return _interactive_menu()
        except KeyboardInterrupt:
            print("\n候选工具已由用户中止。")
            return 130
    args = _parser().parse_args(raw_arguments)
    locale = normalize_locale(args.locale)
    if not args.workspace:
        _parser().error("--workspace is required for this command")
    workspace = Path(args.workspace).expanduser().absolute()
    exit_code = 0
    try:
        app = CandidateApplication(workspace)
        if args.command == "init":
            result = _decorate(app.initialize_defaults(), locale, "initialized")
        elif args.command == "list":
            result = _decorate(app.list_workflows(), locale, "listed")
        elif args.command == "doctor":
            result = _decorate(app.doctor(sci_roots=_sci_roots(args)), locale, "doctor")
            if result["status"] == "FAIL":
                exit_code = 1
        elif args.command == "demo-create":
            result = _decorate(
                app.create_demo(_request(args), idempotency_key=args.idempotency_key),
                locale,
                "demo_created",
            )
        elif args.command == "tick":
            result = _decorate(app.tick(args.run_id), locale, "tick")
        elif args.command == "resume":
            result = _decorate(app.resume(args.run_id), locale, "resumed")
        elif args.command == "timeline":
            result = _decorate(app.timeline(args.run_id), locale, "timeline")
        elif args.command == "accept-offline":
            report = app.accept_offline(sci_roots=_sci_roots(args))
            report_path = Path(args.report).expanduser()
            if not report_path.is_absolute():
                report_path = workspace / report_path
            written = app.write_acceptance_report(report, report_path)
            result = _decorate(
                {
                    **report,
                    "report_path": str(written),
                    "report_sha256": hashlib.sha256(written.read_bytes()).hexdigest(),
                },
                locale,
                "accepted",
            )
            if report["offline_candidate_acceptance"] == "FAIL":
                exit_code = 1
        else:  # pragma: no cover - argparse owns choices
            raise AssertionError(args.command)
    except Exception as exc:
        _emit(
            {
                "status": "error",
                "locale": locale,
                "message": localized_message(locale, "error"),
                "error_type": type(exc).__name__,
                "detail": str(exc),
            }
        )
        return 2
    _emit(result)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
