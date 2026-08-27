"""
stems.py — разделение треков на барабаны и всё остальное (Demucs).

Зачем это вообще понадобилось. Сколько бы точно мы ни выравнивали темп,
такты, фразы и точки входа — а всё это было починено и измерено, — два
готовых стерео-мастера, сложенные вместе, звучат хуже, чем сведение у
живого диджея. Причина не в настройках: обе дорожки занимают одни и те же
полосы. Измерено на реальных переходах: 180-500 Гц и 500-2000 Гц дают
перебор +2..+3 dB над громчайшим из двух, и вычистить это эквалайзером
нельзя — обеим дорожкам эти полосы нужны.

Стемы снимают задачу, а не смягчают её: если у нас отдельно барабаны и
отдельно музыка, то в любой момент времени можно играть барабаны ОДНОГО
трека и музыку ДРУГОГО, и складывать нечему. Ровно это делает Traktor
Stems, и ровно поэтому он звучит иначе.

Почему именно two-stems=drums, а не полные четыре:
  * бас нам отдельно не нужен — его и так чисто вырезает фильтр (см.
    demo_render._split_low), это узкая полоса и она хорошо делится;
  * вокал отдельно нужен только для акапельных наложений, это отдельная
    техника, а не основной ход;
  * барабаны от гармонии фильтром НЕ отделяются никак — вот ради чего
    вообще нужна модель;
  * два стема считаются примерно вдвое быстрее четырёх и занимают вдвое
    меньше места.

Формат — mp3 320: разница со стемами в WAV на слух не ловится (они всё
равно идут под другой трек), а библиотека из 48 треков в WAV это ~12 ГБ
против ~1.5 ГБ.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
STEM_DIR = HERE / "stems"
MODEL = "htdemucs"
PARTS = ("drums", "no_drums")


def device_info() -> dict:
    """Есть ли GPU. От этого зависит, час считать библиотеку или сутки."""
    try:
        import torch
    except Exception:
        return {"torch": False, "cuda": False, "name": None,
                "note": "torch не установлен: pip install -r requirements-stems.txt"}
    try:
        cuda = bool(torch.cuda.is_available())
        name = torch.cuda.get_device_name(0) if cuda else None
    except Exception:
        cuda, name = False, None
    return {"torch": True, "cuda": cuda, "name": name,
            "note": ("GPU: примерно минута-две на трек" if cuda
                     else "только CPU: примерно 10 минут на трек")}


def stem_key(track_path: str) -> str:
    """Ключ папки со стемами. В него входит время правки и размер файла:
    если диджей заменил файл тем же именем, стемы пересчитаются."""
    p = Path(track_path)
    try:
        st = p.stat()
        sig = f"{p.resolve()}|{int(st.st_mtime)}|{st.st_size}"
    except OSError:
        sig = str(p)
    return hashlib.sha1(sig.encode("utf-8")).hexdigest()[:16]


def stem_dir_for(track_path: str) -> Path:
    return STEM_DIR / stem_key(track_path)


def stem_paths(track_path: str) -> dict | None:
    """{'drums': ..., 'no_drums': ...} если стемы посчитаны, иначе None."""
    d = stem_dir_for(track_path)
    out = {}
    for part in PARTS:
        for ext in (".mp3", ".wav", ".flac"):
            f = d / f"{part}{ext}"
            if f.exists() and f.stat().st_size > 1024:
                out[part] = str(f)
                break
    return out if len(out) == len(PARTS) else None


def separate_hpss(track_path: str, sr: int = 44100, margin: float = 4.0) -> dict:
    """Быстрое разделение без модели: гармоника против ударных (HPSS).

    Это заметно хуже Demucs — тарелки утекают в «музыку», а сустейн
    бочки в «барабаны», — но работает мгновенно, ничего не скачивает и
    не требует GPU. Нужно ровно для одного: услышать, что даёт сведение
    по стемам, ДО того как тратить час машинного времени на нормальное
    разделение. Если разница нравится — считаем Demucs.
    """
    import librosa
    import numpy as np
    import soundfile as sf

    dest = stem_dir_for(track_path)
    have = stem_paths(track_path)
    if have:
        return {"ok": True, "paths": have, "seconds": 0.0, "cached": True}
    dest.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    try:
        y, _sr = librosa.load(track_path, sr=sr, mono=True)
    except Exception as exc:
        return {"ok": False, "error": f"не читается: {exc}"}
    if len(y) < sr:
        return {"ok": False, "error": "слишком короткий"}

    # margin > 1 делает разделение жёстче: то, что не уверенно ударное и
    # не уверенно гармоническое, уходит в остаток и не звучит дважды
    harm, perc = librosa.effects.hpss(y, margin=(1.0, margin))
    rest = y - perc      # всё, кроме ударных: гармоника + то, что не попало ни туда, ни туда

    for part, buf in (("drums", perc), ("no_drums", rest)):
        peak = float(np.max(np.abs(buf))) or 1.0
        sf.write(str(dest / f"{part}.wav"), (buf / max(peak, 1e-6) * 0.95).astype("float32"),
                 sr, subtype="PCM_16")
    (dest / "meta.json").write_text(json.dumps(
        {"source": track_path, "model": "hpss", "seconds": round(time.time() - t0, 1)},
        ensure_ascii=False), encoding="utf-8")
    paths = stem_paths(track_path)
    return ({"ok": True, "paths": paths, "seconds": round(time.time() - t0, 1)} if paths
            else {"ok": False, "error": "не записалось"})


def separate_track(track_path: str, fmt: str = "mp3", model: str = MODEL,
                   device: str | None = None, timeout: float = 3600.0,
                   backend: str = "demucs") -> dict:
    """Считает стемы одного трека. Возвращает {'ok', 'paths'|'error', 'seconds'}."""
    if not os.path.exists(track_path):
        return {"ok": False, "error": "файл не найден"}
    if backend == "hpss":
        return separate_hpss(track_path)
    have = stem_paths(track_path)
    if have:
        return {"ok": True, "paths": have, "seconds": 0.0, "cached": True}

    dest = stem_dir_for(track_path)
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / "_work"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, "-m", "demucs", "-n", model, "--two-stems", "drums",
           "-o", str(tmp)]
    if fmt == "mp3":
        cmd += ["--mp3", "--mp3-bitrate", "320"]
    if device:
        cmd += ["-d", device]
    cmd.append(track_path)

    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "error": f"не уложился в {timeout / 60:.0f} мин"}
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "error": "demucs: " + " / ".join(tail)}

    # demucs кладёт результат в <out>/<model>/<имя трека>/{drums,no_drums}.*
    produced = list(tmp.rglob("drums.*")) + list(tmp.rglob("no_drums.*"))
    if not produced:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "error": "demucs отработал, но файлов нет"}
    for f in produced:
        shutil.move(str(f), str(dest / f.name))
    shutil.rmtree(tmp, ignore_errors=True)

    paths = stem_paths(track_path)
    if not paths:
        return {"ok": False, "error": "стемы не собрались"}
    (dest / "meta.json").write_text(json.dumps(
        {"source": track_path, "model": model, "format": fmt,
         "seconds": round(time.time() - t0, 1)}, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "paths": paths, "seconds": round(time.time() - t0, 1)}


def separate_library(track_paths: list[str], fmt: str = "mp3", model: str = MODEL,
                     device: str | None = None, progress=None, log=print,
                     stop_after: float | None = None, backend: str = "demucs") -> dict:
    """Считает стемы для всей библиотеки, пропуская уже посчитанные."""
    todo = [p for p in track_paths if not stem_paths(p)]
    log(f"стемы: {len(track_paths) - len(todo)} уже есть, считать {len(todo)}")
    info = device_info()
    if backend == "hpss":
        log("быстрое разделение (HPSS): без модели и GPU, качество ниже Demucs")
    else:
        log(f"устройство: {'CUDA ' + str(info.get('name')) if info.get('cuda') else 'CPU'} — {info.get('note')}")

    done, failed = 0, []
    t0 = time.time()
    for i, p in enumerate(todo):
        if stop_after and time.time() - t0 > stop_after:
            failed.append({"path": p, "error": "остановлено по времени"})
            break
        r = separate_track(p, fmt=fmt, model=model, device=device, backend=backend)
        if r.get("ok"):
            done += 1
            log(f"  [{i + 1}/{len(todo)}] {os.path.basename(p)[:48]} — {r.get('seconds', 0):.0f}с")
        else:
            failed.append({"path": p, "error": r.get("error")})
            log(f"  [{i + 1}/{len(todo)}] {os.path.basename(p)[:48]} — ОШИБКА: {r.get('error')}")
        if progress:
            progress(i + 1, len(todo), done, len(failed))

    return {"total": len(track_paths), "already": len(track_paths) - len(todo),
            "done": done, "failed": failed, "seconds": round(time.time() - t0, 1),
            "device": info}


def library_coverage(track_paths: list[str]) -> dict:
    have = sum(1 for p in track_paths if stem_paths(p))
    return {"tracks": len(track_paths), "with_stems": have,
            "share": round(have / len(track_paths), 3) if track_paths else 0.0}


def main() -> int:
    ap = argparse.ArgumentParser(description="DARAVE — разделение треков на барабаны и остальное")
    ap.add_argument("--track", help="один файл")
    ap.add_argument("--dir", help="папка с музыкой")
    ap.add_argument("--format", default="mp3", choices=["mp3", "wav"])
    ap.add_argument("--device", default=None, help="cuda | cpu (по умолчанию — что найдёт demucs)")
    ap.add_argument("--backend", default="demucs", choices=["demucs", "hpss"],
                    help="demucs — качественно, но нужна модель и время; hpss — грубо, но сразу")
    ap.add_argument("--info", action="store_true", help="только показать, есть ли GPU")
    args = ap.parse_args()

    if args.info:
        print(json.dumps(device_info(), ensure_ascii=False, indent=2))
        return 0
    paths = []
    if args.track:
        paths = [args.track]
    elif args.dir:
        for root, _d, files in os.walk(args.dir):
            paths += [os.path.join(root, f) for f in files
                      if f.lower().endswith((".mp3", ".wav", ".flac", ".m4a", ".aiff"))]
    if not paths:
        print("Укажите --track или --dir")
        return 1
    r = separate_library(paths, fmt=args.format, device=args.device, backend=args.backend)
    print(json.dumps({k: v for k, v in r.items() if k != "failed"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
