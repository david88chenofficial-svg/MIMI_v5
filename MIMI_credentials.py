"""Load MIMI's OpenAI credential without exposing it to workflow artifacts."""

from __future__ import annotations

import os
from pathlib import Path

from agents import set_default_openai_key
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent
API_KEY_ENV_PATH = PROJECT_ROOT / "API_key.env"


def configure_openai_api_key(
    *,
    required: bool = True,
    env_path: str | Path = API_KEY_ENV_PATH,
) -> Path | None:
    """
    Load OPENAI_API_KEY from MIMI's env file and bind it to the Agents SDK.

    The key value is intentionally never returned, printed, logged, or placed in
    an agent prompt. ``override=True`` guarantees that the selected file, rather
    than an unrelated inherited process variable, is authoritative.
    """
    credential_path = Path(env_path).resolve()
    if not credential_path.is_file():
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if api_key:
            set_default_openai_key(api_key, use_for_tracing=True)
            return None
        if required:
            raise RuntimeError(
                f"OpenAI credentials were not found at {credential_path}. "
                "Create API_key.env with OPENAI_API_KEY=<your key>, or set "
                "OPENAI_API_KEY in the environment."
            )
        return None

    load_dotenv(credential_path, override=True)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        if required:
            raise RuntimeError(
                f"OPENAI_API_KEY is missing or empty in {credential_path}."
            )
        return None

    set_default_openai_key(api_key, use_for_tracing=True)
    return credential_path

