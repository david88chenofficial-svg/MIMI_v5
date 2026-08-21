import base64
import hmac
import json
import mimetypes
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from MIMI_inputs import MIMIInputBundle
from literature_to_predicates import (
    DEFAULT_FOCUS,
    DEFAULT_MODEL,
    ExtractionAbandoned,
    MAX_PDF_BYTES,
    collect_pdf_paths,
    extract_pdfs_to_database,
    write_database,
)
from MIMI_models import (
    AGENT_CAPABILITY_REQUIREMENTS,
    AgentModelConfig,
    MODEL_CATALOG,
    MODEL_CATALOG_METADATA,
    model_options_for_agent,
    setting_options_for_agent,
)
from MIMI_voice import MAX_VOICE_AUDIO_BYTES, create_tool_spec_from_audio


WEB_STATE_LOCK = threading.Lock()
PROJECT_ROOT = Path(__file__).resolve().parent
WEB_ROOT = PROJECT_ROOT / "web"


class MIMIThreadingHTTPServer(ThreadingHTTPServer):
    """HTTP server whose stale browser connections cannot block process exit."""

    daemon_threads = True
    block_on_close = False


WEB_STATE = {
    "running": False,
    "message": "Idle",
    "started_at": None,
    "finished_at": None,
    "error": None,
    "output_dir": None,
    "phase": "idle",
    "planner_output": "",
    "task_breaker": {
        "raw": "",
        "subtasks": [],
    },
    "progress": {
        "current": 0,
        "total": 0,
        "percent": 0,
        "label": "No run in progress",
    },
    "coder_runs": [],
    "activity": [],
}
MAX_ACTIVITY_ENTRIES = 300
MAX_VOICE_REQUEST_BYTES = int(MAX_VOICE_AUDIO_BYTES * 1.4) + 4096
MAX_LITERATURE_PDFS = 20
MAX_LITERATURE_TOTAL_BYTES = 100 * 1024 * 1024
MAX_LITERATURE_REFERENCE_IMAGES = 5
MAX_LITERATURE_REFERENCE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_LITERATURE_REFERENCE_IMAGES_TOTAL_BYTES = 40 * 1024 * 1024
MAX_LITERATURE_REQUEST_BYTES = int(
    (MAX_LITERATURE_TOTAL_BYTES + MAX_LITERATURE_REFERENCE_IMAGES_TOTAL_BYTES) * 1.4
) + 1024 * 1024
MAX_LITERATURE_CONTROL_BYTES = 4096
MAX_TASK_SPEC_CHARS = 200_000
DEFAULT_LITERATURE_MAX_PREDICATES_PER_PAPER = 10
MIN_LITERATURE_MAX_PREDICATES_PER_PAPER = 1
MAX_LITERATURE_MAX_PREDICATES_PER_PAPER = 250
LITERATURE_REFERENCE_IMAGE_TYPES = {
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class LiteratureExtractionRegistry:
    """Thread-safe ownership and cancellation signals for browser extractions."""

    JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}

    def begin(self, job_id: str) -> threading.Event | None:
        if not self.JOB_ID_PATTERN.fullmatch(job_id):
            raise ValueError("The literature extraction job ID is invalid.")
        with self._lock:
            if self._events:
                return None
            event = threading.Event()
            self._events[job_id] = event
            return event

    def abandon(self, job_id: str) -> bool:
        with self._lock:
            event = self._events.get(job_id)
            if event is None:
                return False
            event.set()
            return True

    def finish(self, job_id: str) -> None:
        with self._lock:
            self._events.pop(job_id, None)

    def has_active_job(self) -> bool:
        with self._lock:
            return bool(self._events)


def update_web_state(**changes) -> None:
    with WEB_STATE_LOCK:
        WEB_STATE.update(changes)


def snapshot_web_state() -> dict:
    with WEB_STATE_LOCK:
        return json.loads(json.dumps(WEB_STATE))


def append_planner_output(header: str, text: str) -> None:
    with WEB_STATE_LOCK:
        existing = WEB_STATE.get("planner_output", "")
        separator = "\n\n" if existing else ""
        WEB_STATE["planner_output"] = f"{existing}{separator}--- {header} ---\n{text}"


def add_activity(
    agent: str,
    message: str,
    *,
    status: str = "info",
    task_id: str | None = None,
    attempt: int | None = None,
) -> None:
    """Publish a concise runtime event to both the dashboard and PowerShell."""

    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    entry = {
        "timestamp": timestamp,
        "agent": str(agent),
        "message": str(message),
        "status": str(status),
        "task_id": str(task_id) if task_id else None,
        "attempt": int(attempt) if attempt is not None else None,
    }
    terminal_time = timestamp.split("T", 1)[-1][:8]
    context = f" [{entry['task_id']}]" if entry["task_id"] else ""
    attempt_context = f" attempt {entry['attempt']}" if entry["attempt"] else ""
    print(
        f"[{terminal_time}] {entry['agent']}{context}{attempt_context}: {entry['message']}",
        flush=True,
    )
    with WEB_STATE_LOCK:
        activity = WEB_STATE.setdefault("activity", [])
        activity.append(entry)
        if len(activity) > MAX_ACTIVITY_ENTRIES:
            del activity[:-MAX_ACTIVITY_ENTRIES]


def update_subtask_status(
    task_id: str,
    status: str,
    *,
    attempt_count: int | None = None,
    max_attempts: int | None = None,
    detail: str | None = None,
) -> bool:
    """Update the live badge for one structured task without rebuilding the list."""

    with WEB_STATE_LOCK:
        task_breaker = WEB_STATE.get("task_breaker") or {}
        for subtask in task_breaker.get("subtasks") or []:
            if str(subtask.get("task_id")) != str(task_id):
                continue
            subtask["status"] = str(status)
            if attempt_count is not None:
                subtask["attempt_count"] = int(attempt_count)
            if max_attempts is not None:
                subtask["max_attempts"] = int(max_attempts)
            if detail is not None:
                subtask["detail"] = str(detail)
            return True
    return False


def reset_web_state(message: str) -> None:
    update_web_state(
        running=True,
        message=message,
        started_at=datetime.now().isoformat(timespec="seconds"),
        finished_at=None,
        error=None,
        output_dir=None,
        phase="starting",
        planner_output="",
        task_breaker={"raw": "", "subtasks": []},
        progress={
            "current": 0,
            "total": 0,
            "percent": 0,
            "label": "Starting agent run",
        },
        coder_runs=[],
        activity=[],
    )
    add_activity("MIMI", message, status="running")


def add_coder_run(run_data: dict) -> None:
    with WEB_STATE_LOCK:
        WEB_STATE["coder_runs"].append(run_data)


def set_progress(current: int, total: int, label: str) -> None:
    percent = int((current / total) * 100) if total else 0
    update_web_state(progress={
        "current": current,
        "total": total,
        "percent": percent,
        "label": label,
    })


def task_summary(task: dict, fallback_index: int) -> str:
    for key in (
        "instructions",
        "brief_sub_file_content_description_in_one_sentence",
        "task",
        "description",
        "sub_task",
        "subtask",
        "title",
        "summary",
        "instruction",
    ):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    filename = task.get("sub_filename")
    if filename:
        try:
            text = Path(filename).read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text.splitlines()[0][:240]
        except OSError:
            pass
    return f"Subtask {fallback_index}"


def task_breaker_payload_to_subtasks(payload: dict) -> list[dict]:
    tasks = []
    if isinstance(payload, dict):
        tasks = payload.get("tasks") or []
    subtasks = []
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            continue
        if task.get("is_Coding_Team_required") != True:
            continue
        subtasks.append({
            "number": task.get("task_number") or index,
            "task_id": task.get("task_id", ""),
            "summary": task_summary(task, index),
            "filename": task.get("sub_filename", ""),
            "requires_coding": bool(task.get("is_Coding_Team_required")),
            "status": task.get("status", "pending"),
            "attempt_count": task.get("attempt_count", 0),
            "max_attempts": task.get("max_attempts", 3),
        })
    return subtasks


def text_preview(path: str | Path, max_chars: int = 6000) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"[Could not read {path}: {exc}]"
    if len(text) > max_chars:
        return text[:max_chars] + "\n[TRUNCATED]"
    return text


def artifact_snapshot(plot_paths, plot_descriptions, text_paths, text_descriptions) -> dict:
    images = []
    texts = []
    for path, description in zip(plot_paths, plot_descriptions):
        artifact_path = Path(path)
        item = {
            "path": str(artifact_path),
            "name": artifact_path.name,
            "description": description,
            "data_url": "",
        }
        try:
            mime = mimetypes.guess_type(artifact_path.name)[0] or "image/png"
            encoded = base64.b64encode(artifact_path.read_bytes()).decode("ascii")
            item["data_url"] = f"data:{mime};base64,{encoded}"
        except OSError as exc:
            item["error"] = str(exc)
        images.append(item)

    for path, description in zip(text_paths, text_descriptions):
        artifact_path = Path(path)
        texts.append({
            "path": str(artifact_path),
            "name": artifact_path.name,
            "description": description,
            "content": text_preview(artifact_path),
        })
    return {"images": images, "texts": texts}


def safe_upload_name(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or fallback).name).strip("._")
    return cleaned or fallback


def first_upload_item(value):
    if isinstance(value, list):
        return value[0] if value else {}
    return value or {}


def extract_uploaded_literature(
    payload: dict,
    *,
    should_abandon: Callable[[], bool] | None = None,
) -> dict:
    """Persist browser-uploaded PDFs and turn them into background JSON."""

    def ensure_active() -> None:
        if should_abandon and should_abandon():
            raise ExtractionAbandoned("Literature predicate extraction was abandoned.")

    ensure_active()
    pdf_items = payload.get("pdfs") or []
    if not isinstance(pdf_items, list) or not pdf_items:
        raise ValueError("Drop at least one literature PDF before extracting.")
    if len(pdf_items) > MAX_LITERATURE_PDFS:
        raise ValueError(f"Extract at most {MAX_LITERATURE_PDFS} PDFs at a time.")

    raw_max_predicates = payload.get(
        "maxPredicatesPerPaper",
        DEFAULT_LITERATURE_MAX_PREDICATES_PER_PAPER,
    )
    if isinstance(raw_max_predicates, bool):
        raise ValueError("Maximum predicates per paper must be a whole number.")
    if isinstance(raw_max_predicates, float) and not raw_max_predicates.is_integer():
        raise ValueError("Maximum predicates per paper must be a whole number.")
    if isinstance(raw_max_predicates, str) and not re.fullmatch(r"\d+", raw_max_predicates.strip()):
        raise ValueError("Maximum predicates per paper must be a whole number.")
    try:
        max_predicates = int(raw_max_predicates)
    except (TypeError, ValueError) as exc:
        raise ValueError("Maximum predicates per paper must be a whole number.") from exc
    if not (
        MIN_LITERATURE_MAX_PREDICATES_PER_PAPER
        <= max_predicates
        <= MAX_LITERATURE_MAX_PREDICATES_PER_PAPER
    ):
        raise ValueError(
            "Maximum predicates per paper must be between "
            f"{MIN_LITERATURE_MAX_PREDICATES_PER_PAPER} and "
            f"{MAX_LITERATURE_MAX_PREDICATES_PER_PAPER}."
        )

    validated_uploads: list[tuple[str, bytes]] = []
    saved_names: set[str] = set()
    total_bytes = 0
    for index, item in enumerate(pdf_items, start=1):
        ensure_active()
        if not isinstance(item, dict):
            raise ValueError("Every literature upload must be a PDF file.")
        name = safe_upload_name(item.get("name"), f"paper_{index}.pdf")
        if Path(name).suffix.lower() != ".pdf":
            raise ValueError(f"Literature file must use the .pdf extension: {name}")
        if name.casefold() in saved_names:
            raise ValueError(f"A literature PDF was uploaded more than once: {name}")
        data_url = str(item.get("dataUrl") or "")
        if "," not in data_url:
            raise ValueError(f"Uploaded literature PDF was not valid: {name}")
        try:
            pdf_bytes = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Uploaded literature PDF was not valid base64 data: {name}") from exc
        if not pdf_bytes:
            raise ValueError(f"PDF is empty: {name}")
        if len(pdf_bytes) >= MAX_PDF_BYTES:
            raise ValueError(f"PDF must be under 50 MB: {name}")
        if b"%PDF-" not in pdf_bytes[:1024]:
            raise ValueError(f"File does not appear to be a PDF: {name}")
        total_bytes += len(pdf_bytes)
        if total_bytes > MAX_LITERATURE_TOTAL_BYTES:
            raise ValueError("Literature PDFs must total no more than 100 MB per extraction.")

        validated_uploads.append((name, pdf_bytes))
        saved_names.add(name.casefold())

    task_spec_item = payload.get("taskSpec")
    if task_spec_item is not None and not isinstance(task_spec_item, dict):
        raise ValueError("Task specification context must be a text file.")
    task_spec = str((task_spec_item or {}).get("text") or "").strip() or None
    if task_spec and len(task_spec) > MAX_TASK_SPEC_CHARS:
        raise ValueError(
            f"Task specification must contain no more than {MAX_TASK_SPEC_CHARS:,} characters."
        )
    task_spec_name = (
        safe_upload_name((task_spec_item or {}).get("name"), "task_spec.md")
        if task_spec
        else None
    )

    reference_image_items = payload.get("referenceImages") or []
    if not isinstance(reference_image_items, list):
        raise ValueError("Reference images must be supplied as a list.")
    if reference_image_items and not task_spec:
        raise ValueError("Reference images can only be used with a task specification.")
    if len(reference_image_items) > MAX_LITERATURE_REFERENCE_IMAGES:
        raise ValueError(
            f"Use at most {MAX_LITERATURE_REFERENCE_IMAGES} reference images per extraction."
        )

    validated_reference_images: list[tuple[str, bytes]] = []
    saved_image_names: set[str] = set()
    total_image_bytes = 0
    for index, item in enumerate(reference_image_items, start=1):
        ensure_active()
        if not isinstance(item, dict):
            raise ValueError("Every reference image upload must be an image file.")
        name = safe_upload_name(item.get("name"), f"reference_{index}.png")
        expected_mime = LITERATURE_REFERENCE_IMAGE_TYPES.get(Path(name).suffix.lower())
        if expected_mime is None:
            raise ValueError(f"Reference image must be PNG, JPEG, or WEBP: {name}")
        if name.casefold() in saved_image_names:
            raise ValueError(f"A reference image was uploaded more than once: {name}")
        data_url = str(item.get("dataUrl") or "")
        if "," not in data_url:
            raise ValueError(f"Uploaded reference image was not valid: {name}")
        header, encoded = data_url.split(",", 1)
        if header.casefold() != f"data:{expected_mime};base64":
            raise ValueError(f"Reference image type does not match its filename: {name}")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Uploaded reference image was not valid base64 data: {name}") from exc
        if not image_bytes:
            raise ValueError(f"Reference image is empty: {name}")
        if len(image_bytes) > MAX_LITERATURE_REFERENCE_IMAGE_BYTES:
            raise ValueError(f"Reference image must be no larger than 20 MB: {name}")
        signature_is_valid = (
            expected_mime == "image/png" and image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        ) or (
            expected_mime == "image/jpeg" and image_bytes.startswith(b"\xff\xd8\xff")
        ) or (
            expected_mime == "image/webp"
            and len(image_bytes) >= 12
            and image_bytes.startswith(b"RIFF")
            and image_bytes[8:12] == b"WEBP"
        )
        if not signature_is_valid:
            raise ValueError(f"File does not appear to be a valid {expected_mime} image: {name}")
        total_image_bytes += len(image_bytes)
        if total_image_bytes > MAX_LITERATURE_REFERENCE_IMAGES_TOTAL_BYTES:
            raise ValueError("Reference images must total no more than 40 MB per extraction.")
        validated_reference_images.append((name, image_bytes))
        saved_image_names.add(name.casefold())

    ensure_active()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    upload_dir = PROJECT_ROOT / "uploads" / f"literature_{timestamp}"
    upload_dir.mkdir(parents=True, exist_ok=False)
    saved_paths: list[Path] = []
    for name, pdf_bytes in validated_uploads:
        ensure_active()
        path = upload_dir / name
        path.write_bytes(pdf_bytes)
        saved_paths.append(path)
    reference_image_paths: list[Path] = []
    for name, image_bytes in validated_reference_images:
        ensure_active()
        path = upload_dir / name
        path.write_bytes(image_bytes)
        reference_image_paths.append(path)

    pdf_paths = collect_pdf_paths([str(path) for path in saved_paths])
    focus = str(payload.get("focus") or DEFAULT_FOCUS).strip()
    research_question = str(payload.get("researchQuestion") or "").strip() or None
    database, predicate_count = extract_pdfs_to_database(
        pdf_paths,
        model=DEFAULT_MODEL,
        focus=focus,
        research_question=research_question,
        task_spec=task_spec,
        task_spec_name=task_spec_name,
        reference_image_paths=reference_image_paths,
        max_predicates=max_predicates,
        should_abandon=should_abandon,
    )
    ensure_active()
    output_path = upload_dir / "literature_predicates.json"
    write_database(output_path, database, allow_overwrite=False)
    return {
        "name": output_path.name,
        "text": output_path.read_text(encoding="utf-8"),
        "savedPath": str(output_path),
        "pdfCount": len(pdf_paths),
        "predicateCount": predicate_count,
        "selectionMode": "task_specific" if task_spec else "general",
        "taskSpecName": task_spec_name,
        "referenceImageCount": len(reference_image_paths),
        "maxPredicatesPerPaper": max_predicates,
        "globallyRanked": bool(task_spec and len(pdf_paths) > 1),
    }


def save_uploaded_bundle(payload: dict) -> MIMIInputBundle:
    spec_data = first_upload_item(payload.get("spec"))
    planner_data = first_upload_item(payload.get("plannerOutput"))
    task_breaker_data = first_upload_item(payload.get("taskBreaker"))
    background_data = first_upload_item(payload.get("background"))
    subtask_file_items = payload.get("subtaskFiles") or []
    if not isinstance(subtask_file_items, list):
        raise ValueError("subtaskFiles must be a list.")
    image_items = payload.get("images")
    if image_items is None:
        image_items = payload.get("image")
    if not isinstance(image_items, list):
        image_items = [image_items] if image_items else []

    raw_run_level = payload.get("runLevel")
    if raw_run_level is None:
        raw_run_level = 3 if task_breaker_data.get("text") else 2 if planner_data.get("text") else 1
    try:
        run_level = int(raw_run_level)
    except (TypeError, ValueError) as exc:
        raise ValueError("runLevel must be 1, 2, or 3.") from exc
    if run_level not in {1, 2, 3}:
        raise ValueError("runLevel must be 1, 2, or 3.")

    try:
        max_subtask_attempts = int(payload.get("maxSubtaskAttempts", 3))
    except (TypeError, ValueError) as exc:
        raise ValueError("Maximum subtask attempts must be an integer.") from exc
    if not 1 <= max_subtask_attempts <= 20:
        raise ValueError("Maximum subtask attempts must be between 1 and 20.")
    try:
        max_plan_revisions = int(payload.get("maxPlanRevisions", 3))
    except (TypeError, ValueError) as exc:
        raise ValueError("Maximum plan revisions must be an integer.") from exc
    if not 0 <= max_plan_revisions <= 20:
        raise ValueError("Maximum plan revisions must be between 0 and 20.")

    supplied = {
        "task specification": bool(spec_data.get("text")),
        "background notes": bool(background_data.get("text")),
        "Planner output": bool(planner_data.get("text")),
        "Task Breaker JSON": bool(task_breaker_data.get("text")),
        "referenced subtask files": bool(subtask_file_items),
    }
    required_by_level = {
        1: ("task specification", supplied["task specification"]),
        2: ("Planner output", supplied["Planner output"]),
        3: ("Task Breaker JSON", supplied["Task Breaker JSON"]),
    }
    required_name, required_present = required_by_level[run_level]
    if not required_present:
        raise ValueError(f"Level {run_level} requires a {required_name}.")

    allowed_by_level = {
        1: {"task specification", "background notes"},
        2: {"Planner output"},
        3: {"Task Breaker JSON", "referenced subtask files"},
    }
    incompatible = [
        name
        for name, is_present in supplied.items()
        if is_present and name not in allowed_by_level[run_level]
    ]
    if incompatible:
        raise ValueError(
            f"Level {run_level} does not accept: {', '.join(incompatible)}. "
            "Return to the level selector and choose the matching route."
        )

    model_config = AgentModelConfig(
        models=payload.get("models") or {},
        settings=payload.get("settings") or {},
    )
    model_config.validate()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Upload storage is anchored to the application, not to whichever directory
    # happened to launch the web server.
    upload_dir = PROJECT_ROOT / "uploads" / timestamp
    upload_dir.mkdir(parents=True, exist_ok=True)

    spec_path = upload_dir / safe_upload_name(spec_data.get("name"), "spec.md")
    spec_path.write_text(
        spec_data.get("text") or "No initial task specification was provided for this resumed run.",
        encoding="utf-8",
    )

    background_path = None
    if run_level == 1 and background_data.get("text"):
        background_name = safe_upload_name(background_data.get("name"), "background.md")
        background_suffix = Path(background_name).suffix.lower()
        if background_suffix not in {".md", ".txt", ".json"}:
            raise ValueError("Background must be a Markdown, text, or JSON file.")
        background_text = background_data["text"]
        if background_suffix == ".json":
            try:
                parsed_background = json.loads(background_text)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Background JSON is invalid at line {exc.lineno}, column {exc.colno}."
                ) from exc
            if not isinstance(parsed_background, (dict, list)):
                raise ValueError("Background JSON must contain a top-level object or array.")
            background_text = json.dumps(parsed_background, indent=2, ensure_ascii=False) + "\n"
        background_path = upload_dir / background_name
        background_path.write_text(background_text, encoding="utf-8")

    resume_plan_path = None
    if run_level == 2:
        resume_plan_path = upload_dir / safe_upload_name(planner_data.get("name"), "planner_output.txt")
        resume_plan_path.write_text(planner_data["text"], encoding="utf-8")

    resume_task_breaker_path = None
    if run_level == 3:
        resume_task_breaker_path = upload_dir / safe_upload_name(
            task_breaker_data.get("name"),
            "task_breaker.json",
        )
        parsed = json.loads(task_breaker_data["text"])
        resume_task_breaker_path.write_text(
            json.dumps(parsed, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    saved_subtask_names = set()
    for subtask_item in subtask_file_items:
        if not isinstance(subtask_item, dict) or not subtask_item.get("dataUrl"):
            continue
        subtask_name = Path(str(subtask_item.get("name") or "")).name
        if subtask_name in {"", ".", ".."}:
            raise ValueError("Every referenced subtask file must have a valid filename.")
        if subtask_name in saved_subtask_names:
            raise ValueError(f"Referenced subtask filename was uploaded more than once: {subtask_name}")
        data_url = subtask_item["dataUrl"]
        if "," not in data_url:
            raise ValueError(f"Uploaded subtask file data was not valid: {subtask_name}")
        (upload_dir / subtask_name).write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
        saved_subtask_names.add(subtask_name)

    start_subtask_number = 1
    if run_level == 3:
        start_subtask_number = payload.get("startSubtask") or 1
        try:
            start_subtask_number = max(1, int(start_subtask_number))
        except (TypeError, ValueError):
            start_subtask_number = 1

    image_paths = []
    for index, image_data in enumerate(image_items, start=1):
        if not image_data or not image_data.get("dataUrl"):
            continue
        image_name = safe_upload_name(image_data.get("name"), f"reference_{index}.png")
        image_path = upload_dir / image_name
        data_url = image_data["dataUrl"]
        if "," not in data_url:
            raise ValueError("The uploaded image data was not valid.")
        image_path.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
        image_paths.append(image_path)

    return MIMIInputBundle(
        spec_path=spec_path,
        background_path=background_path,
        image_path=image_paths[0] if image_paths else None,
        image_paths=image_paths,
        resume_plan_path=resume_plan_path,
        resume_task_breaker_path=resume_task_breaker_path,
        start_subtask_number=start_subtask_number,
        max_subtask_attempts=max_subtask_attempts,
        max_plan_revisions=max_plan_revisions,
        model_config=model_config,
    )


def make_web_handler(
    run_bundle_in_background,
    abort_run=None,
    record_heartbeat=None,
    record_disconnect=None,
    record_client_open=None,
    record_client_close=None,
    shutdown_server=None,
    control_token=None,
):
    literature_jobs = LiteratureExtractionRegistry()

    class MIMIWebHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed_url = urlparse(self.path)
            route = parsed_url.path
            if record_heartbeat:
                record_heartbeat()

            if route == "/client-session":
                client_id = parse_qs(parsed_url.query).get("clientId", [""])[0]
                self.stream_client_session(client_id)
                return

            if route == "/status":
                body = json.dumps(snapshot_web_state()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.end_headers()
                self.wfile.write(body)
                return

            if route == "/model-catalog":
                body = json.dumps({
                    "metadata": MODEL_CATALOG_METADATA,
                    "models": MODEL_CATALOG,
                    "agents": {
                        agent: {
                            "requirements": requirements,
                            "models": (
                                agent_models := model_options_for_agent(agent)
                            ),
                            "settings": {
                                model: setting_options_for_agent(model, agent)
                                for model in agent_models
                            },
                        }
                        for agent, requirements in AGENT_CAPABILITY_REQUIREMENTS.items()
                    },
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.end_headers()
                self.wfile.write(body)
                return

            self.serve_static_file()

        def stream_client_session(self, client_id: str) -> None:
            if not client_id:
                self.send_error(400, "clientId is required")
                return

            if record_client_open:
                record_client_open(client_id)

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            try:
                while True:
                    self.wfile.write(b": mimi-session\\n\\n")
                    self.wfile.flush()
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                if record_client_close:
                    record_client_close(client_id)
                self.close_connection = True

        def serve_static_file(self) -> None:
            route = unquote(urlparse(self.path).path)
            if route == "/":
                route = "/index.html"

            requested_path = (WEB_ROOT / route.lstrip("/")).resolve()
            try:
                requested_path.relative_to(WEB_ROOT.resolve())
            except ValueError:
                self.send_error(403)
                return

            if not requested_path.is_file():
                self.send_error(404)
                return

            body = requested_path.read_bytes()
            content_type = mimetypes.guess_type(requested_path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            parsed_url = urlparse(self.path)
            route = parsed_url.path

            if route == "/abort":
                self.discard_request_body()
                accepted = bool(abort_run and abort_run())
                if accepted:
                    self.send_json(202, {"message": "Stopping run."})
                else:
                    self.send_json(409, {"message": "No active run to stop."})
                return

            if route == "/literature-predicates/abandon":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > MAX_LITERATURE_CONTROL_BYTES:
                        raise ValueError("A valid literature extraction job ID is required.")
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    job_id = str(payload.get("jobId") or "").strip()
                    accepted = literature_jobs.abandon(job_id)
                    if accepted:
                        self.send_json(202, {"message": "Abandoning literature extraction."})
                    else:
                        self.send_json(409, {"message": "No matching literature extraction is active."})
                except Exception as exc:
                    self.send_json(400, {"message": str(exc)})
                return

            if route == "/heartbeat":
                self.discard_request_body()
                if record_heartbeat:
                    record_heartbeat()
                self.send_response(204)
                self.end_headers()
                return

            if route == "/disconnect":
                self.discard_request_body()
                client_id = parse_qs(parsed_url.query).get("clientId", [""])[0]
                if client_id and record_client_close:
                    record_client_close(client_id)
                elif record_disconnect:
                    record_disconnect()
                self.send_response(204)
                self.end_headers()
                return

            if route == "/shutdown":
                self.discard_request_body()
                supplied_token = self.headers.get("X-MIMI-Control-Token", "")
                if (
                    not control_token
                    or not supplied_token
                    or not hmac.compare_digest(supplied_token, control_token)
                ):
                    self.send_json(403, {"message": "Invalid MIMI control token."})
                    return
                if not shutdown_server:
                    self.send_json(503, {"message": "Server shutdown is unavailable."})
                    return
                self.send_json(202, {"message": "Stopping MIMI server."})
                threading.Thread(
                    target=shutdown_server,
                    name="mimi-control-shutdown",
                    daemon=True,
                ).start()
                return

            if route == "/voice-spec":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0:
                        raise ValueError("No voice recording was received.")
                    if length > MAX_VOICE_REQUEST_BYTES:
                        raise ValueError("The voice recording is too large.")
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    audio_data = str(payload.get("audioData") or "")
                    if "," not in audio_data:
                        raise ValueError("The voice recording was not valid.")
                    metadata, encoded_audio = audio_data.split(",", 1)
                    if "audio/wav" not in metadata:
                        raise ValueError("The browser must send a WAV recording.")
                    try:
                        audio_bytes = base64.b64decode(encoded_audio, validate=True)
                    except (ValueError, TypeError) as exc:
                        raise ValueError("The voice recording was not valid base64 data.") from exc
                    result = create_tool_spec_from_audio(
                        audio_bytes,
                        audio_format="wav",
                    )
                    self.send_json(200, result)
                except Exception as exc:
                    self.send_json(400, {"message": str(exc)})
                return

            if route == "/literature-predicates":
                if snapshot_web_state()["running"]:
                    self.discard_request_body()
                    self.send_json(409, {"message": "Wait for the active MIMI run to finish."})
                    return
                job_id = self.headers.get("X-MIMI-Literature-Job", "").strip()
                if not job_id:
                    job_id = f"legacy-{time.time_ns()}"
                try:
                    cancel_event = literature_jobs.begin(job_id)
                except ValueError as exc:
                    self.discard_request_body()
                    self.send_json(400, {"message": str(exc)})
                    return
                if cancel_event is None:
                    self.discard_request_body()
                    self.send_json(409, {"message": "Another literature extraction is already active."})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0:
                        raise ValueError("No literature PDFs were received.")
                    if length > MAX_LITERATURE_REQUEST_BYTES:
                        self.close_connection = True
                        self.send_json(413, {"message": "Literature upload is too large."})
                        return
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if cancel_event.is_set():
                        raise ExtractionAbandoned("Literature predicate extraction was abandoned.")
                    result = extract_uploaded_literature(
                        payload,
                        should_abandon=cancel_event.is_set,
                    )
                    self.send_json(200, result)
                except ExtractionAbandoned as exc:
                    self.send_json(409, {"message": str(exc), "abandoned": True})
                except Exception as exc:
                    self.send_json(400, {"message": str(exc)})
                finally:
                    literature_jobs.finish(job_id)
                return

            if route != "/run":
                self.send_error(404)
                return
            if literature_jobs.has_active_job():
                self.discard_request_body()
                self.send_json(409, {"message": "Wait for the literature extraction to finish."})
                return
            if snapshot_web_state()["running"]:
                self.send_json(409, {"message": "An agent run is already in progress."})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                bundle = save_uploaded_bundle(payload)
                thread = threading.Thread(target=run_bundle_in_background, args=(bundle,), daemon=True)
                thread.start()
                self.send_json(202, {"message": f"Files loaded from {bundle.spec_path.parent}. Agents started."})
            except Exception as exc:
                self.send_json(400, {"message": str(exc)})

        def discard_request_body(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 0:
                self.rfile.read(length)

        def send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            if urlparse(self.path).path in {
                "/status",
                "/heartbeat",
                "/disconnect",
                "/client-session",
                "/abort",
                "/literature-predicates/abandon",
                "/shutdown",
            }:
                return
            print("web:", format % args)

    return MIMIWebHandler


def serve_web(
    host: str,
    port: int,
    run_bundle_in_background,
    *,
    abort_run=None,
    auto_shutdown: bool = True,
    disconnect_grace_seconds: float = 2.0,
    heartbeat_timeout_seconds: float = 10.0,
    run_shutdown_grace_seconds: float = 5.0,
    control_token: str | None = None,
) -> None:
    client_lock = threading.Lock()
    client_state = {
        "last_seen": None,
        "disconnect_requested": None,
        "active_clients": set(),
        "had_client_session": False,
        "all_clients_closed_at": None,
    }

    def record_heartbeat() -> None:
        with client_lock:
            client_state["last_seen"] = time.monotonic()
            client_state["disconnect_requested"] = None

    def record_disconnect() -> None:
        with client_lock:
            client_state["disconnect_requested"] = time.monotonic()

    def record_client_open(client_id: str) -> None:
        with client_lock:
            client_state["active_clients"].add(client_id)
            client_state["had_client_session"] = True
            client_state["all_clients_closed_at"] = None
            client_state["disconnect_requested"] = None
            client_state["last_seen"] = time.monotonic()

    def record_client_close(client_id: str) -> None:
        with client_lock:
            client_state["active_clients"].discard(client_id)
            if (
                client_state["had_client_session"]
                and not client_state["active_clients"]
                and client_state["all_clients_closed_at"] is None
            ):
                client_state["all_clients_closed_at"] = time.monotonic()

    server_holder = {}
    shutdown_started = threading.Event()

    def stop_active_run(reason: str) -> None:
        if not snapshot_web_state()["running"]:
            return
        if not shutdown_started.is_set():
            shutdown_started.set()
            print(f"Stopping active MIMI run: {reason}.", flush=True)
            if abort_run:
                abort_run()

        deadline = time.monotonic() + max(0.0, run_shutdown_grace_seconds)
        while snapshot_web_state()["running"] and time.monotonic() < deadline:
            time.sleep(0.05)
        if snapshot_web_state()["running"]:
            print(
                "Active run did not finish cleanup before shutdown; closing the launcher.",
                flush=True,
            )

    def request_server_shutdown(reason: str = "shutdown requested") -> None:
        update_web_state(
            message="Stopping MIMI server",
            phase="stopping",
        )
        stop_active_run(reason)
        server = server_holder.get("server")
        if server:
            server.shutdown()

    handler = make_web_handler(
        run_bundle_in_background,
        abort_run=abort_run,
        record_heartbeat=record_heartbeat,
        record_disconnect=record_disconnect,
        record_client_open=record_client_open,
        record_client_close=record_client_close,
        shutdown_server=request_server_shutdown,
        control_token=control_token,
    )
    server = MIMIThreadingHTTPServer((host, port), handler)
    server_holder["server"] = server
    print(f"MIMI upload interface: http://{host}:{port}")

    monitor_stop = threading.Event()

    def monitor_browser_connection() -> None:
        while not monitor_stop.wait(0.5):
            now = time.monotonic()
            with client_lock:
                last_seen = client_state["last_seen"]
                disconnect_requested = client_state["disconnect_requested"]
                active_clients = set(client_state["active_clients"])
                had_client_session = client_state["had_client_session"]
                all_clients_closed_at = client_state["all_clients_closed_at"]

            beacon_closed = (
                disconnect_requested is not None
                and not active_clients
                and now - disconnect_requested >= disconnect_grace_seconds
            )
            session_closed = (
                had_client_session
                and not active_clients
                and all_clients_closed_at is not None
                and now - all_clients_closed_at >= disconnect_grace_seconds
            )
            heartbeat_expired = (
                last_seen is not None
                and not active_clients
                and now - last_seen >= heartbeat_timeout_seconds
            )
            if not beacon_closed and not session_closed and not heartbeat_expired:
                continue
            reason = (
                "browser page closed"
                if beacon_closed or session_closed
                else "browser heartbeat expired"
            )
            print(f"Stopping MIMI server automatically: {reason}.")
            request_server_shutdown(reason)
            return

    if auto_shutdown:
        monitor_thread = threading.Thread(
            target=monitor_browser_connection,
            name="mimi-browser-monitor",
            daemon=True,
        )
        monitor_thread.start()

    try:
        server.serve_forever()
    finally:
        monitor_stop.set()
        stop_active_run("web server stopped")
        server.server_close()
