# Prompt 04: Single-Figure Revision — ChatGPT Web

## Task

Revise exactly one existing figure for a research paper. Preserve the paper's
claims and the target figure's established design intent while addressing the
operator's revision request.

## Before you start

### ChatGPT web execution contract

1. Work only with files that are attached in this conversation or available in
   the current ChatGPT Project. Do not assume access to any other files, folders,
   drives, repositories, or prior conversations, and do not attempt to discover
   or enumerate them.
2. Paths such as `papers/<paper_name>/...` and `refs/...` are logical archive
   paths only. They describe where the user may place downloaded deliverables
   later; they are not browsable locations and are not proof that a file exists.
3. Before reviewing or revising anything, identify one unambiguous
   `<paper_name>` and one unambiguous target `figureX` from the operator's request
   and the supplied files. Confirm them against filenames and file contents; do
   not infer them from an unrelated figure or invent a name.
4. If either identifier is missing, stop and output only:

   ```text
   Missing:
   - <paper_name and/or figureX>
   ```

5. If either identifier has more than one plausible value, stop and ask only the
   minimum disambiguating question, for example: `Which paper and figure should
   I revise: ...?` Do not begin analysis, reconstruction, or generation while the
   target is ambiguous.
6. Use only the target figure and target paper. Ignore similarly named artifacts
   belonging to another paper or figure.
7. Never say that a figure was generated, plotted, converted, compiled,
   validated, packaged, saved, or updated unless that specific operation was
   actually completed in this chat and its resulting artifact is available.
   Supplying instructions or source code is not the same as executing it.
8. Treat every output path below as a suggested download filename and logical
   archive destination. Do not claim to have written into that path.

### Required supplied material

Use the versions attached in this conversation or stored in the current
ChatGPT Project:

```text
papers/<paper_name>/input.tex
papers/<paper_name>/figure_plan.md
papers/<paper_name>/figure_prompts.md
papers/<paper_name>/drafts/figureX_brief.md
papers/<paper_name>/submit/figureX_xxx.pdf
refs/
```

The current figure itself must be supplied. For a conceptual figure, request the
original raw PNG in addition to the PDF whenever it exists; the raw PNG is
strongly preferred for a faithful image edit. If the requested change requires
editing pixels from the original and no usable raster source is supplied, stop
and list that PNG as missing. If the user explicitly accepts a full regeneration,
the supplied current figure may instead be used as a visual reference.

For a quantitative figure, the user must also supply either:

- the original numeric data and plotting script; or
- source material from which every plotted value can be extracted losslessly,
  such as an explicit table or `CONFIRMED_DATA` block in `input.tex`.

If the quantitative values cannot be recovered exactly from supplied material,
stop and output only the missing data or script items. Do not estimate values
from the appearance of the old plot, OCR uncertain values, digitize curves, or
guess.

Reference-board files or notes are required only when the brief, plan, or prompt
depends on them. If a required source-of-truth artifact is absent and cannot be
reconstructed from other supplied authoritative artifacts, stop and list only
what is missing.

## Source of truth for the target figure

The operator's revision request identifies what to change; it does not authorize
replacing the target with a generic new concept. Read and reconcile the supplied
artifacts in this order:

1. Locate the exact target figure section in `figure_plan.md`.
2. Read the target figure material in `figure_prompts.md`.
3. Read the matching `figureX_brief.md`.
4. Inspect the current submitted figure and its raw PNG when supplied.
5. Read the relevant paper claims, caption, notation, and confirmed numbers in
   `input.tex`.
6. Consult only the supplied reference-board notes relevant to this figure.
7. Treat the operator's revision request as constraints on this established
   design.

Reuse the target figure's visual thesis, panel layout, required elements, caption
intent, renderer, data source, full image prompt, negative prompt, terminology,
and visual reference style unless the revision request explicitly changes one of
them. When sources conflict, preserve `input.tex` for claims and numbers, use the
most figure-specific artifact for visual intent, and report the conflict rather
than silently resolving a material ambiguity.

If `figure_prompts.md` is absent because the paper used an older workflow,
reconstruct the target figure's full prompt from the supplied target section of
`figure_plan.md`, matching `figureX_brief.md`, `input.tex`, current figure, and
relevant reference-board notes before revising. Include the reconstructed target
entry in the updated `figure_prompts.md` deliverable.

If `figureX_brief.md` contains only a short prompt summary, reconstruct a complete
figure-specific prompt from the same authoritative sources before generation.
Include the full reconstructed prompt in both the updated brief and updated
prompt-file deliverables. Reconstruction must not introduce claims, components,
or data absent from the supplied sources.

## Classify the target before revising

Classify the target as exactly one of the following and follow only the matching
renderer policy:

- **Conceptual schematic:** revise with ChatGPT Image Generation using the
  supplied figure as an image reference or edit target.
- **Quantitative data figure:** revise with Data Analysis using Python or with a
  supplied/reconstructed PGFPlots workflow and explicit numeric data. Do not use
  image generation.
- **Hybrid figure:** handle conceptual and quantitative panels separately with
  their respective policies, then assemble only from actually produced panels.
- **Table:** keep it as a LaTeX table. Do not turn it into generated imagery.

Do not use image generation to fabricate numeric axes, bars, curves, scatter
points, error bars, heatmaps, legends tied to values, or tables.

## Conceptual schematic revision policy

1. Use **ChatGPT Image Generation** for the actual conceptual visual. Upload or
   select the original raw PNG as the edit reference when available; also use the
   current PDF only as a visual reference if needed.
2. Use the complete, figure-specific prompt from `figure_prompts.md` and the
   brief, amended only by the operator's requested revisions. Never replace it
   with a generic image prompt.
3. Generate or edit the entire visible figure in one coherent image, including
   its short labels, callouts, arrows, and relationships. The generated image
   must not be merely a decorative background for later semantic overlays.
4. Keep labels short, distinctive, and sparse enough for a two-column paper.
   Prefer a small controlled vocabulary already used in the paper.
5. Do not add or replace titles, module names, labels, callouts, arrows, legends,
   or other meaning-bearing content afterward with Python, PIL, matplotlib,
   TikZ, PGFPlots, SVG editing, presentation software, or any other code-based
   overlay. Only non-semantic cropping, whitespace trimming, resizing, and file
   conversion are allowed after image generation.
6. Produce a paper-specific academic systems figure. Avoid generic stock-style
   AI art, photorealistic laptops, glowing dashboards, decorative 3D hardware,
   random chip icons, irrelevant clouds or servers, and generic box-and-arrow
   filler.
7. The primary conceptual output must be a raster PNG. If the submission requires
   PDF, wrap or convert the finalized PNG without replacing its visual content.
   Provide the raw PNG as a separate deliverable.
8. Inspect the generated labels at readable zoom. Image generation may misspell,
   duplicate, omit, or distort text. If exact wording is not achieved, state the
   defect precisely and iterate with ChatGPT Image Generation. Never claim text
   accuracy, visual fidelity, or successful revision when it has not been
   verified.
9. If the requested change requires guaranteed exact numeric axes, numerous exact
   values, or dense precise text, reclassify the affected content as quantitative
   and use a reproducible plotting workflow. Do not combine generated artwork
   with code overlays to simulate precision.
10. If the generated PNG is not directly available to the file-conversion or
    packaging workspace, pause and ask the user to download it and re-upload it
    under the exact requested filename. Resume conversion, validation, and
    packaging only after that file is actually readable. Do not claim an
    automatic transfer or a completed PDF/ZIP while this checkpoint is open.

## Quantitative data figure revision policy

1. Establish a value-by-value source map before plotting. Every number, label,
   unit, category, series, error definition, and uncertainty value must trace to
   the supplied original data, plotting script, an explicit table, or a
   `CONFIRMED_DATA` block in `input.tex`.
2. Prefer the original plotting script and data. If they are unavailable but all
   values are explicitly present in an authoritative supplied source, extract
   them losslessly into a normalized CSV/JSON file and record the source location
   for each series.
3. Use Data Analysis with Python for a reproducible raster/vector plot, or use
   PGFPlots when the supplied workflow requires it. Do not use ChatGPT Image
   Generation for any quantitative panel.
4. Preserve the paper's statistical meaning: sample definitions, aggregation,
   baselines, error bars, scales, units, ordering, significant digits, and legend
   semantics. A stylistic revision must not alter the underlying data.
5. Keep the plotting script and normalized data as deliverables. Fix random seeds
   when randomness is genuinely required and record package assumptions that
   affect rendering.
6. Validate plotted values against the source data after rendering. Check axis
   scales, tick labels, category order, legend-to-series mapping, clipping,
   missing values, and error bars.
7. If execution is unavailable, provide the revised source artifacts and clearly
   mark the plot as **not rendered or validated**. Do not state that the output
   figure exists.

## Hybrid figure revision policy

1. Split the figure into conceptual and quantitative panels before choosing a
   renderer.
2. Revise every conceptual panel with ChatGPT Image Generation under the complete
   conceptual policy above. Revise every quantitative panel with Data
   Analysis/Python or PGFPlots under the complete quantitative policy above.
3. Make each conceptual panel complete in itself; do not plan to repair its
   meaning with a later code overlay.
4. If the image-generation interface and the code/data-analysis workspace cannot
   transfer artifacts automatically, tell the user to download the generated
   conceptual panel and re-upload it under the exact requested filename, such as
   `figureX_panel_a_raw.png`. Do not pretend that the panel has been transferred.
   Resume assembly only after the correctly named file is available in the
   current conversation or Project.
5. Assemble only actual panel files, preserving their aspect ratios and avoiding
   semantic overlays on generated panels. Keep raw conceptual PNGs, plotting
   scripts, and quantitative data as separate deliverables.

## Review the current figure first

Before revision, assess the supplied current figure against all of these
criteria:

1. Does it have a clear visual thesis?
2. Does it look like a top-conference paper figure rather than a generic slide?
3. Is it too much like a box-and-arrow pipeline?
4. Is the panel layout clear?
5. Is the text density too high?
6. Are labels concise?
7. Are all numbers traceable to `input.tex` or another supplied authoritative
   data source?
8. Is it readable at its intended two-column publication size?
9. Does the caption match the visible figure?
10. Does it follow the intended supplied reference style?

Summarize only the actionable findings that affect the requested revision. Do
not redesign unrelated content.

## Revision rules

- Preserve the paper's actual claims.
- Preserve the target figure's planned visual thesis and layout unless the
  operator explicitly requests a change.
- Never invent, interpolate, beautify, or silently alter data.
- Improve visual hierarchy and make the main contrast or insight more obvious.
- Reduce unnecessary text while preserving required meaning and terminology.
- Use the renderer required by the figure classification.
- Keep the caption, visible content, and figure references mutually consistent.
- Update the brief when the figure's meaning, composition, prompt, data source,
  or renderer changes.
- Update the target entry in `figure_plan.md` and `figure_prompts.md` when the
  revision changes their documented decisions. Leave unrelated entries intact.
- For a conceptual figure, retain the raw generated PNG. For reproducible plots,
  retain the plotting script and normalized data.
- Preserve submission filenames unless the revision request requires a new
  version name.

## Data and claim protection

- Treat `input.tex`, explicit supplied tables, and supplied `CONFIRMED_DATA`
  blocks as authoritative only for the claims and values they actually contain.
- Do not derive a quantitative value from visual spacing, pixel position, color,
  OCR uncertainty, or an illustrative schematic.
- Do not convert qualitative arrows, rankings, or tendencies into numeric
  magnitudes.
- Do not add a baseline, ablation, method, component, result, or comparison that
  is absent from the source-of-truth artifacts.
- Preserve units and distinguish percentages from percentage points, fractions
  from percentages, standard deviation from standard error, and measured values
  from illustrative annotations.
- If sources disagree, identify the exact conflict and stop before rendering the
  affected content. Ask for the authoritative value; do not choose silently.
- Record any lossless transformation of data, such as unit conversion or category
  reordering, in the updated brief and manifest.

## Execution and validation

After revision, validate only what was actually produced:

1. Compare the revised figure with the operator's request and the source-of-truth
   artifacts.
2. Re-run the ten review criteria on the revised artifact.
3. Inspect legibility at the intended publication width, panel order, margins,
   label spelling, caption agreement, and reference-style alignment.
4. For conceptual figures, verify that the full visible meaning came from the
   image-generation result and that no semantic code overlay was used.
5. For quantitative figures, compare rendered series and annotations against the
   normalized data and source map.
6. For hybrid figures, validate each panel under its own policy and inspect the
   assembled result only after all panels are actually available.
7. Convert to PDF only when a PDF tool is available and the conversion is
   actually run. Claim PDF success only after opening or rendering the resulting
   PDF for inspection.
8. Claim LaTeX or PGFPlots compilation success only if compilation was actually
   run and the output was inspected. Otherwise say `source prepared; compilation
   not run`.

## Deliverables

Prefer downloadable artifacts in this order, including only items actually
created or updated:

1. Revised final figure in its submission format.
2. Raw conceptual PNG and, when actually converted, the matching PDF.
3. Updated `figureX_brief.md`.
4. Updated target entries for `figure_plan.md` and `figure_prompts.md` when
   applicable.
5. Plotting script and normalized data for quantitative content.
6. A manifest recording source files, figure classification, renderer used,
   transformations, output filenames, and validation actually performed.
7. A ZIP containing only the artifacts that genuinely exist.

Suggested logical archive destinations are:

```text
papers/<paper_name>/submit/figureX_xxx.pdf
papers/<paper_name>/drafts/figureX_raw.png
papers/<paper_name>/drafts/figureX_brief.md
papers/<paper_name>/figure_plan.md
papers/<paper_name>/figure_prompts.md
papers/<paper_name>/drafts/figureX_plot.py
papers/<paper_name>/drafts/figureX_data.csv
papers/<paper_name>/drafts/figureX_manifest.md
```

Use only the destinations appropriate to this figure and preserve the actual
established basename. These paths remain logical until the user downloads and
archives the files.

If ZIP creation is unavailable, provide each actual artifact individually. If a
**text file** cannot be attached, provide its complete contents in a clearly
labeled code block and state that the user must save it. Binary PNG/PDF artifacts
must never be simulated with a code block, prose, base64-like filler, or a fake
link; report them as not delivered until the real files are available. Never
claim that a missing attachment or logical-path file was created.

## Final report

At the end, report concisely and exactly:

- confirmed `paper_name` and `figureX`;
- figure classification and renderer actually used;
- what changed in the visual, brief, plan, prompt, script, data, and caption;
- the authoritative data or claim sources used;
- validation actually performed and any checks not run;
- every artifact actually provided, with its download filename;
- any unresolved limitation, especially label accuracy, missing raw artwork,
  missing data, unavailable conversion, or unrun compilation.

Do not report a successful revision if the required artifact was not actually
produced and inspected.
