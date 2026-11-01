import ctypes
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys

from audiolib.audio_endpoints import WindowsMicrophoneRouting
from audiolib.startup import (
    cleanup_legacy_autostart,
    register_autostart,
    register_recovery_startup,
    unregister_autostart,
)


ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
INSTANCE_EVENT_NAME = "Local\\MULTIGID.AudioLib.2.6.2.ShowWindow"


def _application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _write_recovery_log(root_dir: Path, message: str) -> None:
    try:
        log_path = root_dir / "data" / "audio_routing.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass


def _recover_audio_if_needed(root_dir: Path) -> bool:
    routing = WindowsMicrophoneRouting(root_dir)
    pending = routing.recovery_path.exists()
    if not pending:
        return True
    restored, hidden = routing.recover_stale_state()
    _write_recovery_log(
        root_dir,
        f"RECOVERY restored={restored} cable_hidden={hidden}",
    )
    return restored and hidden


def _startup_preferences(root_dir: Path) -> tuple[bool, bool]:
    try:
        payload = json.loads(
            (root_dir / "data" / "app_settings.json").read_text(encoding="utf-8")
        )
        enabled = payload.get("autostart_enabled", False) is True
        minimized = payload.get("start_minimized", True) is True
        return enabled, minimized
    except (FileNotFoundError, OSError, ValueError, TypeError, AttributeError):
        return False, True


def _cleanup_old_startup_entries(
    root_dir: Path, preserve_current: bool = False
) -> None:
    removed, errors = cleanup_legacy_autostart(preserve_current=preserve_current)
    if removed:
        _write_recovery_log(
            root_dir,
            "LEGACY_STARTUP_REMOVED " + " | ".join(removed),
        )
    if errors:
        _write_recovery_log(
            root_dir,
            "LEGACY_STARTUP_ERRORS " + " | ".join(errors),
        )


def _create_instance_event(show_existing: bool):
    if sys.platform != "win32":
        return None, None, False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.SetEvent.argtypes = [ctypes.c_void_p]
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateEventW(None, False, False, INSTANCE_EVENT_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    if already_running:
        if show_existing:
            kernel32.SetEvent(handle)
        kernel32.CloseHandle(handle)
        return None, kernel32, True
    return handle, kernel32, False


if __name__ == "__main__":
    application_root = _application_root()
    if "--install-recovery-startup" in sys.argv[1:]:
        _cleanup_old_startup_entries(application_root)
        register_recovery_startup(application_root)
        raise SystemExit(0)
    if "--recover-audio" in sys.argv[1:]:
        raise SystemExit(0 if _recover_audio_if_needed(application_root) else 1)

    start_hidden = "--background" in sys.argv[1:]
    autostart_enabled, autostart_minimized = _startup_preferences(application_root)
    # Old releases used several ways to start the full application with
    # Windows. Remove only verified AudioLib artifacts, then keep the tiny
    # recovery check that exits immediately after login.
    try:
        _cleanup_old_startup_entries(
            application_root, preserve_current=autostart_enabled
        )
        if autostart_enabled:
            register_autostart(application_root, autostart_minimized)
        else:
            unregister_autostart()
        register_recovery_startup(application_root)
    except OSError:
        pass

    _recover_audio_if_needed(application_root)

    event_handle, kernel32, already_running = _create_instance_event(not start_hidden)
    if already_running:
        raise SystemExit(0)

    from audiolib.app import AudioLibraryApp

    app = AudioLibraryApp(application_root, start_hidden=start_hidden)

    def poll_show_event() -> None:
        if event_handle and kernel32.WaitForSingleObject(event_handle, 0) == WAIT_OBJECT_0:
            app.show_window()
        app.root.after(25, poll_show_event)

    if event_handle:
        app.root.after(25, poll_show_event)
    try:
        app.run()
    finally:
        if event_handle:
            kernel32.CloseHandle(event_handle)
    if app.restart_requested:
        if getattr(sys, "frozen", False):
            restart_command = [sys.executable]
        else:
            restart_command = [sys.executable, str(Path(__file__).resolve())]
        subprocess.Popen(restart_command, cwd=application_root)
