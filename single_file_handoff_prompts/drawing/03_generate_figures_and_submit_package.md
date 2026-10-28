# Drawing Step 03: Generate Figures from the Single Handoff File

## Input and output contract — highest priority

- Read only the uploaded `figure_workflow.md` returned by Drawing Step 02.
- Do not ask for `paper.tex`, figure plans, prompts, briefs, reference boards, naming metadata, or directories again; all required textual context must be embedded in the handoff file.
- External numeric data may be requested only when the handoff explicitly identifies genuinely missing fields that cannot be recovered from the embedded paper, tables, or `CONFIRMED_DATA`.
- This is the first drawing step allowed to create multiple binary files.
- Preferred final output: one flat ZIP named `figure_package.zip`.
- Do not claim to read or write the user's local filesystem.
- Do not claim a file, image, plot, ZIP or compiled PDF exists unless it was actually created in this session.

## Required ZIP contents

Use a flat file list with no required folder hierarchy:

- `paper_with_figures.tex`
- `submission_manifest.json`
- `figure_revision_handoff.md`
- each final figure PDF
- each final figure PNG
- each raw conceptual PNG
- each normalized CSV used for a plot
- each plotting script
- `data_requirements.md` when any data is missing
- `paper_with_figures.pdf` only if a real LaTeX compiler was run successfully

If ZIP creation is unavailable, provide the actual individual files. Text files may fall back to complete code blocks; binary images and PDFs may not be simulated with prose.

## Rendering rules

### Conceptual figures

- Use ChatGPT Image Generation.
- Use the complete prompt and brief embedded in the handoff.
- Generate the complete visible figure as a raster PNG.
- Keep labels short and visually inspect spelling, arrows, relationships and readability.
- Do not use Python, TikZ, SVG, PIL or plotting libraries to add semantic labels, arrows, titles or boxes after generation.
- Only crop, resize, compress, color-convert or wrap the unchanged raster to PDF.
- If exact text remains wrong after reasonable iteration, disclose it; do not claim exactness.

### Quantitative figures

- Use Data Analysis / Python, PGFPlots or another reproducible numeric workflow.
- Extract values losslessly from the embedded paper source, LaTeX tables and `CONFIRMED_DATA`, or use explicitly supplied external data requested by the missing-data checkpoint.
- Preserve every value, unit, method label and row relationship.
- Save normalized CSV and plotting source.
- Produce both PDF and PNG when possible.
- Do not use ChatGPT Image Generation for axes, bars, curves, points, heatmaps, error bars, Pareto frontiers, calibration diagrams or numeric annotations.
- The Python/data-analysis environment must not fetch unuploaded experimental data from the web.

### Hybrid figures

- Generate conceptual and quantitative panels separately.
- Keep their source assets separate.
- Combine with LaTeX subfigure/subcaption or another reproducible layout.
- Do not let image generation redraw the quantitative panel.

### Tables

- Keep them as LaTeX tables.

## Phased execution

### Phase 0: Preflight

Verify that the handoff contains:

- complete paper source;
- reference board;
- figure plan;
- complete prompts;
- complete briefs;
- data availability matrix;
- confirmed-data snapshot.

If essential sections are missing, stop and list them.

### Phase 1: Extract and validate data

Before image generation:

1. Extract authoritative numeric values.
2. Create normalized CSVs.
3. Compare every extracted value with its source.
4. Create plotting scripts/specifications.
5. Generate all quantitative plots first.
6. If a genuinely required numeric field is absent, create `data_requirements.md`, a CSV schema and an honest LaTeX placeholder. Do not invent values.

### Phase 2: Generate conceptual figures one at a time

For each conceptual figure:

1. use its complete prompt;
2. generate or edit the raw PNG;
3. inspect it at full resolution;
4. iterate when material errors exist;
5. assign the exact filename from the plan.

### Mandatory cross-tool re-upload checkpoint

ChatGPT Image Generation and the Data Analysis/file-packaging workspace may not automatically share files.

After every conceptual image is generated, verify whether the exact PNG is readable by the packaging workspace.

If it is not:

1. pause;
2. list the exact PNG filenames;
3. ask the user to download those generated images and upload them back in the next message under those exact names;
4. preserve completed data plots and text work;
5. resume only after the PNGs are visible.

Never claim automatic transfer, packaging, hybrid assembly or compilation before this checkpoint is resolved.

### Phase 3: Assemble figures and paper

1. Wrap conceptual PNGs to PDF when required.
2. Assemble hybrid figures reproducibly.
3. Replace figure placeholders in the embedded paper source with actual filenames.
4. Preserve captions, labels and references.
5. Ensure every figure/table is referenced in text.
6. Create `paper_with_figures.tex`.

### Phase 4: Optional compilation

Run LaTeX only if a real compiler and all dependencies are available.

- Set `compiled_pdf_created: true` only when the compiler actually produces `paper_with_figures.pdf`.
- Otherwise set it to `false`, record `not_run` or the exact error, and still deliver all other valid outputs.

### Phase 5: Manifest and handoff

Create valid `submission_manifest.json` with:

```json
{
  "source_handoff": "figure_workflow.md",
  "submit_tex": "paper_with_figures.tex",
  "compiled_pdf": null,
  "compiled_pdf_created": false,
  "figures": [
    {
      "figure": "Figure 1",
      "label": "fig:...",
      "figure_type": "conceptual_schematic",
      "renderer": "chatgpt_image_generation",
      "raw_png": "figure1_raw.png",
      "final_png": "figure1.png",
      "final_pdf": "figure1.pdf",
      "inserted_in_tex": true,
      "caption": "..."
    }
  ],
  "tables": [],
  "data_files": [],
  "plotting_scripts": [],
  "data_requirements": [],
  "image_reupload_checkpoint": {
    "required": false,
    "resolved": true,
    "pending_files": []
  },
  "compile_status": "not_run",
  "compile_engine": null,
  "compile_error": "No compiler was run."
}
```

Renderer values:

- `chatgpt_image_generation`
- `python_matplotlib`
- `pgfplots`
- `r_ggplot`
- `latex_table`
- `mixed`
- `placeholder_missing_data`

Create `figure_revision_handoff.md` containing:

- the complete final figure inventory;
- for each figure, its visual thesis, renderer, prompt/specification, source data, caption, filenames and validation status;
- unresolved label issues;
- missing data;
- compilation status;
- instructions for revising one figure without needing the original Step 01/02 handoff.

This file is what makes Drawing Step 04 possible from the Step 03 package.

## Final validation

- no generated image contains invented numeric evidence;
- every quantitative figure has data and reproducible plotting source;
- every conceptual figure has a real raw PNG and visual inspection record;
- every hybrid lists separate panel renderers;
- every manifest file actually exists;
- the paper cites every figure and table;
- confirmed data are unchanged;
- compilation status is truthful;
- ZIP contents match the manifest.

Deliver `figure_package.zip` or the actual fallback files, then stop.
