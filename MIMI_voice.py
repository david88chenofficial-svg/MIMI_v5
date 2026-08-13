"""Turn a bounded voice recording into a clarified MIMI tool specification."""

from __future__ import annotations

import base64
from datetime import datetime
import os
from pathlib import Path

from openai import OpenAI

from MIMI_credentials import configure_openai_api_key


ROOT = Path(__file__).resolve().parent
VOICE_SPEC_MODEL = os.environ.get("MIMI_VOICE_MODEL", "gpt-audio-mini")
MAX_VOICE_AUDIO_BYTES = 10 * 1024 * 1024

VOICE_SPEC_INSTRUCTIONS = """
You are MIMI's voice-intake agent. Listen carefully to the user's recording
and turn it into a clear Markdown task/tool specification for downstream
planning and coding agents.

The recording may contain hesitations, repetitions, false starts, unclear
wording, background noise, or incomplete technical language. Lightly rewrite
it for clarity and structure while preserving the user's actual intent.

Requirements:
- Preserve every concrete requirement, name, number, constraint, example, and
  requested deliverable that can be heard.
- Remove filler words and harmless repetition.
- Resolve pronouns or vague phrasing only when the intended meaning is clear
  from the recording.
- Never invent requirements, technologies, equations, data, filenames, or
  acceptance criteria.
- Do not choose or suggest a framework, programming language, architecture,
  algorithm, or dependency unless the user named it.
- Do not turn a likely implementation detail into a requirement. If the user
  did not supply information for a section, write "Not specified" or omit that
  section instead of filling it with conventional defaults.
- If something important is uncertain, retain the uncertainty explicitly
  under "Open questions or unclear points"; do not guess.
- Distinguish requested requirements from optional ideas mentioned aloud.
- Write self-contained instructions that downstream agents can understand
  without hearing the recording.
- Return Markdown only. Do not wrap it in a code fence and do not add a
  preamble about your process.

Use this structure, omitting only sections that genuinely do not apply:

# Tool Specification

## Objective

## Functional requirements

## Inputs

## Outputs and deliverables

## Constraints and preferences

## Acceptance criteria

## Open questions or unclear points

## Cleaned voice instruction

The final section should be a faithful, readable rendering of the complete
instruction. It is not required to preserve filler words, stutters, or repeated
fragments.
""".strip()


def _load_api_key() -> None:
    configure_openai_api_key(required=True)


def _normalise_markdown(text: str) -> str:
    markdown = (text or "").strip()
    if markdown.startswith("```") and markdown.endswith("```"):
        lines = markdown.splitlines()
        if len(lines) >= 3:
            markdown = "\n".join(lines[1:-1]).strip()
    if not markdown:
        raise RuntimeError("The voice-intake model returned an empty specification.")
    if not markdown.startswith("#"):
        markdown = f"# Tool Specification\n\n{markdown}"
    return markdown.rstrip() + "\n"


def _write_voice_spec(markdown: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output_dir = ROOT / "uploads" / f"voice_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=False)
    output_path = output_dir / "voice_tool_spec.md"
    output_path.write_text(markdown, encoding="utf-8", newline="\n")
    return output_path


def create_tool_spec_from_audio(
    audio_bytes: bytes,
    *,
    audio_format: str = "wav",
) -> dict[str, str]:
    """Ask the inexpensive audio model for a clarified spec and save it."""
    if audio_format not in {"wav", "mp3"}:
        raise ValueError("Voice recordings must use WAV or MP3 audio.")
    if not audio_bytes:
        raise ValueError("The voice recording was empty.")
    if len(audio_bytes) > MAX_VOICE_AUDIO_BYTES:
        raise ValueError("The voice recording is too large (10 MB maximum).")
    if audio_format == "wav" and not audio_bytes.startswith(b"RIFF"):
        raise ValueError("The voice recording was not a valid WAV file.")

    _load_api_key()
    encoded_audio = base64.b64encode(audio_bytes).decode("ascii")
    client = OpenAI()
    completion = client.chat.completions.create(
        model=VOICE_SPEC_MODEL,
        modalities=["text"],
        max_completion_tokens=3000,
        messages=[
            {
                "role": "developer",
                "content": VOICE_SPEC_INSTRUCTIONS,
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Create the clarified tool specification from this "
                            "voice instruction."
                        ),
                    },
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": encoded_audio,
                            "format": audio_format,
                        },
                    },
                ],
            },
        ],
    )
    markdown = _normalise_markdown(completion.choices[0].message.content or "")
    output_path = _write_voice_spec(markdown)
    return {
        "name": output_path.name,
        "path": str(output_path.relative_to(ROOT)),
        "text": markdown,
        "model": VOICE_SPEC_MODEL,
    }

