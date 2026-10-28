# Writing Step 5 -> Step 6 evidence gate

This controller gate is a read-only, fail-closed check. It does not edit paper
materials, drafts, result files, scheduler state, or any internal workflow
prompt.

## Transition rule and compatibility

- `schema_version: 2` is the only manifest contract that can return
  `status=READY`, `ready=true`, and `transition_ready=true`.
- A structurally valid historical `schema_version: 1` manifest is still
  inspected with its original paper/draft/artifact hash checks. It returns
  `status=LEGACY_V1`, `legacy_read_only=true`,
  `legacy_integrity_ready=true`, and `ready=false`, with the
  `verification_manifest_upgrade_required` blocker. It is never rewritten.
- Completed historical papers are projected from their completed artifact/state
  and are not retroactively downgraded by this transition-only gate.
- Missing, malformed, stale, duplicate, self-referential, or escaping evidence
  fails closed. All paths are relative to the selected paper directory.

## Required files

The gate reads exactly these control files plus the artifact paths named by the
manifest:

```text
paper_materials.md
draft_v2.tex
results/verification_manifest.json
```

`paper_materials.md` must contain one standalone `Status: READY` line and no
conflicting standalone `Status:` line. Schema v2 binds the exact materials bytes
with `paper_materials_sha256`; it does not infer evidence from fragile Markdown
headings.

## Explicit unresolved markers

The draft is blocked by explicit workflow markers, including:

```text
VERIFY
U+3010 / U+9A8C / U+3011 (the Chinese `verify` marker)
SIMULATED_TO_REPLACE
verification-required placeholder
BLOCKING TODO
UNRESOLVED
TODO: ...       [TODO]       \todo{...}
PLACEHOLDER: ... [PLACEHOLDER] \placeholder{...}
SIMULATED: ...  [SIMULATED]  \simulated{...}
```

The contextual forms are deliberate. Normal prose such as "an unresolved
challenge", "placeholder variable", "simulated annealing", or "the TODO list"
does not match by itself.

## Schema v2 contract

The following example is normative for field names and relationships. Hashes
are shortened here only for readability; real values must be 64 hexadecimal
characters.

```json
{
  "schema_version": 2,
  "paper_id": "paper-1",
  "status": "READY",
  "draft_v2_sha256": "<sha256 of draft_v2.tex>",
  "paper_materials_sha256": "<sha256 of paper_materials.md>",
  "artifacts": [
    {
      "id": "raw-trials",
      "role": "raw",
      "path": "results/trials.jsonl",
      "sha256": "<sha256>"
    },
    {
      "id": "aggregate-metrics",
      "role": "aggregate",
      "path": "results/metrics.json",
      "sha256": "<sha256>"
    }
  ],
  "evidence_contract": {
    "schema_version": 1,
    "environment": {
      "description": "CPython 3.12, Windows, fixed local inputs",
      "artifact_ids": ["raw-trials"]
    },
    "workflows": {
      "ids": ["workflow-a"],
      "artifact_ids": ["raw-trials"]
    },
    "faults": {
      "ids": ["no-fault", "crash-after-cut"],
      "artifact_ids": ["raw-trials"]
    },
    "seeds": {
      "values": [7],
      "artifact_ids": ["raw-trials"]
    },
    "metrics": {
      "ids": ["latency-ms"],
      "artifact_ids": ["aggregate-metrics"]
    },
    "limitations": {
      "items": ["Local deterministic prototype only"],
      "artifact_ids": ["aggregate-metrics"]
    },
    "raw_artifact_ids": ["raw-trials"],
    "aggregate_artifact_ids": ["aggregate-metrics"]
  },
  "claim_evidence": {
    "coverage": "all_quantitative_claims_in_draft_v2",
    "draft_v2_sha256": "<sha256 of draft_v2.tex>",
    "claims": [
      {
        "claim_id": "claim-latency",
        "type": "quantitative",
        "draft_excerpt": "Median latency was 1.25 ms across measured trials.",
        "evidence": [
          {"artifact_id": "raw-trials", "row_id": "trial-1"},
          {"artifact_id": "aggregate-metrics", "row_id": "metric-1"}
        ]
      }
    ]
  }
}
```

### Artifacts

- Every v2 artifact has a unique, case-insensitive `id`, a supported `role`, a
  paper-relative `path`, and a matching SHA-256.
- Supported roles are `raw`, `aggregate`, `environment`, `workflows`, `faults`,
  `seeds`, `metrics`, `limitations`, and `other`.
- Duplicate paths are rejected case-insensitively. Absolute paths, Windows drive
  paths, traversal, symlink escape, and missing files are rejected.
- The manifest cannot list itself. `draft_v2.tex`, `paper_materials.md`, and the
  manifest are control files and cannot masquerade as evidence artifacts.

### Machine-readable core evidence

`evidence_contract` is the target-independent sidecar contract embedded in the
manifest. It must declare non-empty environment, workflows, faults, seeds,
metrics, and limitations sections. Every section names one or more existing
hash-bound artifact IDs. It must also name at least one `raw` artifact and one
`aggregate` artifact through `raw_artifact_ids` and
`aggregate_artifact_ids`.

### Claim-to-row evidence

- `coverage` is an explicit completeness attestation and must be exactly
  `all_quantitative_claims_in_draft_v2`.
- `claim_evidence.draft_v2_sha256` must match the final draft bytes.
- The claim list is non-empty. Every entry has a unique `claim_id`, type
  `quantitative`, and an exact `draft_excerpt` that occurs once in the draft and
  contains an explicit numeric/quantity signal.
- Every claim references existing rows in hash-verified artifacts. Each claim
  must resolve to at least one row in an artifact whose role is `raw`.
- Row-bound artifacts use `.json`, `.jsonl`, or `.csv`. JSON is either a list of
  row objects or an object containing a `rows` or `records` list. Every row has a
  unique safe `evidence_id`; `row_id` in the claim map addresses that field.
- Duplicate claims, duplicate row references, duplicate `evidence_id` values,
  unsupported row formats, malformed rows, missing rows, and non-raw-only claim
  support all block the transition.

The gate verifies structural completeness and byte/row bindings. Scientific
truth still depends on the experiment implementation, independent review, and
the declared limitations; the gate does not turn an attestation into external
validation.

## Migration from v1

Do not edit historical manifests merely to make their status look newer. For an
active Step 5 paper, use the evidence importer described below to bind the final
immutable draft, materials, raw rows, and aggregates. Then run:

```powershell
python -m unified_paper_orchestrator writing-evidence-gate <paper-id>
```

Exit code `0` means the v2 transition contract is ready. Any other exit code is
a hard stop for Step 6.

## Spec-driven evidence import

`writing-evidence-import` removes the need to hand-author the hash-bearing
schema-v2 manifest. It does not discover, infer, or generate evidence. The
operator must still declare the actual artifacts, experiment contract, every
quantitative claim excerpt, and the exact supporting row IDs in a JSON spec.
The importer adds only fixed contract fields and SHA-256 hashes of stable reads.

The spec must be inside the selected paper directory. A relative `--spec` path
is resolved from that directory, not from the shell working directory. Artifact
paths also use forward-slash, paper-relative paths. Absolute artifact paths,
backslashes, traversal components, symlinks/reparse points, control files, the
spec itself, and importer-receipt paths are rejected.

Example `results/evidence_import.json`:

```json
{
  "import_spec_version": 1,
  "artifacts": [
    {"id": "raw-trials", "role": "raw", "path": "results/trials.jsonl"},
    {"id": "aggregate-metrics", "role": "aggregate", "path": "results/metrics.json"}
  ],
  "evidence_contract": {
    "environment": {
      "description": "CPython 3.12, Windows, fixed local inputs",
      "artifact_ids": ["raw-trials"]
    },
    "workflows": {"ids": ["workflow-a"], "artifact_ids": ["raw-trials"]},
    "faults": {"ids": ["no-fault", "crash-after-cut"], "artifact_ids": ["raw-trials"]},
    "seeds": {"values": [7], "artifact_ids": ["raw-trials"]},
    "metrics": {"ids": ["latency-ms"], "artifact_ids": ["aggregate-metrics"]},
    "limitations": {
      "items": ["Local deterministic prototype only"],
      "artifact_ids": ["aggregate-metrics"]
    },
    "raw_artifact_ids": ["raw-trials"],
    "aggregate_artifact_ids": ["aggregate-metrics"]
  },
  "claims": [
    {
      "claim_id": "claim-latency",
      "draft_excerpt": "Median latency was 1.25 ms across measured trials.",
      "evidence": [
        {"artifact_id": "raw-trials", "row_id": "trial-1"},
        {"artifact_id": "aggregate-metrics", "row_id": "metric-1"}
      ]
    }
  ]
}
```

Run the read-only preparation first:

```powershell
python -m unified_paper_orchestrator writing-evidence-import <paper-id> --spec results/evidence_import.json --check-only
```

`status=CHECKED` means the candidate can be committed from the bytes that were
read, but it does not authorize Step 6: no receipt or manifest was written and
`transition_ready` remains false. After reviewing the result, commit explicitly:

```powershell
python -m unified_paper_orchestrator writing-evidence-import <paper-id> --spec results/evidence_import.json
```

Commit writes a hash-addressed precommit provenance receipt under
`results/evidence_import_receipts/`, verifies that every input is still the same,
and replaces `results/verification_manifest.json` atomically as the final write.
It then invokes the existing read-only gate. `status=IMPORTED` and
`transition_ready=true` mean that gate accepted the exact manifest. A concurrent
input or manifest change fails closed. The importer never edits scheduler state,
changes prompts, or resumes model work; a later Step 6 action remains explicit.

The EXE exposes the same flow on the **写论文** tab as **导入真实实验证据…**.
It is available only when exactly one projected paper is stopped at Step 5 with
a blocked evidence gate. The file picker selects the paper-local JSON spec, the
EXE shows the check-only artifact/claim/hash summary, and commit requires a
second explicit confirmation bound to that exact candidate hash. After commit
the view refreshes, but Step 6 is not started and no model call is made.
