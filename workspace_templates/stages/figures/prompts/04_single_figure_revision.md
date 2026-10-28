# Prompt 04: Single Figure Revision

Task:

Revise one existing figure for a paper.

Rendering policy:

- Follow `figure_plan.md` and the existing figure brief for this figure.
- Conceptual schematic revisions must use image2.
- Quantitative data figure revisions must use explicit numeric data and a reproducible plotting workflow.
- Hybrid revisions should keep conceptual and quantitative panels separate; conceptual panels must use image2.
- Table revisions should stay as LaTeX tables.
- Do not invent data or use image2 to fabricate numeric axes, bars, curves, or heatmaps.

Inputs:

```text
papers/<paper_name>/input.tex
papers/<paper_name>/figure_plan.md
papers/<paper_name>/figure_prompts.md
papers/<paper_name>/drafts/figureX_brief.md
papers/<paper_name>/submit/figureX_xxx.pdf
refs/
```

## Source of truth for the target figure

The revision request identifies which figure to revise. Do not invent a new
generic figure concept. For the target figure, use the existing Step 2/Step 3
design artifacts as the source of truth:

1. Locate the target figure's section in `papers/<paper_name>/figure_plan.md`.
2. Read `papers/<paper_name>/figure_prompts.md`.
3. Read the matching `papers/<paper_name>/drafts/figureX_brief.md`.
4. Reuse the figure-specific visual thesis, panel layout, required elements,
   caption intent, renderer, data source, image2 prompt, and negative prompt.
5. Treat the operator's revision request as constraints on that existing design,
   not as permission to replace the figure with a generic technology image.

If `figure_prompts.md` is missing because this paper was created by an older
pipeline, reconstruct it before revising the figure. Use the target figure
section of `figure_plan.md`, the matching `drafts/figureX_brief.md`,
`input.tex`, and the reference-board notes. Save the reconstructed prompt into
`figure_prompts.md` and the matching brief before calling imagegen/image2.

If `figureX_brief.md` only contains a short image2 prompt summary, reconstruct
a full image2 prompt from the target figure section of `figure_plan.md`,
`input.tex`, and the reference-board notes before generating the image. Save the
full prompt back into `figureX_brief.md` and `figure_prompts.md`.

For conceptual figures, the raw image2 output must be a paper-specific academic
systems figure. Avoid generic stock-style AI art, photorealistic laptops,
glowing dashboards, decorative 3D hardware, random chip icons, or unrelated
cloud/server illustrations. Do not use a generic image2 prompt and then patch
labels over it.

For conceptual figures, generate a raster PNG with the Codex imagegen/image2
tool. The final paper can wrap that PNG in PDF, but the main visual content must
not be a matplotlib/TikZ/vector drawing. If the output would be vector-only or
PDF-only, stop and report the renderer-policy violation instead of accepting it.

The imagegen/image2 output must be the complete visible conceptual figure, not
only a background for later text overlays. Do not add titles, module names,
callouts, arrows, or labels onto the generated raster with matplotlib, TikZ,
PIL, Figma/Illustrator, or other code. Only non-semantic wrapping/cropping/
resizing/conversion is allowed after generation. If the requested revision needs
guaranteed exact numeric axes or many exact values, switch that figure to a
quantitative plotting workflow instead of mixing generated art with code
overlays.

First review the current figure against the following criteria:

1. Does it have a clear visual thesis?
2. Does it look like a top-conference paper figure rather than a generic slide?
3. Is it too much like a box-and-arrow pipeline?
4. Is the panel layout clear?
5. Is the text density too high?
6. Are labels concise?
7. Are all numbers traceable to input.tex?
8. Is it readable in two-column format?
9. Does the caption match the figure?
10. Does it follow the intended reference style?

Then revise the figure.

Revision rules:

- Preserve the paper's actual claims.
- Preserve the target figure's planned visual thesis and layout unless the
  revision request explicitly asks to change them.
- Do not invent data.
- Improve visual hierarchy.
- Reduce unnecessary text.
- Make the main contrast or insight more visually obvious.
- Regenerate or edit the figure with the renderer required by its figure type.
- For conceptual figures, call imagegen/image2 using the target figure's full
  figure-specific prompt from `figure_prompts.md` and the brief, or reconstruct
  it first from `figure_plan.md`; do not use a generic prompt.
- For conceptual figures, make imagegen/image2 render the complete visible
  figure including short labels and callouts; do not use code overlays.
- Keep output as a PDF if the submission requires PDF figures.
- Keep the raw image2 PNG output in `drafts/` for conceptual schematics and conceptual hybrid panels.
- Keep plotting scripts and normalized data when reproducible plotting is used.
- Update the design brief if the figure meaning changes.

Save the revised figure back into:

```text
papers/<paper_name>/submit/
```

Also update:

```text
papers/<paper_name>/drafts/figureX_brief.md
```

At the end, report exactly what changed.

Also confirm which renderer was used and how the output was validated.
