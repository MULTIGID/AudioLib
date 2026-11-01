from __future__ import annotations

import array
import math
from collections.abc import Sequence
from pathlib import Path

try:
    import miniaudio
    _MINIAUDIO_IMPORT_ERROR = None
except ImportError as error:
    miniaudio = None
    _MINIAUDIO_IMPORT_ERROR = error


TARGET_LOUDNESS_DBFS = -18.0
PEAK_CEILING_DBFS = -1.0
MIN_GAIN_DB = -18.0
MAX_GAIN_DB = 12.0
ABSOLUTE_GATE_DBFS = -60.0
RELATIVE_GATE_DB = 10.0


def decode_pcm16_file(
    path: Path, *, channels: int = 2, sample_rate: int = 48_000
) -> array.array:
    if miniaudio is None:
        raise RuntimeError(f"Модуль miniaudio недоступний: {_MINIAUDIO_IMPORT_ERROR}")
    decoded = miniaudio.decode(
        path.read_bytes(),
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=channels,
        sample_rate=sample_rate,
    )
    samples = array.array("h", decoded.samples)
    if not samples:
        raise RuntimeError("Аудіофайл не містить звуку")
    return samples


def _mean_square_dbfs(square_sum: float, sample_count: int) -> float:
    if sample_count <= 0 or square_sum <= 0:
        return float("-inf")
    mean_square = square_sum / sample_count
    return 10.0 * math.log10(mean_square / (32768.0 * 32768.0))


def analyze_pcm16_loudness(
    samples: Sequence[int],
    *,
    channels: int = 2,
    sample_rate: int = 48_000,
) -> tuple[float, float]:
    """Return gated average loudness in dBFS and a safe playback gain.

    The source PCM is never modified.  Silence is excluded with an absolute
    and relative gate, while the gain is constrained by both a sensible range
    and a -1 dBFS sample-peak ceiling.
    """
    if not samples or channels <= 0 or sample_rate <= 0:
        return -120.0, 1.0

    block_values = max(channels, sample_rate * channels // 10)  # 100 ms
    blocks: list[tuple[float, int, float]] = []
    peak = 0
    for start in range(0, len(samples), block_values):
        block = samples[start:start + block_values]
        count = len(block)
        if count <= 0:
            continue
        square_sum = 0.0
        for raw_sample in block:
            sample = int(raw_sample)
            magnitude = abs(sample)
            if magnitude > peak:
                peak = magnitude
            square_sum += float(sample * sample)
        block_db = _mean_square_dbfs(square_sum, count)
        if block_db >= ABSOLUTE_GATE_DBFS:
            blocks.append((square_sum, count, block_db))

    if not blocks or peak <= 0:
        return -120.0, 1.0

    preliminary_sum = sum(item[0] for item in blocks)
    preliminary_count = sum(item[1] for item in blocks)
    preliminary_db = _mean_square_dbfs(preliminary_sum, preliminary_count)
    relative_threshold = preliminary_db - RELATIVE_GATE_DB
    gated = [item for item in blocks if item[2] >= relative_threshold]
    if not gated:
        gated = blocks

    gated_sum = sum(item[0] for item in gated)
    gated_count = sum(item[1] for item in gated)
    loudness_db = _mean_square_dbfs(gated_sum, gated_count)
    desired_gain_db = max(
        MIN_GAIN_DB,
        min(MAX_GAIN_DB, TARGET_LOUDNESS_DBFS - loudness_db),
    )
    desired_gain = 10.0 ** (desired_gain_db / 20.0)
    peak_ceiling = 32767.0 * (10.0 ** (PEAK_CEILING_DBFS / 20.0))
    peak_safe_gain = peak_ceiling / peak
    gain = max(0.01, min(desired_gain, peak_safe_gain))
    return loudness_db, gain
