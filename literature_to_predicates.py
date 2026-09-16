"""Extract source-grounded research predicates from one or more PDF papers.

Each saved predicate contains a fact, an optional equation, its variable
definitions, the assumptions required for the fact to apply, and precise source
locators. Mathematical notation is stored as ASCII-safe LaTeX. The output can be
supplied directly to MIMI through ``--background`` or the Level 1 Background
control in the web interface.
"""

from __future__ import annotations

import argparse
import base64
import glob
import hashlib
import json
import mimetypes
import os
import re
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from MIMI_credentials import configure_openai_api_key


SCHEMA_VERSION = "mimi.predicates.quantitative.v2"
DEFAULT_MODEL = os.environ.get("MIMI_PREDICATE_MODEL", "gpt-5-mini")
DEFAULT_FOCUS = (
    "mechanical seal design and analysis, including tribology, lubrication, leakage, "
    "materials, geometry, thermal behaviour, deformation, reliability, and operating limits"
)
MAX_PDF_BYTES = 50 * 1024 * 1024
SUPPORTED_REFERENCE_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
}


class ExtractionAbandoned(RuntimeError):
    """Raised when a caller abandons an in-progress multi-PDF extraction."""


class StrictModel(BaseModel):
    """Base class used to reject fields outside the extraction contract."""

    model_config = ConfigDict(extra="forbid")


class SourceMetadata(StrictModel):
    title: str | None = Field(description="Paper title, or null when it cannot be established.")
    authors: list[str] = Field(description="Authors as printed in the paper; use an empty list if absent.")
    publication_year: int | None = Field(description="Publication year, or null when absent or ambiguous.")
    doi: str | None = Field(description="DOI without a URL prefix, or null when absent.")
    publication_venue: str | None = Field(description="Journal, conference, report series, or null.")


class ExtractedVariable(StrictModel):
    """Fixed-shape API representation converted to a symbol-keyed saved object."""

    symbol: str = Field(
        description=(
            "Variable symbol written in ASCII-safe LaTeX, such as C_d or \\alpha. "
            "Never use Unicode Greek letters, superscripts, or mathematical operators."
        )
    )
    definition: str = Field(
        description=(
            "Meaning of the symbol, including its reported unit in square brackets when known. "
            "Do not infer a definition or unit that the source does not establish."
        )
    )


class ExtractedSourceLocator(StrictModel):
    """Location within the supplied paper; the stable source ID is added locally."""

    page: str | None = Field(
        description="Printed page label, or a PDF page locator when no printed label is visible; otherwise null."
    )
    section: str | None = Field(description="Section number or heading supporting the predicate, or null.")
    equation: str | None = Field(description="Equation number or label supporting the predicate, or null.")
    figure: str | None = Field(description="Figure number or label supporting the predicate, or null.")
    table: str | None = Field(description="Table number or label supporting the predicate, or null.")


class ExtractedPredicate(StrictModel):
    fact: str = Field(
        description=(
            "One faithful, standalone fact or conclusion reported by the paper. Preserve "
            "negation, uncertainty, values, units, ranges, entities, and causal strength."
        )
    )
    equation: str | None = Field(
        description=(
            "One equation or quantitative relation supporting the fact, transcribed as "
            "ASCII-safe LaTeX without math delimiters, or null when the fact has no equation."
        )
    )
    variables: list[ExtractedVariable] = Field(
        description=(
            "Definitions for symbols in equation. The application converts these fixed-shape "
            "pairs into a symbol-keyed variables object in the saved predicate."
        )
    )
    assumptions: list[str] = Field(
        description=(
            "Conditions required for the fact to apply, including physical/model assumptions, "
            "system and material scope, boundary or initial conditions, tested ranges, and "
            "operating conditions. Use an empty list only when none can be established."
        )
    )
    sources: list[ExtractedSourceLocator] = Field(
        description=(
            "One or more precise locations in the supplied paper supporting the predicate. "
            "Use null for a locator component that cannot be established."
        )
    )


class PaperPredicateExtraction(StrictModel):
    source: SourceMetadata
    predicates: list[ExtractedPredicate]


class RankedPredicateOrder(StrictModel):
    ordered_predicate_ids: list[str] = Field(
        description=(
            "Every supplied predicate ID exactly once, ordered from most to least useful "
            "for completing the task specification."
        )
    )


EXTRACTION_SYSTEM_PROMPT = r"""You are a conservative scientific-claim extraction agent. Convert one supplied
paper into source-grounded predicates for later engineering research.

The output schema is a data contract, not permission to guess. Follow these rules:

1. The word fact means a proposition reported or concluded by this paper. It does
   not mean the proposition has been independently proven true.
2. Each predicate describes exactly one atomic fact and, when present, exactly
   one equation. Do not add commentary, evidence scores, confidence labels,
   quotations, or extra fields.
3. Extract only predicates relevant to the stated research focus.
4. Do not turn material merely mentioned in related work into a fact asserted by
   this paper. Preserve negation, uncertainty, direction, and causal strength.
5. Never merge different experiments, operating regimes, materials, populations,
   equations, or definitions into one fact. Create separate predicates instead.
6. Put every condition needed for applicability in assumptions: physical and
   mathematical assumptions, system/material/geometry scope, boundary and initial
   conditions, tested ranges, and operating conditions.
7. Keep assumptions atomic: one condition per string. Do not repeat the fact in
   the assumptions list. Use an empty list only when no assumption can be found.
8. Preserve reported values, units, signs, coefficients, ranges, and uncertainty.
   Never use outside knowledge to fill missing information.
9. Actively inspect equations, correlations, definitions, tables, figures, and
   surrounding prose. Prefer quantitative predicates that can later support an
   engineering calculation, while retaining important qualitative predicates.
10. Put the mathematical relation itself in equation. Transcribe it faithfully
    into LaTeX without $ delimiters. Use null only when the predicate genuinely
    has no equation. Do not derive, rearrange, or complete an equation unless the
    paper itself presents that form.
11. Use ASCII-safe LaTeX for every mathematical symbol in facts, equations,
    variable symbols and definitions, and assumptions. Never emit Unicode Greek
    or mathematical glyphs. For example use \alpha not α, \mu not μ, \pi not π,
    x^2 not x², \sqrt{x} not √x, \le not ≤, and \pm not ±.
12. Define each equation symbol once in variables. Include reported units in
    square brackets in the definition. Omit variables whose meaning cannot be
    established instead of guessing. For a qualitative predicate, return null
    for equation and an empty variables list.
13. Every predicate must include at least one source locator. Record the printed
    page plus equation, section, figure, or table label whenever visible. If a
    printed page label is unavailable, identify the PDF page instead. Use null
    only for locator components that cannot be established.
14. A supplied task specification is relevance context only, not a factual
    source. Use it to decide what would help complete the task, but never extract
    its statements as predicates or cite it as evidence. Every predicate must be
    supported by the attached paper.
15. Supplied reference images are also relevance context only. Use them to
    understand the task's geometry, components, layout, and intended result, but
    never treat an image as literature evidence or cite it as a predicate source.
"""


RANKING_SYSTEM_PROMPT = r"""You rank already-extracted, source-grounded predicates by their usefulness for
completing one engineering task. You do not rewrite, merge, remove, or add
predicates. Return every supplied predicate ID exactly once, from most to least
useful. Rank globally across all papers: never group or alternate predicates by
paper merely because they share a source. The task specification and reference
images define relevance but are not evidence."""


GREEK_TO_LATEX = {
    "Α": "A", "Β": "B", "Γ": r"\Gamma", "Δ": r"\Delta", "Ε": "E",
    "Ζ": "Z", "Η": "H", "Θ": r"\Theta", "Ι": "I", "Κ": "K",
    "Λ": r"\Lambda", "Μ": "M", "Ν": "N", "Ξ": r"\Xi", "Ο": "O",
    "Π": r"\Pi", "Ρ": "P", "Σ": r"\Sigma", "Τ": "T", "Υ": r"\Upsilon",
    "Φ": r"\Phi", "Χ": "X", "Ψ": r"\Psi", "Ω": r"\Omega",
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta",
    "ε": r"\epsilon", "ζ": r"\zeta", "η": r"\eta", "θ": r"\theta",
    "ι": r"\iota", "κ": r"\kappa", "λ": r"\lambda", "μ": r"\mu",
    "µ": r"\mu", "ν": r"\nu", "ξ": r"\xi", "ο": "o", "π": r"\pi",
    "ρ": r"\rho", "σ": r"\sigma", "ς": r"\varsigma", "τ": r"\tau",
    "υ": r"\upsilon", "φ": r"\phi", "χ": r"\chi", "ψ": r"\psi",
    "ω": r"\omega", "ϵ": r"\varepsilon", "ϑ": r"\vartheta",
    "ϖ": r"\varpi", "ϱ": r"\varrho", "ϕ": r"\varphi",
}

MATH_TO_LATEX = {
    "−": "-", "–": "-", "×": r"\times", "÷": r"\div", "·": r"\cdot",
    "±": r"\pm", "∓": r"\mp", "≤": r"\le", "≥": r"\ge",
    "≠": r"\ne", "≈": r"\approx", "≃": r"\simeq", "∝": r"\propto",
    "∞": r"\infty", "∑": r"\sum", "∏": r"\prod", "∫": r"\int",
    "∂": r"\partial", "∇": r"\nabla", "°": r"^{\circ}",
}

_SUPERSCRIPT_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ"
SUPERSCRIPT_TO_ASCII = str.maketrans({
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5",
    "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9", "⁺": "+", "⁻": "-",
    "⁼": "=", "⁽": "(", "⁾": ")", "ⁿ": "n", "ⁱ": "i",
})
_SUBSCRIPT_CHARS = "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"
SUBSCRIPT_TO_ASCII = str.maketrans({
    "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4", "₅": "5",
    "₆": "6", "₇": "7", "₈": "8", "₉": "9", "₊": "+", "₋": "-",
    "₌": "=", "₍": "(", "₎": ")", "ₐ": "a", "ₑ": "e", "ₕ": "h",
    "ᵢ": "i", "ⱼ": "j", "ₖ": "k", "ₗ": "l", "ₘ": "m", "ₙ": "n",
    "ₒ": "o", "ₚ": "p", "ᵣ": "r", "ₛ": "s", "ₜ": "t", "ᵤ": "u",
    "ᵥ": "v", "ₓ": "x",
})
_SUPERSCRIPT_PATTERN = re.compile("[" + re.escape(_SUPERSCRIPT_CHARS) + "]+")
_SUBSCRIPT_PATTERN = re.compile("[" + re.escape(_SUBSCRIPT_CHARS) + "]+")
_SQUARE_ROOT_PATTERN = re.compile(r"√\s*(\([^()]*\)|[A-Za-z0-9_]+)")


def normalize_latex_notation(value: str | None) -> str | None:
    """Replace common Unicode mathematics with stable ASCII-safe LaTeX."""

    if value is None:
        return None
    text = _SUPERSCRIPT_PATTERN.sub(
        lambda match: "^{" + match.group(0).translate(SUPERSCRIPT_TO_ASCII) + "}",
        value,
    )
    text = _SUBSCRIPT_PATTERN.sub(
        lambda match: "_{" + match.group(0).translate(SUBSCRIPT_TO_ASCII) + "}",
        text,
    )
    text = _SQUARE_ROOT_PATTERN.sub(
        lambda match: r"\sqrt{" + match.group(1).strip("()") + "}",
        text,
    )
    text = text.replace("√", r"\sqrt{}")

    def replace_symbols(current: str, replacements: dict[str, str]) -> str:
        for symbol, latex in replacements.items():
            source = current
            current = re.sub(
                re.escape(symbol),
                lambda match: (
                    latex + " "
                    if latex.startswith("\\")
                    and match.end() < len(source)
                    and source[match.end()].isascii()
                    and source[match.end()].isalpha()
                    else latex
                ),
                source,
            )
        return current

    text = replace_symbols(text, GREEK_TO_LATEX)
    text = replace_symbols(text, MATH_TO_LATEX)
    return text.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_id(file_hash: str) -> str:
    return f"SRC_{file_hash[:12].upper()}"


def collect_pdf_paths(values: list[str], *, recursive: bool = False) -> list[Path]:
    """Resolve files, directories, and glob patterns into unique PDF paths."""

    discovered: list[Path] = []
    for raw_value in values:
        candidate = Path(raw_value).expanduser()
        if candidate.is_file():
            discovered.append(candidate)
            continue
        if candidate.is_dir():
            iterator = candidate.rglob("*.pdf") if recursive else candidate.glob("*.pdf")
            discovered.extend(iterator)
            continue
        discovered.extend(Path(match) for match in glob.glob(raw_value, recursive=recursive))

    unique: dict[str, Path] = {}
    for path in discovered:
        resolved = path.resolve()
        if resolved.suffix.lower() != ".pdf" or not resolved.is_file():
            continue
        size = resolved.stat().st_size
        if size <= 0:
            raise ValueError(f"PDF is empty: {resolved}")
        if size >= MAX_PDF_BYTES:
            raise ValueError(f"PDF must be under 50 MB: {resolved}")
        unique[str(resolved).casefold()] = resolved
    return sorted(unique.values(), key=lambda item: str(item).casefold())


def extraction_prompt(
    *,
    focus: str,
    research_question: str | None,
    max_predicates: int,
    task_spec: str | None = None,
    task_spec_name: str | None = None,
    reference_image_names: list[str] | None = None,
) -> str:
    question = research_question or "No narrower research question was supplied."
    general_prompt = f"""\
RESEARCH FOCUS:
{focus}

CURRENT RESEARCH QUESTION:
{question}

Extract up to {max_predicates} distinct, decision-relevant predicates from the attached
paper. Prefer facts that affect design choices, equations/models, material or
geometry selection, operating limits, leakage, friction, wear, thermal behaviour,
reliability, validation, or known failure modes. Include the assumptions needed
to prevent misuse of those facts. Return fewer predicates rather
than padding the result with unsupported or repetitive statements.
"""
    if not task_spec:
        return general_prompt

    name = task_spec_name or "uploaded task specification"
    image_names = reference_image_names or []
    image_context = ""
    if image_names:
        rendered_names = "\n".join(f"- {image_name}" for image_name in image_names)
        image_context = f"""

REFERENCE IMAGES TO INTERPRET:
{rendered_names}

Use the attached reference images to understand the task's geometry, components,
layout, and intended result. They control relevance only: they are not literature
evidence and must never be cited as a predicate source.
"""
    return f"""\
RESEARCH FOCUS:
{focus}

CURRENT RESEARCH QUESTION:
{question}

TASK SPECIFICATION TO SUPPORT ({name}):
--- BEGIN TASK SPECIFICATION ---
{task_spec}
--- END TASK SPECIFICATION ---
{image_context}

Build a larger internal candidate list, rank it by direct usefulness to the task,
then return at most the strongest {max_predicates} distinct predicates from this
paper. Return them from most to least useful. A predicate qualifies only when it
directly satisfies a task requirement, supplies an equation/model/input/
coefficient/constraint needed to implement or analyse it, supplies validation or
comparison data, or prevents a concrete task error. Merely sharing the task's
topic is not enough. Prefer actionable quantitative information; retain a
qualitative predicate only when it changes a task decision or prevents misuse.

The task specification controls relevance only. It is not evidence and must not
be used as the source of a predicate. Every fact, equation, variable definition,
assumption, and source locator must be supported by the attached paper. Return
far fewer than {max_predicates} predicates when the paper contains little strong
task evidence. Exclude generic background, marginally relevant, unsupported,
duplicative, and nice-to-know statements.
"""


def _reference_image_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0]
    if mime_type not in SUPPORTED_REFERENCE_IMAGE_TYPES:
        supported = ", ".join(sorted(SUPPORTED_REFERENCE_IMAGE_TYPES))
        raise ValueError(
            f"Unsupported reference image type for {path.name}; use one of: {supported}."
        )
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def rank_predicates_by_task(
    client: OpenAI,
    database: dict,
    *,
    model: str,
    task_spec: str,
    task_spec_name: str | None,
    reference_image_paths: list[Path] | None,
    detail: Literal["auto", "low", "high"],
    reasoning_effort: Literal["low", "medium", "high"],
    max_output_tokens: int,
) -> list[str]:
    """Return all predicate IDs in one cross-paper task-relevance order."""

    original_ids = list(database["predicates"])
    if len(original_ids) <= 1:
        return original_ids

    candidates: list[dict] = []
    for predicate_id, predicate in database["predicates"].items():
        source_id = next(
            (
                source.get("source_id")
                for source in predicate.get("sources", [])
                if source.get("source_id")
            ),
            None,
        )
        source = database.get("sources", {}).get(source_id, {})
        candidates.append({
            "predicate_id": predicate_id,
            "source_file": source.get("file_name"),
            "source_title": source.get("metadata", {}).get("title"),
            "fact": predicate.get("fact"),
            "equation": predicate.get("equation"),
            "variables": predicate.get("variables", {}),
            "assumptions": predicate.get("assumptions", []),
        })

    name = task_spec_name or "uploaded task specification"
    prompt = f"""\
TASK SPECIFICATION ({name}):
--- BEGIN TASK SPECIFICATION ---
{task_spec}
--- END TASK SPECIFICATION ---

CANDIDATE PREDICATES:
{json.dumps(candidates, ensure_ascii=False, separators=(",", ":"))}

Rank every candidate by how directly it helps an agent build, calculate,
validate, or avoid errors in the requested tool. Put indispensable equations,
inputs, coefficients, constraints, and implementation facts first; then useful
validation and decision facts; then supporting context. Put tangential facts
last. Compare candidates across all sources and freely interleave papers. Do not
group predicates by paper. Return each predicate_id exactly once.
"""
    image_paths = reference_image_paths or []
    content: list[dict] = [
        {
            "type": "input_image",
            "image_url": _reference_image_data_url(image_path),
            "detail": detail,
        }
        for image_path in image_paths
    ]
    content.append({"type": "input_text", "text": prompt})
    response = client.responses.parse(
        model=model,
        instructions=RANKING_SYSTEM_PROMPT,
        input=[{"role": "user", "content": content}],
        reasoning={"effort": reasoning_effort},
        text_format=RankedPredicateOrder,
        max_output_tokens=max_output_tokens,
        store=False,
    )
    if response.output_parsed is None:
        detail_text = (response.output_text or "no parsed output").strip()
        raise RuntimeError(f"The predicate ranking response was not parseable: {detail_text[:500]}")

    known_ids = set(original_ids)
    seen: set[str] = set()
    ranked_ids: list[str] = []
    for predicate_id in response.output_parsed.ordered_predicate_ids:
        if predicate_id in known_ids and predicate_id not in seen:
            ranked_ids.append(predicate_id)
            seen.add(predicate_id)
    # A malformed or incomplete ranking must never silently discard evidence.
    ranked_ids.extend(predicate_id for predicate_id in original_ids if predicate_id not in seen)
    return ranked_ids


def extract_pdf(
    client: OpenAI,
    pdf_path: Path,
    *,
    model: str,
    focus: str,
    research_question: str | None,
    max_predicates: int,
    detail: Literal["auto", "low", "high"],
    reasoning_effort: Literal["low", "medium", "high"],
    max_output_tokens: int,
    task_spec: str | None = None,
    task_spec_name: str | None = None,
    reference_image_paths: list[Path] | None = None,
    keep_upload: bool = False,
) -> PaperPredicateExtraction:
    """Upload one PDF, extract a typed result, then remove the temporary upload."""

    uploaded = None
    try:
        with pdf_path.open("rb") as stream:
            uploaded = client.files.create(file=stream, purpose="user_data")
        image_paths = reference_image_paths or []
        content: list[dict] = [
            {
                "type": "input_file",
                "file_id": uploaded.id,
                "detail": detail,
            }
        ]
        content.extend(
            {
                "type": "input_image",
                "image_url": _reference_image_data_url(image_path),
                "detail": detail,
            }
            for image_path in image_paths
        )
        content.append(
            {
                "type": "input_text",
                "text": extraction_prompt(
                    focus=focus,
                    research_question=research_question,
                    max_predicates=max_predicates,
                    task_spec=task_spec,
                    task_spec_name=task_spec_name,
                    reference_image_names=[path.name for path in image_paths],
                ),
            }
        )
        response = client.responses.parse(
            model=model,
            instructions=EXTRACTION_SYSTEM_PROMPT,
            input=[
                {
                    "role": "user",
                    "content": content,
                }
            ],
            reasoning={"effort": reasoning_effort},
            text_format=PaperPredicateExtraction,
            max_output_tokens=max_output_tokens,
            store=False,
        )
        if response.output_parsed is None:
            detail_text = (response.output_text or "no parsed output").strip()
            raise RuntimeError(f"The extraction response was not parseable: {detail_text[:500]}")
        return response.output_parsed
    finally:
        if uploaded is not None and not keep_upload:
            try:
                client.files.delete(uploaded.id)
            except Exception as exc:  # cleanup failure must not hide a valid extraction
                print(
                    f"Warning: could not delete temporary API file {uploaded.id}: {exc}",
                    file=sys.stderr,
                )


def _saved_predicate(
    predicate: ExtractedPredicate,
    *,
    source_id: str,
) -> dict:
    """Convert the fixed-shape API object into the compact persisted contract."""

    variables: dict[str, str] = {}
    for variable in predicate.variables:
        symbol = normalize_latex_notation(variable.symbol) or ""
        definition = normalize_latex_notation(variable.definition) or ""
        if not symbol or not definition:
            continue
        if symbol in variables and definition != variables[symbol]:
            definitions = variables[symbol].split("; ")
            if definition not in definitions:
                variables[symbol] += f"; {definition}"
        else:
            variables[symbol] = definition

    sources: list[dict[str, str]] = []
    locators = predicate.sources or [
        ExtractedSourceLocator(
            page=None,
            section=None,
            equation=None,
            figure=None,
            table=None,
        )
    ]
    for locator in locators:
        source: dict[str, str] = {"source_id": source_id}
        for field_name in ("page", "section", "equation", "figure", "table"):
            value = normalize_latex_notation(getattr(locator, field_name))
            if value:
                source[field_name] = value
        if source not in sources:
            sources.append(source)

    equation = normalize_latex_notation(predicate.equation)
    return {
        "fact": normalize_latex_notation(predicate.fact) or predicate.fact,
        "equation": equation or None,
        "variables": variables,
        "assumptions": [
            normalized
            for assumption in predicate.assumptions
            if (normalized := normalize_latex_notation(assumption))
        ],
        "sources": sources,
    }


def _task_spec_metadata(task_spec: str | None, task_spec_name: str | None) -> dict | None:
    if not task_spec:
        return None
    return {
        "name": task_spec_name or "uploaded task specification",
        "sha256": hashlib.sha256(task_spec.encode("utf-8")).hexdigest(),
        "character_count": len(task_spec),
    }


def _reference_image_metadata(reference_image_paths: list[Path] | None) -> list[dict]:
    return [
        {
            "name": path.name,
            "sha256": _sha256(path),
            "file_size_bytes": path.stat().st_size,
        }
        for path in (reference_image_paths or [])
    ]


def new_database(
    *,
    focus: str,
    research_question: str | None,
    task_spec: str | None = None,
    task_spec_name: str | None = None,
    reference_image_paths: list[Path] | None = None,
) -> dict:
    task_metadata = _task_spec_metadata(task_spec, task_spec_name)
    reference_images = _reference_image_metadata(reference_image_paths)
    return {
        "schema_version": SCHEMA_VERSION,
        "purpose": "source-grounded background knowledge for MIMI",
        "selection_mode": "task_specific" if task_metadata else "general",
        "task_specification": task_metadata,
        "reference_images": reference_images,
        "research_focus": focus,
        "research_question": research_question,
        "fact_semantics": (
            "Each fact is a proposition reported by its source, not an independently verified truth."
        ),
        "extraction_runs": [],
        "sources": {},
        "predicates": {},
    }


def load_database(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Predicate database must contain a top-level JSON object.")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"Expected schema_version={SCHEMA_VERSION!r}; found {data.get('schema_version')!r}."
        )
    required_objects = ("sources", "predicates")
    if any(not isinstance(data.get(key), dict) for key in required_objects):
        raise ValueError(
            "Predicate database must contain sources and predicates objects."
        )
    data.setdefault("selection_mode", "general")
    data.setdefault("task_specification", None)
    data.setdefault("reference_images", [])
    data.setdefault("extraction_runs", [])
    return data


def add_extraction_to_database(
    database: dict,
    *,
    pdf_path: Path,
    extraction: PaperPredicateExtraction,
    model: str,
) -> tuple[str, int]:
    """Add one typed paper extraction to the dictionary-of-dictionaries database."""

    file_hash = _sha256(pdf_path)
    source_id = _source_id(file_hash)
    if source_id in database["sources"]:
        return source_id, 0

    paper = extraction.model_dump(mode="json")
    paper.pop("predicates")
    database["sources"][source_id] = {
        "source_id": source_id,
        "file_name": pdf_path.name,
        "original_path": str(pdf_path.resolve()),
        "sha256": file_hash,
        "file_size_bytes": pdf_path.stat().st_size,
        "extraction_model": model,
        "metadata": paper.pop("source"),
    }

    for index, predicate in enumerate(extraction.predicates, start=1):
        predicate_id = f"{source_id}_P{index:04d}"
        database["predicates"][predicate_id] = _saved_predicate(
            predicate,
            source_id=source_id,
        )
    return source_id, len(extraction.predicates)


def write_database(path: Path, database: dict, *, allow_overwrite: bool) -> None:
    path = path.expanduser().resolve()
    if path.exists() and not allow_overwrite:
        raise FileExistsError(
            f"Output already exists: {path}. Use --append or --force to replace/update it."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(database, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def extract_pdfs_to_database(
    pdf_paths: list[Path],
    *,
    model: str = DEFAULT_MODEL,
    focus: str = DEFAULT_FOCUS,
    research_question: str | None = None,
    task_spec: str | None = None,
    task_spec_name: str | None = None,
    reference_image_paths: list[Path] | None = None,
    max_predicates: int = 60,
    detail: Literal["auto", "low", "high"] = "high",
    reasoning_effort: Literal["low", "medium", "high"] = "medium",
    max_output_tokens: int = 50_000,
    keep_uploads: bool = False,
    database: dict | None = None,
    client: OpenAI | None = None,
    progress: Callable[[int, int, Path, str], None] | None = None,
    should_abandon: Callable[[], bool] | None = None,
) -> tuple[dict, int]:
    """Extract validated PDFs into a new or existing predicate database.

    This is the shared application API used by both the command-line tool and the
    browser upload route. ``progress`` receives the one-based file position, total
    file count, source path, and a short status string after each paper.
    """

    def ensure_active() -> None:
        if should_abandon and should_abandon():
            raise ExtractionAbandoned("Literature predicate extraction was abandoned.")

    ensure_active()
    if not pdf_paths:
        raise ValueError("No PDF files were found in the supplied inputs.")
    if not 1 <= max_predicates <= 250:
        raise ValueError("max_predicates must be between 1 and 250.")
    if max_output_tokens < 1_000:
        raise ValueError("max_output_tokens must be at least 1000.")
    reference_image_paths = [path.resolve() for path in (reference_image_paths or [])]
    if reference_image_paths and not task_spec:
        raise ValueError("Reference images require a task specification.")
    for image_path in reference_image_paths:
        if not image_path.is_file():
            raise ValueError(f"Reference image was not found: {image_path}")
        _reference_image_data_url(image_path)

    database = database or new_database(
        focus=focus,
        research_question=research_question,
        task_spec=task_spec,
        task_spec_name=task_spec_name,
        reference_image_paths=reference_image_paths,
    )
    task_metadata = _task_spec_metadata(task_spec, task_spec_name)
    reference_images = _reference_image_metadata(reference_image_paths)
    database.setdefault("reference_images", reference_images)
    database.setdefault("extraction_runs", []).append({
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "detail": detail,
        "reasoning_effort": reasoning_effort,
        "max_predicates_per_paper": max_predicates,
        "research_focus": focus,
        "research_question": research_question,
        "selection_mode": "task_specific" if task_metadata else "general",
        "task_specification": task_metadata,
        "reference_images": reference_images,
        "input_files": [path.name for path in pdf_paths],
        "predicate_order": (
            "task_relevance_across_all_papers"
            if task_metadata and len(pdf_paths) > 1
            else "task_relevance_within_paper"
            if task_metadata
            else "source_order"
        ),
    })

    if client is None:
        configure_openai_api_key()
        client = OpenAI()

    added_predicates = 0
    total = len(pdf_paths)
    for position, pdf_path in enumerate(pdf_paths, start=1):
        ensure_active()
        existing_source_id = _source_id(_sha256(pdf_path))
        if existing_source_id in database["sources"]:
            if progress:
                progress(position, total, pdf_path, "already present; skipped")
            continue
        extraction = extract_pdf(
            client,
            pdf_path,
            model=model,
            focus=focus,
            research_question=research_question,
            task_spec=task_spec,
            task_spec_name=task_spec_name,
            reference_image_paths=reference_image_paths,
            max_predicates=max_predicates,
            detail=detail,
            reasoning_effort=reasoning_effort,
            max_output_tokens=max_output_tokens,
            keep_upload=keep_uploads,
        )
        if len(extraction.predicates) > max_predicates:
            extraction = extraction.model_copy(
                update={"predicates": extraction.predicates[:max_predicates]}
            )
        # An in-flight API response cannot always be interrupted safely. Discard
        # it at the first boundary after the request if the caller abandoned it.
        ensure_active()
        source_id, predicate_count = add_extraction_to_database(
            database,
            pdf_path=pdf_path,
            extraction=extraction,
            model=model,
        )
        added_predicates += predicate_count
        if progress:
            progress(position, total, pdf_path, f"{source_id}: {predicate_count} predicates")

    if task_spec and len(database["sources"]) > 1 and len(database["predicates"]) > 1:
        ensure_active()
        ordered_ids = rank_predicates_by_task(
            client,
            database,
            model=model,
            task_spec=task_spec,
            task_spec_name=task_spec_name,
            reference_image_paths=reference_image_paths,
            detail=detail,
            reasoning_effort=reasoning_effort,
            max_output_tokens=max_output_tokens,
        )
        ensure_active()
        database["predicates"] = {
            predicate_id: database["predicates"][predicate_id]
            for predicate_id in ordered_ids
        }

    ensure_active()
    return database, added_predicates


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract source-grounded predicates from scientific PDF literature."
    )
    parser.add_argument("pdfs", nargs="*", help="PDF files, directories, or glob patterns.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("seal_predicates.json"),
        help="Predicate database JSON path (default: seal_predicates.json).",
    )
    parser.add_argument("--append", action="store_true", help="Append new PDFs to an existing database.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing output database.")
    parser.add_argument("--recursive", action="store_true", help="Search supplied directories recursively.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--focus",
        default=DEFAULT_FOCUS,
    )
    parser.add_argument("--research-question", default=None)
    parser.add_argument(
        "--max-predicates",
        "--max-claims",
        dest="max_predicates",
        type=int,
        default=60,
        help="Maximum predicates extracted per paper (default: 60).",
    )
    parser.add_argument("--max-output-tokens", type=int, default=50_000)
    parser.add_argument("--detail", choices=("auto", "low", "high"), default="high")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"), default="medium")
    parser.add_argument(
        "--keep-uploads",
        action="store_true",
        help="Do not delete temporary PDFs uploaded to the OpenAI Files API.",
    )
    parser.add_argument(
        "--print-schema",
        action="store_true",
        help="Print the agent's structured-output JSON Schema and exit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.print_schema:
        print(json.dumps(PaperPredicateExtraction.model_json_schema(), indent=2))
        return 0
    if args.append and args.force:
        raise ValueError("Choose either --append or --force, not both.")
    if not 1 <= args.max_predicates <= 250:
        raise ValueError("--max-predicates must be between 1 and 250.")
    if args.max_output_tokens < 1_000:
        raise ValueError("--max-output-tokens must be at least 1000.")

    pdf_paths = collect_pdf_paths(args.pdfs, recursive=args.recursive)
    if not pdf_paths:
        raise ValueError("No PDF files were found in the supplied inputs.")

    output_path = args.output.expanduser().resolve()
    if output_path.exists() and not args.append and not args.force:
        raise FileExistsError(
            f"Output already exists: {output_path}. Use --append or --force before extraction."
        )
    if args.append:
        database = load_database(output_path) if output_path.exists() else new_database(
            focus=args.focus,
            research_question=args.research_question,
        )
    else:
        database = new_database(focus=args.focus, research_question=args.research_question)

    def report_progress(position: int, total: int, path: Path, status: str) -> None:
        print(f"[{position}/{total}] {path.name}: {status}", flush=True)

    database, added_predicates = extract_pdfs_to_database(
        pdf_paths,
        model=args.model,
        focus=args.focus,
        research_question=args.research_question,
        max_predicates=args.max_predicates,
        detail=args.detail,
        reasoning_effort=args.reasoning_effort,
        max_output_tokens=args.max_output_tokens,
        keep_uploads=args.keep_uploads,
        database=database,
        progress=report_progress,
    )

    write_database(
        output_path,
        database,
        allow_overwrite=args.force or args.append,
    )
    print(
        f"Wrote {added_predicates} new predicates from {len(pdf_paths)} PDF(s) to {output_path}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
