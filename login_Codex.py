"""Ensure the bundled Codex runtime is signed in with ChatGPT."""

from __future__ import annotations

import webbrowser

from openai_codex import Codex


def main() -> int:
    with Codex() as codex:
        account = codex.account()
        if account.account is not None:
            print("Codex is already signed in. No action is needed.")
            return 0

        login = codex.login_chatgpt()
        print("Opening the ChatGPT sign-in page in your browser...")
        print(f"If it does not open automatically, visit:\n{login.auth_url}")
        webbrowser.open(login.auth_url)
        print("Waiting for sign-in to finish...")
        login.wait()
        print("Codex sign-in completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
