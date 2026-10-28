# Prompt 03: Generate Figures and Submission Package

Task:

Read:

```text
papers/<paper_name>/input.tex
papers/<paper_name>/figure_plan.md
papers/<paper_name>/figure_prompts.md
refs/
```

Now generate the paper figures and final submission package.

## Core rule

Use the rendering method specified by `figure_plan.md`.

Do not use a single renderer for every figure.

The rendering policy is:

1. Conceptual schematic figures must be generated as raster images with the
   Codex imagegen/image2 tool. The final paper may wrap the raster PNG in a PDF,
   but the conceptual visual content must not be a matplotlib/TikZ/vector
   drawing.
2. Quantitative data figures must be generated from explicit numeric data using a reproducible plotting workflow.
3. Hybrid figures must keep conceptual panels and quantitative panels separate unless they can be combined without changing the numeric content; every conceptual panel must be generated with image2.
4. Tables must be rendered as LaTeX tables, not figure images.
5. If required numeric data is missing, do not invent a fake plot. Create a data requirement file and a clear placeholder in the paper.

## Rendering policy

### A. Conceptual schematic figures

Use the Codex imagegen/image2 tool for all figures classified as:

```text
conceptual_schematic
```

Examples:

* problem-solution hero figure
* method overview
* system architecture
* runtime controller
* state-machine explanation
* mechanism illustration

For these figures:

* find the figure-specific section in `figure_plan.md`
* read `figure_prompts.md` and the matching `drafts/figureX_brief.md`
* use the full figure-specific prompt and negative prompt as the source of truth
* inspect the sampled local reference screenshots named in the prompt/brief and use them only as visual inspiration
* generate the complete visible figure as a raster PNG with imagegen/image2
* keep labels concise
* avoid dense small text
* use clean academic style
* make the result readable in two-column format
* save the raw imagegen/image2 PNG output in `drafts/`
* wrap the PNG to PDF if needed for LaTeX submission

The imagegen/image2 output is the figure, not a background layer. Do not use
matplotlib, TikZ, PIL, Figma/Illustrator, or Python-drawn text to overlay the
main labels, titles, callouts, arrows, or module names onto an imagegen/image2
conceptual figure. Post-processing for conceptual figures is limited to
non-semantic operations: crop, resize, color-profile conversion, compression,
and PNG-to-PDF wrapping. If exact numeric axes, tables, or many precise values
are required, make the figure a quantitative data figure or table instead.

Do not substitute matplotlib patches, TikZ/PGF, hand-designed vector output,
Figma/Illustrator-style drawings, or Python-drawn boxes for imagegen/image2.
If imagegen/image2 cannot be used in the current thread, stop, write the
missing renderer reason, and do not create a fake conceptual PDF with another
renderer.

Do not use a generic "technology", "AI system", "chip", "cloud", "dashboard",
or stock illustration prompt. The raw image2 output must reflect the target
figure's planned layout and paper-specific mechanism from `figure_plan.md`.
If `figure_prompts.md` is missing because this paper was imported from an older
pipeline, create it from `figure_plan.md`, the target `drafts/figureX_brief.md`,
`input.tex`, and the reference-board notes before generating conceptual
figures.

If the existing plan only has a short image2 prompt summary, first expand it
into a full prompt from the target figure section, save that prompt into
`figure_prompts.md` and the brief, and only then call imagegen/image2.

### B. Quantitative data figures

Use reproducible plotting for figures classified as:

```text
quantitative_data_figure
```

Examples:

* main result
* latency / throughput / memory / energy comparison
* quality / accuracy / perplexity comparison
* Pareto frontier
* ablation
* sensitivity
* robustness
* calibration / reliability diagram
* risk-coverage curve
* latency breakdown
* heatmap
* CDF
* distribution plot

Allowed plotting methods:

* matplotlib
* PGFPlots
* R / ggplot
* MATLAB
* Origin
* equivalent reproducible plotting workflow

Default preference:

* use CSV or extracted numeric tables as the data source
* generate vector PDF when possible
* save plotting scripts under `papers/<paper_name>/scripts/`
* save normalized data files under `papers/<paper_name>/results/`
* save generated plot PDFs under `papers/<paper_name>/drafts/`

Do not use image2 for quantitative axes, curves, bars, scatter points, heatmaps, error bars, Pareto frontiers, reliability diagrams, or other numeric graphics.

### C. Hybrid figures

For figures classified as:

```text
hybrid_figure
```

Use this strategy:

* generate conceptual panel(s) with image2
* generate quantitative panel(s) from data using reproducible plotting
* combine panels using LaTeX subfigures or another reproducible layout method
* do not let image2 draw the quantitative panels
* preserve exact numeric labels from the data source

If the hybrid figure's conceptual panel is the main visual panel, image2 is not
optional; generate that panel with image2 and keep the raw image2 output.

### D. Tables

If `figure_plan.md` says an item should be a table:

* write it as a LaTeX `table` environment
* do not wrap tabular content inside a figure
* do not export tables as images unless the target venue requires it

## Step 1: Create working folders

Create or update:

```text
papers/<paper_name>/drafts/
papers/<paper_name>/results/
papers/<paper_name>/scripts/
papers/<paper_name>/submit/
```

## Step 2: Create figure briefs

For each main-paper figure in `figure_plan.md`, save a short design brief:

```text
papers/<paper_name>/drafts/figure1_brief.md
papers/<paper_name>/drafts/figure2_brief.md
...
```

Each brief must include:

* figure title
* visual thesis
* figure type:

  * conceptual_schematic
  * quantitative_data_figure
  * hybrid_figure
  * table
  * placeholder_data_figure
* renderer:

  * image2
  * matplotlib / PGFPlots / other plotting tool
  * LaTeX table
  * mixed
* panel layout
* reference figure style
* required elements
* forbidden elements
* data source
* CSV schema if applicable
* axes and units if applicable
* methods / models / datasets / devices shown
* caption draft
* validation checklist

For conceptual figures, include:

* imagegen/image2 prompt
* imagegen/image2 negative prompt
* sampled reference screenshot paths used for visual inspiration
* raw imagegen/image2 PNG output path

The imagegen/image2 prompt in the brief must be the actual prompt used, not a one-line
summary. Keep it stable so later single-figure revisions can reuse it.
For conceptual figures, the brief must state that no visible labels are added by
code after generation unless the operator explicitly overrides this rule.

For quantitative data figures, include:

* data source path
* plotting script path
* output PDF path
* exact metrics and units
* expected plot type
* legend entries
* whether error bars or confidence intervals are required

## User-confirmed data authority (highest priority)

Numeric values in `input.tex`, existing LaTeX tables, and `CONFIRMED_DATA`
blocks marked with `DATA_STATUS: USER_CONFIRMED_REAL` are authoritative real
experimental data confirmed by the user before writing Step 6. Apply these
rules before creating any data requirement or waiting-data result:

* A missing standalone CSV is not missing experimental data when all values
  required by the figure are already present in `input.tex` or a confirmed data
  block.
* In that case, extract the values losslessly, create the required CSV under
  `papers/<paper_name>/results/`, generate the figure, and continue the package.
* Preserve exact values, units, method labels, row order where meaningful, and
  comparison relationships. Do not recompute, smooth, beautify, replace, or
  invent values.
* Do not add an entry to `data_requirements.md` or the submission manifest
  solely because an independent CSV, raw log, or auxiliary file was absent.
* If Step 02 incorrectly treated a standalone CSV as an external prerequisite
  even though the confirmed values are present, correct the plan/manifest and
  use the extracted CSV.
* Request data only for numeric fields genuinely absent from both `input.tex`
  and existing result files. List only those absent fields; never invalidate or
  delete the confirmed data that is already available.

## Step 3: Prepare data for quantitative figures

For each quantitative or hybrid figure:

1. Check whether the required data exists in:

   * `input.tex`
   * existing LaTeX tables
   * `papers/<paper_name>/results/`
   * other explicitly referenced result files

2. If data exists:

   * extract or normalize it into a CSV file under:

```text
papers/<paper_name>/results/
```

3. If data does not exist:

   * create a CSV schema file with only headers and comments if possible
   * create or update:

```text
papers/<paper_name>/results/data_requirements.md
```

This file must state:

* missing figure number
* missing metrics
* required columns
* required methods / models / workloads / devices
* expected units
* why the data is needed

4. Do not invent missing experimental values.

## Step 4: Generate figures

### For conceptual schematic figures

* generate a complete visible raster PNG with imagegen/image2
* save raw output in `drafts/`
* wrap the PNG to PDF if needed
* do not overlay text, arrows, module labels, or callouts by code after image generation
* keep final figure filename stable, for example:

```text
figure1_hero.pdf
figure2_method.pdf
```

### For quantitative data figures

* generate using the selected reproducible plotting method
* save the plotting script under:

```text
papers/<paper_name>/scripts/
```

* save the data file under:

```text
papers/<paper_name>/results/
```

* save the final plot PDF under:

```text
papers/<paper_name>/drafts/
```

* ensure the figure has:

  * readable axis labels
  * units
  * legend
  * clear method names
  * correct numeric values
  * no invented data
  * no image2-rendered numeric plot content

Recommended plot outputs:

```text
figure3_main_results.pdf
figure4_pareto_tradeoff.pdf
figure5_ablation.pdf
figure6_sensitivity.pdf
figure7_case_study.pdf
```

### For hybrid figures

* generate each panel according to its type
* combine panels reproducibly
* save the combined PDF under `drafts/`
* keep panel labels such as `(a)`, `(b)`, `(c)` clear and consistent

### For placeholder data figures

If required data is missing:

* do not generate a fake numeric plot
* create the data schema and missing-data report
* insert a clear LaTeX placeholder or comment in the paper
* report the missing data in the final report

## Step 5: Create submit folder

Copy final figure PDFs into:

```text
papers/<paper_name>/submit/
```

Create a modified LaTeX file:

```text
papers/<paper_name>/submit/<paper_name>.tex
```

This `.tex` file should be based on `input.tex`, but with generated figures inserted using local filenames.

Example for figure:

```tex
\begin{figure}[t]
    \centering
    \includegraphics[width=\linewidth]{figure1_hero.pdf}
    \caption{...}
    \label{fig:hero}
\end{figure}
```

Example for table:

```tex
\begin{table}[t]
    \centering
    \caption{...}
    \label{tab:main}
    \begin{tabular}{...}
    ...
    \end{tabular}
\end{table}
```

Rules:

* Do not wrap numeric tables inside figure environments.
* Do not insert image2-generated fake plots for quantitative result figures.
* All figures and tables must be referenced in the text.
* Captions must accurately describe the visual content.
* A caption must not claim Pareto / calibration / tradeoff / result if the visual is only a schematic.

## Step 6: Compile

Compile the modified LaTeX file after all figures and tables are inserted.

The compiled paper PDF is a required output and must be placed in:

```text
papers/<paper_name>/submit/<paper_name>.pdf
```

Run enough LaTeX passes to resolve normal cross-references.

Keep auxiliary build files out of the submit folder unless they are strictly required for compilation.

If compilation fails:

* fix the issue if it is caused by figure insertion, table insertion, paths, package conflicts, or packaging
* if it is caused by an unrelated paper-source error that cannot be fixed safely, report the exact error
* still keep the `.tex`, figure files, scripts, data schemas, and available outputs in `submit/`

## Step 7: Final output structure

The final structure should be:

```text
papers/<paper_name>/submit/
|-- <paper_name>.tex
|-- <paper_name>.pdf
|-- submission_manifest.json
|-- figure1_xxx.pdf
|-- figure2_xxx.pdf
|-- figure3_xxx.pdf
|-- figure4_xxx.pdf
|-- figure5_xxx.pdf
`-- figure6_xxx.pdf
```

Additional reproducibility files should remain in:

```text
papers/<paper_name>/results/
papers/<paper_name>/scripts/
papers/<paper_name>/drafts/
```

Do not put unnecessary auxiliary files in `submit/`.

## Step 8: Write submission manifest

Create:

```text
papers/<paper_name>/submit/submission_manifest.json
```

The manifest must be valid JSON and should use this shape:

```json
{
  "paper_id": "<paper_name>",
  "source_tex": "papers/<paper_name>/input.tex",
  "figure_plan": "papers/<paper_name>/figure_plan.md",
  "submit_tex": "papers/<paper_name>/submit/<paper_name>.tex",
  "compiled_pdf": "papers/<paper_name>/submit/<paper_name>.pdf",
  "compiled_pdf_created": true,
  "figures": [
    {
      "figure": "Figure 1",
      "label": "fig:hero",
      "figure_type": "conceptual_schematic",
      "renderer": "image2",
      "raw_output": "papers/<paper_name>/drafts/figure1_hero.png",
      "final_pdf": "papers/<paper_name>/submit/figure1_hero.pdf",
      "inserted_in_tex": true,
      "caption": "..."
    }
  ],
  "tables": [],
  "data_requirements": [],
  "compile_status": "success | failed",
  "compile_error": "",
  "created_at": "<ISO-8601 timestamp>"
}
```

For every final figure PDF in `submit/`, include one `figures[]` entry. If a
figure is a placeholder because data is missing, record that explicitly in
`data_requirements[]` and set `inserted_in_tex` according to what was actually
inserted.

## Step 9: Report

Before finishing, validate the renderer manifest:

* every `conceptual_schematic` entry must have `renderer: "image2"`
* every conceptual or hybrid conceptual panel must have a non-empty raw image2 PNG output path
* no conceptual figure may list "hand-designed vector", "matplotlib hand-designed vector", TikZ/PGF, Figma/Illustrator, or Python-drawn boxes as its renderer
* no conceptual figure may have a PDF-only raw output

After finishing, report:

* which figures were generated
* which figures are conceptual
* which figures are quantitative data figures
* which figures are hybrid
* which items were converted from figure to table
* which figures are placeholders due to missing data
* where each quantitative figure gets its data
* which plotting scripts were created
* which image2 outputs were used
* whether any raster-to-PDF wrapping was needed
* whether any required data was missing
* exact contents of the submit folder
* the path to `submission_manifest.json`
* confirmation that the compiled paper PDF was created, or the exact compilation error if not

## Non-negotiable rules

* Do not use image2 for quantitative data plots.
* Do use image2 for conceptual schematic figures and conceptual panels in hybrid figures.
* Do not draw numeric axes, bars, curves, scatter points, heatmaps, error bars, Pareto frontiers, or reliability diagrams with image2.
* Do not replace image2-required conceptual figures with matplotlib/TikZ/hand-designed vector diagrams.
* Do not invent experimental values.
* Do not disguise tables as figures.
* Do not keep a figure whose caption claims a data result but whose content is only a schematic.
* Do not replace necessary result figures with workflow diagrams.
* Conceptual figures explain the method; quantitative figures prove the method.
* A submission-style empirical paper must contain both.
