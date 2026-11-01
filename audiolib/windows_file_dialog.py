from __future__ import annotations

import ctypes
import os
import threading
import uuid
from ctypes import wintypes
from typing import Iterable


class FileDialogError(RuntimeError):
    pass


if os.name == "nt":
    HRESULT = ctypes.c_long

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        @classmethod
        def from_text(cls, value: str) -> "GUID":
            raw = uuid.UUID(value).bytes_le
            return cls.from_buffer_copy(raw)

    class COMDLG_FILTERSPEC(ctypes.Structure):
        _fields_ = [("pszName", wintypes.LPCWSTR), ("pszSpec", wintypes.LPCWSTR)]

    CLSID_FILE_OPEN_DIALOG = GUID.from_text("dc1c5a9c-e88a-4dde-a5a1-60f82a20aeF7")
    IID_FILE_OPEN_DIALOG = GUID.from_text("d57c7288-d4ad-4768-be02-9d969532d960")
    CLSID_FILE_SAVE_DIALOG = GUID.from_text("c0b4e2f3-ba21-4773-8dba-335ec946eb8b")
    IID_FILE_SAVE_DIALOG = GUID.from_text("84bccd23-5fde-4cdb-aea4-af64b83d78ab")
    DIALOG_CLIENT_GUID = GUID.from_text("68884f52-9db4-46ca-b6d4-9d8483c75976")

    CLSCTX_INPROC_SERVER = 0x1
    COINIT_APARTMENTTHREADED = 0x2
    FOS_OVERWRITEPROMPT = 0x2
    FOS_NOCHANGEDIR = 0x8
    FOS_PICKFOLDERS = 0x20
    FOS_FORCEFILESYSTEM = 0x40
    FOS_ALLOWMULTISELECT = 0x200
    FOS_PATHMUSTEXIST = 0x800
    FOS_FILEMUSTEXIST = 0x1000
    SIGDN_FILESYSPATH = 0x80058000
    ERROR_CANCELLED_HRESULT = 0x800704C7

    _ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    _ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _ole32.CoInitializeEx.restype = HRESULT
    _ole32.CoUninitialize.argtypes = []
    _ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _ole32.CoCreateInstance.restype = HRESULT
    _ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    GA_ROOT = 2


def _hresult_code(value: int) -> int:
    return ctypes.c_ulong(value).value


def _raise_for_hresult(value: int, action: str) -> None:
    if value < 0:
        raise FileDialogError(f"{action}: HRESULT 0x{_hresult_code(value):08X}")


def _com_method(instance: ctypes.c_void_p, index: int, restype, *argtypes):
    table = ctypes.cast(
        instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
    ).contents
    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return prototype(table[index])


def _release(instance: ctypes.c_void_p | None) -> None:
    if instance:
        _com_method(instance, 2, wintypes.ULONG)(instance)


def _modern_windows_dialog(
    owner,
    title: str,
    filetypes: Iterable[tuple[str, str]],
    multiple: bool,
) -> tuple[str, ...]:
    initialized = False
    dialog = ctypes.c_void_p()
    results = ctypes.c_void_p()
    try:
        result = _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        if result >= 0:
            initialized = True
        elif _hresult_code(result) != 0x80010106:  # RPC_E_CHANGED_MODE
            _raise_for_hresult(result, "Не вдалося ініціалізувати діалог Windows")

        result = _ole32.CoCreateInstance(
            ctypes.byref(CLSID_FILE_OPEN_DIALOG),
            None,
            CLSCTX_INPROC_SERVER,
            ctypes.byref(IID_FILE_OPEN_DIALOG),
            ctypes.byref(dialog),
        )
        _raise_for_hresult(result, "Не вдалося створити сучасний діалог вибору файлів")

        options = wintypes.DWORD()
        result = _com_method(
            dialog, 10, HRESULT, ctypes.POINTER(wintypes.DWORD)
        )(dialog, ctypes.byref(options))
        _raise_for_hresult(result, "Не вдалося прочитати параметри діалогу")
        options.value |= FOS_FORCEFILESYSTEM | FOS_FILEMUSTEXIST | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR
        if multiple:
            options.value |= FOS_ALLOWMULTISELECT
        result = _com_method(dialog, 9, HRESULT, wintypes.DWORD)(dialog, options)
        _raise_for_hresult(result, "Не вдалося налаштувати діалог")

        normalized = [
            (name, pattern.replace(" ", ";")) for name, pattern in filetypes
        ]
        filter_array = (COMDLG_FILTERSPEC * len(normalized))(
            *(COMDLG_FILTERSPEC(name, pattern) for name, pattern in normalized)
        )
        result = _com_method(
            dialog, 4, HRESULT, wintypes.UINT, ctypes.POINTER(COMDLG_FILTERSPEC)
        )(dialog, len(normalized), filter_array)
        _raise_for_hresult(result, "Не вдалося встановити типи файлів")

        result = _com_method(dialog, 17, HRESULT, wintypes.LPCWSTR)(dialog, title)
        _raise_for_hresult(result, "Не вдалося встановити назву діалогу")
        result = _com_method(dialog, 24, HRESULT, ctypes.POINTER(GUID))(
            dialog, ctypes.byref(DIALOG_CLIENT_GUID)
        )
        _raise_for_hresult(result, "Не вдалося зберегти папку діалогу")

        if owner:
            _user32.SetForegroundWindow(owner)
        result = _com_method(dialog, 3, HRESULT, wintypes.HWND)(dialog, owner)
        if _hresult_code(result) == ERROR_CANCELLED_HRESULT:
            return ()
        _raise_for_hresult(result, "Діалог вибору файлів завершився з помилкою")

        result = _com_method(
            dialog, 27, HRESULT, ctypes.POINTER(ctypes.c_void_p)
        )(dialog, ctypes.byref(results))
        _raise_for_hresult(result, "Не вдалося отримати вибрані файли")

        count = wintypes.DWORD()
        result = _com_method(
            results, 7, HRESULT, ctypes.POINTER(wintypes.DWORD)
        )(results, ctypes.byref(count))
        _raise_for_hresult(result, "Не вдалося прочитати список файлів")

        selected: list[str] = []
        for index in range(count.value):
            item = ctypes.c_void_p()
            try:
                result = _com_method(
                    results, 8, HRESULT, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)
                )(results, index, ctypes.byref(item))
                _raise_for_hresult(result, "Не вдалося прочитати вибраний файл")
                path_pointer = ctypes.c_void_p()
                result = _com_method(
                    item, 5, HRESULT, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)
                )(item, SIGDN_FILESYSPATH, ctypes.byref(path_pointer))
                _raise_for_hresult(result, "Не вдалося отримати шлях до файлу")
                try:
                    selected.append(ctypes.wstring_at(path_pointer))
                finally:
                    _ole32.CoTaskMemFree(path_pointer)
            finally:
                _release(item)
        return tuple(selected)
    finally:
        _release(results)
        _release(dialog)
        if initialized:
            _ole32.CoUninitialize()


def choose_files(
    parent,
    *,
    title: str,
    filetypes: Iterable[tuple[str, str]],
    multiple: bool = False,
) -> tuple[str, ...]:
    if os.name == "nt":
        owner = None
        if parent is not None:
            # Resolve Tk's child handle on the Tk thread before starting COM.
            tk_handle = wintypes.HWND(parent.winfo_id())
            owner = _user32.GetAncestor(tk_handle, GA_ROOT) or tk_handle

        # Audio backends can initialize COM on Tk's main thread in a mode that
        # is incompatible with IFileOpenDialog.  Run the picker in its own STA
        # thread and keep Tk's event loop alive while the modal window is open.
        result_holder: list[tuple[str, ...]] = []
        error_holder: list[BaseException] = []
        done = threading.Event()

        def worker() -> None:
            try:
                result_holder.append(
                    _modern_windows_dialog(owner, title, tuple(filetypes), multiple)
                )
            except BaseException as error:
                error_holder.append(error)
            finally:
                done.set()

        thread = threading.Thread(
            target=worker,
            name="AudioLibFileDialog",
            daemon=True,
        )
        thread.start()

        if parent is not None:
            variable_name = f"::audiolib_file_dialog_done_{id(done)}"
            parent.setvar(variable_name, "0")

            def poll_dialog() -> None:
                if done.is_set():
                    parent.setvar(variable_name, "1")
                else:
                    parent.after(25, poll_dialog)

            parent.after(25, poll_dialog)
            parent.wait_variable(variable_name)
            try:
                parent.globalunsetvar(variable_name)
            except Exception:
                pass
        else:
            done.wait()

        thread.join()
        if error_holder:
            error = error_holder[0]
            if isinstance(error, FileDialogError):
                raise error
            raise FileDialogError(f"Помилка сучасного діалогу Windows: {error}") from error
        return result_holder[0] if result_holder else ()

    # Non-Windows development fallback; Windows never opens Tk's legacy picker.
    from tkinter import filedialog

    if multiple:
        return tuple(
            filedialog.askopenfilenames(parent=parent, title=title, filetypes=list(filetypes))
        )
    selected = filedialog.askopenfilename(parent=parent, title=title, filetypes=list(filetypes))
    return (selected,) if selected else ()


def _modern_windows_save_dialog(
    owner,
    title: str,
    filetypes: Iterable[tuple[str, str]],
    default_name: str,
    default_extension: str,
) -> str:
    initialized = False
    dialog = ctypes.c_void_p()
    item = ctypes.c_void_p()
    try:
        result = _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        if result >= 0:
            initialized = True
        elif _hresult_code(result) != 0x80010106:
            _raise_for_hresult(result, "Не вдалося ініціалізувати діалог Windows")
        result = _ole32.CoCreateInstance(
            ctypes.byref(CLSID_FILE_SAVE_DIALOG),
            None,
            CLSCTX_INPROC_SERVER,
            ctypes.byref(IID_FILE_SAVE_DIALOG),
            ctypes.byref(dialog),
        )
        _raise_for_hresult(result, "Не вдалося створити сучасний діалог збереження")

        options = wintypes.DWORD()
        result = _com_method(dialog, 10, HRESULT, ctypes.POINTER(wintypes.DWORD))(
            dialog, ctypes.byref(options)
        )
        _raise_for_hresult(result, "Не вдалося прочитати параметри діалогу")
        options.value |= FOS_OVERWRITEPROMPT | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR
        result = _com_method(dialog, 9, HRESULT, wintypes.DWORD)(dialog, options)
        _raise_for_hresult(result, "Не вдалося налаштувати діалог")

        normalized = [(name, pattern.replace(" ", ";")) for name, pattern in filetypes]
        filter_array = (COMDLG_FILTERSPEC * len(normalized))(
            *(COMDLG_FILTERSPEC(name, pattern) for name, pattern in normalized)
        )
        result = _com_method(
            dialog, 4, HRESULT, wintypes.UINT, ctypes.POINTER(COMDLG_FILTERSPEC)
        )(dialog, len(normalized), filter_array)
        _raise_for_hresult(result, "Не вдалося встановити типи файлів")
        _raise_for_hresult(
            _com_method(dialog, 15, HRESULT, wintypes.LPCWSTR)(dialog, default_name),
            "Не вдалося встановити назву файлу",
        )
        _raise_for_hresult(
            _com_method(dialog, 22, HRESULT, wintypes.LPCWSTR)(dialog, default_extension),
            "Не вдалося встановити розширення файлу",
        )
        _raise_for_hresult(
            _com_method(dialog, 17, HRESULT, wintypes.LPCWSTR)(dialog, title),
            "Не вдалося встановити назву діалогу",
        )
        _com_method(dialog, 24, HRESULT, ctypes.POINTER(GUID))(
            dialog, ctypes.byref(DIALOG_CLIENT_GUID)
        )
        if owner:
            _user32.SetForegroundWindow(owner)
        result = _com_method(dialog, 3, HRESULT, wintypes.HWND)(dialog, owner)
        if _hresult_code(result) == ERROR_CANCELLED_HRESULT:
            return ""
        _raise_for_hresult(result, "Діалог збереження завершився з помилкою")

        result = _com_method(dialog, 20, HRESULT, ctypes.POINTER(ctypes.c_void_p))(
            dialog, ctypes.byref(item)
        )
        _raise_for_hresult(result, "Не вдалося отримати вибраний шлях")
        path_pointer = ctypes.c_void_p()
        result = _com_method(
            item, 5, HRESULT, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)
        )(item, SIGDN_FILESYSPATH, ctypes.byref(path_pointer))
        _raise_for_hresult(result, "Не вдалося отримати шлях до файлу")
        try:
            return ctypes.wstring_at(path_pointer)
        finally:
            _ole32.CoTaskMemFree(path_pointer)
    finally:
        _release(item)
        _release(dialog)
        if initialized:
            _ole32.CoUninitialize()


def choose_save_file(
    parent,
    *,
    title: str,
    filetypes: Iterable[tuple[str, str]],
    default_name: str,
    default_extension: str,
) -> str:
    if os.name == "nt":
        owner = None
        if parent is not None:
            tk_handle = wintypes.HWND(parent.winfo_id())
            owner = _user32.GetAncestor(tk_handle, GA_ROOT) or tk_handle
        result_holder: list[str] = []
        error_holder: list[BaseException] = []
        done = threading.Event()

        def worker() -> None:
            try:
                result_holder.append(
                    _modern_windows_save_dialog(
                        owner,
                        title,
                        tuple(filetypes),
                        default_name,
                        default_extension,
                    )
                )
            except BaseException as error:
                error_holder.append(error)
            finally:
                done.set()

        threading.Thread(target=worker, name="AudioLibSaveDialog", daemon=True).start()
        variable_name = f"::audiolib_save_dialog_done_{id(done)}"
        parent.setvar(variable_name, "0")

        def poll_dialog() -> None:
            parent.setvar(variable_name, "1" if done.is_set() else "0")
            if not done.is_set():
                parent.after(25, poll_dialog)

        parent.after(25, poll_dialog)
        parent.wait_variable(variable_name)
        try:
            parent.globalunsetvar(variable_name)
        except Exception:
            pass
        if error_holder:
            error = error_holder[0]
            if isinstance(error, FileDialogError):
                raise error
            raise FileDialogError(f"Помилка сучасного діалогу Windows: {error}") from error
        return result_holder[0] if result_holder else ""

    from tkinter import filedialog

    return str(
        filedialog.asksaveasfilename(
            parent=parent,
            title=title,
            filetypes=list(filetypes),
            initialfile=default_name,
            defaultextension=f".{default_extension.lstrip('.')}",
        )
    )
