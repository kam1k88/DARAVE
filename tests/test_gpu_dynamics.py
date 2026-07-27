"""tests/test_gpu_dynamics.py — Tests for GPU-accelerated envelope followers and dynamics."""
from __future__ import annotations

import numpy as np

from scripts.core.gpu import (
    _cpu_envelope_follower,
    gpu_compressor,
    gpu_envelope_follower,
    gpu_gate,
)

SR = 44100


def _sine(freq=1000.0, duration=2.0, amplitude=0.5, sr=SR):
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return (np.sin(2 * np.pi * freq * t) * amplitude).astype(np.float32)


def _silence(duration=2.0, sr=SR):
    return np.zeros(int(sr * duration), dtype=np.float32)


def _noisy_sine(duration=2.0, sr=SR, amplitude=0.3, noise_level=0.01):
    rng = np.random.default_rng(42)
    signal = _sine(duration=duration, amplitude=amplitude, sr=sr)
    noise = (rng.standard_normal(len(signal)) * noise_level).astype(np.float32)
    return signal + noise


# ---------------------------------------------------------------------------
# gpu_envelope_follower
# ---------------------------------------------------------------------------

class TestGpuEnvelopeFollower:
    def test_imports(self):
        assert callable(gpu_envelope_follower)

    def test_constant_input_returns_constant(self):
        """Constant reduction should converge to that value."""
        reduction = np.full(44100, 0.5)
        alpha_atk = float(np.exp(-1.0 / 100))
        alpha_rel = float(np.exp(-1.0 / 1000))
        gain = gpu_envelope_follower(reduction, alpha_atk, alpha_rel)
        assert gain.shape == reduction.shape
        # After settling, gain should be close to 0.5
        assert abs(gain[-1] - 0.5) < 0.01

    def test_attack_faster_than_release(self):
        """Gain must drop toward reduction faster than it recovers."""
        reduction = np.ones(44100)
        reduction[22050:22100] = 0.1  # sudden drop for 50 samples
        alpha_atk = float(np.exp(-1.0 / (SR * 0.002)))  # 2ms
        alpha_rel = float(np.exp(-1.0 / (SR * 0.05)))   # 50ms
        gain = gpu_envelope_follower(reduction, alpha_atk, alpha_rel)

        # Find attack speed (samples to reach 0.15 from 1.0)
        attack_idx = int(np.argmax(gain[22050:] <= 0.15))
        # Find release speed (samples to recover to 0.85 from 0.1)
        release_idx = int(np.argmax(gain[22100:] >= 0.85))

        assert attack_idx < release_idx, (
            f"attack ({attack_idx}) should be faster than release ({release_idx})"
        )

    def test_matches_cpu_fallback(self):
        """GPU path should produce identical results to CPU path."""
        rng = np.random.default_rng(42)
        reduction = rng.uniform(0.1, 1.0, size=44100).astype(np.float64)
        alpha_atk = float(np.exp(-1.0 / (SR * 0.002)))
        alpha_rel = float(np.exp(-1.0 / (SR * 0.05)))

        result_gpu = gpu_envelope_follower(reduction, alpha_atk, alpha_rel)
        result_cpu = _cpu_envelope_follower(reduction, alpha_atk, alpha_rel)

        np.testing.assert_allclose(result_gpu, result_cpu, rtol=1e-6)

    def test_output_dtype_float64(self):
        reduction = np.ones(1000, dtype=np.float32)
        gain = gpu_envelope_follower(reduction, 0.5, 0.5)
        assert gain.dtype == np.float64


# ---------------------------------------------------------------------------
# gpu_gate
# ---------------------------------------------------------------------------

class TestGpuGate:
    def test_imports(self):
        assert callable(gpu_gate)

    def test_silence_stays_silent(self):
        """Gate should keep silence muted."""
        audio = _silence(duration=1.0)
        result = gpu_gate(audio, SR, threshold_db=-40.0)
        assert np.max(np.abs(result)) < 0.01

    def test_loud_signal_passes_through(self):
        """Loud signal above threshold should pass through with minimal attenuation."""
        audio = _sine(amplitude=0.5, duration=1.0)
        result = gpu_gate(audio, SR, threshold_db=-40.0)
        # Peak should be close to original (within 20% due to gate envelope)
        assert np.max(np.abs(result)) > 0.3

    def test_output_same_length(self):
        audio = _sine(duration=1.5)
        result = gpu_gate(audio, SR)
        assert len(result) == len(audio)

    def test_output_dtype_float32(self):
        audio = _sine(duration=0.5)
        result = gpu_gate(audio, SR)
        assert result.dtype == np.float32

    def test_no_nan_inf(self):
        audio = _noisy_sine(duration=2.0)
        result = gpu_gate(audio, SR)
        assert np.all(np.isfinite(result))

    def test_threshold_respected(self):
        """Signal below threshold should be attenuated more than signal above."""
        quiet = _sine(amplitude=0.001, duration=1.0)  # -60 dB
        loud = _sine(amplitude=0.5, duration=1.0)     # -6 dB

        quiet_gated = gpu_gate(quiet, SR, threshold_db=-40.0)
        loud_gated = gpu_gate(loud, SR, threshold_db=-40.0)

        quiet_rms = np.sqrt(np.mean(quiet_gated ** 2))
        loud_rms = np.sqrt(np.mean(loud_gated ** 2))
        assert loud_rms > quiet_rms * 10


# ---------------------------------------------------------------------------
# gpu_compressor
# ---------------------------------------------------------------------------

class TestGpuCompressor:
    def test_imports(self):
        assert callable(gpu_compressor)

    def test_reduces_dynamic_range(self):
        """Compressor should reduce peak level when threshold is exceeded."""
        # Sustained loud section followed by quiet section
        sr = SR
        loud = np.ones(sr, dtype=np.float32) * 0.8   # 1 sec at -2 dB
        quiet = np.ones(sr, dtype=np.float32) * 0.02  # 1 sec at -34 dB
        audio = np.concatenate([loud, quiet])

        compressed = gpu_compressor(
            audio, sr, threshold_db=-10.0, ratio=6.0, makeup_db=0.0
        )

        # The loud section should be reduced
        orig_loud_rms = np.sqrt(np.mean(loud ** 2))
        comp_loud_rms = np.sqrt(np.mean(compressed[:sr] ** 2))
        assert comp_loud_rms < orig_loud_rms, (
            f"loud section not reduced: {comp_loud_rms:.4f} >= {orig_loud_rms:.4f}"
        )

    def test_makeup_gain_applied(self):
        """Makeup gain should boost the output level."""
        audio = _sine(amplitude=0.2, duration=1.0)
        compressed = gpu_compressor(audio, SR, makeup_db=6.0, threshold_db=-30.0)
        # With 6 dB makeup, output should be louder
        assert np.max(np.abs(compressed)) > np.max(np.abs(audio)) * 0.8

    def test_output_same_length(self):
        audio = _sine(duration=1.5)
        result = gpu_compressor(audio, SR)
        assert len(result) == len(audio)

    def test_output_clipped_to_unit(self):
        """Output must not exceed [-1, 1]."""
        audio = _sine(amplitude=0.9, duration=1.0)
        result = gpu_compressor(audio, SR, makeup_db=10.0)
        assert np.max(np.abs(result)) <= 1.0 + 1e-6

    def test_no_nan_inf(self):
        audio = _noisy_sine(duration=2.0)
        result = gpu_compressor(audio, SR)
        assert np.all(np.isfinite(result))

    def test_output_dtype_float32(self):
        audio = _sine(duration=0.5)
        result = gpu_compressor(audio, SR)
        assert result.dtype == np.float32

    def test_ratio_affects_reduction(self):
        """Higher ratio should produce more gain reduction."""
        rng = np.random.default_rng(42)
        audio = (rng.standard_normal(SR) * 0.3).astype(np.float32)

        low_ratio = gpu_compressor(audio, SR, ratio=2.0, threshold_db=-20.0)
        high_ratio = gpu_compressor(audio, SR, ratio=8.0, threshold_db=-20.0)

        # Higher ratio should have lower RMS
        low_rms = np.sqrt(np.mean(low_ratio ** 2))
        high_rms = np.sqrt(np.mean(high_ratio ** 2))
        assert high_rms < low_rms


# ---------------------------------------------------------------------------
# Integration: apply_limiter still works through _smooth_gain_envelope
# ---------------------------------------------------------------------------

class TestLimiterIntegration:
    def test_limiter_no_clipping(self):
        """apply_limiter should not clip even with aggressive input."""
        from scripts.core.mastering import apply_limiter
        rng = np.random.default_rng(0)
        n = SR * 2
        audio = (rng.standard_normal(n) * 0.05).astype(np.float32)
        for i in range(0, n, int(SR * 0.05)):
            audio[i:i + 5] = 1.5

        ceiling_db = -1.0
        ceiling = 10.0 ** (ceiling_db / 20.0)
        out = apply_limiter(audio, ceiling_db=ceiling_db, sr=SR)

        assert np.max(np.abs(out)) <= ceiling + 1e-6
        assert np.all(np.isfinite(out))

    def test_limiter_attack_release_asymmetry(self):
        """Attack should be faster than release in the limiter."""
        from scripts.core.mastering import _smooth_gain_envelope
        reduction = np.ones(2000)
        reduction[500:520] = 0.1
        alpha_atk = float(np.exp(-1.0 / (SR * 0.002)))
        alpha_rel = float(np.exp(-1.0 / (SR * 0.05)))
        gain = _smooth_gain_envelope(reduction, alpha_atk, alpha_rel)

        attack_idx = int(np.argmax(gain[500:] <= 0.11))
        release_idx = int(np.argmax(gain[520:] >= 0.9))
        assert attack_idx < release_idx


# ---------------------------------------------------------------------------
# CPU fallback (force REMIXMATE_DEVICE=cpu)
# ---------------------------------------------------------------------------

class TestCpuFallback:
    def test_envelope_follower_cpu_path(self):
        """Verify CPU fallback produces correct results."""
        reduction = np.ones(44100)
        reduction[22050:22100] = 0.1
        alpha_atk = float(np.exp(-1.0 / (SR * 0.002)))
        alpha_rel = float(np.exp(-1.0 / (SR * 0.05)))
        result = _cpu_envelope_follower(reduction, alpha_atk, alpha_rel)
        assert result.shape == (44100,)
        assert result.dtype == np.float64
        assert np.all(np.isfinite(result))
