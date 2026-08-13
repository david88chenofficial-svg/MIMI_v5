"""Open or reveal MIMI when the Native Union POP Phone button is pressed.

The current USB-C POP Phone exposes its centre button as a Consumer Control
HID. On the tested handset (VID 2D1D, PID 000F), a press is the raw report
``00 04 00`` and its release is ``00 00 00``.

This listener uses the Windows Raw Input API so it needs no extra Python
packages and ignores consumer-control events from every other device.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from MIMI_control import get_server_status, stop_server


ROOT = Path(__file__).resolve().parent
MIMI_LAUNCHER = ROOT / "lauch_MIMI.py"
POP_PHONE_ID = "VID_2D1D&PID_000F"
BUTTON_DOWN_REPORT = b"\x00\x04\x00"
BUTTON_UP_REPORT = b"\x00\x00\x00"
MIMI_PORTS = range(8000, 8100)
MIMI_PAGE_MARKER = b"<title>MIMI Inputs</title>"
STARTUP_SHORTCUT_NAME = "MIMI POP Phone.lnk"
WINDOW_CLASS_NAME = "MIMIPopPhoneButtonListener"
MUTEX_NAME = r"Local\MIMIPopPhoneButtonListener"

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_INPUT = 0x00FF
RID_INPUT = 0x10000003
RIDI_DEVICENAME = 0x20000007
RIDEV_INPUTSINK = 0x00000100
RIDEV_DEVNOTIFY = 0x00002000
PM_REMOVE = 0x0001
SW_RESTORE = 9
ERROR_ALREADY_EXISTS = 183
INVALID_UINT = 0xFFFFFFFF
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_APP = 0x8000
WM_TRAY_ICON = WM_APP + 1
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_NULL = 0x0000
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
MF_STRING = 0x00000000
MF_GRAYED = 0x00000001
MF_SEPARATOR = 0x00000800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
IDI_APPLICATION = 32512
TRAY_ICON_ID = 1
TRAY_TIMER_ID = 1
MENU_STATUS = 100
MENU_SHOW_STATUS = 101
MENU_OPEN_FILES = 102
MENU_OPEN_VOICE = 103
MENU_STOP_SERVER = 104
MENU_LIVE_LOG = 105
MENU_EXIT = 106
CREATE_NEW_CONSOLE = 0x00000010
MB_OK = 0x00000000
MB_ICONINFORMATION = 0x00000040
MB_ICONERROR = 0x00000010

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)
ENUMWINDOWSPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HWND,
    wintypes.LPARAM,
)


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

user32.GetRawInputData.argtypes = [
    wintypes.HANDLE,
    wintypes.UINT,
    wintypes.LPVOID,
    ctypes.POINTER(wintypes.UINT),
    wintypes.UINT,
]
user32.GetRawInputData.restype = wintypes.UINT
user32.GetRawInputDeviceInfoW.argtypes = [
    wintypes.HANDLE,
    wintypes.UINT,
    wintypes.LPVOID,
    ctypes.POINTER(wintypes.UINT),
]
user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
user32.DefWindowProcW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.DefWindowProcW.restype = LRESULT
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
user32.UnregisterClassW.restype = wintypes.BOOL
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.RegisterRawInputDevices.argtypes = [
    ctypes.POINTER(RAWINPUTDEVICE),
    wintypes.UINT,
    wintypes.UINT,
]
user32.RegisterRawInputDevices.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.EnumWindows.argtypes = [ENUMWINDOWSPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadIconW.restype = wintypes.HICON
user32.SetTimer.argtypes = [
    wintypes.HWND,
    ctypes.c_size_t,
    wintypes.UINT,
    ctypes.c_void_p,
]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.KillTimer.restype = wintypes.BOOL
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.CreatePopupMenu.argtypes = []
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [
    wintypes.HMENU,
    wintypes.UINT,
    ctypes.c_size_t,
    wintypes.LPCWSTR,
]
user32.AppendMenuW.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    ctypes.c_void_p,
]
user32.TrackPopupMenu.restype = wintypes.UINT
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.DestroyMenu.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.PostMessageW.restype = wintypes.BOOL
user32.MessageBoxW.argtypes = [
    wintypes.HWND,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.UINT,
]
user32.MessageBoxW.restype = ctypes.c_int
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.CreateMutexW.argtypes = [
    ctypes.c_void_p,
    wintypes.BOOL,
    wintypes.LPCWSTR,
]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.GetLastError.argtypes = []
kernel32.GetLastError.restype = wintypes.DWORD
shell32.Shell_NotifyIconW.argtypes = [
    wintypes.DWORD,
    ctypes.POINTER(NOTIFYICONDATAW),
]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

TASKBAR_CREATED_MESSAGE = user32.RegisterWindowMessageW("TaskbarCreated")


def app_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", ROOT))
    path = base / "MIMI"
    path.mkdir(parents=True, exist_ok=True)
    return path


def configure_logging(verbose: bool = False) -> Path:
    log_path = app_data_dir() / "pop_phone_button.log"
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    if verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )
    return log_path


def pythonw_path() -> Path:
    executable = Path(sys.executable)
    candidate = executable.with_name("pythonw.exe")
    return candidate if candidate.exists() else executable


def python_console_path() -> Path:
    """Return the console Python executable even when the listener uses pythonw."""
    executable = Path(sys.executable)
    candidate = executable.with_name("python.exe")
    return candidate if candidate.exists() else executable


def quote_powershell(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def backend_console_command(*, voice_mode: bool) -> list[str]:
    """Build a visible backend command that also preserves the existing log."""
    launcher_log_path = app_data_dir() / "mimi_launcher.log"
    launcher_arguments = [
        python_console_path(),
        "-u",
        MIMI_LAUNCHER,
    ]
    if voice_mode:
        launcher_arguments.append("--pop-phone")
    invocation = "& " + " ".join(
        quote_powershell(argument) for argument in launcher_arguments
    )
    command = (
        "$Host.UI.RawUI.WindowTitle='MIMI Backend'; "
        "Write-Host 'MIMI backend is starting. Live output is shown below.'; "
        f"{invocation} 2>&1 | Tee-Object -FilePath "
        f"{quote_powershell(launcher_log_path)} -Append; "
        "$mimiExitCode=$LASTEXITCODE; "
        "Write-Host ''; "
        "if ($mimiExitCode -eq 0) { "
        "Write-Host 'MIMI backend stopped.' "
        "} else { "
        "Write-Host ('MIMI backend stopped with exit code ' + $mimiExitCode) -ForegroundColor Red "
        "}; "
        "Write-Host 'Press Enter to close this window.'; "
        "[void](Read-Host)"
    )
    return [
        "powershell.exe",
        "-NoLogo",
        "-Command",
        command,
    ]


def startup_shortcut_path() -> Path:
    return (
        Path(os.environ["APPDATA"])
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / STARTUP_SHORTCUT_NAME
    )


def install_startup_shortcut() -> Path:
    """Create the per-user Startup shortcut using Windows Script Host."""
    try:
        from win32com.client import Dispatch
    except ImportError as exc:
        raise RuntimeError(
            "Installing the Startup shortcut requires pywin32. "
            "Run `python -m pip install pywin32` once, then retry."
        ) from exc

    shortcut_path = startup_shortcut_path()
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    shortcut = Dispatch("WScript.Shell").CreateShortCut(str(shortcut_path))
    shortcut.Targetpath = str(pythonw_path())
    shortcut.Arguments = subprocess.list2cmdline([str(Path(__file__).resolve())])
    shortcut.WorkingDirectory = str(ROOT)
    shortcut.Description = "Open MIMI with the Native Union POP Phone button"
    shortcut.save()
    return shortcut_path


def remove_startup_shortcut() -> bool:
    shortcut_path = startup_shortcut_path()
    if not shortcut_path.exists():
        return False
    shortcut_path.unlink()
    return True


def raw_input_device_name(device_handle: wintypes.HANDLE) -> str:
    character_count = wintypes.UINT()
    result = user32.GetRawInputDeviceInfoW(
        device_handle,
        RIDI_DEVICENAME,
        None,
        ctypes.byref(character_count),
    )
    if result == INVALID_UINT or not character_count.value:
        return ""

    buffer = ctypes.create_unicode_buffer(character_count.value + 1)
    result = user32.GetRawInputDeviceInfoW(
        device_handle,
        RIDI_DEVICENAME,
        buffer,
        ctypes.byref(character_count),
    )
    return "" if result == INVALID_UINT else buffer.value


def read_raw_hid(lparam: int) -> tuple[str, list[bytes]]:
    """Return the originating device path and the HID reports in WM_INPUT."""
    byte_count = wintypes.UINT()
    header_size = ctypes.sizeof(RAWINPUTHEADER)
    result = user32.GetRawInputData(
        wintypes.HANDLE(lparam),
        RID_INPUT,
        None,
        ctypes.byref(byte_count),
        header_size,
    )
    if result != 0 or not byte_count.value:
        return "", []

    buffer = ctypes.create_string_buffer(byte_count.value)
    result = user32.GetRawInputData(
        wintypes.HANDLE(lparam),
        RID_INPUT,
        buffer,
        ctypes.byref(byte_count),
        header_size,
    )
    if result == INVALID_UINT:
        return "", []

    header = RAWINPUTHEADER.from_buffer_copy(buffer.raw[:header_size])
    device_name = raw_input_device_name(header.hDevice)
    report_size = int.from_bytes(buffer.raw[header_size : header_size + 4], "little")
    report_count = int.from_bytes(
        buffer.raw[header_size + 4 : header_size + 8],
        "little",
    )
    if not report_size or not report_count:
        return device_name, []

    data_start = header_size + 8
    reports = [
        buffer.raw[
            data_start + (index * report_size) : data_start
            + ((index + 1) * report_size)
        ]
        for index in range(report_count)
    ]
    return device_name, reports


def find_running_mimi_port() -> int | None:
    """Locate an existing MIMI web server without depending on its process ID."""
    for port in MIMI_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.01)
            if connection.connect_ex(("127.0.0.1", port)) != 0:
                continue
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/",
                timeout=0.25,
            ) as response:
                if MIMI_PAGE_MARKER in response.read(4096):
                    return port
        except (OSError, urllib.error.URLError):
            continue
    return None


def focus_mimi_window(title_fragment: str = "MIMI Inputs") -> bool:
    """Restore a browser window when its active tab has the requested MIMI UI."""
    match: list[int] = []

    def inspect_window(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title_length = user32.GetWindowTextLengthW(hwnd)
        if title_length <= 0:
            return True
        title = ctypes.create_unicode_buffer(title_length + 1)
        user32.GetWindowTextW(hwnd, title, len(title))
        if title_fragment in title.value:
            match.append(hwnd)
            return False
        return True

    callback = ENUMWINDOWSPROC(inspect_window)
    user32.EnumWindows(callback, 0)
    if not match:
        return False

    hwnd = match[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True


def open_mimi_url(port: int, *, voice_mode: bool = True) -> None:
    from lauch_MIMI import open_in_chrome

    launch_id = uuid.uuid4().hex
    source_query = "&source=pop-phone" if voice_mode else ""
    open_in_chrome(f"http://localhost:{port}/?launch={launch_id}{source_query}")


def launch_or_show_mimi(*, voice_mode: bool = True) -> None:
    port = find_running_mimi_port()
    if port is not None:
        target_title = "MIMI Voice Intake" if voice_mode else "MIMI Inputs"
        if focus_mimi_window(target_title):
            logging.info("Brought the existing %s window to the foreground.", target_title)
        else:
            mode_name = "voice intake" if voice_mode else "file input"
            logging.info("MIMI is already on port %d; opening %s.", port, mode_name)
            open_mimi_url(port, voice_mode=voice_mode)
        return

    if not MIMI_LAUNCHER.exists():
        logging.error("Could not find MIMI launcher: %s", MIMI_LAUNCHER)
        return

    process = subprocess.Popen(
        backend_console_command(voice_mode=voice_mode),
        cwd=ROOT,
        creationflags=CREATE_NEW_CONSOLE,
        close_fds=True,
    )
    logging.info("Started MIMI (process %d).", process.pid)


def open_live_backend_log() -> None:
    listener_log = app_data_dir() / "pop_phone_button.log"
    server_log = app_data_dir() / "mimi_launcher.log"
    listener_log.touch(exist_ok=True)
    server_log.touch(exist_ok=True)

    command = (
        "$Host.UI.RawUI.WindowTitle='MIMI Backend Log'; "
        "Write-Host 'Watching the POP Phone listener and MIMI server logs.'; "
        "Write-Host 'Close this window when you no longer need the monitor.'; "
        f"Get-Content -LiteralPath {quote_powershell(listener_log)},"
        f"{quote_powershell(server_log)} -Tail 40 -Wait"
    )
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoExit",
            "-Command",
            command,
        ],
        cwd=ROOT,
        creationflags=CREATE_NEW_CONSOLE,
        close_fds=True,
    )


class PopPhoneButtonListener:
    def __init__(self, diagnose: bool = False) -> None:
        self.diagnose = diagnose
        self.button_is_down = False
        self.last_triggered_at = 0.0
        self.trigger_lock = threading.Lock()
        self.window_handle: int | None = None
        self.instance_handle: int | None = None
        self.window_callback = WNDPROC(self._window_procedure)
        self.tray_icon_handle: int | None = None
        self.tray_icon_added = False
        self.last_tray_tooltip = ""

    def _backend_status(self) -> dict | None:
        try:
            return get_server_status(timeout=0.15)
        except Exception:
            logging.exception("Could not read MIMI server status.")
            return None

    def _status_summary(self, status: dict | None = None) -> str:
        status = status if status is not None else self._backend_status()
        if not status:
            return "MIMI stopped - POP Phone listener ready"
        web_state = status.get("web") or {}
        phase = str(web_state.get("phase") or "idle").replace("_", " ")
        return f"MIMI running on port {status['port']} - {phase}"

    def _tray_data(self, flags: int, tooltip: str = "") -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self.window_handle
        data.uID = TRAY_ICON_ID
        data.uFlags = flags
        data.uCallbackMessage = WM_TRAY_ICON
        data.hIcon = self.tray_icon_handle
        data.szTip = tooltip[:127]
        return data

    def _add_tray_icon(self) -> None:
        if self.diagnose or not self.window_handle:
            return
        resource = ctypes.cast(
            ctypes.c_void_p(IDI_APPLICATION),
            wintypes.LPCWSTR,
        )
        self.tray_icon_handle = user32.LoadIconW(None, resource)
        tooltip = self._status_summary()
        data = self._tray_data(
            NIF_MESSAGE | NIF_ICON | NIF_TIP,
            tooltip,
        )
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            logging.error("Windows could not add the MIMI tray icon.")
            return
        self.tray_icon_added = True
        self.last_tray_tooltip = tooltip

    def _refresh_tray_icon(self) -> None:
        if not self.tray_icon_added:
            return
        tooltip = self._status_summary()
        if tooltip == self.last_tray_tooltip:
            return
        data = self._tray_data(NIF_TIP, tooltip)
        if shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)):
            self.last_tray_tooltip = tooltip

    def _remove_tray_icon(self) -> None:
        if not self.tray_icon_added:
            return
        data = self._tray_data(0)
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
        self.tray_icon_added = False

    def _show_status_dialog(self) -> None:
        status = self._backend_status()
        if status:
            web_state = status.get("web") or {}
            message = (
                "POP Phone listener: running\n"
                f"Listener process: {os.getpid()}\n\n"
                "MIMI server: running\n"
                f"Server process: {status['pid']}\n"
                f"Address: http://127.0.0.1:{status['port']}\n"
                f"Phase: {web_state.get('phase') or 'idle'}\n"
                f"Message: {web_state.get('message') or 'Idle'}"
            )
        else:
            message = (
                "POP Phone listener: running\n"
                f"Listener process: {os.getpid()}\n\n"
                "MIMI server: stopped\n\n"
                "Press the handset button to start voice intake, or use the "
                "tray menu to open the file-input interface."
            )
        user32.MessageBoxW(
            self.window_handle,
            message,
            "MIMI Backend Status",
            MB_OK | MB_ICONINFORMATION,
        )

    def _stop_server(self) -> None:
        try:
            if stop_server():
                logging.info("Tray requested a graceful MIMI server stop.")
                return
            logging.warning("Tray could not find a controllable MIMI server.")
            user32.MessageBoxW(
                self.window_handle,
                "MIMI is already stopped or did not respond to the stop request.",
                "MIMI Server",
                MB_OK | MB_ICONERROR,
            )
        except Exception:
            logging.exception("Tray could not stop the MIMI server.")
            user32.MessageBoxW(
                self.window_handle,
                "MIMI could not be stopped. Open the live backend log for details.",
                "MIMI Server",
                MB_OK | MB_ICONERROR,
            )

    def _handle_tray_command(self, command: int) -> None:
        if command == MENU_SHOW_STATUS:
            self._show_status_dialog()
        elif command == MENU_OPEN_FILES:
            threading.Thread(
                target=launch_or_show_mimi,
                kwargs={"voice_mode": False},
                name="mimi-tray-file-launch",
                daemon=True,
            ).start()
        elif command == MENU_OPEN_VOICE:
            threading.Thread(
                target=launch_or_show_mimi,
                kwargs={"voice_mode": True},
                name="mimi-tray-voice-launch",
                daemon=True,
            ).start()
        elif command == MENU_STOP_SERVER:
            threading.Thread(
                target=self._stop_server,
                name="mimi-tray-stop",
                daemon=True,
            ).start()
        elif command == MENU_LIVE_LOG:
            try:
                open_live_backend_log()
            except Exception:
                logging.exception("Could not open the live backend log.")
        elif command == MENU_EXIT and self.window_handle:
            user32.DestroyWindow(self.window_handle)

    def _show_tray_menu(self) -> None:
        status = self._backend_status()
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            user32.AppendMenuW(
                menu,
                MF_STRING | MF_GRAYED,
                MENU_STATUS,
                self._status_summary(status),
            )
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, MENU_SHOW_STATUS, "Show Backend Status")
            user32.AppendMenuW(menu, MF_STRING, MENU_LIVE_LOG, "Open Live Backend Log")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, MENU_OPEN_FILES, "Open MIMI - File Input")
            user32.AppendMenuW(menu, MF_STRING, MENU_OPEN_VOICE, "Open MIMI - Voice Intake")
            stop_flags = MF_STRING if status else MF_STRING | MF_GRAYED
            user32.AppendMenuW(
                menu,
                stop_flags,
                MENU_STOP_SERVER,
                "Stop MIMI Server",
            )
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(
                menu,
                MF_STRING,
                MENU_EXIT,
                "Exit POP Phone Listener",
            )
            position = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(position))
            user32.SetForegroundWindow(self.window_handle)
            command = user32.TrackPopupMenu(
                menu,
                TPM_RIGHTBUTTON | TPM_RETURNCMD,
                position.x,
                position.y,
                0,
                self.window_handle,
                None,
            )
            user32.PostMessageW(self.window_handle, WM_NULL, 0, 0)
            if command:
                self._handle_tray_command(command)
        finally:
            user32.DestroyMenu(menu)

    def _trigger_mimi(self) -> None:
        if not self.trigger_lock.acquire(blocking=False):
            return
        try:
            launch_or_show_mimi()
        except Exception:
            logging.exception("Could not launch or show MIMI.")
        finally:
            self.trigger_lock.release()

    def _handle_report(self, report: bytes) -> None:
        if self.diagnose:
            logging.info("POP Phone raw report: %s", report.hex(" "))

        if report == BUTTON_UP_REPORT:
            self.button_is_down = False
            return
        if report != BUTTON_DOWN_REPORT or self.button_is_down:
            return

        self.button_is_down = True
        now = time.monotonic()
        if now - self.last_triggered_at < 0.5:
            return
        self.last_triggered_at = now
        logging.info("POP Phone button pressed.")
        if not self.diagnose:
            threading.Thread(
                target=self._trigger_mimi,
                name="mimi-button-trigger",
                daemon=True,
            ).start()

    def _window_procedure(
        self,
        hwnd: int,
        message: int,
        wparam: int,
        lparam: int,
    ) -> int:
        if message == TASKBAR_CREATED_MESSAGE:
            self.tray_icon_added = False
            self._add_tray_icon()
            return 0
        if message == WM_TRAY_ICON:
            mouse_message = lparam & 0xFFFF
            if mouse_message in {WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU}:
                self._show_tray_menu()
            return 0
        if message == WM_TIMER and wparam == TRAY_TIMER_ID:
            self._refresh_tray_icon()
            return 0
        if message == WM_INPUT:
            device_name, reports = read_raw_hid(lparam)
            if POP_PHONE_ID in device_name.upper():
                for report in reports:
                    self._handle_report(report)
            return 0
        if message == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def run(self) -> None:
        self.instance_handle = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASSW(
            lpfnWndProc=self.window_callback,
            hInstance=self.instance_handle,
            lpszClassName=WINDOW_CLASS_NAME,
        )
        if not user32.RegisterClassW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())

        try:
            self.window_handle = user32.CreateWindowExW(
                0,
                WINDOW_CLASS_NAME,
                WINDOW_CLASS_NAME,
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                self.instance_handle,
                None,
            )
            if not self.window_handle:
                raise ctypes.WinError(ctypes.get_last_error())

            raw_input_device = RAWINPUTDEVICE(
                0x000C,
                0x0001,
                RIDEV_INPUTSINK | RIDEV_DEVNOTIFY,
                self.window_handle,
            )
            if not user32.RegisterRawInputDevices(
                ctypes.byref(raw_input_device),
                1,
                ctypes.sizeof(RAWINPUTDEVICE),
            ):
                raise ctypes.WinError(ctypes.get_last_error())

            if not self.diagnose:
                self._add_tray_icon()
                if not user32.SetTimer(
                    self.window_handle,
                    TRAY_TIMER_ID,
                    2000,
                    None,
                ):
                    logging.warning("Windows could not start the tray status timer.")

            logging.info(
                "Listening for Native Union POP Phone %s.",
                "(diagnostic mode)" if self.diagnose else "button presses",
            )
            message = wintypes.MSG()
            while True:
                result = user32.GetMessageW(
                    ctypes.byref(message),
                    None,
                    0,
                    0,
                )
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0:
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            if self.window_handle:
                user32.KillTimer(self.window_handle, TRAY_TIMER_ID)
            self._remove_tray_icon()
            if self.window_handle:
                user32.DestroyWindow(self.window_handle)
            user32.UnregisterClassW(WINDOW_CLASS_NAME, self.instance_handle)


def acquire_single_instance() -> int | None:
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return None if kernel32.GetLastError() == ERROR_ALREADY_EXISTS else handle


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use the Native Union POP Phone button to show MIMI.",
    )
    command = parser.add_mutually_exclusive_group()
    command.add_argument(
        "--install-startup",
        action="store_true",
        help="start the listener automatically when this Windows user signs in",
    )
    command.add_argument(
        "--remove-startup",
        action="store_true",
        help="remove the automatic-startup shortcut",
    )
    command.add_argument(
        "--diagnose",
        action="store_true",
        help="print and log handset reports without launching MIMI",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.install_startup:
        shortcut = install_startup_shortcut()
        print(f"Installed: {shortcut}")
        return 0
    if args.remove_startup:
        removed = remove_startup_shortcut()
        print("Removed Startup shortcut." if removed else "No Startup shortcut found.")
        return 0

    log_path = configure_logging(verbose=args.diagnose)
    mutex_handle = acquire_single_instance()
    if mutex_handle is None:
        if args.diagnose:
            print("The POP Phone listener is already running.")
        return 0

    try:
        if args.diagnose:
            print(f"Diagnostic events are also logged to {log_path}")
        PopPhoneButtonListener(diagnose=args.diagnose).run()
    except KeyboardInterrupt:
        logging.info("POP Phone listener stopped.")
    except Exception:
        logging.exception("POP Phone listener failed.")
        if args.diagnose:
            raise
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

