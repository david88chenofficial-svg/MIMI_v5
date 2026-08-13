# MIMI

## Progressive browser interface

Start the normal file workflow:

```powershell
python .\lauch_MIMI.py
```

MIMI opens with a Level 1, Level 2, or Level 3 choice. Only the file drop
targets needed by the selected level are then shown.

Start directly in Native Union POP Phone voice-intake mode:

```powershell
python .\lauch_MIMI.py --pop-phone
```

Voice intake records from the POP Phone microphone, creates a clarified
Markdown specification, and places it in the Level 1 specification slot for
review. Agent and voice runs use `OPENAI_API_KEY` from the environment or from
an `API_key.env` file in this folder.

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

Use `python .\pop_phone_button.py --diagnose` to inspect handset button events.
The notification-area MIMI icon can also open file input or voice intake, show
backend status, and stop the server.
