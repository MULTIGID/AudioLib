from __future__ import annotations

import array
from collections import deque
import ctypes
import hashlib
import os
import platform
import shutil
import threading
import urllib.request
import zipfile
from pathlib import Path

from .player import choose_output_device

try:
    import miniaudio
    _MINIAUDIO_IMPORT_ERROR = None
except ImportError as error:  # The source-mode error is shown in the UI with an explanation.
    miniaudio = None
    _MINIAUDIO_IMPORT_ERROR = error


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
        headers={"User-Agent": "AudioLib/2.8.1 (+https://vb-audio.com/Cable/index.htm)"},
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

    def __init__(self) -> None:
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
    ) -> None:
        profile = latency_profile if latency_profile in LATENCY_PROFILES else "balanced"
        restart_required = (
            profile != self._latency_profile
            or bool(local_monitoring) != self._local_monitoring
            or monitor_output_name.strip() != self._monitor_output_name
        )
        was_running = self.is_running()
        if restart_required and was_running:
            self.close()
        self._latency_profile = profile
        self._local_monitoring = bool(local_monitoring)
        self._monitor_output_name = monitor_output_name.strip()
        capacity = LATENCY_PROFILES[profile]["capacity"]
        self._microphone_buffer = deque(maxlen=SAMPLE_RATE * capacity // 1000)

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
            self._capture_device = miniaudio.CaptureDevice(
                input_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=self.capture_channels,
                sample_rate=self.sample_rate,
                buffersize_msec=profile["bridge"],
                device_id=physical_mic["id"],
                app_name="AudioLib Virtual Microphone",
            )
            # VB-CABLE can accept an exclusive WASAPI stream but silently stop
            # forwarding it to CABLE Output on some driver/headset combinations.
            # Shared mode is the driver's normal path and reliably carries both
            # the physical microphone and AudioLib recordings.
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
            self._capture_generator = self._capture_microphone()
            next(self._capture_generator)
            self._cable_generator = self._cable_output()
            next(self._cable_generator)
            if self._monitor_device is not None:
                self._monitor_generator = self._monitor()
                next(self._monitor_generator)
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
                    if self._microphone_buffer_ready:
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

