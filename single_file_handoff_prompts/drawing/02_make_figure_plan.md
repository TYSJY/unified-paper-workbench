# Drawing Step 02: Append the Complete Figure Plan to the Same Handoff File

## Single-file handoff contract — highest priority

- Read only the uploaded `figure_workflow.md` returned by Drawing Step 01.
- Do not request `paper.tex`, reference boards, screenshots, folders, naming metadata, or earlier files again.
- The complete paper source and reference research must already be inside the handoff file.
- Preserve all existing content exactly and append the planning sections.
- Return exactly one complete file named `figure_workflow.md`.
- Do not generate final figures, plotting code, binary images, PDFs, or ZIP files in this step.

## Preflight

The input must contain:

- `Complete Paper Source`
- `Paper Visual Summary`
- `Reference Board`
- `Existing Figure and Table Inventory`
- `Data Authority Snapshot`

If a required section is absent, stop and list only the missing sections.

## Goal

Create a publication-quality, evidence-balanced figure plan. The plan must answer:

- What problem does the paper solve?
- What is the proposed method?
- What is the main empirical evidence?
- What tradeoff improves?
- Which components matter?
- Under what conditions is the method robust or sensitive?
- What should reviewers visually remember?

## Classification and renderer policy

Every proposed item must be exactly one of:

### `conceptual_schematic`

Use ChatGPT Image Generation for a problem–solution teaser, method overview, architecture, controller, workflow, state machine, or qualitative mechanism.

Requirements:

- full web-ready image prompt;
- avoidance/negative prompt;
- short in-image labels, preferably one to four words;
- complete visual generated as one original raster image;
- no later code-based semantic text, arrows, boxes, or callout overlays;
- no generic stock-style AI, chip, cloud, laptop, dashboard, or decorative 3D art.

### `quantitative_data_figure`

Use Data Analysis / Python, PGFPlots, R/ggplot, MATLAB, or another reproducible workflow based on explicit numbers.

Specify:

- exact source section in the handoff file;
- CSV columns and data types;
- units;
- axes and scales;
- methods/models/datasets/devices/workloads;
- aggregation and uncertainty;
- expected output filename;
- validation checks.

Never provide an image-generation prompt for a quantitative plot.

### `hybrid_figure`

Keep conceptual and quantitative panels separate. Give each panel its own renderer and source. Combine them with LaTeX subfigures or another reproducible layout without redrawing the quantitative panel.

### `table`

Keep it as a LaTeX table.

### `placeholder_data_figure`

Use only when a genuinely required numeric field is absent. State the exact missing fields and complete CSV schema. Do not invent expected numbers.

## Planning requirements

1. Audit every existing figure and table in the embedded paper source.
2. Propose a compact main-paper figure set and appropriate appendix figures.
3. Figure 1 should usually be a hero/problem–solution visual, not a plain pipeline.
4. Main-paper conceptual figures should normally be limited to 1–2.
5. At least half of the main-paper figures should be quantitative or data-driven hybrid figures unless a paper-specific exception is justified.
6. Include main results, tradeoff/Pareto, ablation, sensitivity/robustness, and other evidence required by the actual claims.
7. Do not disguise tables as figures.
8. Do not replace missing result figures with additional workflow diagrams.
9. Treat values in `CONFIRMED_DATA`, embedded LaTeX tables and explicit results as authoritative.
10. Missing standalone CSV does not make embedded confirmed data unavailable; instruct Step 03 to extract it losslessly.
11. Never recompute, smooth, modify, reinterpret or delete confirmed values.

## Append these sections to `figure_workflow.md`

```markdown
# Step 02 Figure Plan

## Figure Plan Status
stage: drawing_02_plan_complete

## Evidence-Chain Audit

## Main-Paper Figure Plan
For every figure:
- number and stable filename
- label
- title
- placement
- reviewer question
- visual thesis
- type
- renderer
- source evidence
- panel layout
- required content
- exact labels
- data source and schema when applicable
- caption intent
- validation checklist

## Appendix Figure Plan

## Table Plan

## Data Availability Matrix
For every quantitative/hybrid item:
- authoritative source location
- expected normalized CSV filename
- columns and types
- units
- required series
- complete / partial / missing
- exact missing fields

## Complete Figure Generation Prompts
For every conceptual figure and conceptual hybrid panel, include:
- full ChatGPT Image Generation prompt
- avoidance/negative prompt
- aspect ratio
- short exact labels
- required layout
- visual hierarchy
- originality rule
- expected raw PNG filename

For quantitative figures, include plotting specifications rather than image prompts.

## Figure Briefs

### Figure 1 Brief
- identity and visual thesis
- reviewer question
- type and renderer
- source trace
- panel-by-panel content
- full generation prompt or complete plotting specification
- expected caption
- exact filenames
- dependencies
- validation checklist

Repeat a complete self-contained brief for every main-paper and appendix figure.

## Step 03 Generation Order
1. preflight
2. extract confirmed data
3. generate quantitative figures
4. generate conceptual figures one at a time
5. re-upload generated PNGs if the packaging workspace cannot access them
6. assemble hybrid figures and paper
7. optionally compile
8. create manifest and ZIP

## Handoff Rules for Step 03
- The complete embedded paper source, reference board, plan, prompts, briefs and data authority sections are the sole input.
- Step 03 must not ask for earlier text files again.
```

Update the top workflow stage to `drawing_02_plan_complete`.

Deliver only the complete updated `figure_workflow.md`.
