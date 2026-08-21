"""Filesystem contracts for MIMI agent runs.

MIMI owns every path in a run.  Agents may edit the workspace, while task
instructions, artifacts, reports, and the aggregate manifest live in separate
directories selected by the harness.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SOURCE_SUFFIXES = {".py", ".json", ".md", ".toml", ".txt", ".yaml", ".yml"}
IGNORED_PARTS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}
SECRET_ENV_PATTERN = re.compile(r"(?i)(api[_-]?key|token|secret|password|credential)")


class WorkspaceContractError(ValueError):
    """Raised when an agent-produced path violates the run contract."""


@dataclass(frozen=True, slots=True)
class RunPaths:
    root: Path
    inputs: Path
    workspace: Path
    tasks: Path
    artifacts: Path
    reports: Path
    manifest: Path
    code_index: Path

    @classmethod
    def create(cls, output_root: Path, run_id: str) -> "RunPaths":
        output_root = Path(output_root).resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        root = output_root / run_id
        suffix = 2
        while root.exists():
            root = output_root / f"{run_id}_{suffix}"
            suffix += 1
        root.mkdir()

        paths = cls(
            root=root.resolve(),
            inputs=(root / "inputs").resolve(),
            workspace=(root / "workspace").resolve(),
            tasks=(root / "tasks").resolve(),
            artifacts=(root / "artifacts").resolve(),
            reports=(root / "reports").resolve(),
            manifest=(root / "run_manifest.json").resolve(),
            code_index=(root / "reports" / "code_index.json").resolve(),
        )
        for directory in (
            paths.inputs,
            paths.workspace,
            paths.tasks,
            paths.artifacts,
            paths.reports,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return paths

    def attempt_dir(self, task_id: str, attempt: int) -> Path:
        safe_task_id = safe_identifier(task_id, "task")
        path = self.artifacts / safe_task_id / f"attempt_{attempt}"
        path.mkdir(parents=True, exist_ok=False)
        return path.resolve()


def safe_identifier(value: object, fallback: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._")
    return name or fallback


def ensure_within(path: Path, root: Path, *, label: str = "path") -> Path:
    resolved_path = Path(path).resolve()
    resolved_root = Path(root).resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise WorkspaceContractError(
            f"{label} must stay inside {resolved_root}: {resolved_path}"
        ) from exc
    return resolved_path


def copy_input(source: Path | None, inputs_dir: Path, label: str) -> Path | None:
    if source is None:
        return None
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Input file was not found: {source}")
    destination = Path(inputs_dir) / f"{safe_identifier(label, 'input')}_{source.name}"
    index = 2
    while destination.exists():
        destination = destination.with_name(
            f"{destination.stem}_{index}{destination.suffix}"
        )
        index += 1
    shutil.copy2(source, destination)
    return destination.resolve()


def initialize_workspace(paths: RunPaths) -> None:
    instructions = """# MIMI generated-code workspace

- Work only inside this workspace.
- Read `code_index.json` context supplied by MIMI before opening source files.
- Inspect only files required for the current task unless repairing or diagnosing a failure.
- You may edit multiple source and test files when the task requires it.
- Do not create outputs outside the workspace while developing.
- Runnable demonstrations must write relative artifacts and one `result.json` in their process working directory.
- `result.json` must contain `summary` and `artifacts.plots` / `artifacts.texts` mappings.
- Never read credentials or environment files.
"""
    (paths.workspace / "AGENTS.md").write_text(instructions, encoding="utf-8", newline="\n")


def write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def materialize_tasks(
    tasks: Iterable[dict[str, Any]],
    task_dir: Path,
    *,
    source_dirs: Iterable[Path] = (),
    prefix: str = "task",
    start_number: int = 1,
) -> list[dict[str, Any]]:
    """Persist structured task records without letting an agent choose paths.

    `instructions` is preferred.  Legacy manifests may refer to an exact file
    beside the supplied manifest, but MIMI never searches unrelated old runs.
    """

    task_dir = Path(task_dir).resolve()
    task_dir.mkdir(parents=True, exist_ok=True)
    exact_source_dirs = [Path(item).resolve() for item in source_dirs]
    records: list[dict[str, Any]] = []

    for offset, raw_task in enumerate(tasks):
        if not isinstance(raw_task, dict):
            raise WorkspaceContractError("Every task must be a JSON object.")
        number = start_number + offset
        task_id = f"{safe_identifier(prefix, 'task')}_{number:03d}"
        task = dict(raw_task)
        instructions = str(task.get("instructions") or "").strip()

        if not instructions:
            legacy_name = Path(str(task.get("sub_filename") or "")).name
            if legacy_name not in {"", ".", ".."}:
                for source_dir in exact_source_dirs:
                    candidate = source_dir / legacy_name
                    if candidate.is_file():
                        instructions = candidate.read_text(
                            encoding="utf-8", errors="replace"
                        ).strip()
                        break
        if not instructions:
            description = str(
                task.get("brief_sub_file_content_description_in_one_sentence") or ""
            ).strip()
            if task.get("is_Coding_Team_required") is True:
                raise WorkspaceContractError(
                    f"Coding task {number} has no inline instructions or exact source file."
                )
            instructions = description or f"Non-coding task {number}."

        instruction_path = task_dir / f"{task_id}.md"
        instruction_path.write_text(instructions + "\n", encoding="utf-8", newline="\n")
        task.update(
            {
                "task_id": task_id,
                "task_number": number,
                "instructions": instructions,
                "sub_filename": instruction_path.name,
                "instruction_path": str(instruction_path.resolve()),
            }
        )
        records.append(task)
    return records


def _iter_workspace_files(workspace: Path) -> Iterable[Path]:
    workspace = Path(workspace).resolve()
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
            continue
        if path.suffix.lower() in SOURCE_SUFFIXES or path.name == "AGENTS.md":
            yield path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def workspace_snapshot(workspace: Path) -> dict[str, str]:
    workspace = Path(workspace).resolve()
    return {
        path.relative_to(workspace).as_posix(): sha256_file(path)
        for path in _iter_workspace_files(workspace)
    }


def changed_workspace_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )


def _format_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    arguments: list[str] = []
    positional = list(node.args.posonlyargs) + list(node.args.args)
    defaults_offset = len(positional) - len(node.args.defaults)
    for index, argument in enumerate(positional):
        name = argument.arg
        if argument.annotation is not None:
            name += f": {ast.unparse(argument.annotation)}"
        if index >= defaults_offset:
            name += f" = {ast.unparse(node.args.defaults[index - defaults_offset])}"
        arguments.append(name)
    if node.args.vararg:
        arguments.append(f"*{node.args.vararg.arg}")
    elif node.args.kwonlyargs:
        arguments.append("*")
    for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        name = argument.arg
        if argument.annotation is not None:
            name += f": {ast.unparse(argument.annotation)}"
        if default is not None:
            name += f" = {ast.unparse(default)}"
        arguments.append(name)
    if node.args.kwarg:
        arguments.append(f"**{node.args.kwarg.arg}")
    signature = f"{node.name}({', '.join(arguments)})"
    if node.returns is not None:
        signature += f" -> {ast.unparse(node.returns)}"
    return signature


def _python_record(path: Path, relative_path: str) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    record: dict[str, Any] = {
        "path": relative_path,
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "language": "python",
        "module_doc": "",
        "imports": [],
        "symbols": [],
    }
    try:
        tree = ast.parse(text, filename=relative_path)
    except SyntaxError as exc:
        record["parse_error"] = f"line {exc.lineno}: {exc.msg}"
        return record

    record["module_doc"] = (ast.get_docstring(tree) or "").split("\n", 1)[0]
    imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            record["symbols"].append(
                {
                    "kind": "function",
                    "name": node.name,
                    "signature": _format_signature(node),
                    "line": node.lineno,
                    "doc": (ast.get_docstring(node) or "").split("\n", 1)[0],
                }
            )
        elif isinstance(node, ast.ClassDef):
            methods = [
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            record["symbols"].append(
                {
                    "kind": "class",
                    "name": node.name,
                    "line": node.lineno,
                    "methods": methods,
                    "doc": (ast.get_docstring(node) or "").split("\n", 1)[0],
                }
            )
    record["imports"] = sorted(item for item in imports if item)
    return record


def build_code_index(
    workspace: Path,
    *,
    annotations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    workspace = Path(workspace).resolve()
    annotations = annotations or {}
    files: list[dict[str, Any]] = []
    for path in _iter_workspace_files(workspace):
        relative = path.relative_to(workspace).as_posix()
        if path.suffix.lower() == ".py":
            record = _python_record(path, relative)
        else:
            record = {
                "path": relative,
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
                "language": path.suffix.lower().lstrip(".") or "text",
            }
        annotation = annotations.get(relative)
        if annotation and annotation.get("sha256") == record["sha256"]:
            record["agent_documentation"] = {
                key: value for key, value in annotation.items() if key != "sha256"
            }
        files.append(record)
    return {"schema_version": 1, "workspace": str(workspace), "files": files}


def render_code_index(index: dict[str, Any], max_chars: int = 30_000) -> str:
    compact = json.dumps(index, ensure_ascii=False, separators=(",", ":"))
    if len(compact) <= max_chars:
        return compact
    files = index.get("files", [])
    minimal: dict[str, Any] = {
        "schema_version": index.get("schema_version", 1),
        "truncated": False,
        "files": [],
    }
    for item in files:
        record = {
                "path": item.get("path"),
                "sha256": item.get("sha256"),
                "module_doc": item.get("module_doc", ""),
                "symbols": [symbol.get("signature") or symbol.get("name") for symbol in item.get("symbols", [])],
                "purpose": item.get("agent_documentation", {}).get("purpose", ""),
        }
        minimal["files"].append(record)
        candidate = json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))
        if len(candidate) > max_chars:
            minimal["files"].pop()
            minimal["truncated"] = True
            break
    return json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))


def source_bundle(workspace: Path, relative_paths: Iterable[str], max_chars: int = 120_000) -> str:
    workspace = Path(workspace).resolve()
    parts: list[str] = []
    used = 0
    for relative in sorted(set(relative_paths)):
        path = ensure_within(workspace / relative, workspace, label="documented source")
        if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        remaining = max_chars - used
        if remaining <= 0:
            break
        text = text[:remaining]
        parts.append(f"--- FILE: {relative} ---\n{text}")
        used += len(text)
    return "\n\n".join(parts)


def resolve_entrypoint(workspace: Path, entrypoint: str, changed_files: Iterable[str]) -> Path:
    workspace = Path(workspace).resolve()
    if entrypoint:
        explicit = ensure_within(workspace / entrypoint, workspace, label="entrypoint")
        if explicit.is_file() and explicit.suffix.lower() == ".py":
            return explicit
        raise WorkspaceContractError(
            f"Codex entrypoint does not identify a Python file: {entrypoint}"
        )

    candidates = [
        workspace / relative
        for relative in changed_files
        if Path(relative).suffix.lower() == ".py"
    ]
    for candidate in candidates:
        resolved = ensure_within(candidate, workspace, label="entrypoint")
        if resolved.is_file():
            text = resolved.read_text(encoding="utf-8", errors="replace")
            if "__main__" in text:
                return resolved
    raise WorkspaceContractError(
        "Codex did not identify a runnable Python entrypoint inside the workspace."
    )


@dataclass(slots=True)
class ExecutionResult:
    returncode: int
    stdout: str
    stderr: str
    entrypoint: str
    artifact_dir: str


def sanitized_subprocess_env(workspace: Path, artifact_dir: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not SECRET_ENV_PATTERN.search(key)
    }
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(workspace) + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )
    environment["MIMI_ARTIFACT_DIR"] = str(artifact_dir)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def run_workspace_entrypoint(
    workspace: Path,
    entrypoint: Path,
    artifact_dir: Path,
    *,
    timeout_seconds: int = 120,
) -> ExecutionResult:
    workspace = Path(workspace).resolve()
    entrypoint = ensure_within(entrypoint, workspace, label="entrypoint")
    artifact_dir = Path(artifact_dir).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [sys.executable, "-u", str(entrypoint)],
            cwd=str(artifact_dir),
            env=sanitized_subprocess_env(workspace, artifact_dir),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return ExecutionResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            entrypoint=entrypoint.relative_to(workspace).as_posix(),
            artifact_dir=str(artifact_dir),
        )
    except subprocess.TimeoutExpired as exc:
        return ExecutionResult(
            returncode=124,
            stdout=exc.stdout or "",
            stderr=f"Execution timed out after {timeout_seconds} seconds.",
            entrypoint=entrypoint.relative_to(workspace).as_posix(),
            artifact_dir=str(artifact_dir),
        )


@dataclass(slots=True)
class ArtifactCollection:
    manifest_path: str
    summary: str
    plot_paths: list[str]
    plot_descriptions: list[str]
    text_paths: list[str]
    text_descriptions: list[str]


def collect_artifacts(artifact_dir: Path) -> ArtifactCollection:
    artifact_dir = Path(artifact_dir).resolve()
    manifests = sorted(artifact_dir.rglob("result.json"))
    if not manifests:
        raise WorkspaceContractError(
            f"No result.json was produced inside {artifact_dir}."
        )
    if len(manifests) > 1:
        relative = [str(path.relative_to(artifact_dir)) for path in manifests]
        raise WorkspaceContractError(
            "Exactly one result.json is required per attempt; found: " + ", ".join(relative)
        )
    expected_manifest = artifact_dir / "result.json"
    if manifests[0].resolve() != expected_manifest.resolve():
        raise WorkspaceContractError(
            "result.json must be written directly in the attempt artifact directory, "
            f"not {manifests[0].relative_to(artifact_dir)}."
        )
    manifest_path = ensure_within(manifests[0], artifact_dir, label="result manifest")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise WorkspaceContractError("result.json must contain a JSON object.")
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, dict):
        raise WorkspaceContractError("result.json must contain an artifacts object.")

    def resolve_group(name: str) -> tuple[list[str], list[str]]:
        group = artifacts.get(name, {}) or {}
        if not isinstance(group, dict):
            raise WorkspaceContractError(f"artifacts.{name} must be an object.")
        paths: list[str] = []
        descriptions: list[str] = []
        for item_name, item in group.items():
            if not isinstance(item, dict) or not item.get("path"):
                raise WorkspaceContractError(
                    f"artifacts.{name}.{item_name} must contain a path."
                )
            raw_path = Path(str(item["path"]))
            if raw_path.is_absolute():
                raise WorkspaceContractError(
                    f"artifacts.{name}.{item_name}.path must be relative to result.json."
                )
            candidate = manifest_path.parent / raw_path
            resolved = ensure_within(candidate, artifact_dir, label="artifact")
            if not resolved.is_file():
                raise FileNotFoundError(f"Artifact does not exist: {resolved}")
            paths.append(str(resolved))
            descriptions.append(str(item.get("description") or ""))
        return paths, descriptions

    plot_paths, plot_descriptions = resolve_group("plots")
    text_paths, text_descriptions = resolve_group("texts")
    if not text_paths:
        raise WorkspaceContractError("Every attempt must produce at least one text artifact.")
    return ArtifactCollection(
        manifest_path=str(manifest_path),
        summary=str(data.get("summary") or ""),
        plot_paths=plot_paths,
        plot_descriptions=plot_descriptions,
        text_paths=text_paths,
        text_descriptions=text_descriptions,
    )
