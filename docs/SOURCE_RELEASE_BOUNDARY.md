# Source and customer release boundary

## Customer desktop bundle

The customer download should contain only:

- the signed `UnifiedPaperOrchestrator.exe`;
- release notes, checksum/signature metadata, EULA/privacy notice, and
  third-party notices;
- optional user documentation.

It must not contain the control-plane server, tests, build tools, Git history,
private keys, databases, runtime state, browser profiles, screenshots,
credentials, support spools, or source backups.

## Internal reproducible source

The internal build archive may include tracked source, tests, contracts,
prompts, and documentation, but must be generated from Git-tracked files after
the publish-boundary audit passes. It must not be made from a raw workspace
directory.

The following top-level trees are legacy/internal and are not customer
artifacts:

- `commercial_control_plane/` — deploy separately as a service;
- `unified_paper_orchestrator_v2/` — kernel lab/internal migration code;
- `tests/` and `tools/` — engineering-only;
- `chatgpt_web_*` and `single_file_handoff_prompts/` — historical handoff
  assets, not packaged by the production EXE build.

## Always denied

- `state/`, `build/`, `dist/`, `releases/`, `tmp/`, `.commercial/`, `.git/`;
- database, environment, log, private-key, certificate-bundle, and browser
  profile files;
- `credential_store.local.json`, local API tokens, page snapshots, screenshots;
- personal test fixtures and high-confidence secret patterns.

`tools/audit_publish_boundary.py` enforces this boundary before packaging.
