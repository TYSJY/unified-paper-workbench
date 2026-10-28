# Drawing Step 01: Build the Reference Board in One Handoff File

## Single-file handoff contract — highest priority

- Read only the uploaded `paper.tex`.
- Do not ask for extra naming metadata, directory names, repositories, or local paths.
- Do not claim to inspect or write files on the user's computer.
- Return exactly one complete Markdown file named `figure_workflow.md`.
- The output must contain a verbatim copy of the complete uploaded `paper.tex`, so the next step does not need the paper to be uploaded again.
- If downloadable file creation is available, provide `figure_workflow.md`. Otherwise output its complete contents in one Markdown code block.
- Do not return screenshots, folders, manifests as separate files, or any final paper figure in this step.

## Task

Read the uploaded paper and identify:

- research task;
- method type;
- system setting;
- target venue style, if stated;
- key related areas;
- existing figures and tables;
- confirmed numeric evidence;
- likely figure types required.

Use Deep Research or Search to research at least 20 top-conference or highly relevant papers. Prioritize original paper pages, proceedings, publisher pages, arXiv, OpenReview, DOI pages, and official implementation pages.

Do not rely on remembered citations. Every reference must have a clickable authoritative source.

Analyze visual patterns from accessible Figure 1, system overview, method overview, controller, memory/state, workflow, tradeoff, Pareto, calibration, reliability, ablation, sensitivity, and robustness figures.

Reference images are research inspiration only. Do not copy, trace, or reuse their content.

If an actual figure cannot be opened or inspected:

- do not describe its colors, layout, panels, or visual details as observed facts;
- record the paper URL, intended figure/page, desired pattern, and exact access limitation;
- continue with accessible references.

## Rendering policy for later steps

- `conceptual_schematic`: ChatGPT Image Generation.
- `quantitative_data_figure`: Data Analysis / Python, PGFPlots, or another reproducible numeric workflow.
- `hybrid_figure`: separate conceptual and quantitative panels; combine them reproducibly.
- `table`: LaTeX table.
- Never use image generation to fabricate axes, curves, bars, heatmaps, error bars, Pareto frontiers, calibration plots, or exact experimental values.

## Required `figure_workflow.md` structure

````markdown
# Figure Workflow Handoff

## Workflow Status
stage: drawing_01_reference_board

## Complete Paper Source
```latex
Paste the complete uploaded paper.tex verbatim here.
```

## Paper Visual Summary
- task
- method type
- system setting
- target venue style
- key claims
- existing figures and tables
- confirmed data locations
- likely visual evidence chain

## Existing Figure and Table Inventory
For every existing figure/table, record its label, caption, purpose, content type, and whether it should be preserved, revised, replaced, converted to a table, or moved to an appendix.

## Reference Research Method
Record search tools, queries, research date, source-selection rules, and access limitations.

## Reference Board
At least 20 verified papers. For each:
- title
- venue and year
- clickable original or authoritative source
- relevant figure number/page, if verified
- figure type
- visual narrative
- panel organization
- useful design patterns
- elements not to copy
- relevance to this paper
- inspection status: visually_inspected / metadata_only

## Visual Pattern Taxonomy
A. problem–solution teaser
B. system architecture
C. core-insight illustration
D. tradeoff / Pareto plot
E. runtime controller / routing
F. memory / resource-state visualization
G. calibration / risk / uncertainty visualization

## Most Useful Visual Lessons

## Design Elements to Avoid

## Unavailable Reference Figures
For each unavailable item:
- paper and authoritative URL
- intended figure/page
- exact missing reason
- what should be inspected manually
- explicit statement that it was not visually inspected

## Data Authority Snapshot
Copy every `CONFIRMED_DATA` block and summarize every existing LaTeX table or numeric result needed for later figures. Preserve values, units, method labels, and row relationships exactly.

## Handoff Rules for Step 02
- Preserve the complete paper source and every section above.
- Append the complete figure plan, prompts and figure briefs to this same file.
````

If live research is unavailable, return the same file structure with an honest TODO research section; do not claim the 20-paper requirement was completed.

Deliver only `figure_workflow.md`.
