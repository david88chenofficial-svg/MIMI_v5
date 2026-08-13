import base64
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from MIMI_models import AgentModelConfig


@dataclass
class MIMIInputBundle:
    spec_path: Path
    background_path: Path | None = None
    image_path: Path | None = None
    image_paths: list[Path] | None = None
    resume_plan_path: Path | None = None
    resume_task_breaker_path: Path | None = None
    start_subtask_number: int = 1
    max_subtask_attempts: int = 3
    max_plan_revisions: int = 3
    model_config: AgentModelConfig | None = None

    def __post_init__(self):
        try:
            self.max_subtask_attempts = int(self.max_subtask_attempts)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_subtask_attempts must be an integer.") from exc
        if not 1 <= self.max_subtask_attempts <= 20:
            raise ValueError("max_subtask_attempts must be between 1 and 20.")
        try:
            self.max_plan_revisions = int(self.max_plan_revisions)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_plan_revisions must be an integer.") from exc
        if not 0 <= self.max_plan_revisions <= 20:
            raise ValueError("max_plan_revisions must be between 0 and 20.")
        if self.image_paths is None:
            self.image_paths = [self.image_path] if self.image_path else []
        elif self.image_paths and not self.image_path:
            self.image_path = self.image_paths[0]

    def load_spec(self) -> str:
        return self.spec_path.read_text(encoding="utf-8")

    def load_background(self) -> str:
        if not self.background_path:
            return ""
        return self.background_path.read_text(encoding="utf-8")

    def image_b64(self) -> str:
        if not self.image_path:
            return ""
        return base64.b64encode(self.image_path.read_bytes()).decode("ascii")

    def image_mime(self) -> str:
        if not self.image_path:
            return "image/png"
        return mimetypes.guess_type(self.image_path.name)[0] or "image/png"


def default_input_bundle() -> MIMIInputBundle:
    return MIMIInputBundle(
        spec_path=Path("seal_analysis_tool_spec_v6.md"),
        background_path=Path("seal_tool_background_knowledge_short.md"),
        image_path=Path("seal_diagram.png"),
    )


def build_prompt_with_reference_image(text: str, bundle: MIMIInputBundle):
    content = [{"type": "input_text", "text": text}]
    for image_path in bundle.image_paths or []:
        content.append({
            "type": "input_image",
            "image_url": (
                f"data:{mimetypes.guess_type(image_path.name)[0] or 'image/png'};base64,"
                f"{base64.b64encode(image_path.read_bytes()).decode('ascii')}"
            ),
        })
    return [{
        "role": "user",
        "content": content,
    }]

