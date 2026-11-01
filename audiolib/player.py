from __future__ import annotations

import array
import threading
from pathlib import Path

from .audio_endpoints import default_playback_device_names

try:
    import miniaudio
    _MINIAUDIO_IMPORT_ERROR = None
except ImportError as error:
    miniaudio = None
    _MINIAUDIO_IMPORT_ERROR = error


SAMPLE_RATE = 48_000
CHANNELS = 2


class PlayerError(RuntimeError):
    pass


def _is_cable_name(name: str) -> bool:
    lowered = name.casefold()
    return "cable" in lowered and (
        "vb-audio" in lowered or "input" in lowered or "output" in lowered
    )


def _name_matches(candidate: str, wanted: str) -> bool:
    candidate_key = " ".join(candidate.casefold().split())
    wanted_key = " ".join(wanted.casefold().split())
    return bool(
        candidate_key
        and wanted_key
        and (
            candidate_key == wanted_key
            or (len(wanted_key) >= 5 and wanted_key in candidate_key)
            or (len(candidate_key) >= 5 and candidate_key in wanted_key)
        )
    )


def choose_output_device(playbacks, requested_name: str = ""):
    """Choose an explicit physical endpoint, preferring Windows defaults."""
    physical = [
        item
        for item in playbacks
        if not _is_cable_name(str(item.get("name", "")))
    ]
    if not physical:
        return None
    requested_name = requested_name.strip()
    if requested_name:
        selected = next(
            (
                item
                for item in physical
                if _name_matches(str(item.get("name", "")), requested_name)
            ),
            None,
        )
        if selected is not None:
            return selected
    else:
        for default_name in default_playback_device_names():
            if _is_cable_name(default_name):
                continue
            selected = next(
                (
                    item
                    for item in physical
                    if _name_matches(str(item.get("name", "")), default_name)
                ),
                None,
            )
            if selected is not None:
                return selected

    # When every Windows default role points to VB-CABLE, prefer an endpoint
    # that looks like an audible local device rather than HDMI/monitor output.
    audible_words = (
        "speaker", "headphone", "headset", "speakers",
        "динами", "науш", "навуш", "гарнітур", "гарнит",
    )
    silent_words = ("hdmi", "display", "monitor", "digital", "spdif", "s/pdif")

    def fallback_score(item) -> int:
        name = str(item.get("name", "")).casefold()
        return (
            10 * sum(word in name for word in audible_words)
            - 10 * sum(word in name for word in silent_words)
        )

    return max(physical, key=fallback_score)


def list_output_devices() -> list[str]:
    """Return physical playback endpoint names, keeping the default first."""
    if miniaudio is None:
        return []
    try:
        inventory = miniaudio.Devices()
        playbacks = inventory.get_playbacks()
        selected = choose_output_device(playbacks)
        ordered = ([selected] if selected is not None else []) + [
            item for item in playbacks if item is not selected
        ]
        names = [str(item.get("name", "")).strip() for item in ordered]
    except Exception:
        return []
    return list(
        dict.fromkeys(
            name for name in names
            if name and not _is_cable_name(name)
        )
    )


class WindowsMediaPlayer:
    """Local player backed by miniaudio instead of the legacy Windows MCI."""

    def __init__(self) -> None:
        self.loaded_path: Path | None = None
        self.paused = False
        self._state = "stopped"
        self._samples = array.array("h")
        self._position = 0
        self._volume = 0.5
        self._normalization_gain = 1.0
        self._normalization_enabled = True
        self._device = None
        self._generator = None
        self._inventory = None
        self._output_device_name = ""
        self._lock = threading.RLock()

    @staticmethod
    def _is_cable(name: str) -> bool:
        return _is_cable_name(name)

    def _open_output(self) -> None:
        if miniaudio is None:
            raise PlayerError(f"Модуль miniaudio недоступний: {_MINIAUDIO_IMPORT_ERROR}")
        try:
            # Never let an implicit Windows default route ordinary playback
            # back into CABLE Input.  Windows may make VB-CABLE the default
            # after installation or while another application changes audio
            # routing.  In that case the recording would be injected into the
            # virtual microphone a second time.
            self._inventory = miniaudio.Devices()
            playbacks = self._inventory.get_playbacks()
            selected = choose_output_device(playbacks, self._output_device_name)
            if selected is None:
                raise PlayerError(
                    "Не знайдено фізичний пристрій виведення. "
                    "VB-CABLE не використовується як динамік."
                )
            device_id = selected.get("id")
            self._device = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=CHANNELS,
                sample_rate=SAMPLE_RATE,
                buffersize_msec=30,
                device_id=device_id,
                app_name="AudioLib Player",
            )
            self._generator = self._playback_stream()
            next(self._generator)
            self._device.start(self._generator)
        except Exception as error:
            self.close()
            raise PlayerError(f"Не вдалося відкрити динаміки: {error}") from error

    def _playback_stream(self):
        required_frames = yield b""
        while True:
            output = array.array("h", [0]) * (required_frames * CHANNELS)
            with self._lock:
                if self._state == "playing":
                    start = self._position
                    end = min(len(self._samples), start + required_frames * CHANNELS)
                    source = self._samples[start:end]
                    for index, sample in enumerate(source):
                        gain = (
                            self._normalization_gain
                            if self._normalization_enabled else 1.0
                        )
                        output[index] = max(
                            -32768,
                            min(32767, int(sample * self._volume * gain)),
                        )
                    self._position = end
                    if self._position >= len(self._samples):
                        self._state = "stopped"
                        self.paused = False
            required_frames = yield output

    def load_decoded(
        self,
        path: Path,
        samples: array.array,
        *,
        normalization_gain: float,
        normalize: bool = True,
    ) -> None:
        self.close()
        if not samples:
            raise PlayerError("Аудіофайл не містить звуку")
        self._samples = samples
        self._normalization_gain = max(0.01, min(10.0, normalization_gain))
        self._normalization_enabled = bool(normalize)
        self.loaded_path = path
        self._position = 0
        self._state = "stopped"
        self.paused = False
        self._open_output()

    def play(self) -> None:
        with self._lock:
            if self.loaded_path and self._samples:
                if self._position >= len(self._samples):
                    self._position = 0
                self._state = "playing"
                self.paused = False

    def pause(self) -> None:
        with self._lock:
            if self.loaded_path and self._state == "playing":
                self._state = "paused"
                self.paused = True

    def resume(self) -> None:
        with self._lock:
            if self.loaded_path and self._state == "paused":
                self._state = "playing"
                self.paused = False

    def stop(self) -> None:
        with self._lock:
            if self.loaded_path:
                self._state = "stopped"
                self._position = 0
                self.paused = False

    def seek(self, milliseconds: int) -> None:
        with self._lock:
            if self.loaded_path:
                target = int(max(0, milliseconds) * SAMPLE_RATE * CHANNELS / 1000)
                self._position = min(len(self._samples), target)

    def set_volume(self, value: int) -> None:
        with self._lock:
            self._volume = max(0.0, min(1.0, value / 1000))

    def set_normalization_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._normalization_enabled = bool(enabled)

    def set_output_device(self, name: str) -> None:
        """Use a named output, or an empty string for the Windows default."""
        clean_name = name.strip()
        with self._lock:
            if clean_name == self._output_device_name:
                return
            self._output_device_name = clean_name
        device = self._device
        self._device = None
        if device is not None:
            try:
                device.close()
            except Exception:
                pass
        self._generator = None
        self._inventory = None
        if self.loaded_path and self._samples:
            self._open_output()

    def position(self) -> int:
        with self._lock:
            return int(self._position * 1000 / (SAMPLE_RATE * CHANNELS))

    def length(self) -> int:
        with self._lock:
            return int(len(self._samples) * 1000 / (SAMPLE_RATE * CHANNELS))

    def mode(self) -> str:
        with self._lock:
            return self._state

    def close(self) -> None:
        device = self._device
        self._device = None
        if device is not None:
            try:
                device.close()
            except Exception:
                pass
        with self._lock:
            self.loaded_path = None
            self.paused = False
            self._state = "stopped"
            self._samples = array.array("h")
            self._normalization_gain = 1.0
            self._position = 0
            self._generator = None
            self._inventory = None
