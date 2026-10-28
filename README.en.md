<div align="center">
<img src="docs/assets/github-hero.svg" alt="Unified Paper Workbench" width="100%" />

**One desktop workspace for your paper, figures, and deliverables.**

Writing · Figures · Word / PDF · Submission workflows

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
![Windows](https://img.shields.io/badge/platform-Windows-0078D4)
![Preview](https://img.shields.io/badge/status-early_preview-orange)

[简体中文](README.md) · [Getting started](docs/GETTING_STARTED.md) · [Capabilities](docs/FEATURES.md) · [Roadmap](docs/ROADMAP.md) · [Contributing](CONTRIBUTING.md)
</div>

Unified Paper Workbench is a **local desktop workspace for AI-assisted academic writing**. It brings source materials, writing steps, figures, workflow state, and deliverable files together in a visible, resumable interface.

![Real Qt application with fictional demonstration tasks](docs/assets/workbench-overview.png)

> **Early open-source preview.** The current locally verified desktop version is `0.12.0b11`. Full workflows still depend on legacy workspace templates; clean installation and public release packaging are active work. Screenshots use fictional tasks in an isolated workspace.

## Why a workbench?

A paper is more than a chat response. You need to know which materials were used, which stage is running, which files were produced, and what requires your attention.

The application manages queues and stage state. Codex SDK sessions handle content tasks. File-level checks determine whether required outputs are present and acceptable.

## What is here

| Workflow | Capabilities |
|---|---|
| Journal papers | Topic development, reference work, staged writing, revisions, figure handoff |
| Undergraduate theses | Title and school requirements, humanities/social science and software project prompt schemes |
| Figures | Figure planning, data checks, plotting scripts, manuscript integration |
| Deliverables | Undergraduate Word documents and PDF previews; journal TeX/PDF; direct PDF and folder actions |
| Execution | Per-paper sessions, concurrency, pause/resume, workflow snapshots |
| Models | Model and reasoning options discovered from the configured SDK account |
| Submission workflows | Package preparation and status workflows, subject to site support and user authorization |

![Open the manuscript PDF from the application](docs/assets/workbench-pdf.png)

## Start here

Read [Getting started](docs/GETTING_STARTED.md) for the current installation scope and required workspace templates. Windows is the verified desktop environment. A publicly downloadable release is not implied by the presence of a version number in this README.

Bring your own supported ChatGPT/Codex credentials. AI execution can consume paid model usage. **Local workspace storage does not mean offline inference**: connected services receive the inputs required for their tasks.

Microsoft Word on Windows is currently required for native Word pagination and PDF rendering in the undergraduate delivery workflow.

## Boundaries

- Clean-clone end-to-end operation is not yet claimed; legacy module templates are being separated from local workspace data.
- Cross-platform desktop support has not been verified.
- References, research claims, and data require verification. Simulated data must remain disclosed unless replaced with actual verified evidence.
- The application does not guarantee publication, academic acceptance, or support for every journal website.

## Contribute

The highest-impact areas are clean installation, portable workspace templates, Word styles, reproducible figures, error recovery, and documentation. See [CONTRIBUTING.md](CONTRIBUTING.md) and the [roadmap](docs/ROADMAP.md).

If this is useful to you, a Star helps others discover the project. Specific bug reports and focused pull requests help make it usable.

## License

Project-owned source is available under [GNU AGPL v3.0](LICENSE), SPDX `AGPL-3.0-only`. Third-party components retain their own terms; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
