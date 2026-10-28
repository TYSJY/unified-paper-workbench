from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from unified_paper_orchestrator.workflow.library import WorkflowLibrary  # noqa: E402
from unified_paper_orchestrator.workflow.packs import build_sci_workflow_pack  # noqa: E402
from unified_paper_orchestrator.workflow.resources import (  # noqa: E402
    ContentAddressedPromptStore,
    prompt_resource_directory,
)
from unified_paper_orchestrator.workflow.store import (  # noqa: E402
    WorkflowStore,
    workflow_database_path,
)
from unified_paper_orchestrator.workflow.timeline import WorkflowTimelineService  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline generic linear workflow library CLI.")
    parser.add_argument("--workspace", required=True, help="Explicit workspace root.")
    commands = parser.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("init", help="Initialize the isolated generic ledger and resources.")
    initialize.add_argument("--install-sci", action="store_true")

    commands.add_parser("list", help="List workflow library entries.")
    show = commands.add_parser("show", help="Show one immutable workflow version.")
    show.add_argument("version_id")

    clone = commands.add_parser("clone", help="Clone a protected or custom version to a new draft.")
    clone.add_argument("source_version_id")
    clone.add_argument("--workflow-id", required=True)
    clone.add_argument("--name", required=True)
    clone.add_argument("--description")

    edit_prompt = commands.add_parser("edit-prompt", help="Create a Prompt resource and append a draft.")
    edit_prompt.add_argument("parent_version_id")
    edit_prompt.add_argument("node_id")
    edit_prompt.add_argument("prompt_file")
    edit_prompt.add_argument("--prompt-version", default="")

    publish = commands.add_parser("publish", help="Publish the latest custom draft.")
    publish.add_argument("version_id")

    import_command = commands.add_parser("import", help="Import a portable bundle as a custom draft.")
    import_command.add_argument("bundle_file")
    import_command.add_argument("--workflow-id", required=True)
    import_command.add_argument("--name")
    import_command.add_argument("--description")

    export = commands.add_parser("export", help="Export a version and explicit Prompt files.")
    export.add_argument("version_id")
    export.add_argument("--resource", action="append", default=[], metavar="RESOURCE_ID=PATH")
    export.add_argument("--output", required=True)

    timeline = commands.add_parser("timeline", help="Show a sanitized generic run timeline.")
    timeline.add_argument("run_id")
    return parser


def _library(workspace: Path) -> WorkflowLibrary:
    return WorkflowLibrary(
        WorkflowStore(workflow_database_path(workspace)),
        ContentAddressedPromptStore(prompt_resource_directory(workspace)),
    )


def _require_ledger(library: WorkflowLibrary) -> None:
    if not library.store.path.is_file():
        raise RuntimeError("generic workflow workspace is not initialized; run init first")


def _resources(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        resource_id, separator, raw_path = value.partition("=")
        if not separator or not resource_id or not raw_path or resource_id in result:
            raise ValueError("resource must be a unique RESOURCE_ID=PATH pair")
        result[resource_id] = Path(raw_path).read_text(encoding="utf-8-sig")
    return result


def _version_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": record["version"].to_dict(),
        "is_builtin": bool(record["is_builtin"]),
        "is_readonly": bool(record["is_readonly"]),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    workspace = Path(args.workspace).expanduser().absolute()
    library = _library(workspace)
    try:
        if args.command == "init":
            library.initialize()
            installed = library.install_pack(build_sci_workflow_pack()) if args.install_sci else ()
            result: Any = {
                "status": "initialized",
                "workspace": str(workspace),
                "installed_workflows": len(installed),
            }
        else:
            _require_ledger(library)
            if args.command == "list":
                result = {"workflows": [item.to_dict() for item in library.list_workflows()]}
            elif args.command == "show":
                result = _version_payload(library.store.get_version(args.version_id))
            elif args.command == "clone":
                result = _version_payload(
                    library.clone_version(
                        args.source_version_id,
                        workflow_id=args.workflow_id,
                        name=args.name,
                        description=args.description,
                    )
                )
            elif args.command == "edit-prompt":
                editor = library.open_editor(args.parent_version_id)
                text = Path(args.prompt_file).read_text(encoding="utf-8-sig")
                updated, resource = library.edit_prompt(
                    editor,
                    args.node_id,
                    text,
                    version=args.prompt_version,
                )
                saved = library.save_editor(
                    updated,
                    expected_parent_version_id=args.parent_version_id,
                )
                result = {
                    **_version_payload(saved),
                    "prompt_resource": {
                        "resource_id": resource.resource_id,
                        "content_sha256": resource.content_sha256,
                        "version": resource.version,
                    },
                }
            elif args.command == "publish":
                result = _version_payload(library.publish(args.version_id))
            elif args.command == "import":
                imported = library.import_bundle(
                    Path(args.bundle_file).read_bytes(),
                    workflow_id=args.workflow_id,
                    name=args.name,
                    description=args.description,
                )
                result = {
                    **_version_payload(dict(imported.version_record)),
                    "prompt_resource_count": len(imported.prompt_resources),
                    "bundle_sha256": imported.draft.bundle_sha256,
                }
            elif args.command == "export":
                payload = library.export_version(
                    args.version_id,
                    prompt_text_by_resource_id=_resources(args.resource),
                )
                output = Path(args.output).expanduser().absolute()
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(payload)
                    stream.write("\n")
                result = {"status": "exported", "output": str(output)}
            elif args.command == "timeline":
                result = WorkflowTimelineService(library.store).snapshot(args.run_id).to_dict()
            else:  # pragma: no cover - argparse owns the command choices
                raise AssertionError(args.command)
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
