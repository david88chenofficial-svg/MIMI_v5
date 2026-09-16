# MIMI

MIMI turns a scientific specification into a verified codebase. The harness owns
the filesystem and run state; language-model agents do not choose or search for
physical paths.

## Install

Python 3.10 or newer is required.

```powershell
python -m pip install -r .\requirements.txt
```

Set `OPENAI_API_KEY` in the environment or place it in the local, git-ignored
`API_key.env` file. The Codex SDK is used for product-code edits, while the OpenAI
Agents SDK is used for planning, task shaping, documentation, and verification.

## Agent ownership model

- Planner defines scientific deliverables and acceptance criteria, not paths.
- Task Breaker returns structured task objects; Python persists exact task files.
- One persistent Codex thread receives the complete ordered plan and implements
  the product as one coherent build. It also creates one small validation
  entrypoint per coding stage so intermediate results remain inspectable.
- Python runs all stage entrypoints in isolated artifact directories. The Verifier
  then checks them from the first stage forward and returns a structured `pass`,
  `fail`, or `inconclusive` verdict.
- Documentation is recorded immediately after each stage passes. Its
  records are attached to file hashes, so stale documentation is automatically
  excluded after later edits.
- Verification stops at the first failed stage. The failure, accepted hash-valid
  documentation, and failed/downstream task contracts return to the same Codex
  thread for a suffix repair. Accepted upstream stages are preserved.
- If a repair changes an accepted source file, MIMI automatically restarts
  verification at the earliest affected stage.

Codex uses the compact `code_index.json` as its navigation layer. Product logic
belongs in shared modules; stage entrypoints are demonstrations and validators,
not duplicate implementations.

## Run layout

Every run is self-contained under `agents_output/<run-id>/`:

```text
inputs/                    immutable copies of supplied inputs
workspace/                 accepted and in-progress product source
tasks/                     deterministic task records and manifest
artifacts/<task>/<attempt> isolated runtime output and one result.json
reports/code_index.json    deterministic AST index plus hash-bound documentation
run_manifest.json          task attempts, thread IDs, verdicts, and token totals
```

Generated demonstrations run from the attempt artifact directory with the product
workspace on `PYTHONPATH`. Artifact paths must remain inside that attempt directory;
credentials are removed from the subprocess environment.

The normal runtime order is:

```text
Planner -> Task Breaker -> one whole-plan Codex build
        -> execute all stage entrypoints
        -> verify stage 1, document it, verify stage 2, ...
        -> on failure: repair and regenerate the failed/downstream suffix
        -> resume verification at the earliest affected stage
```

## Progressive browser interface

Start the normal file workflow:

```powershell
python .\lauch_MIMI.py
```

MIMI opens with a Level 1, Level 2, or Level 3 choice. Only the file drop targets
needed by the selected level are then shown.

MIMI defaults its API-backed Planner, Task Breaker, Verifier, Documentation, and
literature extraction to `gpt-5-mini`. The whole-plan Coder defaults to
`gpt-5.6-luna` through the ChatGPT-authenticated Codex runtime. The **Models**
panel can still override individual workflow-agent models.

At Level 1, literature can be added without using PowerShell: drag one or more
PDFs into **Literature PDFs**, click **Extract into Background**, and wait for the
generated `literature_predicates.json` to appear automatically in the Background
slot. Each PDF must be under 50 MB; one extraction may contain at most 20 PDFs and
100 MB total. Extraction uses the configured OpenAI API key. If a Task
specification is loaded, the extractor uses it as relevance context and prioritizes
paper-supported equations, inputs, constraints, assumptions, validation evidence,
and other information useful for completing that task. Any files in **Reference
images** are also supplied as task context so the extractor can recognize relevant
geometry, components, layout, and intended results. The task specification and
reference images are never treated as factual sources; predicates still require a
locator in a literature PDF. Task-specific extraction ranks candidates by direct
usefulness. **Maximum per paper** is configurable in the interface and defaults
to 10; it is a ceiling for each individual paper, with no combined task maximum.
After all task-specific papers are processed, a separate structured ranking pass
orders every predicate globally from most to least useful, freely interleaving
facts from different papers instead of grouping them by source. Papers with little
useful evidence should still return fewer than the selected maximum. Without a
task specification, reference images are not sent to the literature extractor and
extraction keeps the existing general research focus. While extraction is running,
**Abandon
extraction** stops before the next paper and prevents partial predicates from being
saved or placed in Background. An OpenAI request already in flight may finish, and
may still be billed, before its temporary upload is cleaned up and its result is
discarded.

Start directly in Native Union POP Phone voice-intake mode:

```powershell
python .\lauch_MIMI.py --pop-phone
```

Voice intake records from the POP Phone microphone, creates a clarified Markdown
specification, and places it in the Level 1 specification slot for review.

## Predicate background knowledge

Create a source-grounded predicate database from one or more literature PDFs:

```powershell
python .\literature_to_predicates.py `
  "D:\papers\seal-paper-1.pdf" `
  "D:\papers\seal-paper-2.pdf" `
  --output .\seal_predicates.json `
  --research-question "Which assumptions and operating limits affect seal leakage predictions?"
```

The extractor uses the Responses API's PDF input and Pydantic structured output.
It uploads each PDF with the `user_data` purpose, processes papers separately so
their provenance is not mixed, and deletes each temporary API upload after the
response. Use `--append` to add new papers to an existing database and
`--print-schema` to inspect the exact extraction contract without making an API
request.

The JSON uses top-level `sources` and `predicates` registries keyed by stable IDs.
Each saved predicate uses the quantitative `mimi.predicates.quantitative.v2`
contract shown below. The strict API response represents variables as fixed
`symbol`/`definition` pairs; the local serializer converts those pairs into the
symbol-keyed `variables` object.

```json
{
  "fact": "The reported relation calculates mass flow through a straight labyrinth seal.",
  "equation": "m = k_2 C_d A p_{t0} \\sqrt{\\frac{1-(p_n/p_{t0})^2}{R T_{t0}[n-\\ln(p_n/p_{t0})]}}",
  "variables": {
    "m": "mass-flow rate [kg/s]",
    "C_d": "discharge coefficient",
    "A": "clearance area [m^2]",
    "p_{t0}": "inlet total pressure [Pa]"
  },
  "assumptions": [
    "The seal is a straight labyrinth seal.",
    "The working fluid is a compressible gas.",
    "The operating point is within the model's reported validity range."
  ],
  "sources": [
    {
      "source_id": "SRC_CB118EDDD91E",
      "page": "42",
      "equation": "Eq. (17)"
    }
  ]
}
```

Here, `fact` means a proposition reported by the paper, not an independently
verified truth. `equation` is `null` and `variables` is `{}` for a qualitative
predicate. Mathematical notation is ASCII-safe LaTeX: for example, the JSON file
stores `\\alpha`, `\\mu`, `x^2`, `\\sqrt{x}`, and `\\le` instead of Unicode
mathematical glyphs. Every predicate carries its own source IDs and page/equation,
section, figure, or table locators; full bibliographic metadata remains in the
top-level `sources` registry.

Use the generated database as MIMI background knowledge from Python:

```powershell
python .\MIMI.py --spec .\seal_analysis_tool_spec_v6.md --background .\seal_predicates.json
```

The Level 1 browser Background control accepts the same `.json` file, as well as
Markdown and plain text. MIMI validates and pretty-prints JSON before including it
in the planner prompt, and tells the planner to apply a fact only when its listed
assumptions hold.

## Tests

```powershell
python -m unittest discover -s tests -v
```

## POP Phone button listener (Windows)

Run the listener for the current session:

```powershell
python .\pop_phone_button.py
```

Install it for the current user's Windows startup (requires `pywin32`):

```powershell
python -m pip install pywin32
python .\pop_phone_button.py --install-startup
```

Use `python .\pop_phone_button.py --diagnose` to inspect handset button events. The
notification-area MIMI icon can also open file input or voice intake, show backend
status, and stop the server.
