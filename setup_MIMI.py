"""Create MIMI's virtual environment and install all required packages."""

from __future__ import annotations

import shutil
import subprocess
import sys
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
API_KEY_EXAMPLE = ROOT / "API_key.env.example"
API_KEY_FILE = ROOT / "API_key.env"


def virtual_environment_python() -> Path:
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def run(command: list[str]) -> None:
    print(f"\n> {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    if sys.version_info < (3, 10):
        print(
            "MIMI requires Python 3.10 or newer. "
            f"This interpreter is Python {sys.version_info.major}.{sys.version_info.minor}."
        )
        return 1

    if not REQUIREMENTS.is_file():
        print(f"Could not find {REQUIREMENTS}.")
        return 1

    venv_python = virtual_environment_python()
    if not venv_python.is_file():
        print(f"Creating an isolated Python environment in {VENV_DIR.name}...")
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)
    else:
        print(f"Reusing the existing {VENV_DIR.name} environment.")

    run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(venv_python), "-m", "pip", "install", "-r", str(REQUIREMENTS)])
    run(
        [
            str(venv_python),
            "-c",
            (
                "import agents, openai, openai_codex, pydantic, dotenv, pypdf; "
                "print('Dependency check passed.')"
            ),
        ]
    )

    created_key_file = False
    if not API_KEY_FILE.exists() and API_KEY_EXAMPLE.is_file():
        shutil.copyfile(API_KEY_EXAMPLE, API_KEY_FILE)
        created_key_file = True

    print("\nMIMI installation completed successfully.")
    if created_key_file:
        print("Next, open API_key.env and paste your OpenAI API key after OPENAI_API_KEY=.")
    elif API_KEY_FILE.exists():
        print("API_key.env already exists and was left unchanged.")
    else:
        print("Set OPENAI_API_KEY before starting MIMI.")

    if sys.platform == "win32":
        print("Then double-click login_Codex.bat once, followed by run_MIMI.bat.")
        print("You can also start MIMI directly with:")
        print(r"  .\.venv\Scripts\python.exe .\launch_MIMI.py")
    else:
        print("Then run:")
        print("  ./.venv/bin/python ./launch_MIMI.py")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"\nInstallation stopped because a command failed with exit code {exc.returncode}.")
        raise SystemExit(exc.returncode) from exc
