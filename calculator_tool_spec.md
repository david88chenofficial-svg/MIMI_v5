# Simple GUI Calculator Specification

## Goal
Build a very small Python GUI calculator for testing MIMI.

## Final Outcome
The final program should open a calculator window where the user can click buttons to enter numbers and operations.

## Requirements
Create one Python file named `simple_gui_calculator.py`.
Give only 3-4 coding subtasks.

The GUI must include:
- A display area showing the current input or result.
- Number buttons: `0` to `9`.
- Operation buttons: `+`, `-`, `*`, `/`.
- A decimal point button: `.`.
- An equals button: `=`.
- A clear button: `C`.

Behavior:
- Clicking number and operator buttons appends them to the display.
- Clicking `=` evaluates the expression and shows the result.
- Clicking `C` clears the display.
- Division by zero should show `Error` instead of crashing.
- Invalid expressions should show `Error` instead of crashing.

Implementation constraints:
- Use only the Python standard library.
- Prefer `tkinter` for the GUI.
- Keep the code simple and readable.
- Include a `main()` function.
- Run the GUI when the file is executed directly.

## Result Metadata
The program must also create `result.json` when it starts, with this structure:

```json
{
  "summary": "Simple clickable GUI calculator.",
  "artifacts": {
    "plots": {},
    "texts": {}
  }
}
```

## Acceptance Test
The task is complete if:
- Running `python simple_gui_calculator.py` opens a calculator window.
- The user can click buttons to calculate `2 + 3 = 5`.
- Clear resets the display.
- `1 / 0 =` shows `Error`.
