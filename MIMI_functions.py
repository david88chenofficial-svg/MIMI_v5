import subprocess
import os
from pathlib import Path
from typing import Union
import sys
from typing import Tuple
import json
import base64

def update_documentation(filename, *, content, format: str | None = None):
        """
        Log sub-task payloads to disk.

        format:
        - "jsonl"      : append-only newline-delimited JSON (recommended)
        - "json_array" : stores a single JSON list and rewrites the whole file each call
        """
        import json, os, tempfile, shutil, dataclasses
        from pathlib import Path
        from pydantic import BaseModel

        def to_jsonable(obj):
            if isinstance(obj, BaseModel):
                return obj.model_dump()
            if dataclasses.is_dataclass(obj):
                return dataclasses.asdict(obj)
            try:
                import numpy as np
                if isinstance(obj, np.generic):
                    return obj.item()
                if isinstance(obj, np.ndarray):
                    return obj.tolist()
            except Exception:
                pass
            if isinstance(obj, dict):
                return {k: to_jsonable(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple, set)):
                return [to_jsonable(v) for v in obj]
            return obj

        msg = to_jsonable(content)
        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Decide default format
        if format is None:
            format = "jsonl" if path.suffix.lower() == ".jsonl" else "json_array"

        if format == "jsonl":
            # Append one JSON object per line (no rewrite)
            with path.open("a", encoding="utf-8") as f:
                json.dump(msg, f, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            return

        if format != "json_array":
            raise ValueError("format must be 'jsonl' or 'json_array'")

        # ---- json_array mode (your original approach, but kept as an option) ----
        existing = []
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    parsed = json.loads(text)
                    existing = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                backup = path.with_suffix(path.suffix + ".bak")
                try:
                    shutil.copy(str(path), str(backup))
                except Exception:
                    pass
                existing = []

        existing.append(msg)

        # Atomic rewrite
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.remove(tmp_name)
            except Exception:
                pass
            raise

def load_documentation(file_path: str | Path) -> str:
    """
    Load and return the full text content of a file.

    Parameters
    ----------
    file_path : str | Path
        Path to the file.

    Returns
    -------
    content : str
        File contents as a string. If the file does not exist, returns "".
    """
    path = Path(file_path)

    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")

def first_line_value(s: str) -> str:
    first = s.splitlines()[0] if s else ""
    return first.lstrip("#").strip()

def load_artifacts_to_prompt(
    images_b64,
    image_descriptions,
    text_paths,
    text_descriptions,
    instructions,
    image_mime="image/jpeg",
    max_txt_chars=12000,
):
    """
    Build a prompt that includes:
      - images (base64) + their descriptions
      - text files (read from disk) + their descriptions

    images_b64: list[str] base64 strings (no data-url prefix)
    image_descriptions: list[str]
    text_paths: list[str] local paths to .txt files
    text_descriptions: list[str]
    instructions: str
    """
    assert len(images_b64) == len(image_descriptions), "images_b64 and image_descriptions must have same length"
    assert len(text_paths) == len(text_descriptions), "text_paths and text_descriptions must have same length"

    content = []

    # Images
    for i, (b64, desc) in enumerate(zip(images_b64, image_descriptions), start=1):
        content.append({"type": "input_text", "text": f"Image {i} description:\n{desc}"})
        content.append({
            "type": "input_image",
            "detail": "auto",
            "image_url": f"data:{image_mime};base64,{b64}",
        })

    # Text files
    for j, (p, desc) in enumerate(zip(text_paths, text_descriptions), start=1):
        try:
            txt = Path(p).read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            txt = f"[ERROR reading {p!r}: {e}]"

        if len(txt) > max_txt_chars:
            txt = txt[:max_txt_chars] + "\n[TRUNCATED]\n"

        content.append({
            "type": "input_text",
            "text": f"Text {j} description:\n{desc}\nPath: {p}\n\n--- BEGIN TEXT ---\n{txt}\n--- END TEXT ---"
        })

    prompt_nitted = [
        {"role": "user", "content": content},
        {
            "role": "user",
            "content": (
                f"There are {len(images_b64)} plots and {len(text_paths)} text files. "
                f"Process them in order. Initial instructions:\n{instructions}"
            ),
        },
    ]
    return prompt_nitted

def extract_artifact_lists(result_json_path: str):
    result_path = Path(result_json_path).resolve()
    with result_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    artifacts = data.get("artifacts", {}) or {}

    plots = artifacts.get("plots", {}) or {}
    texts = artifacts.get("texts", {}) or {}

    def resolved_artifacts(items):
        paths = []
        descriptions = []
        for item in items.values():
            if not isinstance(item, dict) or not item.get("path"):
                continue
            artifact_path = Path(item["path"])
            if not artifact_path.is_absolute():
                artifact_path = result_path.parent / artifact_path
            paths.append(str(artifact_path.resolve()))
            descriptions.append(item.get("description", ""))
        return paths, descriptions

    plot_path, plot_description = resolved_artifacts(plots)
    text_path, text_description = resolved_artifacts(texts)

    return plot_path, plot_description, text_path, text_description

def load_png_to_prompt(images, descriptions, instructions):
    assert len(images) == len(descriptions), "images and descriptions must have the same length"

    content = []
    for i, (b64, desc) in enumerate(zip(images, descriptions), start=1):
        content.append({
            "type": "input_text",
            "text": f"Image {i} description:\n{desc}",
        })
        content.append({
            "type": "input_image",
            "detail": "auto",
            "image_url": f"data:image/jpeg;base64,{b64}",
        })

    prompt_nitted = [
        {"role": "user", "content": content},
        {
            "role": "user",
            "content": f"There are {len(images)} plots. For each plot (in order), use the description and explain what you see. The initial instructions are {instructions}",
        },
    ]
    return prompt_nitted

def image_to_base64(image_path):
    with open(image_path, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
    return encoded_string

def load_metadata(meta_path) -> dict:
    meta_path = Path(meta_path)
    with meta_path.open("r", encoding="utf-8") as f:
        return json.load(f)

def run_python_file(path: str | Path) -> Tuple[int, str, str, Path]:
    """
    Run an existing Python file using the current interpreter (sys.executable),
    and return (returncode, stdout, stderr, resolved_path).
    """
    script_path = Path(path).expanduser().resolve()
    if not script_path.exists() or not script_path.is_file():
        raise FileNotFoundError(f"Python script not found: {script_path}")
    if script_path.suffix.lower() != ".py":
        raise ValueError(f"Expected a .py file, got: {script_path}")

    result = subprocess.run(
        [sys.executable, "-u", str(script_path)],
        cwd=str(script_path.parent),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, result.stdout, result.stderr, script_path

def run_code_string_locally(code: str, number: int | str, workdir: str | Path = ""):
    workdir_path = Path(workdir or ".").resolve()
    workdir_path.mkdir(parents=True, exist_ok=True)

    script_name = f"temp_script_{number}.py"
    script_path = workdir_path / script_name
    script_path.write_text(code, encoding="utf-8", newline="\n")

    # Run with the same Python interpreter that's running this script
    result = subprocess.run(
        [sys.executable, "-u", str(script_path)],
        cwd=str(workdir_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, result.stdout, result.stderr, script_path

def read_python_file_code(py_file: str | Path) -> str:
    path = Path(py_file)
    if not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

def save_text_to_txt(text: str, filename: str, folder: Union[str, Path] = ".") -> Path:
    """
    Save a long text to a .txt file.

    Args:
        text: The text content to write.
        filename: Output file name (with or without ".txt").
        folder: Output directory (default: current directory).

    Returns:
        Path to the saved file.
    """
    if not isinstance(text, str) or not text:
        raise ValueError("text must be a non-empty string.")
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("filename must be a non-empty string.")

    folder_path = Path(folder)
    folder_path.mkdir(parents=True, exist_ok=True)

    name = filename.strip()
    if not name.lower().endswith(".txt"):
        name += ".txt"

    out_path = folder_path / name
    out_path.write_text(text, encoding="utf-8", newline="\n")
    return out_path

def log_result(filename, role, content):
    import json, os
    msg = {"role": role, "content": content}

    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = []

    data.append(msg)

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def making_prompt_with_history(instruction_file_id, content):
    prompt_nitted = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_file",
                            "file_id": instruction_file_id,
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": 'the following is the history' + '\n' + str(content),
                },
    ]
    return prompt_nitted

def making_prompt(instruction_file_id, content):
    prompt_nitted = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_file",
                            "file_id": instruction_file_id,
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": content,
                },
    ]
    return prompt_nitted

def making_history(role, content):
    return {"role": role, "content": content}

def log_sub_tasks(filename, *, content):
    import json, os, tempfile, shutil, dataclasses
    from pydantic import BaseModel
    from pathlib import Path

    def to_jsonable(obj):
        # Pydantic v2 models
        if isinstance(obj, BaseModel):
            return obj.model_dump()
        # Dataclasses
        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)
        # Numpy scalars/arrays (optional)
        try:
            import numpy as np
            if isinstance(obj, np.generic):
                return obj.item()
            if isinstance(obj, np.ndarray):
                return obj.tolist()
        except Exception:
            pass
        # Containers
        if isinstance(obj, dict):
            return {k: to_jsonable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [to_jsonable(v) for v in obj]
        return obj  # assume JSON-serializable

    msg = to_jsonable(content)

    path = Path(filename)
    existing = []

    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                text = f.read().strip()
                if text:                             # handle empty file
                    parsed = json.loads(text)
                    if isinstance(parsed, list):     # expected shape
                        existing = parsed
                    else:
                        # If file has an object or something else, wrap it
                        existing = [parsed]
        except json.JSONDecodeError:
            # Corrupted/partial—salvage by starting a new list and
            # preserving the old file as .bak
            backup = path.with_suffix(path.suffix + ".bak")
            try:
                shutil.copy(str(path), str(backup))
            except Exception:
                pass
            existing = []

    existing.append(msg)

    # Atomic write to avoid leaving partial JSON on crash
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_name, path)  # atomic on same filesystem
    except Exception:
        # Clean up temp file on failure
        try:
            os.remove(tmp_name)
        except Exception:
            pass
        raise

def donwload_from_container(container_id, file_id, file_name):
    api_key = os.getenv("OPENAI_API_KEY")

    # Run PowerShell command
    subprocess.run([
        "powershell",
        "-Command",
        f"""
        Invoke-WebRequest `
        -Uri 'https://api.openai.com/v1/containers/{container_id}/files/{file_id}/content' `
        -Headers @{{
            'Authorization' = 'Bearer {api_key}'
        }} `
        -OutFile {file_name}
        """
    ], check=True)

    print("✅ File downloaded successfully.")
