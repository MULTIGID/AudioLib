from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


AUTOSTART_NAME = "AudioLib"
RECOVERY_AUTOSTART_NAME = "AudioLibAudioRecovery"
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LEGACY_RUN_KEYS = (
    AUTOSTART_KEY,
    r"Software\Microsoft\Windows\CurrentVersion\RunOnce",
)
STARTUP_APPROVED_KEYS = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run",
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder",
)
LEGACY_TASK_NAMES = (
    r"\AudioLib",
    r"\AudioLib Background",
    r"\AudioLibAutostart",
    r"\AudioLib Autostart",
)
LEGACY_STARTUP_FILENAMES = {
    "audiolib.lnk",
    "audiolib.bat",
    "audiolib.cmd",
    "audiolib.exe",
    "audiolib - shortcut.lnk",
}


def _autostart_command(root_dir: Path, start_hidden: bool) -> str:
    if getattr(sys, "frozen", False):
        arguments = [str(Path(sys.executable).resolve()), "--autostart"]
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        interpreter = pythonw if pythonw.exists() else Path(sys.executable)
        arguments = [
            str(interpreter.resolve()),
            str((root_dir / "main.py").resolve()),
            "--autostart",
        ]
    if start_hidden:
        arguments.append("--background")
    return subprocess.list2cmdline(arguments)


def _recovery_command(root_dir: Path) -> str:
    if getattr(sys, "frozen", False):
        arguments = [str(Path(sys.executable).resolve()), "--recover-audio"]
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        interpreter = pythonw if pythonw.exists() else Path(sys.executable)
        arguments = [
            str(interpreter.resolve()),
            str((root_dir / "main.py").resolve()),
            "--recover-audio",
        ]
    return subprocess.list2cmdline(arguments)


def register_recovery_startup(root_dir: Path) -> None:
    """Run a silent, short recovery check at login without opening AudioLib."""
    if os.name != "nt":
        return
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        AUTOSTART_KEY,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(
            key,
            RECOVERY_AUTOSTART_NAME,
            0,
            winreg.REG_SZ,
            _recovery_command(root_dir),
        )


def _is_legacy_run_value(
    name: str, command: object, preserve_current: bool = False
) -> bool:
    lowered_name = name.casefold()
    if lowered_name == RECOVERY_AUTOSTART_NAME.casefold():
        return False
    if lowered_name == AUTOSTART_NAME.casefold():
        return not preserve_current
    if lowered_name in {
        "audiolibbackground",
        "audiolibautostart",
        "audiolib background",
    }:
        return True
    lowered_command = str(command).casefold()
    points_to_audiolib = (
        "audiolib.exe" in lowered_command
        or "\\audiolib\\main.py" in lowered_command
        or ("audiolib" in lowered_command and "launch.bat" in lowered_command)
    )
    return points_to_audiolib and "--background" in lowered_command


def _remove_legacy_registry_entries(
    preserve_current: bool = False,
) -> tuple[list[str], list[str]]:
    import winreg

    removed: list[str] = []
    errors: list[str] = []
    for key_path in LEGACY_RUN_KEYS:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                key_path,
                0,
                winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
            ) as key:
                values: list[tuple[str, object]] = []
                index = 0
                while True:
                    try:
                        name, value, _kind = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    values.append((name, value))
                    index += 1
                for name, value in values:
                    if _is_legacy_run_value(name, value, preserve_current):
                        winreg.DeleteValue(key, name)
                        removed.append(f"HKCU\\{key_path}\\{name}")
        except FileNotFoundError:
            continue
        except OSError as error:
            errors.append(f"HKCU\\{key_path}: {error}")

    legacy_names = {
        AUTOSTART_NAME.casefold(),
        "audiolibbackground",
        "audiolibautostart",
        "audiolib background",
    }
    for key_path in STARTUP_APPROVED_KEYS:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                key_path,
                0,
                winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
            ) as key:
                names: list[str] = []
                index = 0
                while True:
                    try:
                        name, _value, _kind = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    names.append(name)
                    index += 1
                for name in names:
                    if (
                        name.casefold() in legacy_names
                        and not (
                            preserve_current
                            and name.casefold() == AUTOSTART_NAME.casefold()
                        )
                    ):
                        winreg.DeleteValue(key, name)
                        removed.append(f"HKCU\\{key_path}\\{name}")
        except FileNotFoundError:
            continue
        except OSError as error:
            errors.append(f"HKCU\\{key_path}: {error}")
    return removed, errors


def _remove_legacy_startup_files() -> tuple[list[str], list[str]]:
    removed: list[str] = []
    errors: list[str] = []
    startup_folders = []
    appdata = os.environ.get("APPDATA")
    programdata = os.environ.get("ProgramData")
    if appdata:
        startup_folders.append(
            Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        )
    if programdata:
        startup_folders.append(
            Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        )
    for folder in startup_folders:
        try:
            entries = list(folder.iterdir()) if folder.exists() else []
        except OSError as error:
            errors.append(f"{folder}: {error}")
            continue
        for entry in entries:
            lowered_name = entry.name.casefold()
            looks_like_audiolib_shortcut = (
                entry.suffix.casefold() in {".lnk", ".bat", ".cmd"}
                and (
                    entry.stem.casefold() == "audiolib"
                    or entry.stem.casefold().startswith("audiolib -")
                )
            )
            if lowered_name not in LEGACY_STARTUP_FILENAMES and not looks_like_audiolib_shortcut:
                continue
            try:
                entry.unlink()
                removed.append(str(entry))
            except OSError as error:
                errors.append(f"{entry}: {error}")
    return removed, errors


def _decode_command_output(value: bytes) -> str:
    if value.startswith((b"\xff\xfe", b"\xfe\xff")):
        return value.decode("utf-16", errors="replace")
    if b"\x00" in value[:80]:
        return value.decode("utf-16-le", errors="replace")
    return value.decode("mbcs", errors="replace")


def _remove_legacy_scheduled_tasks() -> tuple[list[str], list[str]]:
    removed: list[str] = []
    errors: list[str] = []
    creation_flags = 0x08000000 if os.name == "nt" else 0
    for task_name in LEGACY_TASK_NAMES:
        query = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", task_name, "/XML"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
            check=False,
        )
        if query.returncode != 0:
            continue
        xml = _decode_command_output(query.stdout).casefold()
        action_is_audiolib = (
            "audiolib.exe" in xml
            or "\\audiolib\\main.py" in xml
            or ("audiolib" in xml and "launch.bat" in xml)
        )
        if not action_is_audiolib:
            continue
        deletion = subprocess.run(
            ["schtasks.exe", "/Delete", "/TN", task_name, "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
            check=False,
        )
        if deletion.returncode == 0:
            removed.append(f"Scheduled task {task_name}")
        else:
            error_text = _decode_command_output(deletion.stderr).strip()
            errors.append(f"Scheduled task {task_name}: {error_text}")
    return removed, errors


def cleanup_legacy_autostart(
    preserve_current: bool = False,
) -> tuple[list[str], list[str]]:
    """Remove known old AudioLib startup methods, preserving recovery startup."""
    if os.name != "nt":
        return [], []
    removed: list[str] = []
    errors: list[str] = []
    cleanup_steps = (
        lambda: _remove_legacy_registry_entries(preserve_current),
        _remove_legacy_startup_files,
        _remove_legacy_scheduled_tasks,
    )
    for cleanup in cleanup_steps:
        cleaned, cleanup_errors = cleanup()
        removed.extend(cleaned)
        errors.extend(cleanup_errors)
    return removed, errors


def register_autostart(root_dir: Path, start_hidden: bool = True) -> None:
    if os.name != "nt":
        return
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        AUTOSTART_KEY,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(
            key,
            AUTOSTART_NAME,
            0,
            winreg.REG_SZ,
            _autostart_command(root_dir, start_hidden),
        )


def unregister_autostart() -> None:
    """Remove both current and legacy AudioLib startup entries."""
    if os.name != "nt":
        return
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            AUTOSTART_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass
