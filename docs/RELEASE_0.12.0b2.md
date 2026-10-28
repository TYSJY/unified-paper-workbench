# 0.12.0b2 customer closed-beta candidate

This candidate connects the commercial foundation to the desktop product shell
while preserving the 0.11.6 rollback baseline, local paper workspaces, and all
four stages' tuned internal prompts.

## Added

- immutable `Internal` and `CustomerClosedBeta` desktop build profiles;
- customer first-run welcome, e-mail one-time-code login, trial/subscription
  recognition, device activation, offline lease handling, expiry read-only
  mode, and account/upgrade entry points;
- account, session, trial, subscription, device, and signed-licence control
  plane contracts and HTTP routes;
- SMTP delivery adapter with an explicit development-code mode for loopback
  testing;
- Chinese and English customer onboarding and account-management UI;
- independent product website with overview, pricing, download, account,
  support, privacy, and terms routes;
- public-key-only customer packaging and packaged distribution self-audit.

## Distribution boundary

`Internal` is the compatibility build and does not enforce commercial access
by default. `CustomerClosedBeta` cannot disable enforcement, change its
control-plane origin, or trust runtime development keys. Customer packaging
requires an explicitly supplied Ed25519 public key; the corresponding private
key remains outside source and release artifacts.

The current control-plane URL is loopback-only and the product website
deployment is private for owner validation. This is therefore a closed
candidate, not a public paid release.

## Public-release blockers

- production HTTPS control plane and durable operated database;
- verified e-mail provider, billing webhooks, abuse controls, backups, and
  incident operations;
- production signing-key custody and rotation;
- Windows Authenticode signing and timestamping;
- final legal identity, EULA, privacy, refund, Qt/PySide, and automated
  submission review;
- a public or invited-customer website access policy and final installer.

## Verification record

- Verification date: `2026-07-28`
- Source branch: `codex/commercial-v0.12`
- Product-site source commit:
  `7dd4772cb325ebe3b0f99108faa6e764d83ddd93`
- Private owner-validation deployment:
  `https://unified-paper-workbench.gptpro20x836074.chatgpt.site`
- Staged artifact:
  `build/release_candidate/UnifiedPaperOrchestrator.exe`
- Artifact size: `211938961` bytes
- Artifact SHA-256:
  `ea5e756b890bec3e86633af5cb509930ce8ea04f1fc2e6c7c742b54749fe441e`
- Windows file/product version: `0.12.0b2`
- Packaged distribution: `customer`, commercial enforcement `true`,
  runtime configuration `false`, development keys `false`, one trusted public
  signing key
- Full repository regression: `1334 passed, 54 subtests passed`
- Repository Ruff audit: passed with zero findings
- Packaged CLI probes: passed, including version, immutable distribution,
  Codex runtime identity, writing/evidence/browser/submission entry points,
  gateway isolation, and invalid-command routing
- Real packaged first-run smoke: welcome screen rendered; loopback e-mail-code
  login created the one-time trial, activated the device, removed onboarding,
  and enabled workflow starts
- Offline restart smoke: with the control plane stopped, the cached signed
  licence verified and the activated desktop remained usable
- Source publish-boundary audit: `431` tracked files, zero findings
- Customer-bundle publish audit: one EXE, zero findings
- Tuned prompt trees and `tests/contracts/v1_internal_prompts.json`: no diff
  from `commercial-baseline-0.11.6`
- Previous production EXE retained unchanged with SHA-256
  `0adc5877cae12495f194245df499ff5abf6883ac570dffc919c9042a376a8885`

The staged EXE has Authenticode status `NotSigned`. The control plane remains
loopback-only and its matching private candidate key is stored only under
ignored local `.commercial/` state. These remain explicit public-release
blockers and are not bypassed by the closed candidate.
