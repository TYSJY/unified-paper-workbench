# 0.12.0b1 commercial-foundation candidate

This candidate introduces a reversible commercial foundation without changing
the tuned internal prompts or replacing the existing local workflow.

## Added

- strict account, subscription, device, entitlement, licence, release, and
  diagnostic contracts;
- purpose-bound Ed25519 signatures and bounded offline licence grace;
- local deterministic control-plane simulator;
- DPAPI-protected commercial session cache;
- signed update-manifest and artifact verification;
- opt-in privacy-minimised diagnostics;
- Account & Licence settings in Chinese and English;
- publish-boundary audit, machine-readable schemas, and release/legal
  checklists.

## Compatibility

Commercial mode is disabled by default. With it disabled, no commercial
network request or device identity is created and 0.11.6 local starts continue
to behave as before.

The production public-key ring is intentionally empty. This build is therefore
a development/release candidate until a production public key and signed
control plane are available.

## Not production-ready

The candidate is not yet code signed and the bundled loopback control plane is
not a production service. Legal identity, EULA/privacy contacts, Qt licensing,
production billing/e-mail/operations, signing-key custody, and independent
release review remain blockers.

## Verified internal candidate — 2026-07-26

- Source branch: `codex/commercial-v0.12`
- Source commit: `de37bbc67c828d8a55bc00c492ed5e285b94b529`
- Candidate source tag: `commercial-candidate-0.12.0b1`
- Frozen rollback tag: `commercial-baseline-0.11.6`
- Staged artifact:
  `build/release_candidate/UnifiedPaperOrchestrator.exe`
- Artifact size: `211922500` bytes
- Artifact SHA-256:
  `89caafb325f9219d49cebe0c0dff6a046eb8e1c751946e7077d5d572858ae1b4`
- Windows file/product version: `0.12.0b1`
- Full repository regression: `1325 passed, 54 subtests passed`
- Repository Ruff audit: passed with zero findings
- Packaged CLI probes: passed, including version, Codex runtime identity,
  writing/evidence/browser/submission entry points, gateway isolation, and
  invalid-command routing
- Isolated GUI smoke: remained healthy for 15 seconds, created only the
  build-local workspace/profile state, and was then terminated cleanly
- Source publish-boundary audit: `422` tracked files, zero findings
- Staged customer-bundle audit: one EXE, zero findings
- Tuned prompt trees and `tests/contracts/v1_internal_prompts.json`: no diff
  from the frozen `fb4dd91` 0.11.6 baseline
- Previous production EXE retained unchanged with SHA-256
  `0adc5877cae12495f194245df499ff5abf6883ac570dffc919c9042a376a8885`
- Production workspace root-state fingerprint remained unchanged:
  `fbe186386466fb40eed32485e1960557ae36c7b0211eade35295cf85892fbd40`

The staged EXE has Authenticode status `NotSigned`, and the packaged production
keyring deliberately contains zero keys. These are explicit public-release
blockers; neither condition is silently downgraded or bypassed.
