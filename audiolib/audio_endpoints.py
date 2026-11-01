from __future__ import annotations

import ctypes
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


class AudioEndpointError(RuntimeError):
    pass


if os.name == "nt":
    import winreg


CAPTURE_ENDPOINTS_KEY = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
)
FRIENDLY_NAME_VALUE = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
DEVICE_DESCRIPTION_VALUE = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"
DEVICE_STATE_ACTIVE = 0x00000001
DEVICE_STATE_NOT_VISIBLE = 0x10000000

# IPolicyConfig is the Windows component also used by the Sound control panel.
# Microsoft does not publish it as a supported application API, so all calls are
# isolated in this module and treated as best-effort operations.
CLSID_POLICY_CONFIG_CLIENT = "{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}"
IID_POLICY_CONFIG = "{F8679F50-850A-41CF-9C72-430F290290C8}"
CLSID_MMDEVICE_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_MMDEVICE_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
CLSCTX_ALL = 23
COINIT_APARTMENTTHREADED = 0x2
RPC_E_CHANGED_MODE = -2147417850
ENDPOINT_ROLES = (0, 1, 2)  # console, multimedia, communications


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, value: str) -> "_GUID":
        import uuid

        raw = uuid.UUID(value).bytes_le
        result = cls()
        ctypes.memmove(ctypes.byref(result), raw, 16)
        return result


@dataclass(frozen=True)
class CaptureEndpoint:
    endpoint_id: str
    friendly_name: str
    description: str
    state: int

    @property
    def active(self) -> bool:
        return (self.state & 0xF) == DEVICE_STATE_ACTIVE

    @property
    def visible(self) -> bool:
        return not bool(self.state & DEVICE_STATE_NOT_VISIBLE)


def _read_capture_endpoints() -> list[CaptureEndpoint]:
    if os.name != "nt":
        return []
    endpoints: list[CaptureEndpoint] = []
    access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, CAPTURE_ENDPOINTS_KEY, 0, access) as root:
            count = winreg.QueryInfoKey(root)[0]
            for index in range(count):
                endpoint_guid = winreg.EnumKey(root, index)
                try:
                    with winreg.OpenKey(root, endpoint_guid) as endpoint_key:
                        state = int(winreg.QueryValueEx(endpoint_key, "DeviceState")[0])
                    with winreg.OpenKey(root, endpoint_guid + r"\Properties") as properties:
                        friendly = str(winreg.QueryValueEx(properties, FRIENDLY_NAME_VALUE)[0])
                        try:
                            description = str(
                                winreg.QueryValueEx(properties, DEVICE_DESCRIPTION_VALUE)[0]
                            )
                        except FileNotFoundError:
                            description = ""
                except (FileNotFoundError, OSError):
                    continue
                endpoints.append(
                    CaptureEndpoint(
                        endpoint_id=f"{{0.0.1.00000000}}.{endpoint_guid}",
                        friendly_name=friendly,
                        description=description,
                        state=state,
                    )
                )
    except (FileNotFoundError, PermissionError, OSError) as error:
        raise AudioEndpointError(f"Не вдалося прочитати список мікрофонів Windows: {error}") from error
    return endpoints


def _is_cable_output(endpoint: CaptureEndpoint) -> bool:
    text = f"{endpoint.friendly_name} {endpoint.description}".casefold()
    return "cable output" in text and "vb-audio" in text


def _find_endpoint(predicate, *, active_only: bool = False) -> CaptureEndpoint | None:
    return next(
        (
            endpoint
            for endpoint in _read_capture_endpoints()
            if predicate(endpoint)
            and ((endpoint.active and endpoint.visible) or not active_only)
        ),
        None,
    )


def _find_cable_output() -> CaptureEndpoint | None:
    cables = [endpoint for endpoint in _read_capture_endpoints() if _is_cable_output(endpoint)]
    if not cables:
        return None
    # Windows can retain disconnected copies of the same driver.  Prefer the
    # endpoint that is actually active, even when AudioLib previously hid it.
    return min(
        cables,
        key=lambda endpoint: (
            not endpoint.active,
            not endpoint.visible,
        ),
    )


def list_physical_microphones() -> list[CaptureEndpoint]:
    return sorted(
        (
            endpoint
            for endpoint in _read_capture_endpoints()
            if endpoint.active and endpoint.visible and not _is_cable_output(endpoint)
        ),
        key=lambda endpoint: (endpoint.friendly_name.casefold(), endpoint.description.casefold()),
    )


def microphone_label(endpoint: CaptureEndpoint) -> str:
    if endpoint.description and endpoint.description.casefold() not in endpoint.friendly_name.casefold():
        return f"{endpoint.friendly_name} ({endpoint.description})"
    return endpoint.friendly_name


def _find_endpoint_by_id(endpoint_id: str, *, active_only: bool = False) -> CaptureEndpoint | None:
    return _find_endpoint(
        lambda endpoint: endpoint.endpoint_id.casefold() == endpoint_id.casefold(),
        active_only=active_only,
    )


class _PolicyConfig:
    """Small ctypes wrapper around the Windows endpoint policy object."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise AudioEndpointError("Керування аудіопристроями підтримується лише у Windows")
        # WinDLL returns HRESULT values verbatim.  OleDLL would turn the
        # harmless RPC_E_CHANGED_MODE result into an exception when miniaudio
        # has already initialized this thread with another COM apartment.
        self._ole32 = ctypes.WinDLL("ole32")
        self._ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self._ole32.CoInitializeEx.restype = ctypes.c_long
        self._ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(_GUID),
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._ole32.CoCreateInstance.restype = ctypes.c_long
        init_result = self._ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        self._uninitialize = init_result >= 0
        if init_result < 0 and init_result != RPC_E_CHANGED_MODE:
            raise AudioEndpointError(
                f"Не вдалося ініціалізувати керування звуком Windows (0x{init_result & 0xFFFFFFFF:08X})"
            )
        class_id = _GUID.from_string(CLSID_POLICY_CONFIG_CLIENT)
        interface_id = _GUID.from_string(IID_POLICY_CONFIG)
        self._object = ctypes.c_void_p()
        result = self._ole32.CoCreateInstance(
            ctypes.byref(class_id),
            None,
            CLSCTX_ALL,
            ctypes.byref(interface_id),
            ctypes.byref(self._object),
        )
        if result < 0:
            self.close()
            raise AudioEndpointError(
                f"Windows не надала доступ до керування аудіопристроями (0x{result & 0xFFFFFFFF:08X})"
            )
        vtable_pointer = ctypes.cast(
            self._object, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        )
        self._vtable = vtable_pointer.contents

    def _call(self, slot: int, *args) -> None:
        prototype = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int
        )
        method = prototype(self._vtable[slot])
        result = method(self._object, *args)
        if result < 0:
            raise AudioEndpointError(
                f"Windows відхилила зміну аудіопристрою (0x{result & 0xFFFFFFFF:08X})"
            )

    def set_default(self, endpoint_id: str) -> None:
        for role in ENDPOINT_ROLES:
            self.set_default_for_role(endpoint_id, role)

    def set_default_for_role(self, endpoint_id: str, role: int) -> None:
        self._call(13, endpoint_id, role)

    def set_visible(self, endpoint_id: str, visible: bool) -> None:
        self._call(14, endpoint_id, int(visible))

    def close(self) -> None:
        if getattr(self, "_object", None) and self._object.value:
            vtable = ctypes.cast(
                self._object, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
            ).contents
            release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
            release(self._object)
            self._object = ctypes.c_void_p()
        if getattr(self, "_uninitialize", False):
            self._ole32.CoUninitialize()
            self._uninitialize = False

    def __enter__(self) -> "_PolicyConfig":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()


def _get_default_capture_endpoint_id(role: int) -> str | None:
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    init_result = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    uninitialize = init_result >= 0
    if init_result < 0 and init_result != RPC_E_CHANGED_MODE:
        return None
    enumerator = ctypes.c_void_p()
    device = ctypes.c_void_p()
    endpoint_id = ctypes.c_wchar_p()
    try:
        class_id = _GUID.from_string(CLSID_MMDEVICE_ENUMERATOR)
        interface_id = _GUID.from_string(IID_MMDEVICE_ENUMERATOR)
        result = ole32.CoCreateInstance(
            ctypes.byref(class_id), None, CLSCTX_ALL, ctypes.byref(interface_id),
            ctypes.byref(enumerator),
        )
        if result < 0:
            return None
        enumerator_vtable = ctypes.cast(
            enumerator, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        get_default = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        )(enumerator_vtable[4])
        # eCapture == 1
        result = get_default(enumerator, 1, role, ctypes.byref(device))
        if result < 0 or not device.value:
            return None
        device_vtable = ctypes.cast(
            device, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        get_id = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)
        )(device_vtable[5])
        result = get_id(device, ctypes.byref(endpoint_id))
        if result < 0 or not endpoint_id.value:
            return None
        return endpoint_id.value
    finally:
        if endpoint_id.value:
            ole32.CoTaskMemFree(ctypes.cast(endpoint_id, ctypes.c_void_p))
        for item in (device, enumerator):
            if item.value:
                vtable = ctypes.cast(
                    item, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
                ).contents
                ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(item)
        if uninitialize:
            ole32.CoUninitialize()


def current_default_microphone() -> CaptureEndpoint | None:
    endpoint_id = _get_default_capture_endpoint_id(0)
    if not endpoint_id:
        return None
    endpoint = _find_endpoint_by_id(endpoint_id, active_only=True)
    if endpoint is None or _is_cable_output(endpoint):
        return None
    return endpoint


class WindowsMicrophoneRouting:
    """Switch the Windows default microphone for the AudioLib lifetime."""

    def __init__(self, root_dir: Path) -> None:
        self.settings_path = root_dir / "data" / "audio_device_settings.json"
        self.recovery_path = root_dir / "data" / "audio_routing_state.json"
        self.cable_endpoint_id: str | None = None
        self.physical_endpoint_id: str | None = None
        self.previous_default_ids: dict[int, str] = {}
        self.activated = False

    def _save_recovery_state(self) -> None:
        """Persist enough information to undo routing after a crash or reboot."""
        self.recovery_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.recovery_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "active": True,
                    "physical_endpoint_id": self.physical_endpoint_id,
                    "cable_endpoint_id": self.cable_endpoint_id,
                    "previous_default_ids": {
                        str(role): endpoint_id
                        for role, endpoint_id in self.previous_default_ids.items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.recovery_path)

    def clear_recovery_state(self) -> None:
        try:
            self.recovery_path.unlink()
        except FileNotFoundError:
            pass

    def load_recovery_state(self) -> bool:
        try:
            payload = json.loads(self.recovery_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return False
        if not payload.get("active"):
            return False
        physical_endpoint_id = payload.get("physical_endpoint_id")
        if physical_endpoint_id:
            self.physical_endpoint_id = str(physical_endpoint_id)
        cable_endpoint_id = payload.get("cable_endpoint_id")
        if cable_endpoint_id:
            self.cable_endpoint_id = str(cable_endpoint_id)
        previous = payload.get("previous_default_ids", {})
        if isinstance(previous, dict):
            self.previous_default_ids = {
                int(role): str(endpoint_id)
                for role, endpoint_id in previous.items()
                if str(role).isdigit() and endpoint_id
            }
        self.activated = True
        return True

    def recover_stale_state(self) -> tuple[bool, bool]:
        """Restore routing left active by a crash or Windows shutdown."""
        if not self.load_recovery_state():
            return False, False
        restored = self.restore_default_microphone()
        hidden = self.disable_cable_output() if restored else False
        if restored and hidden:
            self.clear_recovery_state()
        return restored, hidden

    def selected_microphone_id(self) -> str | None:
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            value = payload.get("physical_microphone_id")
            return str(value) if value else None
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return None

    def selected_microphone(self) -> CaptureEndpoint | None:
        endpoint_id = self.selected_microphone_id()
        if not endpoint_id:
            return None
        endpoint = _find_endpoint_by_id(endpoint_id, active_only=True)
        if endpoint is None or _is_cable_output(endpoint):
            return None
        return endpoint

    def save_selected_microphone(self, endpoint_id: str) -> None:
        endpoint = _find_endpoint_by_id(endpoint_id, active_only=True)
        if endpoint is None or _is_cable_output(endpoint):
            raise AudioEndpointError("Вибраний фізичний мікрофон зараз недоступний")
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "physical_microphone_id": endpoint.endpoint_id,
                    "physical_microphone_name": microphone_label(endpoint),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.settings_path)

    def activate_for_app(self, physical_endpoint_id: str) -> CaptureEndpoint:
        cable = _find_cable_output()
        physical = _find_endpoint_by_id(physical_endpoint_id, active_only=True)
        if cable is None:
            raise AudioEndpointError("Не знайдено CABLE Output (VB-Audio Virtual Cable)")
        if physical is None or _is_cable_output(physical):
            raise AudioEndpointError("Збережений фізичний мікрофон зараз недоступний")
        self.cable_endpoint_id = cable.endpoint_id
        self.physical_endpoint_id = physical.endpoint_id
        self.previous_default_ids = {}
        for role in ENDPOINT_ROLES:
            previous = _get_default_capture_endpoint_id(role)
            previous_endpoint = _find_endpoint_by_id(previous, active_only=True) if previous else None
            if previous_endpoint is None or _is_cable_output(previous_endpoint):
                previous = physical.endpoint_id
            self.previous_default_ids[role] = previous
        try:
            try:
                self._save_recovery_state()
            except OSError as error:
                raise AudioEndpointError(
                    f"Не вдалося зберегти стан відновлення аудіо: {error}"
                ) from error
            with _PolicyConfig() as policy:
                policy.set_visible(cable.endpoint_id, True)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                current = _find_endpoint_by_id(cable.endpoint_id)
                if current is not None and current.active and current.visible:
                    break
                time.sleep(0.05)
            else:
                raise AudioEndpointError("CABLE Output не став активним після ввімкнення")
            with _PolicyConfig() as policy:
                policy.set_default(cable.endpoint_id)
            self.activated = True
            return physical
        except Exception:
            self.restore_after_failed_start()
            raise

    def restore_default_microphone(self) -> bool:
        fallback = (
            _find_endpoint_by_id(self.physical_endpoint_id, active_only=True)
            if self.physical_endpoint_id
            else self.selected_microphone()
        )
        if fallback is None:
            return False
        try:
            with _PolicyConfig() as policy:
                for role in ENDPOINT_ROLES:
                    endpoint_id = self.previous_default_ids.get(role, fallback.endpoint_id)
                    endpoint = _find_endpoint_by_id(endpoint_id, active_only=True)
                    if endpoint is None or _is_cable_output(endpoint):
                        endpoint_id = fallback.endpoint_id
                    policy.set_default_for_role(endpoint_id, role)
            return True
        except AudioEndpointError:
            return False

    def disable_cable_output(self) -> bool:
        cables = [
            endpoint for endpoint in _read_capture_endpoints()
            if _is_cable_output(endpoint)
        ]
        if not cables:
            self.activated = False
            return True
        try:
            with _PolicyConfig() as policy:
                for cable in cables:
                    policy.set_visible(cable.endpoint_id, False)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                remaining = [
                    endpoint for endpoint in _read_capture_endpoints()
                    if _is_cable_output(endpoint) and endpoint.visible
                ]
                if not remaining:
                    self.activated = False
                    return True
                time.sleep(0.05)
            return False
        except AudioEndpointError:
            return False

    def restore_after_failed_start(self) -> None:
        restored = self.restore_default_microphone()
        hidden = self.disable_cable_output() if restored else False
        if restored and hidden:
            self.clear_recovery_state()
        self.activated = False

