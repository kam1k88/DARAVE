"""
fix_stem_shift.py - realign the layers inside existing .stem.mp4 files.

The problem. Layers were written as mp3 by lameenc, which emits a bare
frame stream with no Xing/LAME header, so nothing downstream can strip
LAME's encoder delay. It stayed in the audio and ended up baked into the
mp4: every layer sits ~2210 samples (50 ms) later than the master track,
while the master - built from the original mp3, which does carry the
header - is aligned. Measured on three tracks, identical number on all
four layers of each.

The fix is arithmetic, not re-separation: drop N samples from the head of
each layer and remux. No GPU, minutes for the whole library.

Codec is ALAC, deliberately. The layers are AAC today; remuxing them back
into AAC would spend a whole extra lossy generation on a mechanical
alignment fix. ALAC stores the decoded samples bit for bit. Mixxx does
not require AAC - soundsourcestem.cpp only checks that there are five
streams, that they are stereo, and that all four stems share one codec_id
and one sample rate.

THE TRAP THIS CODE MUST NOT FALL INTO. Stream 0 is the premixed master
and Mixxx never plays it (`foundPremixedStream ... continue`). The master
is roughly equal to the sum of the layers, so reading streams 0..3
instead of 1..4, or summing all five, silently doubles the signal: +6 dB.
Layer indices are derived from STEM_ORDER and asserted, never hardcoded.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

import stem_mp4

SR = 44100
MASTER_STREAM = 0                      # premixed, never played by Mixxx
LAYER_STREAMS = tuple(range(1, 1 + len(stem_mp4.STEM_ORDER)))   # 1..4
assert LAYER_STREAMS == (1, 2, 3, 4), LAYER_STREAMS


def _ff() -> str:
    ff = stem_mp4.ffmpeg_exe()
    if not ff:
        raise SystemExit("ffmpeg не найден")
    return ff


def decode(path: str | Path, stream: int | None = None, seconds: float | None = None,
           mono: bool = True) -> np.ndarray:
    """Whole stream (or the first `seconds`) as float32, via an ffmpeg pipe."""
    cmd = [_ff(), "-v", "error", "-i", str(path)]
    if stream is not None:
        cmd += ["-map", f"0:a:{stream}"]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-f", "f32le", "-ac", "1" if mono else "2", "-ar", str(SR), "pipe:1"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    a = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    return a if mono else a.reshape(-1, 2)


def measure_lag(a: np.ndarray, ref: np.ndarray, maxlag: int = 12000) -> tuple[int, float]:
    """How many samples `a` is LATE relative to `ref`, by cross-correlation."""
    n = min(len(a), len(ref))
    if n < SR:
        return 0, 0.0
    x = a[:n].astype(np.float64); y = ref[:n].astype(np.float64)
    x -= x.mean(); y -= y.mean()
    cc = np.fft.irfft(np.fft.rfft(x, n * 2) * np.conj(np.fft.rfft(y, n * 2)))
    cc = np.concatenate([cc[-maxlag:], cc[:maxlag + 1]])
    k = int(np.argmax(cc)) - maxlag
    denom = np.linalg.norm(x) * np.linalg.norm(y) + 1e-9
    return k, float(cc.max() / denom)


def measure_file(mp4: Path, window: float = 12.0, ref_override: Path | None = None) -> dict:
    """Lag of every stream against the reference (source mp3 if present).

    The number that actually gets applied is `common_lag`, measured on the
    SUM of the four layers rather than on each layer separately. Reason:
    all four went through one and the same encoder, so physically there is
    one delay, not four. A single layer correlates with the full mix only
    weakly - `other` came out at 0.18 on one track - and that noise shows
    up as a few dozen samples of disagreement (2210 vs 2260 on the very
    first file). Their sum is the mix again, correlation lands near 1.0,
    and the estimate is stable. Per-layer numbers are still recorded, but
    only as diagnostics.
    """
    # The rebuilt file is written under a temporary name, so its own name
    # cannot be used to find the source mp3 - the caller passes it in.
    # Verifying the result against the master track inside the same file
    # would be circular: it would only prove the layers match the master,
    # not that they match the track everyone's beatgrid was built from.
    src = ref_override if ref_override is not None else mp4.with_name(mp4.name[:-9] + ".mp3")
    use_src = src is not None and src.exists()
    ref_path = src if use_src else mp4
    ref = decode(ref_path, None if use_src else MASTER_STREAM, window)

    out = {"reference": ref_path.name, "streams": {}}
    layers = []
    for idx, name in [(MASTER_STREAM, "master"), *zip(LAYER_STREAMS, stem_mp4.STEM_ORDER)]:
        buf = decode(mp4, idx, window)
        k, c = measure_lag(buf, ref)
        out["streams"][name] = {"lag": k, "corr": round(c, 3)}
        if name != "master":
            layers.append(buf)

    n = min(len(x) for x in layers)
    mixed = np.sum([x[:n] for x in layers], axis=0)
    k, c = measure_lag(mixed, ref)
    out["common_lag"] = k
    out["common_corr"] = round(c, 3)
    return out


def rebuild_copy(mp4: Path, out: Path, shift_samples: int) -> None:
    """Shift the layers WITHOUT touching a single audio sample.

    The same file is opened twice; the second input carries -itsoffset, and
    only its layer streams are mapped. With -c copy nothing is decoded or
    re-encoded - the correction lives in the container's edit list, which
    is exactly what edit lists are for.

    This is strictly better than remuxing: the file stays the same size
    (28.4 MB vs 156.5 MB for ALAC) and costs no generation of loss at all,
    whereas re-encoding the existing AAC layers into AAC would spend a
    fourth lossy pass on a purely arithmetic fix.

    The one thing it assumes is that the player honours edit lists.
    ffmpeg does. Mixxx reads each stream through SoundSourceFFmpeg, so it
    should too - but "should" is not "does", which is why this is verified
    on one file in Mixxx before the whole library is touched.
    """
    ff = _ff()
    off = shift_samples / SR
    cmd = [ff, "-y", "-v", "error", "-i", str(mp4), "-itsoffset", f"-{off:.6f}", "-i", str(mp4),
           "-map", f"0:a:{MASTER_STREAM}"]
    for idx in LAYER_STREAMS:
        cmd += ["-map", f"1:a:{idx}"]
    cmd += ["-c", "copy", "-f", "mp4", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)

    # -c copy does not carry the udta/stem atom across, so it goes back in.
    manifest = stem_mp4.read_manifest(mp4) or {
        "mastering_dsp": {"compressor": {"enabled": False}, "limiter": {"enabled": False}},
        "stems": [{"name": stem_mp4.STEM_LABELS[p][0],
                   "color": stem_mp4.STEM_LABELS[p][1]} for p in stem_mp4.STEM_ORDER],
        "version": stem_mp4.MANIFEST_VERSION,
    }
    stem_mp4.inject_manifest(out, manifest)
    problem = stem_mp4.verify(out)
    if problem:
        raise SystemExit(f"собранный файл не прошёл проверку: {problem}")


def rebuild(mp4: Path, out: Path, lags: dict[str, int], codec: str = "alac",
            bitrate: str = "320k") -> None:
    """Trim each layer's head by its own lag and remux all five streams."""
    ff = _ff()
    import tempfile
    tmpdir = tempfile.TemporaryDirectory(prefix="darave_stemfix_")
    work = Path(tmpdir.name)
    try:
        inputs, maps = [], []
        for pos, (idx, name) in enumerate(
                [(MASTER_STREAM, "master"), *zip(LAYER_STREAMS, stem_mp4.STEM_ORDER)]):
            wav = work / f"{pos}_{name}.wav"
            cmd = [ff, "-y", "-v", "error", "-i", str(mp4), "-map", f"0:a:{idx}"]
            shift = max(0, int(lags.get(name, 0)))
            if shift:
                # atrim works in seconds; sample_rate is fixed at SR above so
                # the conversion is exact for whole samples.
                cmd += ["-af", f"atrim=start_sample={shift},asetpts=N/SR/TB"]
            cmd += ["-c:a", "pcm_s24le", "-ar", str(SR), "-ac", "2", str(wav)]
            subprocess.run(cmd, check=True, capture_output=True)
            inputs += ["-i", str(wav)]
            maps += ["-map", f"{pos}:a:0"]

        cmd = [ff, "-y", "-v", "error", *inputs, *maps, "-c:a", codec]
        if codec == "aac":
            cmd += ["-b:a", bitrate]
        cmd += ["-ar", str(SR), "-ac", "2", "-f", "mp4", str(out)]
        subprocess.run(cmd, check=True, capture_output=True)

        manifest = stem_mp4.read_manifest(mp4) or {
            "mastering_dsp": {"compressor": {"enabled": False},
                              "limiter": {"enabled": False}},
            "stems": [{"name": stem_mp4.STEM_LABELS[p][0],
                       "color": stem_mp4.STEM_LABELS[p][1]} for p in stem_mp4.STEM_ORDER],
            "version": stem_mp4.MANIFEST_VERSION,
        }
        stem_mp4.inject_manifest(out, manifest)
        problem = stem_mp4.verify(out)
        if problem:
            raise SystemExit(f"собранный файл не прошёл проверку: {problem}")
    finally:
        tmpdir.cleanup()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=r"C:\Users\kam1k88\Music")
    ap.add_argument("--measure-only", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--start", type=int, default=0, help="пропустить первые N файлов")
    ap.add_argument("--codec", default="copy", choices=["copy", "alac", "aac"],
                    help="copy — сдвиг через edit list, без перекодирования (по умолчанию)")
    ap.add_argument("--suffix", default="", help="писать рядом с этим суффиксом, не заменяя оригинал")
    args = ap.parse_args()

    files = sorted(Path(args.dir).rglob("*.stem.mp4"))
    total = len(files)
    files = files[args.start:]
    if args.limit:
        files = files[:args.limit]
    # Хвосты от прерванных прогонов: файл собран, но заменить не успели.
    for stale in Path(args.dir).rglob("*.rebuilt.mp4"):
        stale.unlink(missing_ok=True)
    print(f"обрабатываю {len(files)} из {total} (со сдвигом {args.start})")
    report = []
    for i, f in enumerate(files, 1):
        m = measure_file(f)
        per_layer = {k: v["lag"] for k, v in m["streams"].items() if k != "master"}
        common = int(m["common_lag"])
        lags = {name: common for name in stem_mp4.STEM_ORDER}
        worst = abs(common)
        print(f"[{args.start + i}/{total}] {f.name[:52]:54s} "
              f"мастер {m['streams']['master']['lag']:+5d}  "
              f"общий {common:+5d} (корр {m['common_corr']})  по слоям {sorted(set(per_layer.values()))}",
              flush=True)
        report.append({"file": str(f), **m})
        # Уже починенный файл меряется как ~0 (±пара отсчётов от гранулярности
        # корреляции), и трогать его второй раз незачем. Порог тот же, что в
        # приёмке, — иначе прерванный прогон при перезапуске переписывал бы
        # готовые файлы ради сдвига в три отсчёта.
        if args.measure_only or worst <= 8:
            continue
        out = f.with_name(f.name[:-9] + args.suffix + ".stem.mp4") if args.suffix else f
        tmp = f.with_name(f.name + ".rebuilt.mp4")
        if args.codec == "copy":
            rebuild_copy(f, tmp, common)
        else:
            rebuild(f, tmp, lags, codec=args.codec)
        after = measure_file(tmp, ref_override=f.with_name(f.name[:-9] + ".mp3"))
        # Acceptance uses the same statistic the correction was derived from:
        # the summed layers, which correlate with the mix at ~0.99. Demanding
        # an exact zero from each layer on its own would reject a correct
        # rebuild, and did - `bass` reads 50 samples off simply because it
        # correlates with the full mix at 0.4-0.6. 50 samples is 1.1 ms; a
        # beat at 174 BPM is 345 ms, so the tolerance below is three orders
        # of magnitude under anything audible, while the common lag - the
        # number that actually moves the music - must be spot on.
        # In `copy` mode all four layers are shifted by ONE -itsoffset, so
        # they share one edit list and cannot diverge - checking them
        # individually proves nothing about the operation. And the per-layer
        # estimate is demonstrably unreliable: a bass line correlated against
        # the full mix read 11312 samples on one track whose true shift was
        # 2210, because a periodic part locks onto a neighbouring peak.
        # So acceptance rests on the common lag, measured on the summed
        # layers at correlation 0.98-0.99, and per-layer numbers are kept as
        # diagnostics only.
        bad = {}
        if abs(after.get("common_lag", 0)) > 8:
            bad["common"] = after["common_lag"]
        noisy = {k: v["lag"] for k, v in after["streams"].items()
                 if k != "master" and abs(v["lag"]) > 128}
        if noisy:
            print(f"    (оценка по отдельным слоям шумит: {noisy} — не помеха, "
                  f"сдвиг общий и применён один)", flush=True)
        if bad:
            try:
                tmp.unlink()
            except OSError:
                pass          # на некоторых томах удалять нельзя — оставим как есть
            print(f"    ОТКАЗ: после пересборки сдвиг остался {bad} — оригинал не тронут",
                  flush=True)
            report[-1]["rebuilt"] = False
            continue
        try:
            import os
            os.replace(tmp, out)          # атомарно, без отдельного unlink
        except OSError as exc:
            print(f"    не удалось заменить {out.name}: {exc}\n"
                  f"    готовый файл оставлен как {tmp.name}", flush=True)
            report[-1]["rebuilt"] = "left_as_tmp"
            continue
        report[-1]["rebuilt"] = True
        report[-1]["after"] = after
        print(f"    OK -> {out.name}  {out.stat().st_size/1e6:.1f} МБ", flush=True)

    Path(f"stem_shift_report_{args.start:03d}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("отчёт: stem_shift_report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
