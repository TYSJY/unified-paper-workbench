# Prompt 01: Research Reference Board

Task:

Read `papers/<paper_name>/input.tex`.

Do not generate paper figures yet.

Rendering policy for later steps:

- The active prompt policy overrides `figure_plan.md` if they conflict.
- Conceptual schematic figures must use image2 in later generation steps.
- Quantitative data figures must be generated from explicit numeric data with a reproducible plotting workflow.
- Hybrid figures should keep conceptual panels and quantitative panels separate; conceptual panels must use image2, while quantitative panels must use reproducible plotting.
- Tables should be LaTeX tables, not image-only figures.
- Reference-board research should collect visual inspiration and data-figure design patterns, not templates to copy.

First identify the paper's exact field, including:

- task
- method type
- system setting
- target conference style
- key related areas
- likely figure types needed

Then inspect the existing `refs/` folder.

If an existing reference board is relevant, update it.

If not enough references exist, create a new folder:

```text
refs/<field_name>/
|-- images/
|-- images_manifest.md
`-- notes.md
```

Research and analyze at least 20 top-conference or highly relevant papers from related areas, such as:

1. LLM serving systems
2. efficient inference
3. KV cache optimization
4. LLM quantization / compression
5. edge / local LLM inference
6. confidence calibration
7. selective prediction / abstention
8. LLM routing / cascading
9. resource-aware or cost-aware inference
10. risk-constrained inference
11. efficient ML systems

Prioritize venues such as:

- NeurIPS
- ICML
- ICLR
- MLSys
- OSDI
- SOSP
- ASPLOS
- EuroSys
- ACL
- EMNLP

For each reference paper, write:

- paper name
- venue and year
- link or source
- figure type
- what Figure 1 / system overview / method overview shows
- visual narrative structure
- panel organization
- whether it uses problem -> solution structure
- whether it contains a teaser plot
- useful design elements
- elements that should not be copied
- how it can inspire this paper

## Best-effort reference screenshots

Step 01 should try to save a concrete local reference-image package, but
reference screenshots are non-blocking. Do not fail or stop the workflow just
because screenshots cannot be downloaded, extracted, or saved.

When practical, maintain a reusable field-level reference screenshot pool with
up to 50 images. If `refs/<field_name>/images/` already contains a useful pool,
reuse it and add only missing high-value patterns instead of downloading the
same style again. Prefer screenshots of Figure 1, system overviews, method
overviews, controller diagrams, memory/state visualizations, workflow/state
machine figures, mechanism illustrations, and representative tradeoff figures.
Do not exceed 50 local screenshots for one field without an explicit operator
request.

Save them under:

```text
refs/<field_name>/images/
```

Use stable filenames:

```text
ref01_<short_paper_slug>_fig1.png
ref02_<short_paper_slug>_system_overview.png
ref03_<short_paper_slug>_method.png
...
```

For each saved image, record metadata in both manifests:

```text
refs/<field_name>/images_manifest.md
papers/<paper_name>/reference_images_manifest.md
```

Each manifest entry must include:

- local image path
- source paper title
- venue and year
- source URL or DOI/arXiv/open-access PDF URL
- source figure number or page number
- what visual pattern it shows
- what can inspire this paper
- what must not be copied

Reference screenshots are for private visual analysis only. Do not copy,
trace, or directly reuse them in final paper figures. Final conceptual paper
figures must be newly generated from the paper's own content and the
paper-specific imagegen/image2 prompts.

If a PDF blocks figure extraction, use a page screenshot or browser screenshot
of the figure area. If an individual figure is inaccessible after reasonable
attempts, replace it with another accessible top-conference reference when
possible. If screenshots are not available, create:

```text
refs/<field_name>/missing_reference_images.md
papers/<paper_name>/missing_reference_images.md
```

and list the intended papers, links, figure numbers, exact screenshots that
should be collected manually, and the reason local images were unavailable.
This missing-image record is sufficient for Step 01 to complete.

Classify references into:

A. problem-solution teaser
B. system architecture
C. core insight illustration
D. tradeoff / Pareto frontier plot
E. runtime controller / routing diagram
F. memory / resource-state visualization
G. calibration / risk / uncertainty visualization

Output to:

```text
refs/<field_name>/notes.md
refs/<field_name>/images_manifest.md if local screenshots are saved
papers/<paper_name>/reference_board.md
papers/<paper_name>/reference_images_manifest.md if local screenshots are saved
refs/<field_name>/missing_reference_images.md if screenshots are unavailable
papers/<paper_name>/missing_reference_images.md if screenshots are unavailable
```

If internet access is unavailable, create a structured TODO list of papers and figure types to collect manually, and still create the notes file.

The per-paper `reference_board.md` must record:

- the chosen `field_name`
- the exact reference-board path under `refs/`
- the exact reference screenshot directory under `refs/<field_name>/images/`, if screenshots were saved
- the exact paper-specific screenshot manifest path, if screenshots were saved
- the missing-image record path and reason, if screenshots were unavailable
- the target conference style
- the 5-10 most relevant reference papers to imitate
- the local reference screenshots most useful for Step 02, if any
- the size of the reusable field reference pool, capped at 50 images
- the figure styles most worth carrying into Step 02
- the design elements to avoid

Before finishing, verify:

- `papers/<paper_name>/reference_board.md` exists
- `refs/<field_name>/notes.md` exists
- either local screenshots/manifests exist, or missing-image records explain why they do not

Do not block Step 02 just because local reference screenshots are missing.

Do not generate any paper figures in this step.
