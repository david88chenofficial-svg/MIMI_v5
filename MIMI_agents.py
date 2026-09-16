"""OpenAI Agents used for planning, task shaping, documentation, and verification.

Coding is intentionally not represented by an Agents SDK agent.  It is handled by
the Codex SDK against the real run workspace (see :mod:`MIMI_codex`).
"""

from typing import Literal

from agents import Agent, AgentOutputSchema, ModelSettings
from openai.types.shared import Reasoning
from pydantic import BaseModel, Field

from MIMI_credentials import configure_openai_api_key


# Loading the dashboard must not require credentials.  A key is required only
# when a real run starts.
configure_openai_api_key(required=False)


class FileDescription(BaseModel):
    """One ordered, self-contained unit of work."""

    title: str = Field(description="Short task title")
    instructions: str = Field(
        description="Complete task text, including equations and acceptance criteria"
    )
    is_Coding_Team_required: bool
    insights_from_overview: str = Field(
        description="Context learned here that later tasks should retain"
    )


class BrokenTask(BaseModel):
    tasks: list[FileDescription]


class DocumentedInterface(BaseModel):
    name: str
    kind: Literal["function", "class", "constant", "command", "other"]
    signature: str
    purpose: str


class FileDocumentation(BaseModel):
    path: str = Field(description="Workspace-relative source path")
    purpose: str
    public_interfaces: list[DocumentedInterface]
    dependencies: list[str]
    artifacts: list[str]
    caveats: list[str]


class DocumentationBundle(BaseModel):
    files: list[FileDocumentation]


class VerificationResult(BaseModel):
    verdict: Literal["pass", "fail", "inconclusive"]
    key_numbers: list[str]
    key_equations: list[str]
    insights: list[str]
    predicted_properties: list[str]
    feedback: list[str]
    failure_modes: list[str]
    relevant_files: list[str]


Planner_agent = Agent(
    name="Planner",
    instructions="""
You are MIMI's scientific engineering planner.

Convert the user's request and reference material into a precise project handout.
Define the physics, numerical conventions, deliverables, and pass/fail criteria.
Do not write code and do not decide physical filesystem paths. You may name logical
modules and artifacts, but the runtime owns the run directory and file placement.

Use this structure:
1. Title and goal.
2. In-scope and out-of-scope boundaries.
3. Assumptions, units, coordinate and sign conventions, defaults, tolerances, and
   reproducibility rules.
4. About ten ordered exercises for a nontrivial project. Each exercise must have
   one primary implementation burden and must state:
   - the deliverable and public interface;
   - complete equations or a numbered algorithm, defining every symbol and unit;
   - edge cases and detectable failure modes;
   - a physical demonstration;
   - at least one saved text diagnostic and, when meaningful, one saved figure;
   - quantitative acceptance criteria.
5. Cross-task validation gates and a definition-of-done checklist.

Every runnable exercise must require one JSON-serialisable result manifest named
result.json. The manifest must contain artifact groups and relative paths. Avoid
vague phrases such as "standard method" unless the exact procedure is also given.
Choose explicit defaults instead of asking follow-up questions. Keep notation and
conventions consistent. Write mathematical symbols in LaTeX form.
""",
    model="gpt-5-mini",
    model_settings=ModelSettings(reasoning=Reasoning(effort="medium"), verbosity="high"),
)


TaskBreaker_agent = Agent(
    name="Task Breaker",
    instructions="""
You are MIMI's task shaper. Split the supplied plan into ordered, self-contained
tasks without solving them. Return task content in the structured output itself.
Never create, move, or name physical files; deterministic runtime code persists the
task records.

Preserve all equations, constraints, demonstrations, outputs, and acceptance
criteria. Each coding task must be small enough for one focused Codex thread, but
large enough to produce a meaningful accepted change. Mark overview-only material
as not requiring the coding team and retain its useful context in
insights_from_overview. For revisions, preserve completed work and describe only
the replacement or continuation tasks that remain.
""",
    model="gpt-5-mini",
    output_type=AgentOutputSchema(BrokenTask, strict_json_schema=True),
    model_settings=ModelSettings(reasoning=Reasoning(effort="medium"), verbosity="low"),
)


Documentation_agent = Agent(
    name="Documentation",
    instructions="""
You are MIMI's code ownership documenter. You receive the exact source files that
were changed by one accepted coding task, plus the task contract and a deterministic
filesystem index. Describe only facts evidenced by that source. Do not solve the
task, rewrite code, invent interfaces, or document rejected attempts.

For every supplied changed source file, return one compact ownership record:
- its workspace-relative path and purpose;
- public functions, classes, constants, or commands with concise signatures;
- important local-module and external dependencies;
- artifacts the file produces or consumes;
- caveats that a future coding task must know.

Prefer compact operational knowledge over line-by-line narration. The runtime will
verify paths and hashes before accepting your records, so never describe files that
were not supplied.
""",
    model="gpt-5-mini",
    output_type=AgentOutputSchema(DocumentationBundle, strict_json_schema=True),
    model_settings=ModelSettings(reasoning=Reasoning(effort="low"), verbosity="low"),
)


Verifier_agent = Agent(
    name="Verifier",
    instructions="""
You are MIMI's independent physics and numerical verifier. MIMI verifies an
ordered implementation from the first stage forward. You receive the current
stage contract, its produced artifacts or deterministic execution failure, and
possibly hash-valid documentation from already accepted upstream stages. Judge
only whether the current stage may be accepted so verification can move forward.
Do not edit code and do not infer a pass from successful execution alone.

Check dimensional consistency, signs, scaling, orders of magnitude, bounds,
symmetry, monotonicity, limiting behaviour, convergence, NaN/Inf, and agreement
between figures and text. Compare against canonical physics when relevant. A
runtime/schema/artifact failure is a fail. Use inconclusive only when execution
succeeded but evidence required by the task is missing or genuinely insufficient.

Feedback must be concrete enough for the same Codex thread to repair the workspace.
Record detected failure modes separately. List workspace-relative files only when
the evidence identifies them; do not invent paths. Mathematical symbols must use
LaTeX form rather than Unicode symbols.
""",
    model="gpt-5-mini",
    output_type=AgentOutputSchema(VerificationResult, strict_json_schema=True),
    model_settings=ModelSettings(reasoning=Reasoning(effort="high"), verbosity="low"),
)
