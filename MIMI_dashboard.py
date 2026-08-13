import base64
import hmac
import json
import mimetypes
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import MIMI_functions as mf
from MIMI_inputs import MIMIInputBundle
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
WEB_ROOT = Path(__file__).resolve().parent / "web"

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
}
MAX_VOICE_REQUEST_BYTES = int(MAX_VOICE_AUDIO_BYTES * 1.4) + 4096


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
    )


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
            item["data_url"] = f"data:{mime};base64,{mf.image_to_base64(artifact_path)}"
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
    upload_dir = Path("uploads") / timestamp
    upload_dir.mkdir(parents=True, exist_ok=True)

    spec_path = upload_dir / safe_upload_name(spec_data.get("name"), "spec.md")
    spec_path.write_text(
        spec_data.get("text") or "No initial task specification was provided for this resumed run.",
        encoding="utf-8",
    )

    background_path = None
    if run_level == 1 and background_data.get("text"):
        background_path = upload_dir / safe_upload_name(background_data.get("name"), "background.md")
        background_path.write_text(background_data["text"], encoding="utf-8")

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

            if route != "/run":
                self.send_error(404)
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
    heartbeat_timeout_seconds: float = 30.0,
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

    def request_server_shutdown() -> None:
        update_web_state(
            message="Stopping MIMI server",
            phase="stopping",
        )
        if abort_run:
            abort_run()
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
    server = ThreadingHTTPServer((host, port), handler)
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
            if snapshot_web_state()["running"]:
                continue

            reason = (
                "browser page closed"
                if beacon_closed or session_closed
                else "browser heartbeat expired"
            )
            print(f"Stopping MIMI server automatically: {reason}.")
            server.shutdown()
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
        server.server_close()

