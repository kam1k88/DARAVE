"""tests/test_gpu_benchmark.py — Benchmark suite for GPU vs CPU DSP primitives."""
from __future__ import annotations

import time
import numpy as np

from scripts.core.gpu import (
    get_device,
    gpu_beat_stamp,
    gpu_compressor,
    gpu_envelope_follower,
    gpu_filter_sweep,
    gpu_flanger,
    gpu_gate,
    gpu_phaser,
    gpu_vinyl_stop,
    _cpu_envelope_follower,
)

SR = 44100


def _sine(freq=1000.0, duration=10.0, amplitude=0.5, sr=SR):
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return (np.sin(2 * np.pi * freq * t) * amplitude).astype(np.float32)


def _noise(duration=10.0, sr=SR, amplitude=0.3):
    rng = np.random.default_rng(42)
    return (rng.standard_normal(int(sr * duration)) * amplitude).astype(np.float32)


def _benchmark(func, *args, n_runs=3, **kwargs):
    """Run function n_runs times and return (mean_time, std_time) in ms."""
    times = []
    for _ in range(n_runs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)
    return np.mean(times), np.std(times), result


class TestGPUBenchmarkSuite:
    """Benchmark GPU vs CPU for all DSP primitives."""

    @classmethod
    def setup_class(cls):
        cls.device = get_device()
        cls.audio_10s = _sine(duration=10.0)
        cls.audio_30s = _sine(duration=30.0)
        cls.noise_10s = _noise(duration=10.0)

    def test_envelope_follower_benchmark(self):
        """Benchmark envelope follower: GPU vs CPU (numba)."""
        reduction = np.random.uniform(0.1, 1.0, size=SR * 10).astype(np.float64)
        alpha_atk = float(np.exp(-1.0 / (SR * 0.002)))
        alpha_rel = float(np.exp(-1.0 / (SR * 0.05)))

        mean_cpu, std_cpu, _ = _benchmark(
            _cpu_envelope_follower, reduction, alpha_atk, alpha_rel
        )
        mean_gpu, std_gpu, _ = _benchmark(
            gpu_envelope_follower, reduction, alpha_atk, alpha_rel
        )

        print(f"\n  envelope_follower ({self.device}):")
        print(f"    CPU (numba): {mean_cpu:.1f}ms ± {std_cpu:.1f}ms")
        print(f"    GPU path:    {mean_gpu:.1f}ms ± {std_gpu:.1f}ms")

    def test_gate_benchmark(self):
        """Benchmark noise gate."""
        mean, std, _ = _benchmark(gpu_gate, self.audio_10s, SR, n_runs=3)
        print(f"\n  gate ({self.device}): {mean:.1f}ms ± {std:.1f}ms")

    def test_compressor_benchmark(self):
        """Benchmark compressor."""
        mean, std, _ = _benchmark(gpu_compressor, self.audio_10s, SR, n_runs=3)
        print(f"\n  compressor ({self.device}): {mean:.1f}ms ± {std:.1f}ms")

    def test_vinyl_stop_benchmark(self):
        """Benchmark vinyl stop."""
        mean, std, _ = _benchmark(gpu_vinyl_stop, self.audio_10s, SR, n_runs=3)
        print(f"\n  vinyl_stop ({self.device}): {mean:.1f}ms ± {std:.1f}ms")

    def test_flanger_benchmark(self):
        """Benchmark flanger."""
        mean, std, _ = _benchmark(gpu_flanger, self.audio_10s, SR, 128.0, n_runs=3)
        print(f"\n  flanger ({self.device}): {mean:.1f}ms ± {std:.1f}ms")

    def test_phaser_benchmark(self):
        """Benchmark phaser."""
        mean, std, _ = _benchmark(gpu_phaser, self.audio_10s, SR, 128.0, n_runs=3)
        print(f"\n  phaser ({self.device}): {mean:.1f}ms ± {std:.1f}ms")

    def test_filter_sweep_benchmark(self):
        """Benchmark filter sweep."""
        mean, std, _ = _benchmark(gpu_filter_sweep, self.audio_10s, SR, n_runs=3)
        print(f"\n  filter_sweep ({self.device}): {mean:.1f}ms ± {std:.1f}ms")

    def test_beat_stamp_benchmark(self):
        """Benchmark beat stamp (16 bars)."""
        pattern = [
            ("kick", 0, 0.8), ("hh", 4, 0.5), ("snare", 4, 0.7),
            ("hh", 8, 0.5), ("hh", 12, 0.3),
        ]
        sounds = {
            "kick": np.ones(512, dtype=np.float32),
            "snare": np.ones(512, dtype=np.float32) * 0.7,
            "hh": np.ones(256, dtype=np.float32) * 0.3,
        }
        bars = 16
        step_samples = int(SR * 60 / 128 / 4)
        total_samples = bars * 16 * step_samples

        mean, std, _ = _benchmark(
            gpu_beat_stamp, pattern, sounds, bars, step_samples, total_samples, n_runs=3
        )
        print(f"\n  beat_stamp ({self.device}, {bars} bars): {mean:.1f}ms ± {std:.1f}ms")

    def test_30s_audio_benchmark(self):
        """Benchmark key functions on 30-second audio."""
        print(f"\n  30s audio ({self.device}):")

        mean, std, _ = _benchmark(gpu_gate, self.audio_30s, SR, n_runs=2)
        print(f"    gate:        {mean:.1f}ms ± {std:.1f}ms")

        mean, std, _ = _benchmark(gpu_compressor, self.audio_30s, SR, n_runs=2)
        print(f"    compressor:  {mean:.1f}ms ± {std:.1f}ms")

        mean, std, _ = _benchmark(gpu_filter_sweep, self.audio_30s, SR, n_runs=2)
        print(f"    filter_sweep: {mean:.1f}ms ± {std:.1f}ms")

    def test_device_info(self):
        """Print device info for benchmark context."""
        from scripts.core.gpu import log_device_info
        info = log_device_info()
        print(f"\n  Device: {info}")
