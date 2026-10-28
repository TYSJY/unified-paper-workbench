# Prompt 02: Make Figure Plan

Task:

Read:

```text
papers/<paper_name>/input.tex
```

Also read any paper-specific reference board if it exists:

```text
refs/
papers/<paper_name>/reference_board.md
papers/<paper_name>/reference_images_manifest.md
```

Step 01 is optional for closely related papers in an already-populated field.
If `papers/<paper_name>/reference_board.md` is missing, do not stop. Instead:

* infer the closest field from `input.tex`
* inspect existing `refs/<field_name>/notes.md` and `refs/<field_name>/images_manifest.md`
* select the most relevant existing `refs/<field_name>/images/` pool
* record in `figure_plan.md` and `figure_prompts.md` that Step 02 reused a shared reference pool rather than a paper-specific Step 01 board
* if no close field pool exists, proceed from `input.tex` only and explicitly record `No local reference screenshots found`

If Step 01 has been run, also inspect the local reference screenshots listed in:

```text
refs/<field_name>/images/
refs/<field_name>/images_manifest.md
```

Use these screenshots only as visual inspiration. Do not copy, trace, or
directly reuse any reference image content in the planned final figures.

If `reference_images_manifest.md` or local screenshots are missing, explicitly
state in `figure_plan.md` that Step 02 is running without paper-specific visual
screenshots and is relying on the TeX plus any existing text-only reference
notes. Do not invent reference screenshots.

Do not generate final figures yet.

Create:

```text
papers/<paper_name>/figure_plan.md
papers/<paper_name>/figure_prompts.md
papers/<paper_name>/drafts/figure1_brief.md
papers/<paper_name>/drafts/figure2_brief.md
...
```

## Core goal

Create a publication-quality figure plan for an AI / ML systems paper.

The goal is not to maximize the number of schematic diagrams.
The goal is to create a balanced figure structure that supports the paper's evidence chain:

* What problem does the paper solve?
* What is the proposed method?
* What is the main empirical evidence?
* What tradeoff does the method improve?
* Which modules matter?
* Under what conditions is the method robust or sensitive?
* What should reviewers visually remember?

## Rendering policy

Every figure must first be classified into one of the following types:

1. `conceptual_schematic`

   Use for:

   * problem-solution teaser
   * method overview
   * system architecture
   * runtime controller
   * workflow or state-machine explanation
   * qualitative mechanism illustration

   Required renderer:

   * image2

   Do not recommend Illustrator/Figma, hand-designed vector figures, TikZ,
   PGF/TikZ, or matplotlib-drawn boxes for conceptual schematics unless the
   user explicitly overrides this policy for a specific paper.

2. `quantitative_data_figure`

   Use for:

   * main results
   * latency / throughput / memory / energy plots
   * quality / accuracy / perplexity plots
   * Pareto frontier
   * ablation study
   * sensitivity analysis
   * robustness analysis
   * calibration / reliability diagram
   * risk-coverage curve
   * breakdown plot
   * heatmap or distribution plot

   Required renderer:

   * reproducible plotting from explicit numeric data
   * matplotlib, PGFPlots, R/ggplot, MATLAB, Origin, or equivalent

   Do not use image2 to draw quantitative axes, bars, curves, scatter points, heatmaps, error bars, Pareto frontiers, reliability diagrams, or other numeric plots.

3. `hybrid_figure`

   Use for figures that combine:

   * one conceptual panel
   * one or more quantitative result panels

   Required structure:

   * generate the conceptual panel separately with image2
   * generate quantitative panels from data
   * combine them with LaTeX subfigures or a reproducible layout method
   * do not let image2 invent or render numeric charts

4. `table`

   Use for:

   * exact numeric comparison
   * experimental setup
   * hyperparameters
   * benchmark configuration
   * ablation values better read as exact entries

   If a proposed figure is mostly tabular, recommend a table instead of a figure.

5. `placeholder_data_figure`

   Use only when the paper clearly needs a data figure but the numeric data is not yet available.

   In this case:

   * do not invent values
   * do not generate a fake plot
   * specify the required CSV schema
   * specify the intended plot type
   * specify the axes, metrics, methods, and expected interpretation

## 1. Paper visual summary

Summarize:

* paper topic
* core problem
* core contribution
* method mechanism
* experimental claims
* what reviewers should visually remember
* what makes this paper different from prior work
* what evidence the figures must provide

## 1A. Reference screenshot audit

Before proposing figures, audit the available reference screenshots.

If there are more than 12 local reference screenshots, select a diverse
paper-specific subset of 6-10 screenshots for this paper. The subset should be
chosen from different source papers and visual patterns. Record the chosen
subset in both `figure_plan.md` and `figure_prompts.md`. These screenshots are
style/layout inspiration only; do not copy their content.

Create a short table with:

* local screenshot path
* source paper
* source figure number or page
* visual pattern
* useful design lesson
* element to avoid copying
* which proposed figure it may inform

If no screenshots are available, write `No local reference screenshots found`
and explain whether Step 02 is using only `refs/<field_name>/notes.md`,
`papers/<paper_name>/reference_board.md`, and `input.tex`.

## 2. Figure audit of existing figures

Before proposing new figures, inspect all existing `figure` and `table` environments in `input.tex`.

Create an audit table with:

* current number or label
* caption
* current content type
* whether it is conceptual, quantitative, hybrid, table-like, or placeholder
* whether it should be kept, revised, moved to table, moved to appendix, or replaced
* what reviewer question it answers
* what reviewer question it fails to answer
* recommended action

Flag any case where:

* a figure is actually a table
* a figure caption claims Pareto / calibration / tradeoff / result / sensitivity but the visual content is only text boxes or a schematic
* too many main-paper figures are conceptual
* a necessary data figure is missing

## 3. Proposed figure list

For each proposed figure, include:

* figure number
* figure name
* main purpose
* reviewer question it answers
* visual thesis
* figure type:

  * conceptual_schematic
  * quantitative_data_figure
  * hybrid_figure
  * table
  * placeholder_data_figure
* recommended visual type:

  * architecture diagram
  * workflow diagram
  * bar chart
  * grouped bar chart
  * line chart
  * scatter / Pareto frontier
  * heatmap
  * reliability diagram
  * risk-coverage curve
  * latency breakdown
  * timeline / Gantt chart
  * CDF
  * table
  * other
* required renderer:

  * image2 for conceptual figures and conceptual panels
  * reproducible plotting for quantitative figures
  * LaTeX table for tables
  * mixed rendering for hybrid figures, with image2 for conceptual panels and reproducible plotting for quantitative panels
* required content or data source from `input.tex`
* required external data file if applicable
* suggested CSV schema if the data is not yet available
* expected axes, metrics, methods, series, and legend
* whether it belongs in the main paper or appendix
* expected caption
* validation checklist for this figure

For every `conceptual_schematic`, and for every conceptual panel in a
`hybrid_figure`, include a full figure-specific imagegen/image2 prompt and a
negative prompt in `figure_prompts.md` and the matching
`drafts/figureX_brief.md`. The prompt must be detailed enough to generate that
exact paper figure without relying on later invention. It must specify:

* the intended academic figure style
* panel layout
* visual objects and relationships
* what should be shown as icons or abstract blocks
* the short in-image labels and callouts that imagegen/image2 should render
* which sampled reference screenshots provide visual inspiration
* required exclusions, including generic stock technology art, photorealistic
  devices, decorative 3D dashboards, glowing sci-fi UI, random hardware icons,
  and any numeric chart content

For conceptual figures, do not plan a split workflow where imagegen/image2
draws a background and Python, matplotlib, TikZ, or another tool overlays the
visible labels afterward. If a figure is assigned to imagegen/image2, the
visible conceptual figure, labels, and callouts must be generated as one raster
image. Later code may only wrap, crop, resize, or convert that PNG to PDF for
LaTeX. If exact numeric axes, data labels, or table-like values must be
guaranteed, classify that visual as a quantitative data figure or table instead
of an imagegen/image2 conceptual figure.

## 4. Main-paper figure structure

Choose a compact but evidence-complete main-paper figure set.

For AI / LLM inference / compression / quantization / distillation papers, prefer a structure like:

* Figure 1: problem-solution teaser or hero figure
* Figure 2: method / system / runtime overview
* Figure 3: main quantitative result
* Figure 4: latency-memory-quality tradeoff or Pareto frontier
* Figure 5: ablation study
* Figure 6: sensitivity / robustness / multi-device behavior
* Figure 7: optional case study, timeline, or failure analysis

Rules:

* Main paper should normally contain only 1–2 conceptual schematic figures.
* At least half of the main-paper figures should be quantitative data figures or data-driven hybrid figures.
* Do not fill the main paper with only architecture, workflow, controller, state-machine, or boxed-text figures.
* Do not put exact numeric tables inside figure environments.
* If a table is more appropriate than a figure, explicitly recommend a table.

## 5. Appendix figures

Decide which figures should go to the appendix.

Appendix figures may include:

* extra datasets
* extra calibration curves
* extra latency plots
* extra ablations
* extra sensitivity analysis
* implementation details
* extended case studies
* additional device or workload results

## 6. Data availability check

### User-confirmed data authority (highest priority)

Treat numeric values in `input.tex`, existing LaTeX tables, and
`CONFIRMED_DATA` blocks marked with `DATA_STATUS: USER_CONFIRMED_REAL` as
authoritative real experimental data. These values were confirmed by the user
before the writing workflow entered Step 6. Their validity does not depend on
whether they differ from an earlier draft.

Apply these rules before deciding that data is missing:

* The absence of a standalone CSV, raw log, or auxiliary result file does not
  make confirmed values in `input.tex` incomplete or unverified.
* If `input.tex` contains enough confirmed values for a quantitative figure,
  classify the data as available and instruct Step 03 to extract those values
  losslessly into a CSV under `papers/<paper_name>/results/`.
* Preserve every value, unit, method label, row relationship, and comparison.
  Do not recompute, smooth, replace, reinterpret, or delete confirmed data.
* Plan quantitative figures around the confirmed evidence that actually
  exists. Do not require unrelated new datasets merely because they would be
  desirable additions to the paper.
* Mark data as partial or missing only when a numeric field genuinely required
  by the selected figure is absent from both `input.tex` and existing result
  files. State only the genuinely absent fields.
* A CSV produced by extracting confirmed tables or `CONFIRMED_DATA` is an
  output of Step 03, not a prerequisite that can block Step 02.

For each quantitative data figure or hybrid figure:

* identify where the numeric data appears in `input.tex`
* identify whether an existing results file is needed
* specify the expected data filename
* specify the CSV columns
* specify required units
* specify which methods, models, datasets, devices, or workloads must appear
* state whether the data is complete, partial, or missing

If data is missing or incomplete:

* say exactly what is missing
* do not invent missing numbers
* propose a CSV schema
* mark the figure as `placeholder_data_figure`

## 7. Output requirements

Write the final plan to:

```text
papers/<paper_name>/figure_plan.md
papers/<paper_name>/figure_prompts.md
papers/<paper_name>/drafts/figureX_brief.md
```

The plan and prompts must be actionable by the next generation step. Step 03
must not need to invent a conceptual image prompt from scratch.

## Rules

* Figure 1 should usually be a hero figure / teaser / problem-solution overview.
* Figure 1 should not be a plain pipeline unless strongly justified.
* Avoid low-quality box-and-arrow diagrams.
* Every figure must serve the paper narrative.
* Every figure must have a visual thesis.
* Every quantitative figure must have explicit metrics, axes, and data source.
* Every data-driven figure must be traceable to `input.tex` or a specified results file.
* Do not generate final figure files in this step.
* Do not write full plotting code in this step.
* Do write full imagegen/image2 prompts for conceptual figures and conceptual panels.
* Conceptual schematic figures must specify `image2` as the renderer.
* Hybrid figures must specify `image2` for conceptual panels and reproducible plotting for quantitative panels.
* Do not use image2 for quantitative plots.
* Do not plan Python/matplotlib/TikZ text overlays on top of imagegen/image2 conceptual figures.
* Do not recommend hand-designed vector, Illustrator/Figma, TikZ, PGF/TikZ, or matplotlib-drawn boxes for conceptual schematics unless the user explicitly overrides this policy for a specific paper.
* Do not propose a schematic when the paper needs a result plot.
* Do not disguise tables as figures.
