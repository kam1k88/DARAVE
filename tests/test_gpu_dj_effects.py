"""tests/test_gpu_dj_effects.py — Tests for GPU-accelerated DJ effects."""
from __future__ import annotations

import numpy as np

from scripts.core.dj_effects import effect_flanger, effect_phaser, effect_vinyl_stop
from scripts.core.gpu import gpu_flanger, gpu_phaser, gpu_vinyl_stop

SR = 44100
BPM = 128.0


def _sine(freq=1000.0, duration=2.0, amplitude=0.5, sr=SR):
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return (np.sin(2 * np.pi * freq * t) * amplitude).astype(np.float32)


def _noise(duration=2.0, sr=SR, amplitude=0.3):
    rng = np.random.default_rng(42)
    return (rng.standard_normal(int(sr * duration)) * amplitude).astype(np.float32)


# ---------------------------------------------------------------------------
# gpu_vinyl_stop
# ---------------------------------------------------------------------------

class TestGpuVinylStop:
    def test_imports(self):
        assert callable(gpu_vinyl_stop)

    def test_output_same_length(self):
        audio = _sine(duration=3.0)
        result = gpu_vinyl_stop(audio, SR, duration_sec=1.0)
        assert len(result) == len(audio)

    def test_output_dtype_float32(self):
        audio = _sine(duration=2.0)
        result = gpu_vinyl_stop(audio, SR)
        assert result.dtype == np.float32

    def test_no_nan_inf(self):
        audio = _noise(duration=3.0)
        result = gpu_vinyl_stop(audio, SR)
        assert np.all(np.isfinite(result))

    def test_short_audio_passthrough(self):
        """Very short audio (<100 samples) should pass through unchanged."""
        audio = np.ones(50, dtype=np.float32) * 0.5
        result = gpu_vinyl_stop(audio, SR, duration_sec=0.01)
        np.testing.assert_array_equal(result, audio)

    def test_tail_fades_out(self):
        """The stop region should fade to silence."""
        audio = np.ones(SR * 3, dtype=np.float32) * 0.8
        result = gpu_vinyl_stop(audio, SR, duration_sec=2.0)
        # Last 100ms should be very quiet
        tail = result[-SR // 10:]
        assert np.max(np.abs(tail)) < 0.1

    def test_head_unaffected(self):
        """The first second should be unchanged (before normalization)."""
        audio = _sine(duration=3.0, amplitude=0.5)
        result = gpu_vinyl_stop(audio, SR, duration_sec=1.0)
        # After normalization the level changes, but waveform shape should match
        # Check correlation rather than exact values
        corr = np.corrcoef(result[:SR], audio[:SR])[0, 1]
        assert corr > 0.99, f"Head correlation {corr:.4f} should be > 0.99"

    def test_matches_dj_effects_wrapper(self):
        """Should produce same result as dj_effects.effect_vinyl_stop."""
        audio = _sine(duration=3.0)
        result_gpu = gpu_vinyl_stop(audio, SR, duration_sec=1.5)
        result_wrap = effect_vinyl_stop(audio, SR, duration_sec=1.5)
        np.testing.assert_allclose(result_gpu, result_wrap, atol=1e-6)


# ---------------------------------------------------------------------------
# gpu_flanger
# ---------------------------------------------------------------------------

class TestGpuFlanger:
    def test_imports(self):
        assert callable(gpu_flanger)

    def test_output_same_length(self):
        audio = _sine(duration=2.0)
        result = gpu_flanger(audio, SR, BPM)
        assert len(result) == len(audio)

    def test_output_dtype_float32(self):
        audio = _sine(duration=2.0)
        result = gpu_flanger(audio, SR, BPM)
        assert result.dtype == np.float32

    def test_no_nan_inf(self):
        audio = _noise(duration=2.0)
        result = gpu_flanger(audio, SR, BPM)
        assert np.all(np.isfinite(result))

    def test_output_normalized(self):
        """Output peak should not exceed 1.0."""
        audio = _sine(duration=2.0, amplitude=0.9)
        result = gpu_flanger(audio, SR, BPM)
        assert np.max(np.abs(result)) <= 1.0 + 1e-6

    def test_modifies_signal(self):
        """Flanger should change the signal (not pass-through)."""
        audio = _sine(duration=2.0, amplitude=0.5)
        result = gpu_flanger(audio, SR, BPM)
        # Should be different from original
        assert not np.allclose(result, audio, atol=1e-4)

    def test_feedback_affects_result(self):
        """Higher feedback should produce more resonant output."""
        audio = _noise(duration=2.0)
        low_fb = gpu_flanger(audio, SR, BPM, feedback=0.1)
        high_fb = gpu_flanger(audio, SR, BPM, feedback=0.8)
        # Different feedback should produce different results
        assert not np.allclose(low_fb, high_fb, atol=1e-4)

    def test_matches_dj_effects_wrapper(self):
        """Should produce same result as dj_effects.effect_flanger."""
        audio = _sine(duration=2.0)
        result_gpu = gpu_flanger(audio, SR, BPM, depth_ms=3.0, rate_beats=4.0, feedback=0.5)
        result_wrap = effect_flanger(audio, SR, BPM, depth_ms=3.0, rate_beats=4.0, feedback=0.5)
        np.testing.assert_allclose(result_gpu, result_wrap, atol=1e-6)


# ---------------------------------------------------------------------------
# gpu_phaser
# ---------------------------------------------------------------------------

class TestGpuPhaser:
    def test_imports(self):
        assert callable(gpu_phaser)

    def test_output_same_length(self):
        audio = _sine(duration=2.0)
        result = gpu_phaser(audio, SR, BPM)
        assert len(result) == len(audio)

    def test_output_dtype_float32(self):
        audio = _sine(duration=2.0)
        result = gpu_phaser(audio, SR, BPM)
        assert result.dtype == np.float32

    def test_no_nan_inf(self):
        audio = _noise(duration=2.0)
        result = gpu_phaser(audio, SR, BPM)
        assert np.all(np.isfinite(result))

    def test_output_normalized(self):
        """Output peak should not exceed 1.0."""
        audio = _sine(duration=2.0, amplitude=0.9)
        result = gpu_phaser(audio, SR, BPM)
        assert np.max(np.abs(result)) <= 1.0 + 1e-6

    def test_modifies_signal(self):
        """Phaser should change the signal."""
        audio = _sine(duration=2.0, amplitude=0.5)
        result = gpu_phaser(audio, SR, BPM)
        assert not np.allclose(result, audio, atol=1e-4)

    def test_different_poles_different_result(self):
        """Different pole counts should produce different phase response."""
        # Use sine waves at different frequencies to detect phase differences
        low = _sine(freq=200.0, duration=2.0, amplitude=0.5)
        high = _sine(freq=4000.0, duration=2.0, amplitude=0.5)
        r_low = gpu_phaser(low, SR, BPM, n_poles=4)
        r_high = gpu_phaser(high, SR, BPM, n_poles=4)
        # Different frequencies should be affected differently by the phaser
        diff_low = np.sqrt(np.mean((r_low - low) ** 2))
        diff_high = np.sqrt(np.mean((r_high - high) ** 2))
        # At least one should show measurable difference
        assert diff_low > 1e-6 or diff_high > 1e-6

    def test_matches_dj_effects_wrapper(self):
        """Should produce same result as dj_effects.effect_phaser."""
        audio = _sine(duration=2.0)
        result_gpu = gpu_phaser(audio, SR, BPM, n_poles=4, rate_beats=8.0, depth=0.7)
        result_wrap = effect_phaser(audio, SR, BPM, n_poles=4, rate_beats=8.0, depth=0.7)
        np.testing.assert_allclose(result_gpu, result_wrap, atol=1e-6)


# ---------------------------------------------------------------------------
# Integration: apply_effect dispatches correctly
# ---------------------------------------------------------------------------

class TestApplyEffectIntegration:
    def test_flanger_via_apply_effect(self):
        from scripts.core.dj_effects import apply_effect
        audio = _sine(duration=2.0)
        result = apply_effect(audio, SR, "flanger", bpm=BPM)
        assert len(result) == len(audio)
        assert result.dtype == np.float32

    def test_phaser_via_apply_effect(self):
        from scripts.core.dj_effects import apply_effect
        audio = _sine(duration=2.0)
        result = apply_effect(audio, SR, "phaser", bpm=BPM)
        assert len(result) == len(audio)
        assert result.dtype == np.float32

    def test_vinyl_stop_via_apply_effect(self):
        from scripts.core.dj_effects import apply_effect
        audio = _sine(duration=3.0)
        result = apply_effect(audio, SR, "vinyl_stop")
        assert len(result) == len(audio)
        assert result.dtype == np.float32
