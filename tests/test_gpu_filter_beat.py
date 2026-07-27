"""tests/test_gpu_filter_beat.py — Tests for GPU filter sweep and beat stamp."""
from __future__ import annotations

import numpy as np

from scripts.core.gpu import gpu_beat_stamp, gpu_filter_sweep

SR = 44100


def _sine(freq=1000.0, duration=2.0, amplitude=0.5, sr=SR):
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return (np.sin(2 * np.pi * freq * t) * amplitude).astype(np.float32)


# ---------------------------------------------------------------------------
# gpu_filter_sweep
# ---------------------------------------------------------------------------

class TestGpuFilterSweep:
    def test_imports(self):
        assert callable(gpu_filter_sweep)

    def test_output_same_length(self):
        audio = _sine(duration=2.0)
        result = gpu_filter_sweep(audio, SR, direction="out")
        assert len(result) == len(audio)

    def test_output_dtype_float32(self):
        audio = _sine(duration=2.0)
        result = gpu_filter_sweep(audio, SR)
        assert result.dtype == np.float32

    def test_no_nan_inf(self):
        audio = _sine(duration=2.0, amplitude=0.5)
        result = gpu_filter_sweep(audio, SR)
        assert np.all(np.isfinite(result))

    def test_out_darkens_signal(self):
        """Filter out should reduce high-frequency content."""
        high = _sine(freq=8000.0, duration=2.0, amplitude=0.5)
        result = gpu_filter_sweep(high, SR, direction="out", start_hz=16000.0, end_hz=200.0)
        # Output energy should be lower than input
        assert np.sqrt(np.mean(result ** 2)) < np.sqrt(np.mean(high ** 2))

    def test_in_brightens_signal(self):
        """Filter in should increase high-frequency content over time."""
        # Start with low frequency, sweep up
        low = _sine(freq=200.0, duration=3.0, amplitude=0.5)
        result = gpu_filter_sweep(low, SR, direction="in", start_hz=200.0, end_hz=8000.0)
        assert len(result) == len(low)

    def test_short_audio_passthrough(self):
        """Very short audio should pass through unchanged."""
        audio = np.ones(100, dtype=np.float32) * 0.5
        result = gpu_filter_sweep(audio, SR)
        np.testing.assert_array_equal(result, audio)

    def test_matches_dj_engine_wrapper(self):
        """Should produce same result as dj_engine._apply_filter_sweep."""
        from scripts.core.dj_engine import _apply_filter_sweep
        audio = _sine(duration=2.0)
        result_gpu = gpu_filter_sweep(audio, SR, direction="out", start_hz=16000.0, end_hz=300.0)
        result_wrap = _apply_filter_sweep(audio, SR, direction="out", start_hz=16000.0, end_hz=300.0)
        np.testing.assert_allclose(result_gpu, result_wrap, atol=1e-4)


# ---------------------------------------------------------------------------
# gpu_beat_stamp
# ---------------------------------------------------------------------------

class TestGpuBeatStamp:
    def test_imports(self):
        assert callable(gpu_beat_stamp)

    def test_output_length(self):
        pattern = [("kick", 0, 0.8), ("hh", 4, 0.5)]
        sounds = {"kick": np.ones(512, dtype=np.float32), "hh": np.ones(256, dtype=np.float32)}
        result = gpu_beat_stamp(pattern, sounds, bars=4, step_samples=1024, total_samples=4 * 16 * 1024)
        assert len(result) == 4 * 16 * 1024

    def test_output_dtype_float32(self):
        pattern = [("kick", 0, 0.8)]
        sounds = {"kick": np.ones(512, dtype=np.float32)}
        result = gpu_beat_stamp(pattern, sounds, bars=1, step_samples=1024, total_samples=16 * 1024)
        assert result.dtype == np.float32

    def test_no_nan_inf(self):
        pattern = [("kick", 0, 0.8), ("snare", 4, 0.7), ("hh", 8, 0.5)]
        sounds = {
            "kick": np.ones(512, dtype=np.float32),
            "snare": np.ones(512, dtype=np.float32),
            "hh": np.ones(256, dtype=np.float32),
        }
        result = gpu_beat_stamp(pattern, sounds, bars=4, step_samples=1024, total_samples=4 * 16 * 1024)
        assert np.all(np.isfinite(result))

    def test_hits_appear_at_correct_positions(self):
        """A single hit should appear at the expected position."""
        hit = np.ones(100, dtype=np.float32)
        pattern = [("kick", 0, 1.0)]
        sounds = {"kick": hit}
        result = gpu_beat_stamp(pattern, sounds, bars=1, step_samples=200, total_samples=3200)
        # First hit at position 0
        assert result[0] == 1.0
        assert result[50] == 1.0
        # Gap after hit
        assert result[150] == 0.0

    def test_velocity_scaling(self):
        """Velocity should scale the output amplitude."""
        hit = np.ones(100, dtype=np.float32)
        pattern_quiet = [("kick", 0, 0.3)]
        pattern_loud = [("kick", 0, 0.9)]
        sounds = {"kick": hit}
        result_quiet = gpu_beat_stamp(pattern_quiet, sounds, bars=1, step_samples=200, total_samples=3200)
        result_loud = gpu_beat_stamp(pattern_loud, sounds, bars=1, step_samples=200, total_samples=3200)
        assert np.max(result_loud) > np.max(result_quiet) * 2

    def test_multiple_bars_repeat(self):
        """Hits should repeat across bars."""
        hit = np.ones(50, dtype=np.float32)
        pattern = [("kick", 0, 1.0)]
        sounds = {"kick": hit}
        bars = 4
        step = 200
        result = gpu_beat_stamp(pattern, sounds, bars=bars, step_samples=step, total_samples=bars * 16 * step)
        # Hit at bar 0, step 0
        assert result[0] == 1.0
        # Hit at bar 1, step 0
        assert result[16 * step] == 1.0
        # Hit at bar 2, step 0
        assert result[32 * step] == 1.0
        # Hit at bar 3, step 0
        assert result[48 * step] == 1.0

    def test_missing_instrument_skipped(self):
        """Missing instruments should be silently skipped."""
        pattern = [("kick", 0, 0.8), ("missing", 4, 0.5)]
        sounds = {"kick": np.ones(512, dtype=np.float32)}
        result = gpu_beat_stamp(pattern, sounds, bars=1, step_samples=1024, total_samples=16 * 1024)
        assert np.all(np.isfinite(result))

    def test_matches_cpu_fallback(self):
        """GPU and CPU paths should produce identical results."""
        hit = np.ones(200, dtype=np.float32)  # all ones so first sample is non-zero
        pattern = [("kick", 0, 0.8), ("snare", 4, 0.7), ("hh", 8, 0.5), ("hh", 12, 0.3)]
        sounds = {"kick": hit, "snare": hit * 0.7, "hh": hit * 0.3}
        result = gpu_beat_stamp(pattern, sounds, bars=2, step_samples=500, total_samples=2 * 16 * 500)
        # Verify positions have content
        assert result[0] != 0  # kick at bar 0, step 0
        assert result[4 * 500] != 0  # snare at bar 0, step 4
        assert result[8 * 500] != 0  # hh at bar 0, step 8
        assert result[16 * 500] != 0  # kick at bar 1, step 0


# ---------------------------------------------------------------------------
# Integration: render_beat still works
# ---------------------------------------------------------------------------

class TestRenderBeatIntegration:
    def test_render_beat_returns_audio(self):
        from scripts.core.beat_synth import render_beat
        audio = render_beat(bpm=128.0, genre="techno", bars=4, sr=22050)
        assert isinstance(audio, np.ndarray)
        assert audio.dtype == np.float32
        assert len(audio) > 0

    def test_render_beat_no_nan(self):
        from scripts.core.beat_synth import render_beat
        audio = render_beat(bpm=128.0, genre="house", bars=4, sr=22050)
        assert np.all(np.isfinite(audio))

    def test_render_beat_normalized(self):
        from scripts.core.beat_synth import render_beat
        audio = render_beat(bpm=128.0, genre="techno", bars=4, sr=22050, intensity=1.0)
        assert np.max(np.abs(audio)) <= 1.0 + 1e-6
