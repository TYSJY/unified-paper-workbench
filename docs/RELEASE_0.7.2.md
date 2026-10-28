# Unified Paper Orchestrator 0.7.2

## Journal and author workflow

- Automatically derives a planned paper-specific GitHub repository from the manuscript method name, falling back to the paper title when no method macro exists.
- Automatically records the standing AI-use statement before the journal/package SDK task starts.
- Synchronizes repository and AI-use declarations into `author_info.md`, package metadata, declarations, and the author-visible manuscript.
- Flattens referenced figure paths for LaTeX submission systems that reject subfolders and recompiles the final PDF.
- Treats a recommended/optional publisher template as a non-blocking note instead of an artificial manual blocker.
- Enforces target-journal abstract word limits recorded in `journal.md`.
- Rejects stale PDFs whose modification time predates `main.tex`.
- Shows concrete remaining manual actions in the live journal/author task UI.

## Current two-paper recovery

- `2026-07-16_001_phase-scoped-backend-governor`: repository and AI-use declarations repaired; only four expert suggestions remain for the online form.
- `2026-07-16_002_kv-churn-speculation`: abstract shortened to 135 words, figure paths flattened, declarations repaired, PDF recompiled, and package revalidated as `READY_TO_SUBMIT`.

The tuned writing, figure, journal-selection, and package-generation prompt files were not modified.
