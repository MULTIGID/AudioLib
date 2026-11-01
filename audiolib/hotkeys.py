from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Mapping


MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_RELOAD_HOTKEYS = 0x8001
PM_NOREMOVE = 0x0000


SPECIAL_KEYS = {
    "Backspace": 0x08,
    "Space": 0x20,
    "Page Up": 0x21,
    "Page Down": 0x22,
    "End": 0x23,
    "Home": 0x24,
    "Left": 0x25,
    "Up": 0x26,
    "Right": 0x27,
    "Down": 0x28,
    "Insert": 0x2D,
    "Delete": 0x2E,
}


@dataclass(frozen=True)
class ParsedHotkey:
    modifiers: int
    virtual_key: int
    canonical: str


@dataclass
class ReloadRequest:
    bindings: dict[int, ParsedHotkey]
    failures: dict[int, str]
    done: threading.Event


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


def _canonical_key_name(value: str) -> tuple[str, int]:
    key = value.strip()
    if len(key) == 1 and key.isalpha():
        key = key.upper()
        return key, ord(key)
    if len(key) == 1 and key.isdigit():
        return key, ord(key)
    upper = key.upper()
    if upper.startswith("F") and upper[1:].isdigit():
        number = int(upper[1:])
        if 1 <= number <= 24:
            return f"F{number}", 0x70 + number - 1
    aliases = {
        "BACKSPACE": "Backspace",
        "SPACE": "Space",
        "PAGEUP": "Page Up",
        "PAGE UP": "Page Up",
        "PAGEDOWN": "Page Down",
        "PAGE DOWN": "Page Down",
        "END": "End",
        "HOME": "Home",
        "LEFT": "Left",
        "UP": "Up",
        "RIGHT": "Right",
        "DOWN": "Down",
        "INSERT": "Insert",
        "DELETE": "Delete",
    }
    canonical = aliases.get(upper)
    if canonical is None:
        raise ValueError("Непідтримувана клавіша")
    return canonical, SPECIAL_KEYS[canonical]


def parse_hotkey(value: str) -> ParsedHotkey:
    parts = [part.strip() for part in value.split("+") if part.strip()]
    if not parts:
        raise ValueError("Гаряча клавіша не задана")
    modifier_names: list[str] = []
    modifiers = 0
    key_parts: list[str] = []
    modifier_map = {
        "CTRL": ("Ctrl", MOD_CONTROL),
        "CONTROL": ("Ctrl", MOD_CONTROL),
        "ALT": ("Alt", MOD_ALT),
        "SHIFT": ("Shift", MOD_SHIFT),
        "WIN": ("Win", MOD_WIN),
        "WINDOWS": ("Win", MOD_WIN),
    }
    for part in parts:
        item = modifier_map.get(part.upper())
        if item is None:
            key_parts.append(part)
            continue
        name, flag = item
        if not modifiers & flag:
            modifiers |= flag
            modifier_names.append(name)
    if len(key_parts) != 1:
        raise ValueError("Комбінація має містити одну основну клавішу")
    key_name, virtual_key = _canonical_key_name(key_parts[0])
    ordered = [
        name
        for name, flag in (
            ("Ctrl", MOD_CONTROL),
            ("Alt", MOD_ALT),
            ("Shift", MOD_SHIFT),
            ("Win", MOD_WIN),
        )
        if modifiers & flag
    ]
    return ParsedHotkey(modifiers, virtual_key, "+".join((*ordered, key_name)))


def hotkey_from_tk_event(
    keysym: str,
    state: int,
    keycode: int | None = None,
) -> str:
    del state
    key_aliases = {
        "BackSpace": "Backspace",
        "space": "Space",
        "Prior": "Page Up",
        "Next": "Page Down",
        "End": "End",
        "Home": "Home",
        "Left": "Left",
        "Up": "Up",
        "Right": "Right",
        "Down": "Down",
        "Insert": "Insert",
        "Delete": "Delete",
    }
    if keysym in {
        "Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
        "Meta_L", "Meta_R", "Super_L", "Super_R",
    }:
        raise ValueError("Натисніть також основну клавішу")
    if keycode is not None and 0x41 <= keycode <= 0x5A:
        # Tk reports a localized keysym (for example, Cyrillic_tse for the
        # physical W key). Windows global hotkeys use layout-independent VKs.
        key = chr(keycode)
    elif keycode is not None and 0x30 <= keycode <= 0x39:
        key = chr(keycode)
    else:
        key = key_aliases.get(keysym, keysym)
    modifiers: list[str] = []
    user32 = ctypes.windll.user32
    # GetKeyState may retain the previous Alt state while a modal Tk window is
    # being activated.  GetAsyncKeyState reflects what is physically held at
    # the moment the main key is pressed and prevents a plain F1 from becoming
    # Alt+F1 unexpectedly.
    ctrl_down = bool(user32.GetAsyncKeyState(0x11) & 0x8000)
    alt_down = bool(user32.GetAsyncKeyState(0x12) & 0x8000)
    shift_down = bool(user32.GetAsyncKeyState(0x10) & 0x8000)
    win_down = bool(user32.GetAsyncKeyState(0x5B) & 0x8000) or bool(
        user32.GetAsyncKeyState(0x5C) & 0x8000
    )
    if ctrl_down:
        modifiers.append("Ctrl")
    if alt_down:
        modifiers.append("Alt")
    if shift_down:
        modifiers.append("Shift")
    if win_down:
        modifiers.append("Win")
    return parse_hotkey("+".join((*modifiers, key))).canonical


def display_hotkey(value: str | None) -> str:
    return value.replace("+", " + ") if value else "—"


class GlobalHotkeyManager:
    """Keeps one Windows message thread and updates its global hotkeys in place."""

    def __init__(self, callback: Callable[[int], None]) -> None:
        self._callback = callback
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._pending_request: ReloadRequest | None = None

    def _ensure_thread(self) -> bool:
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._ready.clear()
                thread = threading.Thread(
                    target=self._run,
                    daemon=True,
                    name="AudioLibGlobalHotkeys",
                )
                self._thread = thread
                thread.start()
        return self._ready.wait(0.5)

    def replace(
        self, bindings: Mapping[int, str], *, wait: bool = True
    ) -> dict[int, str]:
        parsed: dict[int, ParsedHotkey] = {}
        failures: dict[int, str] = {}
        for track_id, value in bindings.items():
            try:
                parsed[int(track_id)] = parse_hotkey(value)
            except ValueError as error:
                failures[int(track_id)] = str(error)
        if not self._ensure_thread():
            failures.update(
                {track_id: "Не вдалося запустити обробник гарячих клавіш" for track_id in parsed}
            )
            return failures
        request = ReloadRequest(parsed, {}, threading.Event())
        with self._lock:
            self._pending_request = request
            thread_id = self._thread_id
        if thread_id is None or not ctypes.windll.user32.PostThreadMessageW(
            thread_id, WM_RELOAD_HOTKEYS, 0, 0
        ):
            failures.update(
                {track_id: "Не вдалося оновити гарячі клавіші" for track_id in parsed}
            )
            return failures
        if not wait:
            return failures
        if not request.done.wait(0.5):
            failures.update(
                {track_id: "Windows не відповіла під час призначення клавіші" for track_id in parsed}
            )
            return failures
        failures.update(request.failures)
        return failures

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        thread_id = int(kernel32.GetCurrentThreadId())
        message = MSG()
        user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_NOREMOVE)
        with self._lock:
            self._thread_id = thread_id
        self._ready.set()
        registered: dict[int, int] = {}
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY:
                    track_id = registered.get(int(message.wParam))
                    if track_id is not None:
                        self._callback(track_id)
                elif message.message == WM_RELOAD_HOTKEYS:
                    with self._lock:
                        request = self._pending_request
                        self._pending_request = None
                    if request is None:
                        continue
                    for hotkey_id in registered:
                        user32.UnregisterHotKey(None, hotkey_id)
                    registered.clear()
                    for hotkey_id, (track_id, hotkey) in enumerate(
                        request.bindings.items(), start=1
                    ):
                        ok = user32.RegisterHotKey(
                            None,
                            hotkey_id,
                            hotkey.modifiers | MOD_NOREPEAT,
                            hotkey.virtual_key,
                        )
                        if ok:
                            registered[hotkey_id] = track_id
                        else:
                            request.failures[track_id] = (
                                "Ця комбінація вже використовується Windows або іншою програмою"
                            )
                    request.done.set()
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
            with self._lock:
                if self._thread_id == thread_id:
                    self._thread_id = None
                request = self._pending_request
                self._pending_request = None
            if request is not None:
                request.done.set()

    def close(self) -> None:
        with self._lock:
            thread = self._thread
            thread_id = self._thread_id
            self._thread = None
        if thread is None:
            return
        if thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        thread.join(timeout=0.5)
        self._ready.clear()

