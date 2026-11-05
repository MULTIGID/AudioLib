from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Mapping


MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
WM_QUIT = 0x0012
WM_RELOAD_HOTKEYS = 0x8001
PM_NOREMOVE = 0x0000
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105


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
    virtual_keys: tuple[int, ...]
    key_names: tuple[str, ...]
    canonical: str

    @property
    def virtual_key(self) -> int:
        """Compatibility alias for older callers expecting one main key."""
        return self.virtual_keys[-1]


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


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
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
    expanded_key_parts: list[str] = []
    for part in key_parts:
        # Allow the compact notation requested by users: Alt+102 is stored as
        # the ordered chord Alt+1+0+2.
        if len(part) > 1 and part.isdigit():
            expanded_key_parts.extend(part)
        else:
            expanded_key_parts.append(part)
    if not expanded_key_parts:
        raise ValueError("Комбінація має містити хоча б одну основну клавішу")
    if len(expanded_key_parts) > 8:
        raise ValueError("Комбінація може містити не більше восьми клавіш")
    key_names: list[str] = []
    virtual_keys: list[int] = []
    for part in expanded_key_parts:
        key_name, virtual_key = _canonical_key_name(part)
        if virtual_key in virtual_keys:
            raise ValueError("Одна клавіша не може повторюватися в комбінації")
        key_names.append(key_name)
        virtual_keys.append(virtual_key)
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
    return ParsedHotkey(
        modifiers,
        tuple(virtual_keys),
        tuple(key_names),
        "+".join((*ordered, *key_names)),
    )


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
    """Observe global keys without consuming them from the active application."""

    _MODIFIER_KEYS = {
        0x10, 0xA0, 0xA1,  # Shift
        0x11, 0xA2, 0xA3,  # Ctrl
        0x12, 0xA4, 0xA5,  # Alt
        0x5B, 0x5C,        # Win
    }

    def __init__(self, callback: Callable[[int], None]) -> None:
        self._callback = callback
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._hook_installed = False
        self._pending_request: ReloadRequest | None = None

    @staticmethod
    def _modifier_mask(pressed: set[int]) -> int:
        modifiers = 0
        if pressed.intersection({0x12, 0xA4, 0xA5}):
            modifiers |= MOD_ALT
        if pressed.intersection({0x11, 0xA2, 0xA3}):
            modifiers |= MOD_CONTROL
        if pressed.intersection({0x10, 0xA0, 0xA1}):
            modifiers |= MOD_SHIFT
        if pressed.intersection({0x5B, 0x5C}):
            modifiers |= MOD_WIN
        return modifiers

    def _ensure_thread(self) -> bool:
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._ready.clear()
                self._hook_installed = False
                thread = threading.Thread(
                    target=self._run,
                    daemon=True,
                    name="AudioLibGlobalHotkeys",
                )
                self._thread = thread
                thread.start()
        return self._ready.wait(0.8) and self._hook_installed

    def replace(
        self, bindings: Mapping[int, str], *, wait: bool = True
    ) -> dict[int, str]:
        parsed: dict[int, ParsedHotkey] = {}
        failures: dict[int, str] = {}
        used: dict[str, int] = {}
        for track_id, value in bindings.items():
            action_id = int(track_id)
            try:
                hotkey = parse_hotkey(value)
            except ValueError as error:
                failures[action_id] = str(error)
                continue
            identity = hotkey.canonical.casefold()
            if identity in used:
                failures[action_id] = "Ця комбінація вже призначена іншій дії"
                continue
            used[identity] = action_id
            parsed[action_id] = hotkey
        if not self._ensure_thread():
            failures.update(
                {
                    action_id: "Не вдалося запустити пасивний обробник клавіш"
                    for action_id in parsed
                }
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
                {action_id: "Не вдалося оновити гарячі клавіші" for action_id in parsed}
            )
            return failures
        if not wait:
            return failures
        if not request.done.wait(0.8):
            failures.update(
                {
                    action_id: "Windows не відповіла під час призначення клавіші"
                    for action_id in parsed
                }
            )
            return failures
        failures.update(request.failures)
        return failures

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        thread_id = int(kernel32.GetCurrentThreadId())
        message = MSG()
        bindings: dict[int, ParsedHotkey] = {}
        pressed: set[int] = set()
        sequence: list[int] = []
        sequence_modifiers = 0
        last_sequence_time = 0.0

        hook_callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        )
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            hook_callback_type,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p

        def hook_callback(n_code: int, w_param: int, l_param: int) -> int:
            nonlocal sequence_modifiers, last_sequence_time
            if n_code >= 0:
                event = int(w_param)
                data = ctypes.cast(
                    l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)
                ).contents
                virtual_key = int(data.vkCode)
                if event in {WM_KEYDOWN, WM_SYSKEYDOWN}:
                    repeated = virtual_key in pressed
                    pressed.add(virtual_key)
                    if not repeated and virtual_key not in self._MODIFIER_KEYS:
                        now = time.monotonic()
                        modifiers = self._modifier_mask(pressed)
                        if (
                            now - last_sequence_time > 1.5
                            or modifiers != sequence_modifiers
                        ):
                            sequence.clear()
                        sequence_modifiers = modifiers
                        last_sequence_time = now
                        sequence.append(virtual_key)
                        del sequence[:-8]

                        candidates = [
                            (action_id, hotkey)
                            for action_id, hotkey in bindings.items()
                            if hotkey.modifiers == modifiers
                            and len(sequence) >= len(hotkey.virtual_keys)
                            and tuple(sequence[-len(hotkey.virtual_keys):])
                            == hotkey.virtual_keys
                        ]
                        if candidates:
                            action_id, _hotkey = max(
                                candidates, key=lambda item: len(item[1].virtual_keys)
                            )
                            try:
                                self._callback(action_id)
                            except Exception:
                                pass
                            sequence.clear()
                elif event in {WM_KEYUP, WM_SYSKEYUP}:
                    pressed.discard(virtual_key)
                    if virtual_key in self._MODIFIER_KEYS:
                        sequence.clear()
                        sequence_modifiers = self._modifier_mask(pressed)

            # The hook is deliberately passive. Returning CallNextHookEx for
            # every event guarantees that games and other applications keep
            # receiving W, Space and all other bound keys normally.
            return int(user32.CallNextHookEx(None, n_code, w_param, l_param))

        hook_proc = hook_callback_type(hook_callback)
        module = kernel32.GetModuleHandleW(None)
        hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, hook_proc, module, 0)
        user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_NOREMOVE)
        with self._lock:
            self._thread_id = thread_id
            self._hook_installed = bool(hook)
        self._ready.set()
        if not hook:
            return
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message != WM_RELOAD_HOTKEYS:
                    continue
                with self._lock:
                    request = self._pending_request
                    self._pending_request = None
                if request is None:
                    continue
                bindings = dict(request.bindings)
                pressed.clear()
                sequence.clear()
                request.done.set()
        finally:
            user32.UnhookWindowsHookEx(hook)
            with self._lock:
                if self._thread_id == thread_id:
                    self._thread_id = None
                self._hook_installed = False
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
        thread.join(timeout=0.8)
        self._ready.clear()

