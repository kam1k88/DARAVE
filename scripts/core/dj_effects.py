"""
scripts/core/dj_effects.py — DJ transition effects library.

Implements the most popular DJ mixing effects used in live sets and transitions:

  LOOP      — Beat-synced short loop (stutter/repeat)
  ECHO      — Beat-synced ping-pong delay (already in dj_engine, refactored here)
  WOBBLE    — LFO-modulated volume/filter (dubstep-style wobble)
  SLICER    — Chop audio into beat-synced slices, rearrange/repeat
  FLANGER   — Comb filter sweep (jet plane effect)
  PHASER    — All-pass filter sweep (swirling effect)
  VINYL_STOP — Turntable power-down simulation
  BITCRUSH  — Sample rate/bit depth reduction (lo-fi effect)
  REVERB    — Diffuse reverb tail (already in dj_engine, refactored here)
  FILTER    — Low/high-pass sweep (already in dj_engine, refactored here)

Usage:
    from scripts.core.dj_effects import apply_effect, EFFECTS
    result = apply_effect(audio, sr, "wobble", bpm=128, depth=0.7)
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

import numpy as np

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Effect registry
# ---------------------------------------------------------------------------

EFFECTS: Dict[str, str] = {
    "loop":        "Beat-synced short loop (stutter/repeat)",
    "echo":        "Beat-synced ping-pong delay",
    "wobble":      "LFO-modulated volume/filter (dubstep wobble)",
    "slicer":      "Chop into beat slices, rearrange/repeat",
    "flanger":     "Comb filter sweep (jet plane)",
    "phaser":      "All-pass filter sweep (swirling)",
    "vinyl_stop":  "Turntable power-down simulation",
    "bitcrush":    "Sample rate reduction (lo-fi)",
    "reverb":      "Diffuse reverb tail",
    "filter":      "Low/high-pass sweep",
    "reverse":     "Audio reverse (backwards playback)",
    "scratch":     "Digital scratch simulation (reverse + pitch ramp)",
    "texture":     "Atmospheric texture layering (synthesized noise)",
    "pitch_bend":  "Pitch bend (key shift without tempo change)",
    "glitch":      "Time-stretch glitch (extreme stretch artifacts)",
    "none":        "No effect (pass-through)",
}

# ---------------------------------------------------------------------------
# DSP helpers
# ---------------------------------------------------------------------------

def _ensure_float(audio: np.ndarray) -> np.ndarray:
    if audio.dtype == np.int16:
        return audio.astype(np.float32) / 32768.0
    if audio.dtype == np.int32:
        return audio.astype(np.float32) / 2147483648.0
    return audio.astype(np.float32)


def _normalize(audio: np.ndarray, peak: float = 1.0) -> np.ndarray:
    p = float(np.abs(audio).max())
    if p > peak:
        audio = audio * (peak / p)
    return audio


def _beat_samples(sr: int, bpm: float) -> int:
    return max(1, int(sr * 60.0 / bpm))


def _bar_samples(sr: int, bpm: float) -> int:
    return _beat_samples(sr, bpm) * 4


# ---------------------------------------------------------------------------
# Individual effects
# ---------------------------------------------------------------------------

def effect_loop(
    audio: np.ndarray,
    sr: int,
    bpm: float,
    beats: int = 4,
    repeats: int = 2,
    fade_ms: float = 10.0,
) -> np.ndarray:
    """
    Beat-synced short loop (stutter effect).

    Repeats the last `beats` beats of the audio `repeats` times.
    Common DJ technique: loop the last bar before a drop.

    Args:
        audio:   Input audio (mono float32).
        sr:      Sample rate.
        bpm:     Tempo for beat calculation.
        beats:   Number of beats to loop (1-8, default 4 = one bar).
        repeats: How many times to repeat the loop.
        fade_ms: Crossfade ms between loop iterations to avoid clicks.
    """
    audio = _ensure_float(audio)
    beat_s = _beat_samples(sr, bpm)
    loop_len = beat_s * max(1, min(beats, 8))
    fade_s = int(sr * fade_ms / 1000.0)

    if loop_len >= len(audio):
        return audio

    loop_section = audio[-loop_len:].copy()

    # Build repeated loop
    result_parts = []
    for r in range(repeats):
        part = loop_section.copy()
        # Apply fade-in on first iteration, crossfade between iterations
        if r > 0 and fade_s > 0 and fade_s < loop_len:
            part[:fade_s] *= np.linspace(0, 1, fade_s, dtype=np.float32)
            # Crossfade overlap with previous
            if result_parts:
                prev = result_parts[-1]
                overlap_start = max(0, len(prev) - fade_s)
                prev[overlap_start:] *= np.linspace(1, 0, len(prev) - overlap_start, dtype=np.float32)
        result_parts.append(part)

    looped = np.concatenate(result_parts)

    # Append the loop after the original audio (minus the loop section)
    prefix = audio[:-loop_len] if loop_len < len(audio) else np.array([], dtype=np.float32)
    result = np.concatenate([prefix, looped])
    return _normalize(result)


def effect_echo(
    audio: np.ndarray,
    sr: int,
    bpm: float,
    decay: float = 0.4,
    n_echoes: int = 4,
    ping_pong: bool = True,
) -> np.ndarray:
    """
    Beat-synced ping-pong delay echo.

    Each echo is placed one beat apart and attenuated by `decay` per step.
    Ping-pong mode alternates echoes between left/right channels (simulated
    on mono by alternating sign).

    Args:
        audio:      Input audio (mono float32).
        sr:         Sample rate.
        bpm:        Tempo for echo spacing.
        decay:      Amplitude decay per echo (0.4 ≈ -8 dB/echo).
        n_echoes:   Number of echoes (default 4 = one bar at 4/4).
        ping_pong:  Alternate echo polarity for stereo-like effect.
    """
    audio = _ensure_float(audio)
    beat_s = _beat_samples(sr, bpm)
    result = audio.copy()

    for i in range(1, n_echoes + 1):
        delay = i * beat_s
        gain = decay ** i
        # Ping-pong: alternate polarity
        if ping_pong and i % 2 == 1:
            gain = -gain
        if delay < len(result):
            result[delay:] += audio[:len(result) - delay] * gain

    return _normalize(result)


def effect_wobble(
    audio: np.ndarray,
    sr: int,
    bpm: float,
    depth: float = 0.7,
    rate: float = 0.5,
    filter_mode: bool = True,
) -> np.ndarray:
    """
    LFO-modulated volume/filter wobble (dubstep-style).

    Uses a sine LFO to modulate either volume or a low-pass filter cutoff,
    creating the characteristic "wub wub" sound.

    Args:
        audio:       Input audio (mono float32).
        sr:          Sample rate.
        bpm:         Tempo (wobble rate synced to beat fractions).
        depth:       Modulation depth 0-1 (0.7 = 70% modulation).
        rate:        Wobble rate as fraction of beat (0.25 = 1/4 beat, 0.5 = half beat, 1.0 = full beat).
        filter_mode: True = filter modulation, False = volume modulation.
    """
    audio = _ensure_float(audio)
    n = len(audio)
    beat_s = _beat_samples(sr, bpm)

    # LFO frequency: rate beats per beat = rate * bpm / 60 Hz
    lfo_freq = rate * bpm / 60.0
    t = np.arange(n, dtype=np.float32) / sr
    lfo = np.sin(2.0 * np.pi * lfo_freq * t)  # -1 to +1

    if filter_mode:
        # Modulate low-pass filter cutoff between 200 Hz and 8000 Hz
        try:
            from scipy.signal import butter, lfilter
            base_cutoff = 200.0
            sweep_range = 7800.0
            result = np.empty_like(audio)
            chunk_size = max(256, beat_s // 4)
            pos = 0
            while pos < n:
                end = min(pos + chunk_size, n)
                mid_lfo = float(np.mean(lfo[pos:end]))
                cutoff = base_cutoff + (mid_lfo * 0.5 + 0.5) * sweep_range * depth
                cutoff = max(100.0, min(12000.0, cutoff))
                nyq = sr / 2.0
                normal = max(0.001, min(0.999, cutoff / nyq))
                b, a = butter(2, normal, btype="low")
                result[pos:end] = lfilter(b, a, audio[pos:end]).astype(np.float32)
                pos = end
            return _normalize(result)
        except ImportError:
            # Fallback to volume modulation
            pass

    # Volume modulation
    mod = 1.0 - depth * (lfo * 0.5 + 0.5)  # 1-depth to 1
    return _normalize(audio * mod.astype(np.float32))


def effect_slicer(
    audio: np.ndarray,
    sr: int,
    bpm: float,
    slice_beats: int = 2,
    shuffle: float = 0.3,
    repeat_slice: int = 0,
) -> np.ndarray:
    """
    Beat-synced slicer — chop audio into slices and rearrange.

    Cuts the audio into beat-aligned slices, optionally shuffles their order,
    and can repeat a specific slice for a stutter effect.

    Args:
        audio:         Input audio (mono float32).
        sr:            Sample rate.
        bpm:           Tempo for beat alignment.
        slice_beats:   Beats per slice (1, 2, or 4).
        shuffle:       Shuffle amount 0-1 (0 = no shuffle, 1 = full random).
        repeat_slice:  Index of slice to repeat once (0 = no repeat).
    """
    audio = _ensure_float(audio)
    beat_s = _beat_samples(sr, bpm)
    slice_len = beat_s * max(1, min(slice_beats, 4))

    # Pad to full slices
    n = len(audio)
    n_slices = max(1, n // slice_len)
    padded_len = n_slices * slice_len
    padded = audio[:padded_len] if padded_len <= n else np.pad(audio, (0, padded_len - n))

    # Split into slices
    slices = [padded[i * slice_len:(i + 1) * slice_len] for i in range(n_slices)]

    # Shuffle
    if shuffle > 0 and n_slices > 1:
        rng = np.random.default_rng(42)
        n_shuffle = max(1, int(n_slices * shuffle))
        indices = list(range(n_slices))
        for _ in range(n_shuffle):
            a, b = rng.choice(n_slices, 2, replace=False)
            indices[a], indices[b] = indices[b], indices[a]
        slices = [slices[i] for i in indices]

    # Repeat a slice
    if 0 < repeat_slice < len(slices):
        slices.insert(repeat_slice, slices[repeat_slice].copy())

    result = np.concatenate(slices)[:n]
    return _normalize(result)


def effect_flanger(
    audio: np.ndarray,
    sr: int,
    bpm: float,
    depth_ms: float = 3.0,
    rate_beats: float = 4.0,
    feedback: float = 0.5,
) -> np.ndarray:
    """
    Flanger effect — comb filter with LFO-modulated delay.

    Creates the classic "jet plane" sweep by mixing the audio with a
    slightly delayed copy whose delay varies over time.

    Args:
        audio:      Input audio (mono float32).
        sr:         Sample rate.
        bpm:        Tempo (rate_beats = delay modulation period in beats).
        depth_ms:   Max delay in milliseconds (1-10 ms typical).
        rate_beats: LFO period in beats (4 = one bar sweep).
        feedback:   Feedback amount 0-0.9 (higher = more resonant).
    """
    audio = _ensure_float(audio)
    n = len(audio)
    max_delay_s = int(sr * depth_ms / 1000.0)
    lfo_freq = bpm / (60.0 * rate_beats)
    t = np.arange(n, dtype=np.float32) / sr
    lfo = np.sin(2.0 * np.pi * lfo_freq * t)  # -1 to +1

    result = audio.copy()
    delayed = np.zeros(n + max_delay_s * 2, dtype=np.float32)

    for i in range(n):
        delay_samples = int((lfo[i] * 0.5 + 0.5) * max_delay_s)
        if i + delay_samples < len(delayed):
            delayed[i + delay_samples] = audio[i]

    # Apply feedback
    for i in range(max_delay_s, n + max_delay_s):
        if i < len(delayed) and i - max_delay_s >= 0:
            delayed[i] += delayed[i - max_delay_s] * feedback * 0.3

    # Mix dry + wet
    wet = delayed[:n]
    result = audio + wet * 0.4
    return _normalize(result)


def effect_phaser(
    audio: np.ndarray,
    sr: int,
    bpm: float,
    n_poles: int = 4,
    rate_beats: float = 8.0,
    depth: float = 0.7,
) -> np.ndarray:
    """
    Phaser effect — cascaded all-pass filters with LFO modulation.

    Creates a swirling, sweeping sound by modulating the resonant
    frequencies of all-pass filters.

    Args:
        audio:      Input audio (mono float32).
        sr:         Sample rate.
        bpm:        Tempo (rate_beats = sweep period in beats).
        n_poles:    Number of all-pass filter poles (2-6, higher = more peaks).
        rate_beats: LFO period in beats (8 = two bar sweep).
        depth:      Modulation depth 0-1.
    """
    audio = _ensure_float(audio)
    n = len(audio)
    lfo_freq = bpm / (60.0 * rate_beats)
    t = np.arange(n, dtype=np.float32) / sr

    result = audio.copy().astype(np.float64)

    for pole in range(n_poles):
        # All-pass filter coefficient modulation
        lfo = np.sin(2.0 * np.pi * lfo_freq * t + pole * np.pi / n_poles)
        # Map LFO to feedback coefficient (0.1 to 0.7)
        coeff = 0.1 + (lfo * 0.5 + 0.5) * 0.6 * depth

        # Simple first-order all-pass: y[n] = coeff * y[n-1] + x[n] - coeff * x[n-1]
        y = np.zeros(n, dtype=np.float64)
        for i in range(1, n):
            y[i] = coeff[i] * y[i - 1] + result[i] - coeff[i] * result[i - 1]
        result = y

    # Mix dry + wet
    wet = result.astype(np.float32)
    mixed = audio * 0.6 + wet * 0.4
    return _normalize(mixed.astype(np.float32))


def effect_vinyl_stop(
    audio: np.ndarray,
    sr: int,
    duration_sec: float = 2.0,
) -> np.ndarray:
    """
    Turntable power-down simulation.

    Gradually slows down playback speed and pitch until the audio stops,
    simulating a vinyl turntable being turned off.

    Args:
        audio:        Input audio (mono float32).
        sr:           Sample rate.
        duration_sec: How long the slowdown takes (seconds).
    """
    audio = _ensure_float(audio)
    n = len(audio)
    stop_samples = min(n, int(sr * duration_sec))

    if stop_samples < 100:
        return audio

    result = audio.copy()
    # Apply slowdown to the last `stop_samples`
    start = n - stop_samples
    for i in range(stop_samples):
        idx = start + i
        if idx >= n:
            break
        # Speed factor goes from 1.0 to 0.0
        progress = i / stop_samples
        speed = 1.0 - progress ** 2  # Quadratic deceleration
        # Read position advances slower and slower
        src_pos = int(start + i * speed)
        if src_pos < n:
            result[idx] = audio[src_pos] * (1.0 - progress)  # Fade out volume too

    # Add a slight low-pass filter at the end for realism
    try:
        from scipy.signal import butter, lfilter
        nyq = sr / 2.0
        b, a = butter(2, max(0.001, min(0.999, 200.0 / nyq)), btype="low")
        result[start:] = lfilter(b, a, result[start:]).astype(np.float32)
    except ImportError:
        pass

    return _normalize(result)


def effect_bitcrush(
    audio: np.ndarray,
    sr: int,
    bits: int = 4,
    sample_rate_reduction: float = 0.5,
) -> np.ndarray:
    """
    Bitcrusher — sample rate and bit depth reduction.

    Creates a lo-fi, digital distortion effect by reducing the resolution
    of the audio signal.

    Args:
        audio:                Input audio (mono float32).
        sr:                   Sample rate.
        bits:                 Target bit depth (1-16, lower = more crush).
        sample_rate_reduction: Factor to reduce sample rate by (0.1-1.0).
    """
    audio = _ensure_float(audio)
    bits = max(1, min(16, bits))

    # Bit depth reduction
    levels = 2 ** bits
    quantized = np.round(audio * (levels - 1)) / (levels - 1)

    # Sample rate reduction
    if sample_rate_reduction < 1.0:
        skip = max(1, int(1.0 / sample_rate_reduction))
        downsampled = quantized[::skip]
        # Interpolate back to original length
        indices = np.linspace(0, len(downsampled) - 1, len(audio))
        quantized = np.interp(indices, np.arange(len(downsampled)), downsampled).astype(np.float32)

    return _normalize(quantized.astype(np.float32))


def effect_reverb(
    audio: np.ndarray,
    sr: int,
    reverb_time: float = 1.2,
    wet: float = 0.25,
    seed: int = 42,
) -> np.ndarray:
    """
    Diffuse reverb tail.

    Uses a decaying noise impulse response for a natural room/hall sound.

    Args:
        audio:        Input audio (mono float32).
        sr:           Sample rate.
        reverb_time:  RT60 approximation in seconds.
        wet:          Wet/dry mix 0-1.
        seed:         RNG seed for reproducibility.
    """
    try:
        from scipy.signal import fftconvolve
    except ImportError:
        return audio

    audio = _ensure_float(audio)
    rng = np.random.default_rng(seed)
    ir_len = int(sr * reverb_time)
    ir = rng.standard_normal(ir_len).astype(np.float64)
    ir *= np.exp(-3.0 * np.arange(ir_len) / ir_len)
    ir /= np.abs(ir).max() + 1e-10

    wet_sig = fftconvolve(audio.astype(np.float64), ir, mode="full")
    wet_sig = wet_sig[:len(audio)].astype(np.float32)

    result = audio * (1.0 - wet) + wet_sig * wet
    return _normalize(result.astype(np.float32))


def effect_filter(
    audio: np.ndarray,
    sr: int,
    direction: str = "out",
    start_hz: float = 18000.0,
    end_hz: float = 300.0,
    num_chunks: int = 32,
) -> np.ndarray:
    """
    Low-pass filter sweep.

    direction='out': cutoff closes (darkening as track exits).
    direction='in':  cutoff opens (brightening as track enters).
    """
    try:
        from scipy.signal import butter, lfilter
    except ImportError:
        return audio

    audio = _ensure_float(audio)
    n = len(audio)
    if n < 512:
        return audio

    if direction == "in":
        start_hz, end_hz = end_hz, start_hz

    chunk_size = max(128, n // num_chunks)
    out = np.empty_like(audio)
    nyq = sr / 2.0

    pos = 0
    chunk_idx = 0
    while pos < n:
        t = chunk_idx / max(num_chunks - 1, 1)
        cutoff = float(np.clip((start_hz + t * (end_hz - start_hz)) / nyq, 0.001, 0.999))
        end = min(pos + chunk_size, n)
        try:
            b, a = butter(2, cutoff, btype="low")
            out[pos:end] = lfilter(b, a, audio[pos:end]).astype(np.float32)
        except Exception:
            out[pos:end] = audio[pos:end]
        pos += chunk_size
        chunk_idx += 1

    return out.astype(np.float32)


# ---------------------------------------------------------------------------
# New effects for DnB techniques
# ---------------------------------------------------------------------------

def effect_reverse(
    audio: np.ndarray,
    sr: int,
    segment_bars: int = 2,
    bpm: float = 120.0,
    **kwargs,
) -> np.ndarray:
    """
    Reverse audio segment for DNB-13 (Reverse Drop).

    Takes the last `segment_bars` bars and reverses them,
    creating an intriguing backwards sound before a drop.
    """
    audio = _ensure_float(audio)
    bar_s = _bar_samples(sr, bpm)
    seg_len = bar_s * max(1, min(segment_bars, 4))

    if seg_len >= len(audio):
        return np.flip(audio).copy()

    prefix = audio[:-seg_len]
    segment = np.flip(audio[-seg_len:]).copy()
    return _normalize(np.concatenate([prefix, segment]))


def effect_scratch(
    audio: np.ndarray,
    sr: int,
    bpm: float = 120.0,
    scratches: int = 3,
    **kwargs,
) -> np.ndarray:
    """
    Digital scratch simulation for DNB-19 (Scratch In).

    Creates a scratch effect by repeating short reverse segments
    with pitch ramp, simulating vinyl scratching.
    """
    audio = _ensure_float(audio)
    beat_s = _beat_samples(sr, bpm)
    scratch_len = beat_s // 2  # half-beat scratch

    if scratch_len >= len(audio):
        return audio

    result_parts = []
    for i in range(scratches):
        # Each scratch: reverse + pitch ramp (shorter each time)
        seg_len = max(32, int(scratch_len * (1.0 - i * 0.2)))
        seg_len = min(seg_len, len(audio))
        segment = np.flip(audio[:seg_len]).copy()

        # Apply fade-in and fade-out for smooth scratch sound
        fade = min(64, seg_len // 4)
        if fade > 0:
            segment[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
            segment[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)

        result_parts.append(segment)
        # Small gap between scratches
        gap = np.zeros(max(1, beat_s // 8), dtype=np.float32)
        result_parts.append(gap)

    # Add the main audio after scratches
    main_start = min(scratch_len, len(audio))
    result_parts.append(audio[main_start:])

    result = np.concatenate(result_parts)[:len(audio)]
    return _normalize(result)


def effect_texture(
    audio: np.ndarray,
    sr: int,
    intensity: float = 0.15,
    seed: int = 42,
    **kwargs,
) -> np.ndarray:
    """
    Atmospheric texture layering for DNB-18 (Texture Layering).

    Generates a synthesized noise texture and layers it over the audio.
    The texture is bandpass-filtered (500Hz-8kHz) to avoid bass clashes.
    """
    audio = _ensure_float(audio)
    n = len(audio)

    # Generate noise texture
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n).astype(np.float32)

    # Bandpass filter: 500 Hz to 8 kHz
    try:
        from scipy.signal import butter, lfilter
        nyq = sr / 2.0
        low = max(0.001, min(0.999, 500.0 / nyq))
        high = max(0.001, min(0.999, 8000.0 / nyq))
        b, a = butter(2, [low, high], btype="band")
        noise = lfilter(b, a, noise).astype(np.float32)
    except ImportError:
        pass

    # Normalize and apply intensity
    peak = float(np.abs(noise).max())
    if peak > 0:
        noise = noise / peak * intensity

    return _normalize(audio + noise)


def effect_pitch_bend(
    audio: np.ndarray,
    sr: int,
    semitones: float = 2.0,
    bpm: float = 120.0,
    bars: int = 4,
    **kwargs,
) -> np.ndarray:
    """
    Pitch bend for DNB-20 (Tone Play).

    Gradually shifts pitch up or down by `semitones` over `bars` bars.
    Uses resampling to change pitch without changing tempo.
    """
    audio = _ensure_float(audio)
    n = len(audio)
    bar_s = _bar_samples(sr, bpm)
    bend_len = bar_s * max(1, min(bars, 8))
    bend_len = min(bend_len, n)

    if bend_len < 100:
        return audio

    # Split into bend region and tail
    bend_part = audio[:bend_len].copy()
    tail = audio[bend_len:]

    # Apply pitch bend via resampling
    # Positive semitones = pitch up = speed up = shorter
    # Negative semitones = pitch down = slow down = longer
    ratio = 2.0 ** (semitones / 12.0)
    target_len = int(bend_len / ratio)

    if target_len < 100:
        return audio

    # Resample to change pitch
    indices = np.linspace(0, bend_len - 1, target_len)
    bent = np.interp(indices, np.arange(bend_len), bend_part).astype(np.float32)

    # Pad or trim to original length
    if len(bent) < bend_len:
        bent = np.pad(bent, (0, bend_len - len(bent)))
    else:
        bent = bent[:bend_len]

    # Apply fade at the transition point
    fade = min(256, bend_len // 4)
    if fade > 0 and len(tail) > 0:
        bent[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)

    return _normalize(np.concatenate([bent, tail]))


def effect_glitch(
    audio: np.ndarray,
    sr: int,
    bpm: float = 120.0,
    stretch_factor: float = 0.5,
    segment_bars: int = 2,
    **kwargs,
) -> np.ndarray:
    """
    Time-stretch glitch for DNB-16 (Time Stretch Glitch).

    Applies extreme time stretch to a segment, creating digital artifacts
    and 'glitch' sounds characteristic of experimental DnB.
    """
    audio = _ensure_float(audio)
    bar_s = _bar_samples(sr, bpm)
    seg_len = bar_s * max(1, min(segment_bars, 4))
    seg_len = min(seg_len, len(audio))

    if seg_len < 256:
        return audio

    prefix = audio[:len(audio) - seg_len]
    segment = audio[len(audio) - seg_len:]

    # Extreme time stretch via resampling (creates artifacts)
    target_len = int(seg_len * stretch_factor)
    if target_len < 64:
        return audio

    indices = np.linspace(0, seg_len - 1, target_len)
    stretched = np.interp(indices, np.arange(seg_len), segment).astype(np.float32)

    # Add digital artifacts: random amplitude modulation
    rng = np.random.default_rng(42)
    mod_freq = bpm / 60.0 * 4  # 4x beat frequency
    t = np.arange(len(stretched), dtype=np.float32) / sr
    mod = 0.7 + 0.3 * np.sin(2.0 * np.pi * mod_freq * t)
    stretched = (stretched * mod).astype(np.float32)

    # Pad back to original segment length
    if len(stretched) < seg_len:
        stretched = np.pad(stretched, (0, seg_len - len(stretched)))
    else:
        stretched = stretched[:seg_len]

    return _normalize(np.concatenate([prefix, stretched]))


# ---------------------------------------------------------------------------
# Effect dispatcher
# ---------------------------------------------------------------------------

_EFFECT_FUNCS: Dict[str, Callable] = {
    "loop":       effect_loop,
    "echo":       effect_echo,
    "wobble":     effect_wobble,
    "slicer":     effect_slicer,
    "flanger":    effect_flanger,
    "phaser":     effect_phaser,
    "vinyl_stop": effect_vinyl_stop,
    "bitcrush":   effect_bitcrush,
    "reverb":     effect_reverb,
    "filter":     effect_filter,
    "reverse":    effect_reverse,
    "scratch":    effect_scratch,
    "texture":    effect_texture,
    "pitch_bend": effect_pitch_bend,
    "glitch":     effect_glitch,
}


def apply_effect(
    audio: np.ndarray,
    sr: int,
    effect: str,
    bpm: float = 120.0,
    **kwargs,
) -> np.ndarray:
    """
    Apply a named effect to audio.

    Parameters
    ----------
    audio  : Input audio (mono float32).
    sr     : Sample rate.
    effect : Effect name (see EFFECTS dict for options).
    bpm    : Tempo for beat-synced effects.
    **kwargs : Additional effect-specific parameters.

    Returns
    -------
    np.ndarray — Processed audio.
    """
    if effect == "none" or effect not in _EFFECT_FUNCS:
        return audio

    func = _EFFECT_FUNCS[effect]
    try:
        return func(audio, sr, bpm=bpm, **kwargs)
    except TypeError:
        # Fallback: some effects don't take bpm
        return func(audio, sr, **kwargs)
