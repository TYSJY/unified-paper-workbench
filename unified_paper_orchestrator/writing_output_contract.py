from __future__ import annotations

import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .writing_completion import expected_step_artifacts
from .writing_evidence_gate import FORBIDDEN_DRAFT_MARKERS


_DOCUMENT_CLASS_RE = re.compile(r"\\documentclass(?:\[[^\]]*\])?\{[^}]+\}")
_SECTION_RE = re.compile(r"\\section\*?\s*\{", re.IGNORECASE)
_BIBITEM_RE = re.compile(r"\\bibitem(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_CITE_RE = re.compile(r"\\cite\w*\s*(?:\[[^\]]*\]\s*)*\{([^}]+)\}")
_FIGURE_RE = re.compile(r"\\begin\s*\{figure\*?\}", re.IGNORECASE)
_TABLE_RE = re.compile(r"\\begin\s*\{table\*?\}", re.IGNORECASE)
_VERIFY_START_RE = re.compile(r"(?m)^\s*%\s*VERIFY_DATA_START\s*:\s*(\S.*?)\s*$")
_VERIFY_END_RE = re.compile(r"(?m)^\s*%\s*VERIFY_DATA_END\s*$")
_SIMULATED_STATUS_RE = re.compile(r"(?m)^\s*%\s*DATA_STATUS\s*:\s*SIMULATED_TO_REPLACE\s*$")
_RAW_VERIFY_DATA_RE = re.compile(r"(?<![\\A-Za-z0-9])VERIFY_DATA(?![A-Za-z0-9])")
_URL_RE = re.compile(r"https://[^\s<>{}\[\]|\"']+", re.IGNORECASE)
_LATEX_MATH_ENVIRONMENTS = frozenset(
    {
        "align",
        "align*",
        "aligned",
        "alignedat",
        "alignedat*",
        "alignat",
        "alignat*",
        "array",
        "Bmatrix",
        "bmatrix",
        "bmatrix*",
        "cases",
        "dcases",
        "dcases*",
        "displaymath",
        "equation",
        "equation*",
        "eqnarray",
        "eqnarray*",
        "flalign",
        "flalign*",
        "gather",
        "gather*",
        "gathered",
        "math",
        "matrix",
        "matrix*",
        "multline",
        "multline*",
        "multlined",
        "pmatrix",
        "pmatrix*",
        "smallmatrix",
        "split",
        "Vmatrix",
        "vmatrix",
        "vmatrix*",
    }
)
_LATEX_VERBATIM_ENVIRONMENTS = frozenset(
    {
        "Verbatim",
        "Verbatim*",
        "lstlisting",
        "minted",
        "verbatim",
        "verbatim*",
        "verbatimout",
        "verbatimtab",
    }
)
_LATEX_PROTECTED_ARGUMENT_COMMANDS = frozenset(
    {
        "Cpageref",
        "Cref",
        "autoref",
        "bibliography",
        "bibliographystyle",
        "bibitem",
        "cpageref",
        "cref",
        "eqref",
        "href",
        "include",
        "includegraphics",
        "includegraphics*",
        "input",
        "label",
        "nameref",
        "pageref",
        "path",
        "ref",
        "url",
        "vref",
    }
)
_LATEX_PROTECTED_TWO_ARGUMENT_COMMANDS = frozenset(
    {
        "DeclareRobustCommand",
        "DeclareRobustCommand*",
        "newcommand",
        "newcommand*",
        "providecommand",
        "providecommand*",
        "renewcommand",
        "renewcommand*",
    }
)
_LATEX_CITATION_COMMANDS = frozenset(
    {
        "autocite",
        "autocites",
        "cite",
        "cite*",
        "citealp",
        "citealt",
        "citeauthor",
        "citep",
        "citep*",
        "cites",
        "citet",
        "citet*",
        "citeyear",
        "citeyearpar",
        "footcite",
        "parencite",
        "smartcite",
        "supercite",
        "textcite",
    }
)
_UNICODE_VERIFY_MARKER = "【验】"
_LATEX_COMPILE_CONTRACT_SCHEMA = "unified-paper-orchestrator/latex-compile-contract/v1"
WRITING_RETRY_VALIDATOR_FEEDBACK_SCHEMA = (
    "unified-paper-orchestrator/writing-retry-validator-feedback/v1"
)
_WRITING_RETRY_MAX_RAW_ISSUES = 50
_WRITING_RETRY_MAX_ISSUES = 20

# Retry prompts must never interpolate validator prose read from the journal.
# Each stable validator code is translated back to a bounded, controller-owned
# instruction here.  This keeps the retry useful without turning the journal
# into an arbitrary prompt-injection channel.
WRITING_RETRY_ISSUE_MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        "latex_unescaped_verify_data_identifier": (
            r"Write VERIFY\_DATA in LaTeX prose while leaving embedded VERIFY_DATA comment markers unchanged."
        ),
        "latex_unescaped_text_underscore": (
            r"Escape every raw underscore in LaTeX prose (for example, write io\_uring and novelty\_check.md) "
            "without changing math subscripts, citation or bibliography keys, labels, URLs, paths, comments, "
            "or verbatim content."
        ),
        "latex_documentclass_missing": r"Add the required LaTeX \documentclass declaration.",
        "latex_document_begin_missing": r"Add the required \begin{document} marker.",
        "latex_document_end_missing": r"Add the required \end{document} marker.",
        "latex_bibliography_missing": r"Add the required \begin{thebibliography} block.",
        "latex_sections_insufficient": "Meet the tuned prompt's minimum complete-paper section count.",
        "latex_bibitems_insufficient": "Meet the tuned prompt's minimum unique-bibitem count.",
        "latex_bibitem_duplicate": "Make every bibliography item key unique.",
        "latex_bibitems_uncited": "Cite every bibliography item in the draft.",
        "latex_figures_insufficient": "Meet the tuned prompt's minimum figure count for this step.",
        "latex_tables_insufficient": "Meet the tuned prompt's minimum table count for this step.",
        "latex_verify_data_contract_invalid": (
            "Restore the required balanced VERIFY_DATA comment blocks and DATA_STATUS markers."
        ),
        "latex_verify_data_label_duplicate": "Make every VERIFY_DATA block label unique.",
        "latex_forbidden_marker_remaining": "Remove every forbidden draft marker identified by the tuned contract.",
        "idea_decision_card_missing": "Create the required Idea Decision Card.",
        "idea_required_heading_missing": "Add every required Idea Decision Card heading.",
        "idea_scouting_sources_insufficient": "Meet the tuned prompt's minimum unique scouting-source count.",
        "reference_count_insufficient": (
            "Create at least 50 distinct structured reference records as required by Prompt 02."
        ),
        "reference_bib_count_insufficient": (
            "Create at least 50 complete, parseable, distinct BibTeX records as required by Prompt 02."
        ),
        "reference_bib_parse_error": "Repair every malformed or unterminated BibTeX record.",
        "reference_bib_key_duplicate": "Make every BibTeX entry key unique.",
        "reference_bib_identity_missing": (
            "Give every BibTeX record a real arXiv id, DOI, or official HTTPS URL."
        ),
        "reference_identity_alignment_insufficient": (
            "Bind at least 50 distinct Markdown reference records one-to-one to BibTeX records by real identity."
        ),
        "verified_references_insufficient": (
            "Individually verify and mark at least 50 identity-aligned references; do not invent verification."
        ),
        "reference_author_missing_or_placeholder": (
            "Replace missing or placeholder reference authors with verified real author metadata."
        ),
        "novelty_questions_incomplete": "Answer all five numbered novelty questions required by Prompt 02.",
        "novelty_decision_missing": (
            "State one explicit evidence-based keep, modify, or abandon decision for the idea."
        ),
        "novelty_decision_ambiguous": (
            "Resolve the novelty recommendation to one unambiguous evidence-based keep, modify, or abandon decision."
        ),
        "review_required_heading_missing": "Add every required review heading.",
        # Kept for journals produced by the immediately preceding validator
        # vocabulary; it remains a stable, non-freeform repair instruction.
        "missing_required_section": "Add every required section specified by the tuned prompt.",
    }
)

_WRITING_RETRY_COUNT_KEYS = frozenset(
    {
        "raw_verify_data_identifier_count",
        "unescaped_text_underscore_count",
        "section_count",
        "bibitem_count",
        "citation_key_count",
        "figure_count",
        "table_count",
        "verify_data_block_count",
        "scouting_source_count",
        "review_heading_count",
        "reference_count",
        "verified_reference_count",
        "bib_entry_count",
        "identity_match_count",
    }
)

_IDEA_REQUIRED_HEADINGS = (
    "Idea ID",
    "Top 候选比较摘要",
    "文献侦察摘要",
    "第 2 步必须重点验证的 claims",
    "最终决定",
)

_REVIEW_REQUIRED_HEADINGS = (
    "Overall Verdict",
    "One-sentence Reason",
    "Summary",
    "Strengths",
    "Major Weaknesses",
    "Rejection-level Issues",
    "Reviewer A: Systems Review",
    "Reviewer B: ML Efficiency Review",
    "Reviewer C: Novelty Review",
    "Reviewer D: Writing and Presentation Review",
    "Reviewer E: Figure and Evidence Review",
    "Figure and Quantitative Evidence Review",
    "Claim-Evidence Problems",
    "Required Revisions",
    "Final Action Plan",
)


def _issue(issues: list[dict[str, str]], code: str, message: str) -> None:
    issues.append({"code": code, "message": message})


def writing_retry_validator_feedback(validation: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return only bounded, allowlisted semantic-validator retry facts.

    Messages in ``validation`` are deliberately ignored.  A caller may pass a
    journal snapshot here, but only stable issue codes, canonical messages, and
    allowlisted integer counts can reach a model execution envelope.
    """

    if validation.get("ready") is not False:
        return None
    raw_issues = validation.get("issues")
    if not isinstance(raw_issues, list) or not raw_issues or len(raw_issues) > _WRITING_RETRY_MAX_RAW_ISSUES:
        return None
    issues: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_issue in raw_issues:
        if not isinstance(raw_issue, Mapping):
            return None
        raw_code = raw_issue.get("code")
        if not isinstance(raw_code, str):
            return None
        code = raw_code.strip()
        message = WRITING_RETRY_ISSUE_MESSAGES.get(code)
        if message is None:
            return None
        if code in seen:
            continue
        seen.add(code)
        issues.append({"code": code, "message": message})
        if len(issues) > _WRITING_RETRY_MAX_ISSUES:
            return None

    raw_counts = validation.get("counts")
    counts: dict[str, int] = {}
    if isinstance(raw_counts, Mapping):
        for key in sorted(_WRITING_RETRY_COUNT_KEYS):
            value = raw_counts.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000:
                counts[key] = value
    return {
        "schema": WRITING_RETRY_VALIDATOR_FEEDBACK_SCHEMA,
        "ready": False,
        "issues": issues,
        "counts": counts,
    }


def _strip_latex_comments(text: str) -> str:
    cleaned: list[str] = []
    for line in text.splitlines():
        kept: list[str] = []
        escaped = False
        for character in line:
            if character == "%" and not escaped:
                break
            kept.append(character)
            escaped = character == "\\" and not escaped
            if character != "\\":
                escaped = False
        cleaned.append("".join(kept))
    return "\n".join(cleaned)


def _citation_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for raw in _CITE_RE.findall(text):
        keys.update(key.strip() for key in raw.split(",") if key.strip())
    return keys


def _latex_command(source: str, index: int) -> tuple[str, int]:
    """Read one TeX control sequence starting at ``index`` in bounded time."""

    cursor = index + 1
    if cursor >= len(source):
        return "", cursor
    if source[cursor].isalpha() or source[cursor] == "@":
        start = cursor
        while cursor < len(source) and (source[cursor].isalpha() or source[cursor] == "@"):
            cursor += 1
        command = source[start:cursor]
        if cursor < len(source) and source[cursor] == "*":
            cursor += 1
            command += "*"
        return command, cursor
    return source[cursor], cursor + 1


def _braced_token(source: str, index: int) -> tuple[str, int] | None:
    """Read one short environment-name token with bounded look-ahead."""

    cursor = index
    while cursor < len(source) and source[cursor].isspace():
        cursor += 1
    if cursor >= len(source) or source[cursor] != "{":
        return None
    token_start = cursor + 1
    token_limit = min(len(source), token_start + 256)
    cursor = token_start
    while cursor < token_limit:
        character = source[cursor]
        if character == "\\":
            cursor = min(cursor + 2, token_limit)
            continue
        if character == "}":
            return source[token_start:cursor], cursor + 1
        if character == "{":
            return None
        cursor += 1
    return None


def _unescaped_text_underscore_count(source: str) -> int:
    """Count raw prose underscores with a deterministic linear TeX scanner.

    Regexes that try to match an entire math expression can backtrack
    catastrophically when a delimiter is missing.  This scanner instead keeps
    explicit delimiter/environment state and consumes each source region once.
    Protected command arguments are handled by a small pending-argument stack;
    this also keeps optional display text visible to prose validation.
    """

    count = 0
    index = 0
    math_delimiter: str | None = None
    math_environments: list[str] = []
    math_candidate_underscores = 0
    verbatim_environment: str | None = None
    verbatim_candidate_underscores = 0
    protected_group_depth = 0
    protected_candidate_underscores = 0
    # Each pending command waits for its first mandatory argument while any
    # optional ``[...]`` groups remain ordinary, validated LaTeX prose.
    pending_arguments: list[dict[str, int]] = []

    while index < len(source):
        character = source[index]

        if verbatim_environment is not None:
            if character == "\\":
                command, command_end = _latex_command(source, index)
                if command == "end":
                    token = _braced_token(source, command_end)
                    if token is not None and token[0].strip() == verbatim_environment:
                        verbatim_environment = None
                        verbatim_candidate_underscores = 0
                        index = token[1]
                        continue
            elif character == "_":
                verbatim_candidate_underscores += 1
            index += 1
            continue

        if protected_group_depth:
            if character == "%":
                newline = source.find("\n", index + 1)
                index = len(source) if newline < 0 else newline
                continue
            if character == "\\":
                _, index = _latex_command(source, index)
                continue
            if character == "{":
                protected_group_depth += 1
            elif character == "}":
                protected_group_depth -= 1
                if protected_group_depth == 0:
                    protected_candidate_underscores = 0
                    pending = pending_arguments[-1]
                    pending["remaining"] -= 1
                    if pending["remaining"] == 0:
                        pending_arguments.pop()
            elif character == "_":
                protected_candidate_underscores += 1
            index += 1
            continue

        if math_delimiter is not None:
            if character == "%":
                newline = source.find("\n", index + 1)
                index = len(source) if newline < 0 else newline
                continue
            if math_delimiter == "$$":
                if source.startswith("$$", index):
                    math_delimiter = None
                    math_candidate_underscores = 0
                    index += 2
                    continue
            elif math_delimiter == "$":
                if character == "$":
                    math_delimiter = None
                    math_candidate_underscores = 0
                    index += 1
                    continue
            elif math_delimiter == r"\[":
                if source.startswith(r"\]", index):
                    math_delimiter = None
                    math_candidate_underscores = 0
                    index += 2
                    continue
            elif math_delimiter == r"\(" and source.startswith(r"\)", index):
                math_delimiter = None
                math_candidate_underscores = 0
                index += 2
                continue
            if character == "\\":
                _, index = _latex_command(source, index)
            elif character == "_":
                math_candidate_underscores += 1
                index += 1
            else:
                index += 1
            continue

        if math_environments:
            if character == "%":
                newline = source.find("\n", index + 1)
                index = len(source) if newline < 0 else newline
                continue
            if character == "\\":
                command, command_end = _latex_command(source, index)
                if command in {"begin", "end"}:
                    token = _braced_token(source, command_end)
                    if token is not None:
                        environment = token[0].strip()
                        if command == "begin" and environment in _LATEX_MATH_ENVIRONMENTS:
                            math_environments.append(environment)
                        elif command == "end" and environment == math_environments[-1]:
                            math_environments.pop()
                            if not math_environments:
                                math_candidate_underscores = 0
                        index = token[1]
                        continue
                index = command_end
            elif character == "_":
                math_candidate_underscores += 1
                index += 1
            else:
                index += 1
            continue

        if character == "%":
            newline = source.find("\n", index + 1)
            index = len(source) if newline < 0 else newline
            continue

        # Resolve a protected command's first mandatory argument without
        # hiding optional display text.  Nested protected commands are stacked.
        reprocess = True
        while pending_arguments and reprocess:
            pending = pending_arguments[-1]
            optional_depth = pending["optional_depth"]
            optional_brace_depth = pending["optional_brace_depth"]
            if optional_depth:
                if character == "{" and optional_brace_depth >= 0:
                    pending["optional_brace_depth"] += 1
                elif character == "}" and optional_brace_depth:
                    pending["optional_brace_depth"] -= 1
                elif optional_brace_depth == 0 and character == "[":
                    pending["optional_depth"] += 1
                elif optional_brace_depth == 0 and character == "]":
                    pending["optional_depth"] -= 1
                reprocess = False
            elif character.isspace():
                reprocess = False
            elif character == "[":
                pending["optional_depth"] = 1
                reprocess = False
            elif character == "{":
                protected_group_depth = 1
                protected_candidate_underscores = 0
                index += 1
                reprocess = False
                character = ""
            else:
                pending_arguments.pop()
                reprocess = True
        if character == "":
            continue

        if character == "$":
            if source.startswith("$$", index):
                math_delimiter = "$$"
                math_candidate_underscores = 0
                index += 2
            else:
                math_delimiter = "$"
                math_candidate_underscores = 0
                index += 1
            continue

        if character == "\\":
            command, command_end = _latex_command(source, index)
            if command == "[":
                math_delimiter = r"\["
                math_candidate_underscores = 0
                index = command_end
                continue
            if command == "(":
                math_delimiter = r"\("
                math_candidate_underscores = 0
                index = command_end
                continue
            if command in {"begin", "end"}:
                token = _braced_token(source, command_end)
                if token is not None:
                    environment = token[0].strip()
                    if command == "begin" and environment in _LATEX_MATH_ENVIRONMENTS:
                        math_environments.append(environment)
                        math_candidate_underscores = 0
                    elif command == "begin" and environment in _LATEX_VERBATIM_ENVIRONMENTS:
                        verbatim_environment = environment
                        verbatim_candidate_underscores = 0
                    index = token[1]
                    continue
            if command == "verb":
                cursor = command_end
                if cursor < len(source):
                    delimiter = source[cursor]
                    if not delimiter.isalnum() and not delimiter.isspace():
                        line_end = source.find("\n", cursor + 1)
                        if line_end < 0:
                            line_end = len(source)
                        closing = source.find(delimiter, cursor + 1, line_end)
                        if closing >= 0:
                            index = closing + 1
                        else:
                            count += source.count("_", cursor + 1, line_end)
                            index = line_end
                        continue
            if command in _LATEX_CITATION_COMMANDS or command in _LATEX_PROTECTED_ARGUMENT_COMMANDS:
                pending_arguments.append(
                    {"optional_depth": 0, "optional_brace_depth": 0, "remaining": 1}
                )
            elif command in _LATEX_PROTECTED_TWO_ARGUMENT_COMMANDS:
                # Macro replacement text is tokenized at definition time; a
                # math subscript there is not prose and must not be rewritten.
                pending_arguments.append(
                    {"optional_depth": 0, "optional_brace_depth": 0, "remaining": 2}
                )
            index = command_end
            continue

        if character == "_":
            count += 1
        index += 1

    if math_delimiter is not None or math_environments:
        count += math_candidate_underscores
    if protected_group_depth:
        count += protected_candidate_underscores
    if verbatim_environment is not None:
        count += verbatim_candidate_underscores
    return count


def _latex_compile_contract(source: str) -> dict[str, Any]:
    marker_count = source.count(_UNICODE_VERIFY_MARKER)
    if marker_count:
        return {
            "schema": _LATEX_COMPILE_CONTRACT_SCHEMA,
            "profile": "unicode_cjk_verification_marker",
            "preferred_engine": "xelatex",
            "compatible_engines": ["xelatex", "lualatex"],
            "requires_cjk_font": True,
            "pdflatex_marker_compatible": False,
            "unicode_verification_marker_count": marker_count,
            "validation_scope": "deterministic_source_preflight_no_compiler_invocation",
        }
    return {
        "schema": _LATEX_COMPILE_CONTRACT_SCHEMA,
        "profile": "default_latex",
        "preferred_engine": "pdflatex",
        "compatible_engines": ["pdflatex", "xelatex", "lualatex"],
        "requires_cjk_font": False,
        "pdflatex_marker_compatible": True,
        "unicode_verification_marker_count": 0,
        "validation_scope": "deterministic_source_preflight_no_compiler_invocation",
    }


def _validate_latex(
    text: str,
    step: int,
    issues: list[dict[str, str]],
    warnings: list[dict[str, str]],
    counts: dict[str, int],
    result: dict[str, Any],
    *,
    is_terminal: bool = False,
    minimums: tuple[int, int, int, int] = (6, 50, 5, 4),
) -> None:
    source = _strip_latex_comments(text)
    result["compile_contract"] = _latex_compile_contract(source)
    # Steps 3-5 are intermediate manuscripts.  Step 6 is the first workflow
    # step that owns final verification-marker cleanup and LaTeX hygiene.  A
    # raw underscore in an intermediate draft therefore remains visible as a
    # warning, but must not deadlock the Step 5 -> manual-data -> Step 6 route.
    # If a custom workflow ends before Step 6, ``is_terminal`` keeps the same
    # finding blocking so an invalid terminal manuscript is never accepted.
    defer_latex_cleanup = step < 6 and not is_terminal
    latex_cleanup_findings = warnings if defer_latex_cleanup else issues
    raw_verify_data_count = len(_RAW_VERIFY_DATA_RE.findall(source))
    counts["raw_verify_data_identifier_count"] = raw_verify_data_count
    if raw_verify_data_count:
        _issue(
            latex_cleanup_findings,
            "latex_unescaped_verify_data_identifier",
            (
                "LaTeX prose contains raw VERIFY_DATA; keep embedded VERIFY_DATA comment markers unchanged, "
                r"but write prose as VERIFY\_DATA"
                + ("; cleanup is deferred to Step 6" if defer_latex_cleanup else "")
            ),
        )
    # This scanner handles comments itself so percent characters inside
    # ``\verb`` and verbatim environments retain their real TeX meaning.
    unescaped_underscore_count = _unescaped_text_underscore_count(text)
    counts["unescaped_text_underscore_count"] = unescaped_underscore_count
    if unescaped_underscore_count:
        _issue(
            latex_cleanup_findings,
            "latex_unescaped_text_underscore",
            (
                "LaTeX prose contains raw underscores outside protected math, citation, bibliography, label, "
                "URL, path, comment, or verbatim contexts; escape them as \\_."
                + (" Cleanup is deferred to Step 6." if defer_latex_cleanup else "")
            ),
        )
    if not _DOCUMENT_CLASS_RE.search(source):
        _issue(issues, "latex_documentclass_missing", "the draft must contain a LaTeX documentclass")
    for marker, code in (
        (r"\begin{document}", "latex_document_begin_missing"),
        (r"\end{document}", "latex_document_end_missing"),
        (r"\begin{thebibliography}", "latex_bibliography_missing"),
    ):
        if marker not in source:
            _issue(issues, code, f"the draft must contain {marker}")

    section_count = len(_SECTION_RE.findall(source))
    counts["section_count"] = section_count
    min_sections, min_references, min_figures, min_tables = minimums
    if section_count < min_sections:
        _issue(
            issues,
            "latex_sections_insufficient",
            f"a complete paper requires at least {min_sections} sections; found {section_count}",
        )

    bib_keys = [key.strip() for key in _BIBITEM_RE.findall(source) if key.strip()]
    unique_bib_keys = set(bib_keys)
    cited_keys = _citation_keys(source)
    counts["bibitem_count"] = len(unique_bib_keys)
    counts["citation_key_count"] = len(cited_keys)
    if len(unique_bib_keys) < min_references:
        _issue(
            issues,
            "latex_bibitems_insufficient",
            f"the prompt requires at least {min_references} unique bibitems; found {len(unique_bib_keys)}",
        )
    if len(bib_keys) != len(unique_bib_keys):
        _issue(issues, "latex_bibitem_duplicate", "bibitem keys must be unique")
    uncited = sorted(unique_bib_keys - cited_keys)
    if uncited:
        _issue(issues, "latex_bibitems_uncited", f"every bibitem must be cited; uncited count={len(uncited)}")

    figure_count = len(_FIGURE_RE.findall(source))
    table_count = len(_TABLE_RE.findall(source))
    counts["figure_count"] = figure_count
    counts["table_count"] = table_count
    if step >= 5 and figure_count < min_figures:
        _issue(issues, "latex_figures_insufficient", f"Step {step} requires at least {min_figures} figures; found {figure_count}")
    if step >= 5 and table_count < min_tables:
        _issue(issues, "latex_tables_insufficient", f"Step {step} requires at least {min_tables} tables; found {table_count}")

    if step == 5:
        starts = _VERIFY_START_RE.findall(text)
        ends = len(_VERIFY_END_RE.findall(text))
        statuses = len(_SIMULATED_STATUS_RE.findall(text))
        counts["verify_data_block_count"] = len(starts)
        simulated = "SIMULATED_TO_REPLACE" in text or r"\VERIFY" in text or _UNICODE_VERIFY_MARKER in text
        if simulated and (len(starts) < 3 or ends != len(starts) or statuses != len(starts)):
            _issue(
                issues,
                "latex_verify_data_contract_invalid",
                "simulated Step 5 output requires at least 3 balanced VERIFY_DATA blocks with DATA_STATUS",
            )
        if len(starts) != len(set(starts)):
            _issue(issues, "latex_verify_data_label_duplicate", "VERIFY_DATA labels must be unique")

    if step >= 6:
        for label, pattern in FORBIDDEN_DRAFT_MARKERS:
            match = pattern.search(source)
            if match is None:
                continue
            line = source.count("\n", 0, match.start()) + 1
            if step < 10 and not is_terminal:
                _issue(
                    warnings,
                    "latex_forbidden_marker_deferred",
                    f"Step {step} still contains {label} at line {line}; cleanup remains due by Step 10",
                )
            else:
                _issue(
                    issues,
                    "latex_forbidden_marker_remaining",
                    f"Step {step} still contains {label} at line {line}",
                )


def _validate_idea(text: str, issues: list[dict[str, str]], counts: dict[str, int]) -> None:
    if "# Idea Decision Card" not in text:
        _issue(issues, "idea_decision_card_missing", "Step 1 must create one Idea Decision Card")
    for heading in _IDEA_REQUIRED_HEADINGS:
        if not re.search(rf"(?m)^##\s+{re.escape(heading)}\s*$", text):
            _issue(issues, "idea_required_heading_missing", f"missing required idea heading: {heading}")
    source_count = len(set(_URL_RE.findall(text)))
    counts["scouting_source_count"] = source_count
    if source_count < 8:
        _issue(
            issues,
            "idea_scouting_sources_insufficient",
            f"the final card requires at least 8 source URLs; found {source_count}",
        )


def _validate_review(text: str, issues: list[dict[str, str]], counts: dict[str, int]) -> None:
    headings = {match.group(1).strip() for match in re.finditer(r"(?m)^##\s+(.+?)\s*$", text)}
    counts["review_heading_count"] = len(headings)
    for heading in _REVIEW_REQUIRED_HEADINGS:
        if heading not in headings:
            _issue(issues, "review_required_heading_missing", f"missing required review heading: {heading}")


def validate_writing_step_output(
    root: Path | str,
    papers_dir: Path | str,
    paper_id: str,
    step: int,
    *,
    is_terminal: bool = False,
) -> dict[str, Any]:
    """Validate semantic requirements already present in the tuned step prompt."""

    result: dict[str, Any] = {
        "paper_id": str(paper_id),
        "step": int(step),
        "status": "BLOCKED",
        "ready": False,
        "counts": {},
        "issues": [],
        "warnings": [],
        "is_terminal": bool(is_terminal),
    }
    issues: list[dict[str, str]] = result["issues"]
    warnings: list[dict[str, str]] = result["warnings"]
    paths = expected_step_artifacts(Path(root), Path(papers_dir), str(paper_id), int(step))
    if len(paths) != 1:
        _issue(issues, "semantic_validator_not_applicable", "semantic validation requires one step-owned output")
        return result
    path = paths[0]
    result["path"] = str(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        _issue(issues, "semantic_output_unreadable", f"cannot read output: {type(exc).__name__}")
        return result
    if step == 1:
        _validate_idea(text, issues, result["counts"])
    elif step == 7:
        _validate_review(text, issues, result["counts"])
    elif step in {3, 4, 5, 6, 8, 9, 10}:
        _validate_latex(
            text,
            step,
            issues,
            warnings,
            result["counts"],
            result,
            is_terminal=is_terminal,
        )
    else:
        _issue(issues, "semantic_validator_not_applicable", f"no semantic validator is defined for Step {step}")
    if not issues:
        result["status"] = "READY_WITH_WARNINGS" if warnings else "READY"
        result["ready"] = True
    return result


__all__ = [
    "WRITING_RETRY_ISSUE_MESSAGES",
    "WRITING_RETRY_VALIDATOR_FEEDBACK_SCHEMA",
    "validate_writing_step_output",
    "writing_retry_validator_feedback",
]
