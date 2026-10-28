# Prompt 02: Make Figure Plan — ChatGPT Web Version

## Before you start

Use this prompt in ChatGPT on the web. Attach the target paper's `input.tex`, or make it available in the current ChatGPT Project. When available, also provide the Step 01 outputs—`reference_board.md`, `notes.md`, both manifests, and the actual reference images—or paste their complete contents into the chat.

Before planning figures, uniquely confirm `paper_name` from both:

1. the uploaded/Project file name or the enclosing paper identifier supplied by the user; and
2. the title, author block, project name, or other identifying content inside `input.tex`.

If `input.tex` is missing, inaccessible, empty, or there is more than one plausible paper, stop immediately. Do not infer a plan or create partial deliverables. Reply only with a concise list of the missing or ambiguous inputs. If the paper itself is clear but its archive-safe `paper_name` is not, ask the user to provide the exact `paper_name`.

## ChatGPT web execution contract

- You may read only files attached in the current chat, files available to the current ChatGPT Project, and text pasted into the conversation. Do not claim to inspect a computer, terminal, repository, or unshared directory.
- Paths such as `papers/<paper_name>/...` and `refs/<field_name>/...` are **logical archive paths** for organizing deliverables. They are not evidence that you read from or wrote to the user's machine.
- Do not automatically traverse `papers/`, `refs/`, or any other directory. A named file is usable only if its contents are actually available in this chat or Project.
- Never say that a file was saved on the user's device, written to a desktop path, or verified on disk. State exactly which downloadable files ChatGPT actually created.
- Prefer creating complete downloadable text files with the exact filenames below and packaging them as `figure_plan_<paper_name>.zip` with the required logical directory structure.
- If this ChatGPT session cannot create downloadable attachments or a ZIP, provide the complete content of every required text file in its own fenced code block. Put its exact logical archive path immediately above the block. Do not use ellipses, summaries, or “same as above” in place of file content.
- Do not claim that a reference image, PDF page, existing figure asset, or plot was inspected unless it was actually opened and visually inspected in this session.
- Do not claim that Data Analysis / Python, image generation, plotting, compilation, or file generation ran unless the corresponding action actually completed in this session.

## Task

Read:

```text
papers/<paper_name>/input.tex
```

Here and below, that is a logical archive path; use the supplied `input.tex` content.

Also read any relevant Step 01 material that is actually attached or Project-accessible:

```text
refs/<field_name>/notes.md
refs/<field_name>/images_manifest.md
refs/<field_name>/images/
papers/<paper_name>/reference_board.md
papers/<paper_name>/reference_images_manifest.md
```

Do **not** generate final figures in this step.

Create a publication-quality figure plan for an AI or ML systems paper and produce:

```text
papers/<paper_name>/figure_plan.md
papers/<paper_name>/figure_prompts.md
papers/<paper_name>/drafts/figure1_brief.md
papers/<paper_name>/drafts/figure2_brief.md
...
```

Create one complete brief for every proposed figure, including appendix figures and placeholder data figures. Keep numbering and filenames consistent across all outputs.

## Step 01 availability and fallback

Step 01 is optional for closely related papers when the user has supplied a sufficiently populated shared field board.

If `papers/<paper_name>/reference_board.md` is unavailable, do not stop. Instead:

1. infer the closest field from `input.tex`;
2. use any actually supplied `refs/<field_name>/notes.md` and `refs/<field_name>/images_manifest.md`;
3. use only reference images that are actually attached/Project-accessible and that you can inspect;
4. record in both `figure_plan.md` and `figure_prompts.md` that Step 02 reused a shared reference board rather than a paper-specific Step 01 board;
5. if no close field material is supplied, proceed from `input.tex` only and write exactly: `No reference screenshots were provided or inspected.`

Missing screenshots are non-blocking. A manifest entry or archive path does not prove that an image is available. If a listed screenshot is not actually accessible, state that it was not inspected and do not describe its colors, layout, panels, or other visual properties as observed facts.

Use reference screenshots only as inspiration for visual hierarchy, pacing, and layout. Do not copy, trace, or directly reuse their content in planned final figures.

## Core goal

Create a balanced figure structure that supports the paper's complete evidence chain. The goal is not to maximize the number of schematic diagrams. The plan must help a reviewer answer:

- What problem does the paper solve?
- What is the proposed method?
- What is the main empirical evidence?
- What tradeoff does the method improve?
- Which modules matter?
- Under what conditions is the method robust or sensitive?
- What should reviewers visually remember?

## Rendering policy

Classify every planned visual as exactly one of the following types before selecting its renderer.

### 1. `conceptual_schematic`

Use for:

- problem–solution teaser;
- method overview;
- system architecture;
- runtime controller;
- workflow or state-machine explanation;
- qualitative mechanism illustration.

Required renderer:

- **ChatGPT Image Generation**

The prompt must be ready to paste into ChatGPT's web image-generation interface. Use short, unambiguous in-image labels, preferably one to four words each. Keep the number of labels small enough for reliable rendering.

Do not recommend Illustrator, Figma, hand-designed vector figures, TikZ, PGF/TikZ, or matplotlib-drawn boxes for conceptual schematics unless the user explicitly overrides this policy for the specific paper.

The visible conceptual content, objects, relationships, arrows, short labels, and callouts must be generated together as one original raster image. Do not plan a workflow in which Python, matplotlib, TikZ, SVG, or another program later overlays or replaces semantic labels, arrows, boxes, callouts, or meanings. Later processing may only wrap, crop, resize, or convert the unchanged raster image for LaTeX. If a label is wrong or illegible, regenerate the image rather than planning a code-based semantic repair.

### 2. `quantitative_data_figure`

Use for:

- main results;
- latency, throughput, memory, or energy plots;
- quality, accuracy, or perplexity plots;
- Pareto frontier;
- ablation study;
- sensitivity or robustness analysis;
- calibration or reliability diagram;
- risk–coverage curve;
- breakdown plot;
- heatmap or distribution plot.

Required renderer:

- **Data Analysis / Python** from explicit numeric data by default; or
- PGFPlots, R/ggplot, MATLAB, Origin, or another reproducible data workflow when justified.

Never use ChatGPT Image Generation to draw quantitative axes, bars, curves, scatter points, heatmaps, error bars, Pareto frontiers, reliability diagrams, numeric legends, or other evidence-bearing chart content. Every plotted value must trace to explicit numeric data in `input.tex`, an authoritative supplied table, a `CONFIRMED_DATA` block, or a specified result file.

### 3. `hybrid_figure`

Use only when a figure genuinely needs both:

- one conceptual panel; and
- one or more quantitative result panels.

Required structure:

- generate the conceptual panel separately with **ChatGPT Image Generation**;
- generate each quantitative panel separately with **Data Analysis / Python** or another reproducible plotting workflow from explicit numeric data;
- combine the completed panels using LaTeX subfigures or another reproducible layout method;
- do not ask ChatGPT Image Generation to invent or render any numeric chart;
- do not overlay code-generated semantic content onto the conceptual panel.

The plan must identify each panel, renderer, input, output filename, and combination order separately.

### 4. `table`

Use for:

- exact numeric comparison;
- experimental setup;
- hyperparameters;
- benchmark configuration;
- ablation values better read as exact entries.

Required renderer:

- **LaTeX table**

If a proposed visual is mainly tabular, recommend a LaTeX table instead of an image or chart.

### 5. `placeholder_data_figure`

Use only when the paper clearly needs a data figure but the required numeric values are not available.

In this case:

- do not invent values;
- do not generate or request a fake plot;
- assign the stable renderer label `placeholder_missing_data` until the required numeric data is supplied;
- specify the exact required CSV schema;
- specify the intended plot type;
- specify axes, metrics, methods, units, and expected interpretation;
- state exactly which required fields are missing.

## 1. Paper visual summary

In `figure_plan.md`, summarize:

- paper topic;
- core problem;
- core contribution;
- method mechanism;
- experimental claims;
- what reviewers should visually remember;
- what makes this paper different from prior work;
- what evidence the figures must provide.

Separate claims directly supported by `input.tex` from planning recommendations. Do not upgrade speculative language into a confirmed experimental claim.

## 1A. Reference screenshot audit

Audit the reference materials actually available in this session before proposing figures.

If more than 12 screenshots are accessible, select a diverse paper-specific subset of 6–10 images from different source papers and visual patterns. Record the chosen subset in both `figure_plan.md` and `figure_prompts.md`. These images are style and layout inspiration only; never copy their content.

Create a table with:

- logical archive path or attachment name;
- actual inspection status: `inspected`, `available but not inspected`, or `listed but unavailable`;
- source paper;
- source figure number or page;
- visual pattern, only when verified;
- useful design lesson, clearly marked as observed or inferred;
- element to avoid copying;
- which proposed figure it may inform.

If no screenshot was actually inspected, write exactly:

```text
No reference screenshots were provided or inspected.
```

Then explain whether the plan uses `refs/<field_name>/notes.md`, `papers/<paper_name>/reference_board.md`, and/or `input.tex`. Do not pretend to have viewed a screenshot merely because a manifest describes it. Missing screenshots must not block the plan.

## 2. Audit existing figures and tables

Inspect every `figure`, `figure*`, `table`, and `table*` environment in the supplied `input.tex`. Also inspect relevant `\input{...}` or `\include{...}` content only if those files are supplied and accessible.

When a TeX environment references an external image or data file that was not supplied, audit the caption, label, commands, and declared structure, but explicitly mark the underlying visual asset as `not inspected`.

Create an audit table with:

- current number or label;
- caption;
- current content type;
- evidence inspected: TeX only, source data, and/or actual visual asset;
- whether it is conceptual, quantitative, hybrid, table-like, or placeholder;
- whether it should be kept, revised, converted to a LaTeX table, moved to the appendix, or replaced;
- what reviewer question it answers;
- what reviewer question it fails to answer;
- recommended action.

Flag every case where:

- a figure is actually a table;
- a caption claims a Pareto result, calibration, tradeoff, result, or sensitivity analysis while the visible/declared content is only text boxes or a schematic;
- too many main-paper figures are conceptual;
- a necessary data figure is missing;
- an asset could not be inspected and therefore requires later verification.

## 3. Proposed figure list

For every proposed main-paper and appendix figure, include:

- figure number;
- figure name;
- main purpose;
- reviewer question it answers;
- visual thesis;
- exactly one figure type:
  - `conceptual_schematic`;
  - `quantitative_data_figure`;
  - `hybrid_figure`;
  - `table`;
  - `placeholder_data_figure`;
- recommended visual form:
  - architecture diagram;
  - workflow diagram;
  - bar chart;
  - grouped bar chart;
  - line chart;
  - scatter or Pareto frontier;
  - heatmap;
  - reliability diagram;
  - risk–coverage curve;
  - latency breakdown;
  - timeline or Gantt chart;
  - CDF;
  - LaTeX table;
  - other, with justification;
- required renderer using the exact policy labels:
  - `ChatGPT Image Generation` for conceptual figures and conceptual panels;
  - `Data Analysis / Python` or another named reproducible data workflow for quantitative figures and panels;
  - `LaTeX table` for tables;
  - separate named renderers and assembly method for hybrid figures;
  - `placeholder_missing_data` for a data figure that cannot yet be rendered because required numeric fields are genuinely absent;
- required content or data source from `input.tex`;
- required external data file, if applicable;
- suggested CSV schema when data is unavailable;
- expected axes, metrics, methods, series, and legend;
- whether it belongs in the main paper or appendix;
- expected caption;
- validation checklist.

For a hybrid figure, provide the same information separately for every panel and state that the conceptual and quantitative panels must be produced independently before assembly.

## 3A. Complete prompts for conceptual visuals

For every `conceptual_schematic` and every conceptual panel in a `hybrid_figure`, include a complete, figure-specific prompt and a matching exclusion/negative prompt in both:

```text
papers/<paper_name>/figure_prompts.md
papers/<paper_name>/drafts/figureX_brief.md
```

Each image prompt must be detailed enough to generate the exact paper figure in ChatGPT on the web without later invention. It must specify:

- renderer: `ChatGPT Image Generation`;
- intended publication-quality academic figure style;
- canvas orientation, aspect ratio, and panel layout;
- visual objects and their relationships;
- information flow and arrow directions;
- what should appear as icons, abstract blocks, states, resources, or actors;
- the exact short in-image labels and callouts to render;
- a strict label budget and the instruction to render each label exactly once;
- which actually inspected reference screenshots provide high-level layout inspiration, if any;
- paper-specific colors and hierarchy, while avoiding imitation of a source figure;
- required exclusions;
- an explicit instruction that the image contain no invented numbers, axes, plots, table cells, citations, logos, watermarks, or decorative prose;
- an explicit instruction to generate the whole conceptual panel—including all semantic labels and callouts—as one raster image;
- an explicit instruction that no code-based semantic overlay or post-generation label replacement is planned.

The exclusion/negative prompt must reject at least:

- generic stock technology art;
- photorealistic devices or people;
- decorative 3D dashboards;
- glowing sci-fi interfaces;
- random hardware icons;
- unnecessary gradients, shadows, or textures;
- dense paragraphs and tiny text;
- invented metrics or numeric chart content;
- copied composition, shapes, or styling unique to a reference paper;
- meaningless arrows or disconnected boxes.

Use short labels, normally one to four words. When the paper requires exact numeric axes, data labels, table-like values, or long technical text, classify that visual as a quantitative data figure or LaTeX table rather than forcing it into image generation.

For each conceptual prompt, add a validation checklist that verifies label spelling, label count, object relationships, arrow directions, originality, lack of numeric-chart content, and legibility at the expected paper-column size. The remedy for incorrect semantic labels is regeneration, not a code overlay.

## 4. Main-paper figure structure

Choose a compact but evidence-complete main-paper figure set.

For AI, LLM inference, compression, quantization, or distillation papers, prefer a structure such as:

- Figure 1: problem–solution teaser or hero figure;
- Figure 2: method, system, or runtime overview;
- Figure 3: main quantitative result;
- Figure 4: latency–memory–quality tradeoff or Pareto frontier;
- Figure 5: ablation study;
- Figure 6: sensitivity, robustness, or multi-device behavior;
- Figure 7: optional case study, timeline, or failure analysis.

Adapt this structure to the actual evidence rather than forcing all seven figures.

Rules:

- The main paper should normally contain only **1–2 conceptual schematic figures**.
- At least half of the main-paper figures should be quantitative data figures or data-driven hybrid figures.
- Do not fill the main paper with only architecture, workflow, controller, state-machine, or boxed-text figures.
- Do not place exact numeric tables inside figure environments.
- If a LaTeX table communicates the evidence better, explicitly recommend a table.
- Figure 1 should normally be a hero or teaser, not a plain pipeline, unless the paper's contribution genuinely requires a pipeline-first story.

## 5. Appendix figures

Decide which figures belong in the appendix. Candidates include:

- additional datasets;
- extra calibration curves;
- extra latency plots;
- extended ablations;
- sensitivity analyses;
- implementation details;
- extended case studies;
- additional devices or workloads;
- failure analyses.

For each appendix item, explain why it is useful but not essential to the main narrative. Apply the same renderer and data-traceability policies as for main-paper figures.

## 6. Data availability and authority check

### User-confirmed data authority — highest priority

Treat numeric values in `input.tex`, existing supplied LaTeX tables, and `CONFIRMED_DATA` blocks marked with `DATA_STATUS: USER_CONFIRMED_REAL` as authoritative real experimental data. These values were confirmed by the user before the later writing workflow. Their authority does not depend on whether they differ from an earlier draft.

Apply these rules before deciding that data is missing:

- The absence of a standalone CSV, raw log, or auxiliary result file does not make confirmed values in `input.tex` incomplete or unverified.
- If `input.tex` contains enough confirmed values for a quantitative figure, classify the required data as available. Instruct the next generation step to extract those values losslessly into a CSV under the logical archive path `papers/<paper_name>/results/`.
- Preserve every value, unit, method label, row relationship, and comparison.
- Do not recompute, smooth, replace, reinterpret, normalize, or delete confirmed data unless the paper explicitly defines that transformation and the plan records it.
- Plan quantitative figures around the confirmed evidence that actually exists. Do not require unrelated new datasets merely because they would be desirable additions.
- Mark data as partial or missing only when a numeric field genuinely required by the selected figure is absent from both the supplied paper content and supplied result files. Name only the genuinely absent fields.
- A CSV produced by losslessly extracting confirmed tables or `CONFIRMED_DATA` is an output of the next generation step, not a prerequisite that can block Step 02.

For each `quantitative_data_figure` and each quantitative panel in a `hybrid_figure`:

- identify exactly where the numeric data appears in `input.tex`, using the table/section/label or quoted row identifier rather than an invented line number;
- identify whether an existing result file is required and whether it was actually supplied;
- specify the expected logical data filename;
- specify CSV columns and data types;
- specify required units;
- specify required methods, models, datasets, devices, and workloads;
- state whether data is `complete`, `partial`, or `missing`;
- name the renderer, preferably `Data Analysis / Python`;
- specify deterministic visual encodings, axes, scale, ordering, uncertainty representation, and legend;
- state every planned calculation or aggregation and verify that it is justified by the supplied data.

If any required data is missing or incomplete:

- say exactly what is missing;
- do not invent numbers or expected results;
- provide a complete CSV schema;
- classify the proposed visual as `placeholder_data_figure` until the data is supplied.

Do not use qualitative expectations such as “method A should be best” as numeric data.

## 7. Required file contents

### `papers/<paper_name>/figure_plan.md`

Include, in order:

1. confirmed paper identity and source-material inventory;
2. paper visual summary;
3. reference-board and screenshot audit;
4. existing figure/table audit;
5. proposed main-paper figure list;
6. proposed appendix figure list;
7. renderer assignment by figure and panel;
8. data availability matrix;
9. evidence-chain coverage check;
10. unresolved inputs and non-blocking limitations;
11. final validation checklist.

### `papers/<paper_name>/figure_prompts.md`

Include, in order:

1. global style rules for the paper;
2. originality and reference-use policy;
3. list of screenshots actually inspected and those merely listed but unavailable;
4. complete prompt and exclusion/negative prompt for every conceptual figure and conceptual hybrid panel;
5. quantitative figure specifications containing renderer, data source, schema, axes, metrics, units, series, legend, and validation rules—never an image-generation prompt for a numeric chart;
6. hybrid assembly instructions that keep conceptual and quantitative panels separate;
7. exact expected output filename for every future figure or panel.

### `papers/<paper_name>/drafts/figureX_brief.md`

Each brief must be self-contained and include:

- figure identity, placement, and visual thesis;
- reviewer question;
- figure type and exact renderer;
- source material and evidence trace;
- panel-by-panel content;
- complete image-generation prompt and negative prompt when conceptual content is present;
- complete data and plotting specification when quantitative content is present;
- LaTeX-table specification when the item is a table;
- exact CSV schema and missing fields when it is a placeholder;
- expected caption;
- output filenames;
- validation checklist;
- dependencies and unresolved inputs.

The next generation step must not need to invent a conceptual prompt, data source, metric, visual thesis, or panel relationship from scratch.

## 8. Rules

- Figure 1 should usually be a hero figure, teaser, or problem–solution overview.
- Figure 1 should not be a plain pipeline unless strongly justified by the paper.
- Avoid low-quality box-and-arrow diagrams.
- Every figure must serve the paper narrative and have a visual thesis.
- Every quantitative figure must have explicit metrics, axes, units, and a traceable data source.
- Every data-driven visual must trace to supplied `input.tex` content or a specifically named supplied/required result file.
- Do not generate final figure files in this step.
- Do not write full plotting code in this step.
- Do write complete ChatGPT Image Generation prompts for all conceptual figures and conceptual hybrid panels.
- Use `ChatGPT Image Generation` only for conceptual schematics and conceptual panels.
- Use `Data Analysis / Python` or another named reproducible plotting workflow only for quantitative figures and panels based on explicit numeric data.
- For hybrid figures, create conceptual and quantitative panels separately and combine them reproducibly.
- Do not plan Python, matplotlib, TikZ, SVG, or other code-based semantic overlays on conceptual images.
- Do not recommend hand-designed vector, Illustrator/Figma, TikZ, PGF/TikZ, or matplotlib-drawn boxes for conceptual schematics unless the user explicitly overrides this policy for the specific paper.
- Use LaTeX for tables.
- Do not propose a schematic when the paper needs a result plot.
- Do not disguise tables as figures.
- Do not invent reference-image observations, citations, numeric values, data files, or completed tool actions.

## 9. Final verification and delivery

Before finishing, verify from the content actually available and the files actually created in this session that:

- `paper_name` is identical across all logical archive paths and file contents;
- every proposed figure has exactly one type and an allowed renderer;
- every conceptual figure and conceptual hybrid panel has a complete web-ready prompt, negative prompt, short exact labels, and no planned code-based semantic overlay;
- every quantitative figure and quantitative hybrid panel uses explicit numeric data or is marked `placeholder_data_figure`;
- every hybrid figure separates its conceptual and quantitative panels;
- every table uses the LaTeX-table workflow;
- all supplied existing figure and table environments were audited;
- every claimed screenshot inspection actually occurred;
- missing screenshots are disclosed and did not block planning;
- main-paper conceptual figures normally number only 1–2;
- at least half of the planned main-paper figures are quantitative or data-driven hybrid figures, unless a paper-specific exception is explicitly justified;
- every figure has a matching, complete `drafts/figureX_brief.md`;
- filenames and numbering match across `figure_plan.md`, `figure_prompts.md`, and every brief;
- no final figure was generated in this step.

When attachment creation is available, deliver `figure_plan_<paper_name>.zip` containing the complete logical `papers/<paper_name>/...` structure. Also provide a concise inventory of the figure count by type, screenshots actually inspected, missing data, and non-blocking limitations.

If attachment or ZIP creation is unavailable, provide every required text file in full using the complete-code-block fallback from the execution contract. Do not claim that a ZIP or downloadable file exists unless it was actually generated.
