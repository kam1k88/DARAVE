"""Quick smoke test for all synth modules."""
from scripts.core.synth import (
    Oscillator, BiquadFilter, ADSREnvelope, LFO,
    SoftClipper, Compressor, Delay,
    stft, spectral_features, phase_correlation, detect_transients, assess_mix_quality,
    SynthEngine, SynthParams,
)
import numpy as np

sr = 44100
n = 2048
buf = np.zeros(n, dtype=np.float32)

osc = Oscillator()
osc.set_params({"osc_type": 0, "osc_freq": 440, "osc_amp": 0.5})
o = osc.process(buf, sr)
print(f"Oscillator: {o.shape}, peak={np.abs(o).max():.3f}")

filt = BiquadFilter()
filt.set_params({"filter_type": 0, "filter_cutoff": 1000, "filter_resonance": 0.7})
o = filt.process(np.random.randn(n).astype(np.float32), sr)
print(f"Filter: {o.shape}")

env = ADSREnvelope()
env.trigger_on()
env.set_params({"env_a": 0.01, "env_d": 0.1, "env_s": 0.7, "env_r": 0.2})
o = env.process(buf, sr)
print(f"Envelope: {o.shape}, max={o.max():.3f}")

lfo = LFO()
lfo.set_params({"lfo_rate": 1, "lfo_depth": 0.5})
o = lfo.process(buf, sr)
print(f"LFO: {o.shape}, range=[{o.min():.3f},{o.max():.3f}]")

clip = SoftClipper()
clip.set_params({"clip_drive": 2, "clip_threshold": 0.9})
o = clip.process(np.ones(n) * 2, sr)
print(f"Clipper: peak={np.abs(o).max():.3f}")

comp = Compressor()
comp.set_params({"comp_threshold": -20, "comp_ratio": 4})
o = comp.process(np.ones(n) * 0.8, sr)
print(f"Compressor: mean={np.abs(o).mean():.3f}")

delay = Delay()
delay.set_params({"delay_time": 0.1, "delay_feedback": 0.3, "delay_mix": 0.5})
o = delay.process(buf, sr)
print(f"Delay: {o.shape}")

audio = np.sin(2 * np.pi * 440 * np.arange(8192) / sr).astype(np.float32)
mags, phases, times = stft(audio, sr)
print(f"STFT: {mags.shape}")

feats = spectral_features(mags, sr)
print(f"Spectral: centroid={feats['spectral_centroid']:.0f}Hz")

corr = phase_correlation(audio, audio, sr)
print(f"Phase corr: {corr['correlation']:.3f}")

t = detect_transients(audio, sr)
print(f"Transients: {len(t)}")

q = assess_mix_quality(audio, sr)
print(f"Quality: DR={q.dynamic_range_db:.1f}dB")

engine = SynthEngine()
p = SynthParams(osc_type=1, osc_freq=220, filter_cutoff=2000, delay_time=0.1, delay_mix=0.3)
a = engine.render(p, 44100, sr)
print(f"Engine render: {len(a)} samples, peak={np.abs(a).max():.3f}")

f = engine.analyze(a, sr)
print(f"Engine analyze: {list(f.keys())}")

r = engine.compute_reward(f, f, 1.0)
print(f"Reward: {r:.3f}")

print("ALL MODULES OK")
