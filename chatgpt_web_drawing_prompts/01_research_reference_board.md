# Prompt 01: Research Reference Board — ChatGPT Web Version

## Before you start

Use this prompt in ChatGPT on the web. Attach the target paper's `input.tex`, or make it available in the current ChatGPT Project, before running this step. You may also attach an existing `reference_board.md`, `notes.md`, `images_manifest.md`, `reference_images_manifest.md`, and any reference screenshots that should be reused.

Turn on **Deep Research** when it is available. Otherwise use **Search**. This step requires live web research; do not substitute remembered citations for an actual search.

Before doing any research, uniquely confirm `paper_name` from both:

1. the uploaded/Project file name or enclosing user-provided paper identifier; and
2. the title, author block, project name, or other identifying content inside `input.tex`.

If `input.tex` is missing, inaccessible, empty, or there is more than one plausible paper, stop immediately. Do not research, draft, or create files. Reply only with a concise list of the missing or ambiguous inputs. If the paper itself is clear but its archive-safe `paper_name` is not, ask the user to provide the exact `paper_name`.

## ChatGPT web execution contract

- You may read only files attached in the current chat, files available to the current ChatGPT Project, and text pasted into the conversation. Do not claim to inspect a computer, terminal, repository, or unshared folder.
- Paths such as `papers/<paper_name>/...` and `refs/<field_name>/...` are **logical archive paths** used to organize the deliverable. They are not evidence that you read from or wrote to the user's machine.
- Never say that a file was saved on the user's device, written to a desktop path, or verified on disk. State exactly which downloadable files you actually created in ChatGPT.
- Prefer creating downloadable files with the exact filenames and logical directory structure specified below. Package multiple outputs as `reference_board_<paper_name>.zip`.
- If this ChatGPT session cannot create downloadable attachments, provide the complete content of every required text file in a separate fenced code block. Put the exact logical archive path immediately above each block. Do not abbreviate content with ellipses.
- Include a file in the ZIP only if it was actually created or extracted during this chat. Never create a placeholder image and present it as a screenshot from a paper.
- Do not claim that Deep Research, Search, a PDF inspection, a screenshot extraction, image inspection, or file generation happened unless the corresponding tool/action actually completed in this session.
- If a referenced Project file cannot actually be opened, treat it as unavailable and say so. Do not infer its contents from its filename.

## Task

Read the confirmed `input.tex` and build a research reference board for the paper. Do **not** generate any paper figures in this step.

### Rendering policy for later steps

The active prompt policy overrides any conflicting instruction in an older `figure_plan.md`.

- Conceptual schematic figures must use **ChatGPT Image Generation** in later generation steps.
- Quantitative data figures must be generated only from explicit numeric data with a reproducible workflow such as **Data Analysis / Python**, PGFPlots, R/ggplot, MATLAB, or an equivalent reproducible plotting method.
- Hybrid figures must keep the conceptual and quantitative panels separate. Use **ChatGPT Image Generation** only for conceptual panels and a reproducible data workflow for quantitative panels, then combine the independently generated panels in a reproducible layout.
- Tables must remain LaTeX tables, not image-only figures.
- Reference-board research must collect visual inspiration and data-figure design patterns, not templates or artwork to copy.

## 1. Identify the paper and field

From `input.tex`, identify the paper's exact field, including:

- task;
- method type;
- system setting;
- target conference style;
- key related areas;
- likely figure types needed.

Choose a concise, archive-safe `field_name`. Record the evidence used to infer it. Do not silently invent a target conference if it is not stated; label it `not specified` and, if useful, discuss the closest relevant publication styles as recommendations rather than facts.

## 2. Audit available prior reference material

Inspect only the relevant reference-board materials that are actually attached to this chat or accessible in the current Project, such as:

```text
refs/<field_name>/notes.md
refs/<field_name>/images_manifest.md
refs/<field_name>/images/
papers/<paper_name>/reference_board.md
papers/<paper_name>/reference_images_manifest.md
```

These are logical archive paths. Do not attempt to browse the user's device filesystem or assume that an unshared `refs/` folder exists.

If an available existing reference board is relevant, update and extend it. If the supplied board belongs to a different field, preserve useful cross-field lessons but create a distinct board for the selected `field_name`. If no relevant board is supplied, create this logical package structure:

```text
refs/<field_name>/
|-- images/                       # only screenshots actually extracted or supplied
|-- images_manifest.md
`-- notes.md

papers/<paper_name>/
|-- reference_board.md
`-- reference_images_manifest.md
```

## 3. Required web research

Using Deep Research or Search, research and analyze at least **20** top-conference or highly relevant papers from related areas, as applicable to this paper. Candidate areas include:

1. LLM serving systems;
2. efficient inference;
3. KV-cache optimization;
4. LLM quantization or compression;
5. edge or on-device LLM inference;
6. confidence calibration;
7. selective prediction or abstention;
8. LLM routing or cascading;
9. resource-aware or cost-aware inference;
10. risk-constrained inference;
11. efficient ML systems.

Prioritize venues such as:

- NeurIPS;
- ICML;
- ICLR;
- MLSys;
- OSDI;
- SOSP;
- ASPLOS;
- EuroSys;
- ACL;
- EMNLP.

Adapt the search areas to the actual paper. Do not force irrelevant categories merely to fill a quota.

For every reference paper, provide a clickable link to the **original paper or authoritative publication source**: an official proceedings page, publisher page, DOI, author-hosted paper page, or arXiv paper page. Do not cite a search-results page, an unsourced image repost, or an invented URL. Verify the title, venue, and year against the linked source.

For each paper, write:

- paper name;
- venue and year;
- clickable original-paper or publisher source;
- relevant figure number and, when available, page number;
- figure type;
- what Figure 1, the system overview, or the method overview shows;
- visual narrative structure;
- panel organization;
- whether it uses a problem → solution structure;
- whether it contains a teaser plot;
- useful design elements;
- elements that must not be copied;
- how it can inspire this paper.

Clearly distinguish observations confirmed by inspecting the paper from inferences based only on accessible metadata or text. Never claim to have seen a figure that was not actually accessible.

## 4. Best-effort reference screenshots

Try to assemble a concrete, reusable reference-image package, but screenshots are **best-effort and non-blocking**. Failure to download, extract, inspect, or package a screenshot must not stop this step.

When practical, maintain a field-level pool of up to **50** reference screenshots. If the user supplied a useful existing pool, reuse it and add only missing high-value patterns. Do not duplicate the same paper or visual pattern simply to increase the count. Do not exceed 50 images for one field without an explicit user request.

Prioritize actual screenshots or figure crops of:

- Figure 1 or a teaser;
- system and method overviews;
- controller diagrams;
- memory or resource-state visualizations;
- workflow or state-machine figures;
- mechanism illustrations;
- representative tradeoff or Pareto figures.

Use stable logical filenames only for images that were actually supplied, downloaded, or extracted:

```text
refs/<field_name>/images/ref01_<short_paper_slug>_fig1.png
refs/<field_name>/images/ref02_<short_paper_slug>_system_overview.png
refs/<field_name>/images/ref03_<short_paper_slug>_method.png
...
```

Do not use ChatGPT Image Generation to recreate a source-paper figure. Do not fabricate a crop, screenshot, filename, or inspection result.

If the web interface cannot obtain a requested screenshot, record an `unavailable` entry in both manifests. That entry must contain the source URL, intended figure number or page, and the exact reason it was unavailable. The manifest entry—not a fake image—is the required fallback. Examples of valid reasons include inaccessible PDF, paywall, image extraction unavailable in this session, figure number not verifiable, or source image resolution too low.

Whenever one or more intended screenshots remain unavailable, also create these two text records:

```text
refs/<field_name>/missing_reference_images.md
papers/<paper_name>/missing_reference_images.md
```

Each record must consolidate the unavailable items, intended stable filenames, authoritative links, figure/page identifiers, exact collection instructions, and exact failure reasons. These files supplement the manifests; they do not authorize placeholder or fabricated screenshots. Omit them only when every listed reference image is actually available and verified in the generated package.

If a PDF blocks figure extraction, a real page screenshot or browser screenshot of the figure area may be used only when the interface actually supports and completes that action. If an individual figure remains inaccessible after reasonable attempts, prefer another accessible, equally relevant top-conference paper. Retain the unavailable record when the missing paper is especially important.

For every `available` image, record metadata in both:

```text
refs/<field_name>/images_manifest.md
papers/<paper_name>/reference_images_manifest.md
```

Each available-image entry must include:

- logical archive path and exact ZIP member name;
- screenshot status: `available`;
- source paper title;
- venue and year;
- clickable source URL, DOI, or open-access PDF URL;
- source figure number or page number;
- how the screenshot was obtained in this chat;
- what visual pattern it shows;
- what can inspire this paper;
- what must not be copied;
- notice: `Research reference only; do not publish, trace, or reuse directly.`

Each unavailable-image entry must include:

- intended stable filename;
- screenshot status: `unavailable`;
- source paper title;
- venue and year;
- clickable source URL;
- intended source figure number or page number;
- exact missing reason;
- what visual pattern was sought;
- what can still be learned from verified text or metadata;
- an explicit statement that the figure itself was not visually inspected.

Reference screenshots are for research and visual analysis only. Do not copy, trace, publish, or directly reuse them in final paper figures. Final conceptual figures must be newly generated from the target paper's own content and paper-specific prompts for **ChatGPT Image Generation**.

## 5. Reference taxonomy

Classify the references into:

A. problem–solution teaser;
B. system architecture;
C. core-insight illustration;
D. tradeoff or Pareto-frontier plot;
E. runtime controller or routing diagram;
F. memory or resource-state visualization;
G. calibration, risk, or uncertainty visualization.

A paper may appear in more than one category when justified. Keep the classification useful for figure planning rather than mechanically exhaustive.

## 6. Required deliverables

Create the following complete text files using these exact logical archive paths:

```text
refs/<field_name>/notes.md
refs/<field_name>/images_manifest.md
papers/<paper_name>/reference_board.md
papers/<paper_name>/reference_images_manifest.md
```

When any intended screenshot is unavailable, also create:

```text
refs/<field_name>/missing_reference_images.md
papers/<paper_name>/missing_reference_images.md
```

If actual screenshots were obtained, also include them under:

```text
refs/<field_name>/images/
```

`refs/<field_name>/notes.md` must contain:

- the field definition and paper-fit rationale;
- search method and research date;
- at least 20 verified reference papers;
- the required per-paper analysis;
- the A–G classification;
- cross-paper design patterns;
- recommended visual patterns;
- anti-copying and originality notes.

`papers/<paper_name>/reference_board.md` must record:

- the confirmed `paper_name`;
- the chosen `field_name`;
- the logical reference-board path under `refs/`;
- the logical screenshot directory;
- the paper-specific screenshot manifest path;
- the screenshot availability summary and all non-blocking limitations;
- the target conference style, or `not specified`;
- the 5–10 most relevant reference papers to learn from;
- the available reference screenshots most useful for Step 02, if any;
- the size of the reusable field reference pool, capped at 50 actual images;
- the figure styles most worth carrying into Step 02;
- the design elements to avoid;
- a reminder that screenshots are research references only and final figures must be original.

Both manifests must use the same stable IDs and distinguish `available` from `unavailable` entries. Do not count unavailable entries toward the screenshot-pool size.

## 7. Internet or tool failure fallback

Because this step requires live research, do not present a memory-only list as completed research.

If Deep Research and Search are both unavailable or fail:

1. state that the research requirement was not completed;
2. provide a structured TODO list of paper topics, target venues, suggested search queries, and figure types to collect manually;
3. draft only the portions of `notes.md` and `reference_board.md` that can be supported by `input.tex` and supplied files;
4. mark all unverified bibliographic entries as TODO rather than facts;
5. do not claim that the complete reference board or ZIP was generated unless it actually was.

## 8. Final verification and response

Before finishing, verify from the outputs actually created in this chat that:

- `paper_name` and `field_name` are consistent across all files;
- `papers/<paper_name>/reference_board.md` is complete;
- `refs/<field_name>/notes.md` contains at least 20 researched papers when live research succeeded;
- both manifests exist as complete text outputs;
- every cited paper has a clickable authoritative or original-paper source;
- every available screenshot exists in the generated package and has matching provenance metadata;
- every unavailable screenshot has a URL, figure/page identifier, exact missing reason, and a statement that it was not visually inspected;
- both `missing_reference_images.md` records exist and agree with the manifests whenever any intended screenshot is unavailable;
- no unavailable image is counted in the pool;
- the reference-image pool contains no more than 50 actual images;
- no paper figure was generated in this step.

Deliver `reference_board_<paper_name>.zip` when file creation is available. Briefly list the ZIP contents, the number of researched papers, the number of actual screenshots, the number of unavailable screenshot records, and any tool limitations. If file creation is unavailable, use the complete-code-block fallback from the execution contract.

Do not block Step 02 solely because screenshots are missing. Do not generate any final paper figure in this step.
