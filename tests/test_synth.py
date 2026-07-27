"""
tests/test_synth.py — Tests for DARAVE mini-synthesizer.
"""

import numpy as np
import pytest

from scripts.core.synth import (
    SynthParams,
    SynthGraph,
    SynthEngine,
    Oscillator,
    BiquadFilter,
    ADSREnvelope,
    LFO,
    SoftClipper,
    Compressor,
    Delay,
    stft,
    spectral_features,
    phase_correlation,
    detect_transients,
    assess_mix_quality,
)


SR = 44100


class TestSynthParams:
    def test_vector_roundtrip(self):
        p = SynthParams(osc_freq=220.0, filter_cutoff=500.0, delay_time=0.2)
        vec = p.to_vector()
        p2 = SynthParams.from_vector(vec)
        assert p2.osc_freq == pytest.approx(220.0)
        assert p2.filter_cutoff == pytest.approx(500.0)
        assert p2.delay_time == pytest.approx(0.2)

    def test_vector_size(self):
        p = SynthParams()
        assert p.vector_size == len(SynthParams.__dataclass_fields__)


class TestOscillator:
    def test_sine_output(self):
        osc = Oscillator()
        osc.set_params({"osc_type": 0.0, "osc_freq": 440.0, "osc_amp": 0.5})
        out = osc.process(np.zeros(1024, dtype=np.float32), SR)
        assert out.shape == (1024,)
        assert out.dtype == np.float32
        assert np.abs(out).max() > 0

    def test_saw_output(self):
        osc = Oscillator()
        osc.set_params({"osc_type": 1.0, "osc_freq": 220.0, "osc_amp": 0.8})
        out = osc.process(np.zeros(2048, dtype=np.float32), SR)
        assert out.shape == (2048,)
        assert np.abs(out).max() > 0

    def test_noise_output(self):
        osc = Oscillator()
        osc.set_params({"osc_type": 4.0, "osc_freq": 0.0, "osc_amp": 1.0})
        out = osc.process(np.zeros(4096, dtype=np.float32), SR)
        assert np.abs(out).std() > 0.1

    def test_reset(self):
        osc = Oscillator()
        osc.process(np.zeros(512, dtype=np.float32), SR)
        osc.reset()
        assert osc._phase == 0.0


class TestFilter:
    def test_lowpass(self):
        filt = BiquadFilter()
        filt.set_params({"filter_type": 0.0, "filter_cutoff": 200.0, "filter_resonance": 0.7})
        # White noise should be attenuated
        noise = np.random.randn(4096).astype(np.float32)
        out = filt.process(noise, SR)
        assert out.shape == noise.shape
        # High frequencies should be reduced
        in_rms = np.sqrt(np.mean(noise ** 2))
        out_rms = np.sqrt(np.mean(out ** 2))
        assert out_rms < in_rms

    def test_highpass(self):
        filt = BiquadFilter()
        filt.set_params({"filter_type": 1.0, "filter_cutoff": 5000.0, "filter_resonance": 0.7})
        # Sine at 100 Hz should be attenuated
        t = np.arange(4096, dtype=np.float64) / SR
        sine_100 = np.sin(2 * np.pi * 100 * t).astype(np.float32)
        out = filt.process(sine_100, SR)
        out_rms = np.sqrt(np.mean(out ** 2))
        assert out_rms < 0.1


class TestEnvelope:
    def test_trigger_on_off(self):
        env = ADSREnvelope()
        env.set_params({"env_a": 0.01, "env_d": 0.05, "env_s": 0.5, "env_r": 0.05})
        env.trigger_on()
        out = env.process(np.zeros(2048, dtype=np.float32), SR)
        # Should ramp up then decay to sustain
        assert out[0] < out[100]  # attack
        env.trigger_off()
        out2 = env.process(np.zeros(1024, dtype=np.float32), SR)
        # Should release to 0
        assert out2[-1] < out2[0]


class TestLFO:
    def test_lfo_range(self):
        lfo = LFO()
        lfo.set_params({"lfo_rate": 1.0, "lfo_depth": 1.0})
        out = lfo.process(np.zeros(4096, dtype=np.float32), SR)
        # LFO output should be in [0, 1] * depth
        assert out.min() >= -0.01
        assert out.max() <= 1.01

    def test_lfo_zero_depth(self):
        lfo = LFO()
        lfo.set_params({"lfo_rate": 1.0, "lfo_depth": 0.0})
        out = lfo.process(np.zeros(4096, dtype=np.float32), SR)
        assert np.abs(out).max() < 0.01


class TestFX:
    def test_soft_clipper(self):
        clip = SoftClipper()
        clip.set_params({"clip_drive": 2.0, "clip_threshold": 0.9})
        loud = np.ones(1024, dtype=np.float32) * 2.0
        out = clip.process(loud, SR)
        assert np.abs(out).max() <= 0.91

    def test_compressor(self):
        comp = Compressor()
        comp.set_params({"comp_threshold": -20.0, "comp_ratio": 4.0, "comp_attack": 0.001, "comp_release": 0.01})
        # Loud signal should be compressed
        loud = np.ones(4096, dtype=np.float32) * 0.8
        out = comp.process(loud, SR)
        # Output should be quieter than input
        assert np.abs(out).mean() < np.abs(loud).mean()

    def test_delay(self):
        delay = Delay()
        delay.set_params({"delay_time": 0.1, "delay_feedback": 0.0, "delay_mix": 0.5})
        impulse = np.zeros(4096, dtype=np.float32)
        impulse[0] = 1.0
        out = delay.process(impulse, SR)
        # Should have delayed copy
        delay_samples = int(0.1 * SR)
        assert out[delay_samples] > 0.5


class TestAnalytics:
    def test_stft(self):
        audio = np.sin(2 * np.pi * 440 * np.arange(8192) / SR).astype(np.float32)
        mags, phases, times = stft(audio, SR)
        assert mags.shape[0] > 0
        assert mags.shape[1] > 0
        assert len(times) == mags.shape[0]

    def test_spectral_features(self):
        audio = np.sin(2 * np.pi * 440 * np.arange(8192) / SR).astype(np.float32)
        mags, _, _ = stft(audio, SR)
        feats = spectral_features(mags, SR)
        assert "spectral_centroid" in feats
        assert feats["spectral_centroid"] > 0

    def test_phase_correlation(self):
        audio = np.sin(2 * np.pi * 440 * np.arange(4096) / SR).astype(np.float32)
        result = phase_correlation(audio, audio, SR)
        assert result["correlation"] > 0.99  # identical signals

    def test_detect_transients(self):
        # Signal with sharp onsets
        audio = np.zeros(8192, dtype=np.float32)
        audio[0] = 1.0
        audio[2048] = 1.0
        audio[4096] = 1.0
        transients = detect_transients(audio, SR, threshold=0.1)
        assert len(transients) > 0

    def test_assess_mix_quality(self):
        audio = np.random.randn(8192).astype(np.float32) * 0.5
        hints = assess_mix_quality(audio, SR)
        assert hints.dynamic_range_db >= 0


class TestSynthEngine:
    def test_render(self):
        engine = SynthEngine()
        params = SynthParams(osc_type=0, osc_freq=440.0, osc_amp=0.5)
        audio = engine.render(params, n_samples=4096, sample_rate=SR)
        assert audio.shape == (4096,)
        assert audio.dtype == np.float32
        assert np.abs(audio).max() > 0

    def test_analyze(self):
        engine = SynthEngine()
        params = SynthParams(osc_type=0, osc_freq=440.0, osc_amp=0.5)
        audio = engine.render(params, n_samples=8192, sample_rate=SR)
        features = engine.analyze(audio, SR)
        assert "spectral_features" in features
        assert "transients" in features
        assert "mix_quality" in features

    def test_render_full_patch(self):
        engine = SynthEngine()
        params = SynthParams(
            osc_type=1, osc_freq=220.0, osc_amp=0.6,
            filter_type=0, filter_cutoff=2000.0, filter_resonance=0.5,
            env_a=0.01, env_d=0.1, env_s=0.7, env_r=0.2,
            comp_threshold=-20.0, comp_ratio=4.0,
            delay_time=0.1, delay_feedback=0.3, delay_mix=0.2,
        )
        audio = engine.render(params, n_samples=44100, sample_rate=SR)
        assert len(audio) == 44100
        assert np.abs(audio).max() > 0

    def test_load_patch(self):
        engine = SynthEngine()
        engine.load_patch({
            "modules": ["osc", "filter"],
            "params": {"osc_freq": 330.0, "filter_cutoff": 3000.0},
        })
        audio = engine.render(n_samples=4096, sample_rate=SR)
        assert len(audio) == 4096

    def test_reward(self):
        engine = SynthEngine()
        params = SynthParams()
        audio = engine.render(params, n_samples=8192, sample_rate=SR)
        features = engine.analyze(audio, SR)
        reward = engine.compute_reward(features, features, user_reward=1.0)
        assert reward > 0  # identical features + user reward
