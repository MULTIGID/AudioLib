from __future__ import annotations

import array
from collections import deque
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path

from .player import choose_output_device

try:
    import miniaudio
    _MINIAUDIO_IMPORT_ERROR = None
except ImportError as error:  # The source-mode error is shown in the UI with an explanation.
    miniaudio = None
    _MINIAUDIO_IMPORT_ERROR = error

try:
    import sounddevice
    _SOUNDDEVICE_IMPORT_ERROR = None
except ImportError as error:
    sounddevice = None
    _SOUNDDEVICE_IMPORT_ERROR = error


DRIVER_PAGE = "https://vb-audio.com/Cable/index.htm"
DRIVER_URL = "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip"
EXPECTED_ARCHIVE_SHA256 = "B950E39F01AF1D04EA623C8F6D8EB9B6EA5C477C637295FABF20631C85116BFB"
EXPECTED_SETUP_SHA256 = {
    "VBCABLE_Setup_x64.exe": "734C35DFA6D98F48782A451633CEB471166EC70D60482FD89A1123D0EE3C4F41",
    "VBCABLE_Setup.exe": "01FFC86B623FF3C75A883AA900C0215A89482988E1C8E55988FC0A9FB513DBED",
}
SAMPLE_RATE = 48_000
BRIDGE_BUFFER_MSEC = 50
MONITOR_BUFFER_MSEC = 30
MIC_BUFFER_TARGET_MSEC = 40
MIC_BUFFER_MAX_LATENCY_MSEC = 100
MIC_BUFFER_CAPACITY_MSEC = 250

LATENCY_PROFILES = {
    "low": {
        "bridge": 25,
        "monitor": 20,
        "target": 20,
        "maximum": 65,
        "capacity": 160,
    },
    "balanced": {
        "bridge": BRIDGE_BUFFER_MSEC,
        "monitor": MONITOR_BUFFER_MSEC,
        "target": MIC_BUFFER_TARGET_MSEC,
        "maximum": MIC_BUFFER_MAX_LATENCY_MSEC,
        "capacity": MIC_BUFFER_CAPACITY_MSEC,
    },
    "stable": {
        "bridge": 80,
        "monitor": 50,
        "target": 70,
        "maximum": 180,
        "capacity": 400,
    },
}


def _soft_limit(sample: int) -> int:
    """Compress only loud peaks so mixed voice and audio cannot hard-clip."""
    threshold = 28_000
    magnitude = abs(sample)
    if magnitude <= threshold:
        return sample
    headroom = 32_767 - threshold
    excess = magnitude - threshold
    limited = threshold + (headroom * excess) // (headroom + excess)
    return limited if sample >= 0 else -limited


class VirtualAudioError(RuntimeError):
    pass


def _wdmks_cable_output_device() -> int:
    if sounddevice is None:
        raise VirtualAudioError(
            f"Модуль Kernel Streaming недоступний: {_SOUNDDEVICE_IMPORT_ERROR}"
        )
    host_apis = sounddevice.query_hostapis()
    candidates: list[tuple[int, dict]] = []
    for index, device in enumerate(sounddevice.query_devices()):
        host_name = str(host_apis[device["hostapi"]]["name"])
        name = str(device["name"])
        if (
            "WDM-KS" in host_name
            and int(device["max_output_channels"]) >= 2
            and "vb-audio point" in name.casefold()
        ):
            candidates.append((index, device))
    if not candidates:
        raise VirtualAudioError("Не знайдено вихідний WDM/KS-пін VB-CABLE")
    return min(
        candidates,
        key=lambda item: (
            not str(item[1]["name"]).casefold().startswith("output"),
            int(item[0]),
        ),
    )[0]


class _KernelStreamingPlaybackDevice:
    """Feed the existing bridge generator directly into VB-CABLE's KS pin."""

    def __init__(
        self,
        *,
        sample_rate: int,
        channels: int,
        buffersize_msec: int,
    ) -> None:
        if sounddevice is None:
            raise VirtualAudioError(
                f"Модуль Kernel Streaming недоступний: {_SOUNDDEVICE_IMPORT_ERROR}"
            )
        self._generator = None
        self._callback_error: Exception | None = None
        blocksize = max(128, sample_rate * buffersize_msec // 1000)
        self._stream = sounddevice.RawOutputStream(
            device=_wdmks_cable_output_device(),
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            blocksize=blocksize,
            latency="low",
            callback=self._callback,
        )

    def _callback(self, outdata, frames: int, _time_info, _status) -> None:
        byte_count = len(outdata)
        if self._generator is None or self._callback_error is not None:
            outdata[:] = b"\0" * byte_count
            return
        try:
            samples = self._generator.send(frames)
            payload = samples.tobytes() if hasattr(samples, "tobytes") else bytes(samples)
            if len(payload) < byte_count:
                payload += b"\0" * (byte_count - len(payload))
            outdata[:] = payload[:byte_count]
        except Exception as error:
            self._callback_error = error
            outdata[:] = b"\0" * byte_count

    def start(self, generator) -> None:
        self._generator = generator
        self._stream.start()

    def close(self) -> None:
        self._generator = None
        try:
            self._stream.stop()
        finally:
            self._stream.close()


def driver_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "AudioLib" / "VB-CABLE"


def _setup_name() -> str:
    machine = platform.machine().casefold()
    return "VBCABLE_Setup_x64.exe" if "64" in machine else "VBCABLE_Setup.exe"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _verify_official_package(archive: Path, setup: Path) -> None:
    if not archive.exists() or _sha256(archive) != EXPECTED_ARCHIVE_SHA256:
        raise VirtualAudioError("Контрольна сума офіційного архіву VB-CABLE не збігається")
    expected_setup = EXPECTED_SETUP_SHA256.get(setup.name)
    if not setup.exists() or not expected_setup or _sha256(setup) != expected_setup:
        raise VirtualAudioError("Контрольна сума підписаного інсталятора VB-CABLE не збігається")


def prepare_official_installer() -> Path:
    target = driver_directory()
    setup = target / "package" / _setup_name()
    archive = target / "VBCABLE_Driver_Pack45.zip"
    if setup.exists():
        _verify_official_package(archive, setup)
        return setup

    target.mkdir(parents=True, exist_ok=True)
    partial = archive.with_suffix(".download")
    request = urllib.request.Request(
        DRIVER_URL,
        headers={"User-Agent": "AudioLib/2.8.6 (+https://vb-audio.com/Cable/index.htm)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response, partial.open("wb") as output:
            if response.geturl().split("/", 3)[2].casefold() != "download.vb-audio.com":
                raise VirtualAudioError("Завантаження було перенаправлено на невідомий сайт")
            total = 0
            while chunk := response.read(128 * 1024):
                total += len(chunk)
                if total > 10 * 1024 * 1024:
                    raise VirtualAudioError("Файл драйвера має неочікувано великий розмір")
                output.write(chunk)
        os.replace(partial, archive)
        if _sha256(archive) != EXPECTED_ARCHIVE_SHA256:
            raise VirtualAudioError("Завантажений архів VB-CABLE не пройшов перевірку цілісності")
        extract_to = target / "package"
        if extract_to.exists():
            shutil.rmtree(extract_to)
        extract_to.mkdir()
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                destination = (extract_to / member.filename).resolve()
                if extract_to.resolve() not in destination.parents and destination != extract_to.resolve():
                    raise VirtualAudioError("Архів драйвера містить небезпечний шлях")
            package.extractall(extract_to)
        setup = extract_to / _setup_name()
        if not setup.exists():
            raise VirtualAudioError("В офіційному архіві не знайдено інсталятор")
        _verify_official_package(archive, setup)
        return setup
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def launch_driver_setup(setup: Path) -> None:
    _verify_official_package(setup.parent.parent / "VBCABLE_Driver_Pack45.zip", setup)
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", str(setup), None, str(setup.parent), 1
    )
    if result <= 32:
        raise VirtualAudioError("Windows не запустила інсталятор з правами адміністратора")


class VirtualMicrophoneBridge:
    """Mix the default physical microphone and a selected sound into VB-CABLE."""

    def __init__(self, *, mix_microphone: bool = True) -> None:
        self._mix_microphone = bool(mix_microphone)
        self._kernel_streaming_cable = False
        self._inventory = None
        self._capture_device = None
        self._capture_generator = None
        self._cable_device = None
        self._cable_generator = None
        self._monitor_device = None
        self._monitor_generator = None
        self._lock = threading.Lock()
        self._latency_profile = "balanced"
        self._local_monitoring = True
        self._monitor_output_name = ""
        self._microphone_buffer = deque(maxlen=SAMPLE_RATE * MIC_BUFFER_CAPACITY_MSEC // 1000)
        self._microphone_buffer_ready = False
        self._microphone_underruns = 0
        self._microphone_overruns = 0
        self._track = array.array("h")
        self._track_position = 0
        self._monitor_position = 0
        self._track_paused = False
        self._track_volume = 0.5
        self._normalization_gain = 1.0
        self._normalization_enabled = True
        self.sample_rate = SAMPLE_RATE
        self.capture_channels = 1
        self.playback_name = ""
        self.capture_name = ""
        self.monitor_name = ""
        self.screen_capture_protected = False
        self.screen_capture_protection_error = ""

    @staticmethod
    def _is_cable(name: str) -> bool:
        lowered = name.casefold()
        return "cable" in lowered and ("vb-audio" in lowered or "input" in lowered or "output" in lowered)

    def is_running(self) -> bool:
        return self._cable_device is not None

    def configure(
        self,
        *,
        latency_profile: str = "balanced",
        local_monitoring: bool = True,
        monitor_output_name: str = "",
        kernel_streaming_cable: bool | None = None,
        mix_microphone: bool | None = None,
    ) -> None:
        profile = latency_profile if latency_profile in LATENCY_PROFILES else "balanced"
        requested_kernel_streaming = (
            self._kernel_streaming_cable
            if kernel_streaming_cable is None
            else bool(kernel_streaming_cable)
        )
        requested_mix_microphone = (
            self._mix_microphone
            if mix_microphone is None
            else bool(mix_microphone)
        )
        restart_required = (
            profile != self._latency_profile
            or bool(local_monitoring) != self._local_monitoring
            or monitor_output_name.strip() != self._monitor_output_name
            or requested_kernel_streaming != self._kernel_streaming_cable
            or requested_mix_microphone != self._mix_microphone
        )
        was_running = self.is_running()
        if restart_required and was_running:
            self.close()
        self._latency_profile = profile
        self._local_monitoring = bool(local_monitoring)
        self._monitor_output_name = monitor_output_name.strip()
        self._kernel_streaming_cable = requested_kernel_streaming
        self._mix_microphone = requested_mix_microphone
        capacity = LATENCY_PROFILES[profile]["capacity"]
        self._microphone_buffer = deque(maxlen=SAMPLE_RATE * capacity // 1000)

    @property
    def uses_kernel_streaming(self) -> bool:
        return self._kernel_streaming_cable

    def device_status(self) -> tuple[bool, str]:
        if miniaudio is None:
            return False, f"Модуль miniaudio недоступний: {_MINIAUDIO_IMPORT_ERROR}"
        inventory = miniaudio.Devices()
        playbacks = inventory.get_playbacks()
        cable_candidates = [
            item for item in playbacks if self._is_cable(item["name"])
        ]
        cable = min(
            cable_candidates,
            key=lambda item: (
                "16ch" in item["name"].casefold(),
                "cable input" not in item["name"].casefold(),
            ),
            default=None,
        )
        if cable:
            return True, f"Знайдено: {cable['name']}"
        return False, "VB-CABLE не знайдено"

    @staticmethod
    def _native_formats(device: dict) -> list[dict]:
        return [item for item in device.get("formats", []) if isinstance(item, dict)]

    @classmethod
    def _select_capture_channels(cls, capture: dict) -> int:
        channels = {
            int(item.get("channels", 0)) for item in cls._native_formats(capture)
            if int(item.get("channels", 0)) > 0
        }
        if 1 in channels:
            return 1
        if 2 in channels:
            return 2
        return 1

    def start(
        self,
        preferred_capture_name: str | None = None,
        preferred_capture_description: str | None = None,
    ) -> None:
        if self._cable_device is not None:
            return
        if miniaudio is None:
            raise VirtualAudioError(
                f"Модуль miniaudio недоступний: {_MINIAUDIO_IMPORT_ERROR}"
            )
        self._inventory = miniaudio.Devices()
        playbacks = self._inventory.get_playbacks()
        captures = self._inventory.get_captures()
        cable = next((item for item in playbacks if self._is_cable(item["name"])), None)
        if cable is None:
            raise VirtualAudioError("VB-CABLE не знайдено. Спочатку встановіть драйвер і перезавантажте Windows")
        physical_captures = [item for item in captures if not self._is_cable(item["name"])]
        physical_mic = None
        if preferred_capture_name:
            wanted_name = preferred_capture_name.casefold()
            wanted_description = (preferred_capture_description or "").casefold()

            def match_score(item) -> int:
                candidate = item["name"].casefold()
                score = 0
                if wanted_name == candidate:
                    score += 8
                elif wanted_name in candidate:
                    score += 4
                if wanted_description and wanted_description in candidate:
                    score += 3
                return score

            physical_mic = max(physical_captures, key=match_score, default=None)
            if physical_mic is not None and match_score(physical_mic) == 0:
                physical_mic = None
        if physical_mic is None:
            physical_mic = next(iter(physical_captures), None)
        if physical_mic is None:
            raise VirtualAudioError("Не знайдено фізичний мікрофон для змішування")
        self.playback_name = cable["name"]
        self.capture_name = physical_mic["name"]
        self.monitor_name = ""
        # VB-CABLE and the supported headsets use 48 kHz.  Keeping the bridge
        # fixed at that rate avoids an extra device-to-device conversion.
        self.sample_rate = SAMPLE_RATE
        self.capture_channels = self._select_capture_channels(physical_mic)
        try:
            # Capture and VB-CABLE are separate devices with independent audio
            # clocks.  A DuplexStream ties their callbacks together and some
            # USB/C-Media devices then periodically repeat or lose samples.
            # Decouple both sides with a bounded ring buffer instead.
            profile = LATENCY_PROFILES[self._latency_profile]
            if self._mix_microphone:
                self._capture_device = miniaudio.CaptureDevice(
                    input_format=miniaudio.SampleFormat.SIGNED16,
                    nchannels=self.capture_channels,
                    sample_rate=self.sample_rate,
                    buffersize_msec=profile["bridge"],
                    device_id=physical_mic["id"],
                    app_name="AudioLib Physical Microphone Relay",
                )
            if self._kernel_streaming_cable:
                self._cable_device = _KernelStreamingPlaybackDevice(
                    sample_rate=self.sample_rate,
                    channels=2,
                    buffersize_msec=profile["bridge"],
                )
                self.playback_name = "VB-CABLE WDM/KS"
                self.screen_capture_protected = True
                self.screen_capture_protection_error = ""
            else:
                # Shared mode remains available as a compatibility fallback.
                self.screen_capture_protected = False
                self.screen_capture_protection_error = (
                    "Використовується сумісний спільний режим VB-CABLE"
                )
                self._cable_device = miniaudio.PlaybackDevice(
                    output_format=miniaudio.SampleFormat.SIGNED16,
                    nchannels=2,
                    sample_rate=self.sample_rate,
                    buffersize_msec=profile["bridge"],
                    device_id=cable["id"],
                    app_name="AudioLib Virtual Microphone",
                )
            if self._local_monitoring:
                # Local monitoring must never use CABLE Input.  Passing no
                # device ID here used to mean "Windows default"; if VB-CABLE
                # became the default endpoint, the selected recording was fed
                # into the cable twice (main mix + monitor), causing echo,
                # distortion and duplicate audio in Discord screen sharing.
                monitor = choose_output_device(
                    playbacks,
                    self._monitor_output_name,
                )
                if monitor is not None:
                    self.monitor_name = str(monitor.get("name", ""))
                    self._monitor_device = miniaudio.PlaybackDevice(
                        output_format=miniaudio.SampleFormat.SIGNED16,
                        nchannels=2,
                        sample_rate=self.sample_rate,
                        buffersize_msec=profile["monitor"],
                        device_id=monitor.get("id"),
                        app_name="AudioLib Microphone Monitor",
                    )
            if self._capture_device is not None:
                self._capture_generator = self._capture_microphone()
                next(self._capture_generator)
            self._cable_generator = self._cable_output()
            next(self._cable_generator)
            if self._monitor_device is not None:
                self._monitor_generator = self._monitor()
                next(self._monitor_generator)
            if self._capture_device is not None and self._capture_generator is not None:
                self._capture_device.start(self._capture_generator)
            self._cable_device.start(self._cable_generator)
            if self._monitor_device is not None and self._monitor_generator is not None:
                self._monitor_device.start(self._monitor_generator)
        except Exception as error:
            self.close()
            raise VirtualAudioError(f"Не вдалося запустити віртуальний мікрофон: {error}") from error

    def _capture_microphone(self):
        captured = yield b""
        while True:
            microphone = array.array("h")
            microphone.frombytes(captured)
            with self._lock:
                frame_count = len(microphone) // self.capture_channels
                incoming = []
                for frame in range(frame_count):
                    capture_start = frame * self.capture_channels
                    if self.capture_channels == 1:
                        mic_sample = int(microphone[capture_start])
                    else:
                        capture_end = capture_start + self.capture_channels
                        mic_sample = int(
                            sum(microphone[capture_start:capture_end])
                            / self.capture_channels
                        )
                    incoming.append(mic_sample)
                capacity = self._microphone_buffer.maxlen or len(incoming)
                overflow = max(0, len(self._microphone_buffer) + len(incoming) - capacity)
                if overflow:
                    self._microphone_overruns += 1
                self._microphone_buffer.extend(incoming)
            captured = yield None

    def _cable_output(self):
        required_frames = yield b""
        profile = LATENCY_PROFILES[self._latency_profile]
        target_frames = self.sample_rate * profile["target"] // 1000
        latency_ceiling = self.sample_rate * profile["maximum"] // 1000
        while True:
            output = array.array("h")
            with self._lock:
                # Capture and playback devices have independent clocks.  Do not
                # allow their small drift to accumulate into a noticeable voice
                # delay: retain enough samples for stable playback and discard
                # only the oldest excess audio.
                if len(self._microphone_buffer) > latency_ceiling:
                    keep_frames = max(target_frames, required_frames * 2)
                    discard_frames = max(
                        0, len(self._microphone_buffer) - keep_frames
                    )
                    for _ in range(discard_frames):
                        self._microphone_buffer.popleft()
                    if discard_frames:
                        self._microphone_overruns += 1
                if not self._microphone_buffer_ready:
                    self._microphone_buffer_ready = len(self._microphone_buffer) >= target_frames
                if self._microphone_buffer_ready and len(self._microphone_buffer) < required_frames:
                    self._microphone_buffer_ready = False
                    self._microphone_underruns += 1

                position = self._track_position
                track = self._track
                paused = self._track_paused
                for frame in range(required_frames):
                    if self._mix_microphone and self._microphone_buffer_ready:
                        mic_sample = self._microphone_buffer.popleft()
                    else:
                        mic_sample = 0
                    left = right = mic_sample
                    track_index = position + frame * 2
                    if not paused and track_index + 1 < len(track):
                        # Leave headroom while mixing instead of hard-clipping the
                        # physical microphone and the selected recording.
                        left = _soft_limit(
                            int(left * 0.90)
                            + int(
                                track[track_index]
                                * self._track_volume
                                * (self._normalization_gain if self._normalization_enabled else 1.0)
                                * 0.75
                            )
                        )
                        right = _soft_limit(
                            int(right * 0.90)
                            + int(
                                track[track_index + 1]
                                * self._track_volume
                                * (self._normalization_gain if self._normalization_enabled else 1.0)
                                * 0.75
                            )
                        )
                    output.append(max(-32768, min(32767, left)))
                    output.append(max(-32768, min(32767, right)))
                if not paused:
                    self._track_position = min(len(track), position + required_frames * 2)
            required_frames = yield output

    def _monitor(self):
        required_frames = yield b""
        while True:
            output = array.array("h", [0]) * (required_frames * 2)
            with self._lock:
                if not self._track_paused:
                    start = self._monitor_position
                    end = min(len(self._track), start + required_frames * 2)
                    for index, sample in enumerate(self._track[start:end]):
                        gain = (
                            self._normalization_gain
                            if self._normalization_enabled else 1.0
                        )
                        output[index] = max(
                            -32768,
                            min(32767, int(sample * self._track_volume * gain)),
                        )
                    self._monitor_position = end
            required_frames = yield output

    def play_decoded(
        self,
        samples: array.array,
        volume: float = 0.5,
        *,
        normalization_gain: float,
        normalize: bool = True,
    ) -> None:
        self.start()
        if not samples:
            raise VirtualAudioError("Аудіофайл не містить звуку")
        with self._lock:
            self._track = samples
            self._normalization_gain = max(0.01, min(10.0, normalization_gain))
            self._normalization_enabled = bool(normalize)
            self._track_position = 0
            self._monitor_position = 0
            self._track_paused = False
            self._track_volume = max(0.0, min(1.0, volume))

    def pause_sound(self) -> None:
        with self._lock:
            if self._track and self.playback_state_unlocked() == "playing":
                self._track_paused = True

    def resume_sound(self) -> None:
        with self._lock:
            if self._track and self.playback_state_unlocked() == "paused":
                self._track_paused = False

    def set_volume(self, volume: float) -> None:
        with self._lock:
            self._track_volume = max(0.0, min(1.0, volume))

    def set_normalization_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._normalization_enabled = bool(enabled)

    def playback_state_unlocked(self) -> str:
        if not self._track:
            return "stopped"
        position = (
            min(self._track_position, self._monitor_position)
            if self._monitor_device is not None
            else self._track_position
        )
        if position >= len(self._track):
            return "stopped"
        return "paused" if self._track_paused else "playing"

    def playback_state(self) -> str:
        with self._lock:
            return self.playback_state_unlocked()

    def stop_sound(self) -> None:
        with self._lock:
            self._track = array.array("h")
            self._track_position = 0
            self._monitor_position = 0
            self._track_paused = False
            self._normalization_gain = 1.0

    def close(self) -> None:
        self.stop_sound()
        if self._cable_device is not None:
            try:
                self._cable_device.close()
            except Exception:
                pass
        if self._monitor_device is not None:
            try:
                self._monitor_device.close()
            except Exception:
                pass
        if self._capture_device is not None:
            try:
                self._capture_device.close()
            except Exception:
                pass
        self._capture_device = None
        self._capture_generator = None
        self._cable_device = None
        self._cable_generator = None
        self._monitor_device = None
        self._monitor_generator = None
        self.screen_capture_protected = False
        with self._lock:
            self._microphone_buffer.clear()
            self._microphone_buffer_ready = False
        self._inventory = None


def _write_relay_result(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, path)


class _WindowsIsolatedProcess:
    """Small process-handle wrapper compatible with the Popen methods we use."""

    def __init__(self, handle: int, pid: int, command: list[str]) -> None:
        self._handle = handle
        self.pid = pid
        self.args = command

    def poll(self) -> int | None:
        if not self._handle:
            return 0
        exit_code = wintypes.DWORD()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        if not kernel32.GetExitCodeProcess(self._handle, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return None if exit_code.value == 259 else int(exit_code.value)

    def wait(self, timeout: float | None = None) -> int:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        milliseconds = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        result = kernel32.WaitForSingleObject(self._handle, milliseconds)
        if result == 0x00000102:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if result != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(self.poll() or 0)

    def terminate(self) -> None:
        if not self._handle:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        if not kernel32.TerminateProcess(self._handle, 1):
            error = ctypes.get_last_error()
            if error != 5 or self.poll() is None:
                raise ctypes.WinError(error)

    kill = terminate

    def close(self) -> None:
        if not self._handle:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(self._handle)
        self._handle = 0


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _STARTUPINFOW),
        ("lpAttributeList", wintypes.LPVOID),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _spawn_outside_current_process_tree(
    command: list[str],
    cwd: Path,
) -> _WindowsIsolatedProcess:
    """Create a helper whose logical parent is Explorer, not AudioLib.

    Discord's application audio capture includes the selected process tree. A
    normal Popen child therefore remains audible in the screen share even when
    its audio is separated into another process.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    user32.GetShellWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    shell_window = user32.GetShellWindow()
    shell_pid = wintypes.DWORD()
    if not shell_window or not user32.GetWindowThreadProcessId(shell_window, ctypes.byref(shell_pid)):
        raise VirtualAudioError("Не вдалося знайти процес оболонки Windows для ізоляції мікрофона")

    process_create_process = 0x0080
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    shell_handle = kernel32.OpenProcess(process_create_process, False, shell_pid.value)
    if not shell_handle:
        raise VirtualAudioError(
            f"Не вдалося відкрити оболонку Windows для ізоляції мікрофона: {ctypes.WinError(ctypes.get_last_error())}"
        )

    attribute_list = None
    attribute_list_initialized = False
    process_info = _PROCESS_INFORMATION()
    try:
        size = ctypes.c_size_t()
        kernel32.InitializeProcThreadAttributeList.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attribute_list = ctypes.create_string_buffer(size.value)
        attribute_pointer = ctypes.cast(attribute_list, wintypes.LPVOID)
        if not kernel32.InitializeProcThreadAttributeList(
            attribute_pointer, 1, 0, ctypes.byref(size)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        attribute_list_initialized = True

        kernel32.UpdateProcThreadAttribute.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.c_size_t,
            wintypes.LPVOID,
            ctypes.c_size_t,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]
        kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        parent_value = wintypes.HANDLE(shell_handle)
        if not kernel32.UpdateProcThreadAttribute(
            attribute_pointer,
            0,
            0x00020000,
            ctypes.byref(parent_value),
            ctypes.sizeof(parent_value),
            None,
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        startup = _STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.lpAttributeList = attribute_pointer
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        kernel32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPCWSTR,
            ctypes.POINTER(_STARTUPINFOW),
            ctypes.POINTER(_PROCESS_INFORMATION),
        ]
        kernel32.CreateProcessW.restype = wintypes.BOOL
        creation_flags = 0x00080000 | 0x08000000
        if not kernel32.CreateProcessW(
            command[0],
            command_line,
            None,
            None,
            False,
            creation_flags,
            None,
            str(cwd),
            ctypes.byref(startup.StartupInfo),
            ctypes.byref(process_info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(process_info.hThread)
        return _WindowsIsolatedProcess(
            process_info.hProcess,
            int(process_info.dwProcessId),
            command,
        )
    finally:
        if attribute_list is not None and attribute_list_initialized:
            kernel32.DeleteProcThreadAttributeList.argtypes = [wintypes.LPVOID]
            kernel32.DeleteProcThreadAttributeList.restype = None
            kernel32.DeleteProcThreadAttributeList(ctypes.cast(attribute_list, wintypes.LPVOID))
        kernel32.CloseHandle(shell_handle)


def run_microphone_relay_worker(
    control_dir: Path,
    parent_pid: int,
    preferred_capture_name: str,
    preferred_capture_description: str,
) -> int:
    """Relay only the physical microphone in a process separate from the UI."""
    control_dir.mkdir(parents=True, exist_ok=True)
    ready_path = control_dir / "ready.json"
    stop_path = control_dir / "stop"
    bridge = VirtualMicrophoneBridge(mix_microphone=True)
    bridge.configure(local_monitoring=False)
    parent_handle = None
    kernel32 = None
    try:
        if sys.platform == "win32":
            synchronize = 0x00100000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                ctypes.c_uint32,
                ctypes.c_bool,
                ctypes.c_uint32,
            ]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.WaitForSingleObject.restype = ctypes.c_uint32
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_bool
            parent_handle = kernel32.OpenProcess(
                synchronize,
                False,
                int(parent_pid),
            )
            if not parent_handle:
                raise VirtualAudioError("Не вдалося відкрити процес AudioLib")
        bridge.start(preferred_capture_name, preferred_capture_description)
        _write_relay_result(ready_path, {"ok": True})
        while not stop_path.exists():
            if parent_handle is not None and kernel32 is not None:
                wait_result = kernel32.WaitForSingleObject(
                    parent_handle,
                    100,
                )
                if wait_result == 0:
                    break
            else:
                time.sleep(0.1)
        return 0
    except Exception as error:
        try:
            _write_relay_result(ready_path, {"ok": False, "error": str(error)})
        except OSError:
            pass
        return 1
    finally:
        bridge.close()
        if parent_handle is not None and kernel32 is not None:
            kernel32.CloseHandle(parent_handle)


class MicrophoneRelayProcess:
    """Own a microphone-only helper so screen capture cannot tap the UI stream."""

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.process: subprocess.Popen | _WindowsIsolatedProcess | None = None
        self.control_dir: Path | None = None

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(
        self,
        preferred_capture_name: str,
        preferred_capture_description: str,
        timeout: float = 8.0,
    ) -> None:
        self.stop()
        relay_root = Path(tempfile.gettempdir()) / "AudioLibRelay"
        relay_root.mkdir(parents=True, exist_ok=True)
        self.control_dir = relay_root / uuid.uuid4().hex
        self.control_dir.mkdir(parents=True, exist_ok=False)
        if getattr(sys, "frozen", False):
            command = [sys.executable]
        else:
            command = [sys.executable, str(self.root_dir / "main.py")]
        command.extend(
            [
                "--microphone-relay",
                "--relay-control",
                str(self.control_dir),
                "--relay-parent",
                str(os.getpid()),
                "--relay-name",
                preferred_capture_name,
                "--relay-description",
                preferred_capture_description,
            ]
        )
        try:
            if sys.platform == "win32":
                self.process = _spawn_outside_current_process_tree(
                    command,
                    self.root_dir,
                )
            else:
                self.process = subprocess.Popen(
                    command,
                    cwd=self.root_dir,
                    close_fds=True,
                )
            ready_path = self.control_dir / "ready.json"
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if ready_path.exists():
                    payload = json.loads(ready_path.read_text(encoding="utf-8"))
                    if payload.get("ok") is True:
                        return
                    raise VirtualAudioError(
                        str(payload.get("error") or "Не вдалося запустити реле мікрофона")
                    )
                if self.process.poll() is not None:
                    raise VirtualAudioError("Процес реле мікрофона передчасно завершився")
                time.sleep(0.05)
            raise VirtualAudioError("Реле фізичного мікрофона не відповіло вчасно")
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        process = self.process
        control_dir = self.control_dir
        if control_dir is not None:
            try:
                (control_dir / "stop").touch()
            except OSError:
                pass
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
        if isinstance(process, _WindowsIsolatedProcess):
            process.close()
        self.process = None
        self.control_dir = None
        if control_dir is not None:
            shutil.rmtree(control_dir, ignore_errors=True)

