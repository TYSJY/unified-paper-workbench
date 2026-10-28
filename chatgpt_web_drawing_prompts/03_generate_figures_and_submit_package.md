# ChatGPT Web Prompt 03: Generate the Figures and Build the Submission Package

## Before you start

This prompt is designed for the ChatGPT web interface. ChatGPT cannot browse arbitrary folders on your computer or resolve a path merely because that path appears in a document. Before running this prompt, upload the required files to this conversation, or make sure they are available and readable in the current ChatGPT Project.

Required core inputs:

- `input.tex`
- `figure_plan.md`

Required when already available:

- `figure_prompts.md`
- existing `figureN_brief.md` files
- reference-board notes and the specific reference screenshots named by the plan, prompt, or brief
- existing result files, CSV files, logs, tables, scripts, and figures referenced by the plan

If the paper uses additional LaTeX source files, bibliography files, style files, class files, or assets needed to understand or compile `input.tex`, upload those as well. If the expected paper name is not explicit in the attachments, state it in the same message as the uploads.

Paths such as `papers/<paper_name>/input.tex`, `papers/<paper_name>/drafts/`, and `refs/` are **logical package paths only**. They describe how files should be named and arranged in the downloadable deliverables. They do not grant ChatGPT access to any folder on the user's computer. Never claim to have discovered, read, created, copied, or modified a local path that was not supplied as an attachment or made accessible in the current Project.

## ChatGPT web execution contract — highest priority

The following rules override any later instruction that assumes direct computer, folder, renderer, or compiler access.

1. Use only files actually readable from the current conversation attachments or the current ChatGPT Project. Do not search, traverse, or infer the contents of the user's local directories.
2. Determine exactly one `paper_name` before doing any work. Confirm it from the uploaded `input.tex`, the actual attachment names, an explicit user statement, and any unambiguous identifiers inside the supplied files. All available evidence must agree. Do not invent a name from the paper title, today's date, a guessed folder, or a generic placeholder.
3. If there is more than one plausible paper, more than one unmatched `input.tex`, conflicting identifiers, or no unique `paper_name`, stop and list the exact ambiguity. Ask the user to identify the intended `input.tex` and `paper_name`.
4. If either required core input is missing or unreadable, stop before generating figures. List every missing or unreadable file by exact name in one concise preflight report. Do not silently continue with assumptions.
5. Missing experimental values are handled by the missing-data schema and placeholder workflow below; they do not authorize invented values. Missing files that are essential to interpret the plan, reproduce an existing result, or compile the paper must be named explicitly.
6. Treat all output paths below as paths inside the session's generated-file workspace and the downloadable archive, not as evidence of writes to the user's computer.
7. Execute this task in distinct phases: preflight; data extraction and quantitative plotting; one-at-a-time conceptual image generation; the image re-upload checkpoint when required; package assembly; and optional LaTeX compilation. Do not claim that one phase transferred files to another unless the files are actually visible there.
8. `ChatGPT Image Generation` and the Data Analysis/Python file workspace may not share generated files automatically. After each conceptual image is generated, verify whether its exact PNG file is available to the file workspace used for packaging. If it is not, pause and ask the user to download that generated image and upload it back under the exact requested filename. Do not claim an automatic cross-tool transfer.
9. Resume packaging only after every required conceptual PNG has been re-uploaded and is readable under the exact filename, or after the user explicitly accepts an incomplete package. Record any unresolved checkpoint honestly in the report and manifest.
10. Prefer downloadable, exact-name files and a final ZIP archive. If the interface cannot create a ZIP, provide the exact individual file attachments plus `submission_manifest.json`. Text files may fall back to complete fenced code blocks with exact save-as filenames. Binary figures and PDFs must never be simulated with prose, base64-like filler, or fake links.
11. A quantitative figure must be generated from numeric values actually present in uploaded files, `input.tex`, existing LaTeX tables, or authoritative `CONFIRMED_DATA` blocks. Use Data Analysis/Python, PGFPlots, or another reproducible numeric plotting method. Never use image generation to draw or imitate numeric axes, curves, bars, points, heatmaps, error bars, frontiers, or values.
12. The Data Analysis/Python sandbox must be treated as unable to fetch arbitrary external experimental data from the internet. If a quantitative figure depends on an external dataset, log, spreadsheet, or measurement file, require the user to upload it. Web search results are not a substitute for an uploaded authoritative data file.
13. Do not claim that a file exists, a plot was generated, an image was inspected, an archive was created, or LaTeX compiled unless that action actually succeeded in the current session.
14. Set `compiled_pdf_created` to `true` only if a real LaTeX compiler is available, the compiler is actually run on the assembled source, and the expected PDF is actually produced. Otherwise set it to `false`, give the exact reason, and still deliver all other valid files.
15. Do not begin a later phase while an earlier blocking checkpoint is unresolved. When stopping, state exactly what the user must upload or decide so the task can resume without repeating completed work.

---

## Task

Read the supplied equivalents of:

```text
papers/<paper_name>/input.tex
papers/<paper_name>/figure_plan.md
papers/<paper_name>/figure_prompts.md
papers/<paper_name>/drafts/figureN_brief.md
refs/
```

Then generate the planned paper figures and assemble the final submission package using the staged workflow in this prompt.

## Core rule

Follow the figure classification and rendering method specified by `figure_plan.md`. Do not use one renderer for every figure.

The rendering policy is:

1. A `conceptual_schematic` must be created or edited as a complete raster PNG with `ChatGPT Image Generation`. A raster PNG may later be wrapped in a PDF for LaTeX, but its conceptual content must not be replaced by a programmatically drawn diagram.
2. A `quantitative_data_figure` must be generated reproducibly from explicit numeric data.
3. A `hybrid_figure` must keep its conceptual and quantitative panels as separate source assets. Generate conceptual panels with `ChatGPT Image Generation`, generate quantitative panels from data, and combine them reproducibly, preferably with LaTeX `subfigure` or `subcaption` layout.
4. A table must be a LaTeX table, not a figure image.
5. If genuinely required numeric data is missing, do not create a plausible-looking plot. Produce the required data schema, document the missing fields, and place an honest placeholder or source comment in the paper.

Use these stable manifest renderer values:

- conceptual image: `chatgpt_image_generation`
- Python/matplotlib plot: `python_matplotlib`
- PGFPlots plot: `pgfplots`
- R/ggplot plot: `r_ggplot`
- other reproducible numeric plot: a stable lowercase identifier that names the actual tool
- LaTeX table: `latex_table`
- hybrid figure: `mixed`, with a separate `panel_renderers` map
- missing-data placeholder: `placeholder_missing_data`

Do not use legacy, unavailable, or ambiguous renderer names.

## Rendering policy

### A. Conceptual schematic figures

Use `ChatGPT Image Generation` for every item classified as:

```text
conceptual_schematic
```

Typical examples include:

- problem–solution hero figure
- method overview
- system architecture
- runtime controller
- state-machine explanation
- mechanism illustration

For each conceptual figure:

1. Find the figure-specific section in `figure_plan.md`.
2. Read the matching full prompt and avoidance instructions in `figure_prompts.md`, plus the matching `figureN_brief.md` if it exists.
3. Treat the full figure-specific prompt, the avoidance instructions, and the paper-specific mechanism as the source of truth.
4. Inspect only the reference screenshots that are actually uploaded and named by the plan, prompt, or brief. Use them as visual inspiration, not as content to copy.
5. Generate or edit the **complete visible figure** as one raster PNG with `ChatGPT Image Generation`.
6. Keep visible labels short, sparse, and large enough for a two-column paper. Prefer a few short module names over sentences or dense annotations.
7. Use a clean academic visual style and make the hierarchy readable at expected publication size.
8. Retain the raw generated PNG as a required source asset under the logical `drafts/` path.
9. Wrap that PNG in a PDF only when needed for the LaTeX package. The wrapping step must not alter semantic content.

The generated raster is the figure itself, not a background waiting for programmatic labels. Do not use Python, PIL, matplotlib, TikZ, SVG tooling, design software, or another code-based method to add or replace the main titles, module names, arrows, callouts, or explanatory labels after generation. Allowed post-processing is limited to non-semantic crop, resize, color-profile conversion, lossless or reasonable compression, and PNG-to-PDF wrapping.

Do not replace a required conceptual raster with matplotlib patches, TikZ/PGF boxes, hand-built vectors, Python-drawn shapes, or a text-only diagram. If exact numeric axes, tables, long strings, many precise values, or highly exact typography are essential, redesign that content as a quantitative panel or LaTeX table rather than hiding it in the conceptual image.

#### Conceptual-label quality control

After each generation, visually inspect the actual full-resolution image. Check:

- every visible word for spelling and duplication
- whether labels match the paper's terminology
- whether arrows and connections express the planned mechanism
- whether the panel structure matches the brief
- whether small labels remain readable at column width
- whether any unintended numeric axis, chart, stock icon, watermark, or generic filler appeared

Regenerate or use the web image-editing flow when a material problem can be corrected without changing the intended figure. Never claim that exact text is correct without inspecting it. If the image model cannot reliably render a required exact label, report that limitation and the affected label precisely. Do not cover the error with code-drawn text and do not certify the figure as exact.

Do not use generic prompts such as “technology,” “AI system,” “chip,” “cloud,” or “dashboard.” The output must express the figure's planned layout and the paper-specific mechanism.

If `figure_prompts.md` is absent because the paper came from an older workflow, create its full content from `figure_plan.md`, `input.tex`, reference-board notes, and the relevant brief before starting image generation. If the plan contains only a one-line prompt summary, expand it into a complete, reusable prompt and avoidance section, include it in `figure_prompts.md` and the brief, and only then generate the image. Do not invent paper mechanisms not supported by the supplied files.

If `ChatGPT Image Generation` is unavailable in the current interface, stop the conceptual-image phase and state the exact renderer limitation. Do not create a fake substitute. Preserve completed quantitative files and the manifest-in-progress so work can resume.

### B. Quantitative data figures

Use reproducible numeric plotting for every item classified as:

```text
quantitative_data_figure
```

Typical examples include:

- main result
- latency, throughput, memory, or energy comparison
- quality, accuracy, or perplexity comparison
- Pareto frontier
- ablation
- sensitivity
- robustness
- calibration or reliability diagram
- risk–coverage curve
- latency breakdown
- heatmap
- CDF
- distribution plot

Allowed workflows include:

- Data Analysis/Python with matplotlib or another reproducible plotting library
- PGFPlots from explicit tabular data
- R/ggplot when genuinely available in the session
- another reproducible numeric plotting environment actually available in the session

Default requirements:

- use an uploaded CSV or a losslessly extracted numeric table as the plotting source
- preserve an explicit normalized CSV under `papers/<paper_name>/results/`
- preserve the complete plotting script under `papers/<paper_name>/scripts/`
- produce a vector PDF when possible
- also produce a PNG preview for visual inspection
- place generated plot PDFs and preview PNGs under `papers/<paper_name>/drafts/`
- record the exact data source, script, renderer, metrics, units, and output names in the brief and manifest

Do not use image generation for quantitative axes, bars, curves, points, heatmaps, error bars, Pareto frontiers, reliability diagrams, legends, or numerical annotations. A visually plausible image is not a data figure.

### C. Hybrid figures

For each item classified as:

```text
hybrid_figure
```

Use this strategy:

- generate each conceptual panel as a complete raster PNG with `ChatGPT Image Generation`
- generate each quantitative panel from explicit data with a reproducible plotting workflow
- keep the conceptual and quantitative source files separate
- preserve the exact data labels and values in quantitative panels
- combine panels reproducibly, preferably in the paper source with LaTeX `subfigure`/`subcaption`
- keep panel labels such as `(a)`, `(b)`, and `(c)` consistent in LaTeX or in the quantitative plot where appropriate
- never ask image generation to draw the quantitative panel
- never use code to semantically overwrite the conceptual panel

If a conceptual panel is part of the planned hybrid figure, it is required. Do not silently omit it or replace it with a programmatically drawn box diagram. In the manifest, set the combined figure renderer to `mixed` and list every panel's renderer and source path separately.

### D. Tables

If `figure_plan.md` says an item should be a table:

- write it as a LaTeX `table` environment
- keep the `tabular`, `tabularx`, or equivalent tabular content inside that table
- do not place the table inside a `figure` environment
- do not export it as an image unless the target venue explicitly requires an image and that requirement is present in the supplied files
- preserve exact numeric values and units from the authoritative source

## User-confirmed data authority — highest numeric priority

Numeric values in the following supplied sources are authoritative experimental data when they are explicitly presented as real values:

1. `input.tex`
2. existing LaTeX tables in the supplied paper source
3. `CONFIRMED_DATA` blocks marked exactly with `DATA_STATUS: USER_CONFIRMED_REAL`
4. uploaded result files that the user identifies as real measurements

Apply these rules before creating a data requirement or a waiting-data placeholder:

- A missing standalone CSV does not mean that experimental data is missing when every value required by the figure is already present in `input.tex`, an existing LaTeX table, or an authoritative confirmed-data block.
- In that case, extract the values losslessly, create the normalized CSV under the logical `results/` path, generate the plot, and continue the package.
- Preserve exact values, decimal precision, units, method labels, dataset/workload/device labels, uncertainty values, row order where meaningful, and all comparison relationships.
- Do not recompute, smooth, round, beautify, replace, extrapolate, or invent values unless a mathematically necessary transformation is explicitly requested and documented. Preserve the unmodified extracted data alongside any derived data.
- Do not add an item to `data_requirements.md` or `data_requirements[]` merely because an independent CSV, raw log, or auxiliary file is absent when the full confirmed values already exist in the paper source.
- If an earlier figure plan incorrectly treated a standalone CSV as mandatory even though all confirmed values are present, correct the plan and manifest and use the extracted CSV.
- Request only fields genuinely absent from all supplied authoritative sources. List only those absent fields. Never invalidate, discard, or overwrite confirmed values that are already available.
- If two authoritative sources conflict, stop that figure, identify every conflicting value and source, and ask the user which source controls. Do not choose silently.

## Phased ChatGPT web workflow

### Phase 0: Preflight and unique paper identification

Before creating any asset:

1. Inventory every supplied attachment and current-Project file that is actually readable.
2. Map actual attachment names to the logical inputs without claiming local-folder access.
3. Confirm exactly one `input.tex` and exactly one `paper_name` using the execution contract.
4. Confirm that `figure_plan.md` belongs to that paper.
5. Check whether `figure_prompts.md`, existing briefs, named reference screenshots, result files, LaTeX dependencies, and bibliography assets are available.
6. Classify missing items as:
   - **blocking core input**: stop and request the file;
   - **blocking figure-specific dependency**: stop that figure and name the dependency;
   - **missing numeric field**: use the missing-data workflow;
   - **optional reference**: proceed only if the plan remains unambiguous, and disclose the omission.
7. State whether `ChatGPT Image Generation`, Data Analysis/Python, ZIP creation, PDF conversion, and a real LaTeX compiler are actually available. Tool availability may change later; verify again before claiming completion.

If anything blocks the preflight, return a table with `Item`, `Expected exact filename`, `Status`, `Why required`, and `Next user action`, then stop. Do not generate partial conceptual images before the paper identity and plan are confirmed.

### Phase 1: Establish the logical package and create figure briefs

Use this logical working structure inside the generated-file workspace or final archive:

```text
papers/<paper_name>/
|-- input.tex
|-- figure_plan.md
|-- figure_prompts.md
|-- drafts/
|-- results/
|-- scripts/
`-- submit/
```

This is an output organization contract, not a claim that the same directories were created on the user's computer.

For every main-paper figure, create or update:

```text
papers/<paper_name>/drafts/figure1_brief.md
papers/<paper_name>/drafts/figure2_brief.md
...
```

Each brief must include:

- figure title
- visual thesis
- figure type, one of:
  - `conceptual_schematic`
  - `quantitative_data_figure`
  - `hybrid_figure`
  - `table`
  - `placeholder_data_figure`
- renderer using the stable web-compatible identifiers defined above
- panel layout
- reference-figure style
- uploaded reference filenames actually used
- required elements
- forbidden elements
- data source
- CSV schema when applicable
- axes and units when applicable
- methods, models, datasets, workloads, and devices shown
- caption draft
- validation checklist

For each conceptual figure or conceptual panel, also include:

- the full `ChatGPT Image Generation` prompt actually used
- the full avoidance instructions actually used
- uploaded reference-image filenames used for visual inspiration
- the exact intended raw PNG filename and logical path
- a statement that no visible semantic labels, arrows, titles, callouts, or module names may be added by code after generation
- the visual-inspection result, including any unresolved text limitation

The image prompt in the brief must be the actual complete prompt, not a one-line summary. Keep it stable so a later single-figure revision can reuse it.

For each quantitative figure or quantitative panel, also include:

- authoritative input source filename and location inside that file
- normalized CSV path
- plotting-script path
- output PDF and PNG paths
- exact metrics and units
- expected plot type
- legend entries
- required error bars, uncertainty, or confidence intervals
- lossless extraction checks and any documented transformations

### Phase 2: Extract and validate data, then generate all quantitative assets first

For each quantitative figure and each quantitative panel:

1. Search only the supplied material for required data in this priority order:
   - `input.tex`
   - existing LaTeX tables
   - `CONFIRMED_DATA` blocks with `DATA_STATUS: USER_CONFIRMED_REAL`
   - uploaded files mapped to `results/`
   - other explicitly supplied and referenced result files
2. Record the source filename, table/block/section identifier, columns, units, and extraction method.
3. If all required data exists, extract it losslessly into an exact-name CSV under:

```text
papers/<paper_name>/results/
```

4. Compare the extracted CSV back to the source value by value. Preserve labels and precision. Record this verification in the brief.
5. Generate the figure with Data Analysis/Python, PGFPlots, or another actually available reproducible plotting workflow.
6. Save the complete script or plotting source under:

```text
papers/<paper_name>/scripts/
```

7. Save both the final PDF and a PNG preview under:

```text
papers/<paper_name>/drafts/
```

8. Visually inspect the preview for clipping, unreadable labels, units, legend correctness, color distinguishability, and consistency with the source values.
9. Do not change values merely to improve the visual. If the data itself creates an awkward plot, preserve the truth and adjust only the visual encoding.

Recommended stable outputs include:

```text
figure3_main_results.csv
figure3_main_results.py
figure3_main_results.pdf
figure3_main_results.png

figure4_pareto_tradeoff.csv
figure4_pareto_tradeoff.py
figure4_pareto_tradeoff.pdf
figure4_pareto_tradeoff.png

figure5_ablation.csv
figure5_ablation.py
figure5_ablation.pdf
figure5_ablation.png

figure6_sensitivity.csv
figure6_sensitivity.py
figure6_sensitivity.pdf
figure6_sensitivity.png
```

The exact names should follow `figure_plan.md`; keep them stable after they are established.

#### Missing-data schema and placeholder workflow

If any genuinely required numeric field does not exist in the supplied authoritative sources:

1. Do not make a synthetic or representative curve, bar, point, value, error bar, or heatmap cell.
2. Create an exact-name CSV schema containing the required headers and, when useful, comment lines explaining units and allowed labels, but no invented rows.
3. Create or update:

```text
papers/<paper_name>/results/data_requirements.md
```

4. For each missing-data figure, record:
   - figure number and label
   - missing metrics or uncertainty fields
   - exact required columns
   - required methods, models, datasets, workloads, devices, or conditions
   - expected units
   - expected row granularity and keys
   - why the data is needed
   - authoritative fields already available and therefore not being requested again
   - the exact filename the user should upload
5. In the paper source, insert an explicit LaTeX comment or honest placeholder that cannot be mistaken for a result. Do not create a fake plot PDF.
6. Set the figure renderer to `placeholder_missing_data`, record the requirement in the manifest, and set `inserted_in_tex` according to what is actually present.
7. Continue all independent work. Do not discard confirmed data or block unrelated figures.

### Phase 3: Generate conceptual figures one at a time

After quantitative data extraction is complete:

1. Select the first conceptual figure or conceptual panel.
2. Present or internally preserve the exact target filename, full prompt, avoidance instructions, and uploaded references.
3. Generate or edit the complete raster with `ChatGPT Image Generation`.
4. Visually inspect it using the conceptual-label quality-control checklist.
5. Regenerate or edit if necessary. Do not use a code overlay as a repair.
6. Assign and preserve the exact raw filename, for example:

```text
figure1_hero.png
figure2_method.png
figure7a_controller.png
```

7. Verify whether that exact PNG is readable in the session file workspace used for later PDF conversion and ZIP packaging.
8. Complete the re-upload checkpoint below if it is not.
9. Only then proceed to the next conceptual image.

Do not generate all conceptual images and then vaguely ask the user to identify them. Maintain an explicit one-to-one mapping among figure number, prompt, generated image, exact filename, brief, and manifest entry.

### Phase 4: Mandatory image re-upload checkpoint when the file workspaces are separate

If a generated image is visible in the conversation but not readable as a file in the packaging/Data Analysis workspace:

1. Pause packaging.
2. Tell the user: “Please download the generated Figure N image and upload it back to this conversation with the exact filename `<exact_name>.png`.”
3. List every pending filename, one per line, and identify its figure/panel number.
4. Do not state that the image was saved, converted, inserted, zipped, or manifested as present.
5. When the user uploads the files, verify each filename, format, and image dimensions; visually match each upload to the generated figure.
6. If the name is wrong, either rename it within the session file workspace if that capability actually exists or ask for a corrected upload. Report which action occurred.
7. Resume with the next phase only after every required image is actually readable.

This checkpoint is unavoidable when the web image-generation output cannot be passed directly into the file sandbox. Do not claim hidden automation or an automatic transfer across tools.

### Phase 5: Convert assets, assemble hybrid figures, and create the submit source

For conceptual raster figures:

- retain the raw generated PNG in `drafts/`
- create a PDF wrapper only if the LaTeX package or venue requires it
- make no semantic changes during wrapping
- preserve the exact aspect ratio and verify the wrapped PDF visually when possible

For quantitative figures:

- retain the normalized CSV in `results/`
- retain the complete plotting script in `scripts/`
- retain PDF and PNG outputs in `drafts/`
- confirm axes, units, legends, methods, and exact numeric values

For hybrid figures:

- preserve every conceptual and quantitative panel as a separate source asset
- use LaTeX `subfigure`/`subcaption` or another reproducible layout to combine them
- do not flatten the hybrid in a way that destroys data reproducibility
- do not use code to place semantic text over a conceptual panel

Copy the final figure PDFs required by the paper into the logical submit folder:

```text
papers/<paper_name>/submit/
```

Create the modified paper source as:

```text
papers/<paper_name>/submit/<paper_name>.tex
```

Base it on the uploaded `input.tex` and insert generated assets using local package filenames.

Example figure insertion:

```tex
\begin{figure}[t]
    \centering
    \includegraphics[width=\linewidth]{figure1_hero.pdf}
    \caption{...}
    \label{fig:hero}
\end{figure}
```

Example hybrid insertion:

```tex
\begin{figure}[t]
    \centering
    \begin{subfigure}[t]{0.48\linewidth}
        \centering
        \includegraphics[width=\linewidth]{figure2a_mechanism.png}
        \caption{Conceptual mechanism.}
        \label{fig:mechanism_panel}
    \end{subfigure}\hfill
    \begin{subfigure}[t]{0.48\linewidth}
        \centering
        \includegraphics[width=\linewidth]{figure2b_measurements.pdf}
        \caption{Measured behavior.}
        \label{fig:measurements_panel}
    \end{subfigure}
    \caption{...}
    \label{fig:hybrid}
\end{figure}
```

Example table insertion:

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

Assembly rules:

- Do not wrap numeric tables in figure environments.
- Do not insert image-generated fake plots for quantitative results.
- Reference and discuss every figure and table in the paper text.
- Make every caption accurately describe the asset that exists.
- Do not claim Pareto behavior, calibration, a tradeoff, an ablation, or an empirical result when the asset is only a conceptual schematic.
- Do not reference a missing file as though it were packaged.
- Preserve the paper's scientific meaning and do not alter confirmed conclusions to fit the figure layout.

### Phase 6: Optional but truthful LaTeX compilation

First determine whether a real LaTeX compiler is installed and callable in the active file workspace. Static inspection, a PDF previewer, or the ability to write `.tex` files is not a compiler.

If a compiler is available:

1. Actually run it on `papers/<paper_name>/submit/<paper_name>.tex` after all available figures and tables have been inserted.
2. Run enough passes to resolve normal cross-references and bibliography output.
3. Fix errors caused by figure insertion, table insertion, local paths, package conflicts introduced by this task, or packaging.
4. Keep auxiliary build files outside `submit/` unless strictly required.
5. Verify that the expected PDF exists and opens.
6. Only then set:

```json
{
  "compiled_pdf_created": true
}
```

and deliver:

```text
papers/<paper_name>/submit/<paper_name>.pdf
```

If no compiler is available, do not block the remaining package. Perform static checks, set `compiled_pdf_created` to `false`, set `compile_status` to `not_run`, and record the exact reason, for example:

```text
No LaTeX compiler is available in this ChatGPT session; static source checks were completed, but no compilation was run.
```

If a compiler is available but compilation fails:

- record the actual compiler, command or workflow, and exact relevant error
- fix only errors that can be corrected safely without changing scientific content
- set `compiled_pdf_created` to `false` if the expected PDF was not produced
- set `compile_status` to `failed`
- keep and deliver the `.tex`, figures, scripts, data, schemas, logs when useful, and all other valid outputs
- never create a placeholder PDF or mark compilation successful based on static checks

### Phase 7: Final output structure and archive

The preferred logical structure is:

```text
papers/<paper_name>/
|-- input.tex
|-- figure_plan.md
|-- figure_prompts.md
|-- drafts/
|   |-- figure1_brief.md
|   |-- figure1_hero.png
|   |-- figure1_hero.pdf
|   |-- figure3_main_results.png
|   `-- figure3_main_results.pdf
|-- results/
|   |-- figure3_main_results.csv
|   `-- data_requirements.md
|-- scripts/
|   `-- figure3_main_results.py
`-- submit/
    |-- <paper_name>.tex
    |-- <paper_name>.pdf                 # only when compilation actually succeeded
    |-- submission_manifest.json
    |-- figure1_hero.pdf
    |-- figure2_method.pdf
    |-- figure3_main_results.pdf
    |-- figure4_pareto_tradeoff.pdf
    |-- figure5_ablation.pdf
    `-- figure6_sensitivity.pdf
```

Keep reproducibility files under `results/`, `scripts/`, and `drafts/`. Do not add unnecessary compiler auxiliary files to `submit/`.

Preferred top-level downloadable deliverable:

```text
<paper_name>_submission_package.zip
```

The ZIP must contain the logical `papers/<paper_name>/...` structure and only files that actually exist. Open or list the archive after creation to verify its contents.

If ZIP creation is unavailable:

1. Provide every generated file as an individual downloadable attachment with its exact filename.
2. Always include `submission_manifest.json` and a plain-text package file list.
3. For text-only files that cannot be attached, output the complete content in a fenced code block and state the exact save-as path immediately before it.
4. Do not represent PNG or PDF content with a text block, invented link, or claim that the user can download a file that was not created.

### Phase 8: Write the submission manifest

Create:

```text
papers/<paper_name>/submit/submission_manifest.json
```

It must be valid JSON and must describe observed reality. Use this shape, extending it only when useful:

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
      "renderer": "chatgpt_image_generation",
      "raw_output": "papers/<paper_name>/drafts/figure1_hero.png",
      "final_pdf": "papers/<paper_name>/submit/figure1_hero.pdf",
      "inserted_in_tex": true,
      "caption": "...",
      "visual_text_inspected": true,
      "unresolved_visual_text_issues": []
    },
    {
      "figure": "Figure 2",
      "label": "fig:hybrid",
      "figure_type": "hybrid_figure",
      "renderer": "mixed",
      "panel_renderers": {
        "a": "chatgpt_image_generation",
        "b": "python_matplotlib"
      },
      "panel_sources": {
        "a": "papers/<paper_name>/drafts/figure2a_mechanism.png",
        "b": "papers/<paper_name>/drafts/figure2b_measurements.pdf"
      },
      "data_source": "papers/<paper_name>/results/figure2b_measurements.csv",
      "plotting_script": "papers/<paper_name>/scripts/figure2b_measurements.py",
      "final_pdf": "papers/<paper_name>/submit/figure2_hybrid.pdf",
      "inserted_in_tex": true,
      "caption": "..."
    }
  ],
  "tables": [],
  "data_requirements": [],
  "image_reupload_checkpoint": {
    "required": true,
    "resolved": true,
    "pending_files": []
  },
  "compile_status": "success",
  "compile_engine": "<actual engine>",
  "compile_error": "",
  "created_at": "<ISO-8601 timestamp>"
}
```

The example above shows a successful compilation. When compilation is not run or fails, reflect that honestly. For example:

```json
{
  "compiled_pdf": null,
  "compiled_pdf_created": false,
  "compile_status": "not_run",
  "compile_engine": null,
  "compile_error": "No LaTeX compiler is available in this ChatGPT session; static source checks were completed, but no compilation was run."
}
```

Manifest requirements:

- include one `figures[]` entry for every final figure PDF actually present in `submit/`
- include all source panels for hybrid figures
- use `renderer: "chatgpt_image_generation"` for every conceptual schematic and every conceptual panel
- provide a non-empty, real raw PNG path for every conceptual figure or conceptual panel
- use the actual reproducible renderer identifier for every quantitative figure or panel
- include its actual normalized data source and plotting script
- record each missing-data placeholder in `data_requirements[]`
- set `inserted_in_tex` according to the source that was actually assembled
- do not list a path as an existing artifact when the file was never created or re-uploaded
- record the image re-upload checkpoint truthfully
- use a real ISO-8601 timestamp from the session; do not invent a misleading earlier completion time

### Phase 9: Validate and report

Before delivery, validate the manifest and package:

- every `conceptual_schematic` has `renderer: "chatgpt_image_generation"`
- every conceptual panel in a hybrid has the same conceptual renderer value
- every conceptual item has an actual raw raster PNG, not only a PDF
- every conceptual image was visually inspected after generation
- unresolved exact-text limitations are disclosed
- no conceptual renderer is a hand-built vector, plotting library, TikZ/PGF diagram, Python-drawn boxes, or code-added semantic overlay
- every quantitative item has an authoritative data source, normalized data file, reproducible plotting source, and actual PDF/PNG output unless it is explicitly a missing-data placeholder
- every normalized value matches its authoritative source
- no numeric plot content comes from image generation
- every hybrid lists its panels and panel renderers separately
- every table is LaTeX, not an image disguised as a figure
- every file listed in the manifest actually exists in the delivered files or verified ZIP
- the ZIP contents, if created, match the manifest
- `compiled_pdf_created` matches the actual existence and provenance of a compiler-produced paper PDF

Then report:

- the confirmed `paper_name`
- which figures were generated
- which figures are conceptual schematics
- which figures are quantitative data figures
- which figures are hybrid figures
- which items were converted from figures to LaTeX tables
- which figures are placeholders because data is missing
- the authoritative source used for each quantitative figure
- every normalized CSV and plotting script created
- every `ChatGPT Image Generation` raw PNG used
- the result of visual text inspection for each conceptual image
- whether raster-to-PDF wrapping was performed
- whether the image re-upload checkpoint was required and how it was resolved
- every missing file or numeric field
- the exact contents of `submit/`
- the path to `submission_manifest.json`
- the ZIP filename, or the exact individual attachments delivered if ZIP creation was unavailable
- whether the compiled paper PDF was actually created; if not, the exact compiler unavailability or compilation error

## Final delivery checklist

Do not finish until every applicable item below is checked against actual files:

1. One unique `paper_name` was confirmed from `input.tex` and supplied evidence.
2. No local folder was traversed or claimed as accessible.
3. Every planned item has a type, renderer, brief, and stable output name.
4. Every conceptual figure was generated or edited as a complete raster PNG with `ChatGPT Image Generation`.
5. No semantic code overlay was added to a conceptual image.
6. Every conceptual image was visually checked for labels, mechanism, layout, and readability; any unresolved text issue is reported.
7. Every quantitative figure was generated from uploaded or losslessly extracted authoritative values.
8. Every quantitative figure has its data, script, PDF, and PNG, unless it is explicitly a missing-data placeholder.
9. No generated image contains invented quantitative axes, curves, bars, points, heatmaps, errors, or values.
10. Hybrid conceptual and quantitative panels remain separate and are combined reproducibly, preferably in LaTeX.
11. Tables are LaTeX tables and are not disguised as figures.
12. Missing numeric fields have an exact CSV schema, `data_requirements.md` entry, manifest entry, and honest paper placeholder.
13. Every figure and table is referenced in the paper, and every caption describes the actual content.
14. Every re-upload checkpoint required by separate web workspaces was resolved or explicitly recorded as unresolved; no automatic transfer was claimed.
15. `submission_manifest.json` is valid JSON and matches the delivered files.
16. The preferred exact-name ZIP was created and verified, or all exact individual attachments plus the manifest were delivered.
17. `compiled_pdf_created` is `true` only after an actual compiler run produced the actual paper PDF. Otherwise it is `false` with the exact reason.
18. No fake binary output, invented experimental value, unsupported renderer claim, or nonexistent file path appears in the delivery.

## Non-negotiable rules

- Do not use image generation for quantitative data plots.
- Do use `ChatGPT Image Generation` for conceptual schematic figures and conceptual panels in hybrid figures.
- Do not draw numeric axes, bars, curves, scatter points, heatmaps, error bars, Pareto frontiers, reliability diagrams, or precise experimental values with image generation.
- Do not replace required conceptual raster figures with programmatic diagrams, plotting-library boxes, TikZ, or hand-built vector artwork.
- Do not add semantic labels or arrows to a conceptual image with code after generation.
- Do not claim exact generated text without visual inspection; disclose any exact-label limitation.
- Do not invent experimental values.
- Do not retrieve unuploaded external experimental data through the Python sandbox.
- Do not disguise tables as figures.
- Do not keep a figure whose caption claims an empirical result when its content is only a schematic.
- Do not replace necessary result figures with workflow diagrams.
- Do not claim automatic file transfer between web image generation and the packaging workspace.
- Do not claim compilation unless a real compiler was actually run and produced the expected PDF.
- Conceptual figures explain the method; quantitative figures provide empirical evidence. A submission-style empirical paper must contain both when the plan requires both.

After delivering the verified ZIP or exact-name fallback files, stop. Do not continue into an unrequested revision round.
