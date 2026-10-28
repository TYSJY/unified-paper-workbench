# Privacy-minimised diagnostics

Diagnostics are opt-in and contract-first. The client does not serialize an
exception object or general application state.

Allowed top-level fields:

- schema and event ID;
- UTC timestamp and application version;
- event type, severity, error code, component, and operation category;
- duration, OS family, release channel, and licence state;
- random anonymous installation ID;
- allow-listed metadata only.

Allowed metadata keys are `adapter`, `exception_class`, `feature`,
`http_status`, `retry_count`, `ui_action`, and `workflow_stage`.

The validator rejects e-mail addresses, Windows paths, overlong values,
unstructured text, bearer/API-key prefixes, unknown fields, and unsupported
metadata keys. The controller records a sanitised action key and exception
class, not the raw error message.

The following categories are intentionally impossible to represent in the
contract:

- manuscript, title, abstract, author, or reviewer content;
- local files, paths, prompts, screenshots, HTML, or browser page text;
- passwords, API keys, bearer tokens, cookies, session IDs, or credential refs;
- raw exception messages or stack traces.

The JSON Schema is
`contracts/commercial/diagnostic.schema.json`; executable validation is in
`unified_paper_orchestrator/commercial/diagnostics.py`.
