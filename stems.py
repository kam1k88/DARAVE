"""
stems.py — разделение трека на четыре слоя: барабаны, бас, гармония, вокал.

Зачем это вообще понадобилось. Сколько бы точно мы ни выравнивали темп,
такты, фразы и точки входа — а всё это было починено и измерено, — два
готовых стерео-мастера, сложенные вместе, звучат хуже, чем сведение у
живого диджея. Причина не в настройках: обе дорожки занимают одни и те же
полосы. Измерено на реальных переходах: 180-500 Гц и 500-2000 Гц дают
перебор +2..+3 dB над громчайшим из двух, и вычистить это эквалайзером
нельзя — обеим дорожкам эти полосы нужны.

Стемы снимают задачу, а не смягчают её: если слои разделены, то в любой
момент можно играть барабаны ОДНОГО трека и гармонию ДРУГОГО, и
складывать нечему. Ровно это делает Traktor Stems, и ровно поэтому он
звучит иначе.

## Почему теперь четыре стема, а не два

Раньше здесь было two-stems=drums, и рассуждение было такое: бас чисто
режется фильтром, вокал нужен редко. Оба довода оказались неверны, как
только техники стали работать ПО СЛОЯМ:

* **бас фильтром не режется.** Фильтр режет ПОЛОСУ, а не инструмент. Всё,
  что живёт ниже 180 Гц, уходит вместе с басом: бочка, низ пэда, тело
  саба. Обмен басом через EQ — это всегда обмен «бас плюс низ барабанов»,
  и именно поэтому он слышен как провал, а не как обмен;
* **вокал нужен не «редко», а в самом ценном приёме.** Акапелла поверх
  чужого инструментала — то, чем открытый формат живёт; и он же нужен,
  чтобы НЕ наложить два вокала (единственный клэш, который слышат все);
* гармония отдельно от барабанов — то, ради чего модель и нужна: фильтром
  это не делится никак.

Четыре стема считаются дольше и занимают больше места. Это и есть цена.

## Лестница качества

    roformer  — htdemucs_ft на четыре слоя ПЛЮС Mel-Band/BS-Roformer на
                вокал, и «гармония» пересчитывается как микс минус
                барабаны, бас и роформерный вокал. Лучшее, что есть
                сегодня; нужен пакет audio-separator и GPU.
    demucs    — htdemucs_ft: четыре слоя одной моделью. Заметно лучше
                базовой htdemucs (это её дообученная версия, считает
                вчетверо дольше), новых зависимостей не нужно.
    fast      — базовая htdemucs. Прилично, но на вокале слышны артефакты.
    hpss      — без модели вообще: гармоника против ударных за секунды.
                Грубо (тарелки утекают в гармонию, сустейн бочки в
                барабаны), вокала не даёт совсем. Нужно ровно для одного:
                услышать, что даёт сведение по слоям, ДО того как тратить
                час машинного времени.

`backend="auto"` выбирает лучшее из установленного, а не падает.

Формат — mp3 320: стемы всё равно звучат под другой трек, разницу с WAV
на них не поймать, а четыре слоя для 48 треков это ~3 ГБ против ~24 ГБ.
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

try:
    import stem_mp4
except Exception:  # модуль независим от stems.py, но без него нет живого режима
    stem_mp4 = None

HERE = Path(__file__).parent
STEM_DIR = HERE / "stems"

MODEL = "htdemucs_ft"        # дообученная htdemucs: лучше базовой, считает вчетверо дольше
FAST_MODEL = "htdemucs"

# Слои, которые модель отдаёт. Порядок важен только для читаемости логов.
PARTS = ("drums", "bass", "other", "vocals")

# Комбинации, которые нужны техникам. Считаются суммой на лету — хранить
# их отдельно значит хранить те же секунды дважды.
COMBOS = {
    "no_drums": ("bass", "other", "vocals"),
    "instrumental": ("drums", "bass", "other"),
    "acapella": ("vocals",),
    "music": ("other", "vocals"),          # гармония с вокалом, без ритм-секции
    "rhythm": ("drums", "bass"),           # ритм-секция целиком
}

# Модель роформера для вокала. Имена файлов в audio-separator меняются от
# версии к версии, поэтому это список кандидатов: берём первый, который
# отработал. Если ни один — остаёмся на вокале от demucs и честно пишем
# это в meta.json, а не притворяемся, что посчитали лучшее.
ROFORMER_MODELS = (
    "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
    "mel_band_roformer_kim_ft_unwa.ckpt",
    "model_mel_band_roformer_ep_3005_sdr_11.4360.ckpt",
)

AUDIO_EXT = (".mp3", ".wav", ".flac", ".m4a", ".aiff", ".aif", ".ogg", ".opus")


# --------------------------------------------------------------- окружение

def has_module(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# Кандидаты в интерпретаторы, где может стоять demucs. Бэкенд DARAVE и
# разделение стемов — это РАЗНЫЕ задачи с разными требованиями к версии
# Python: companion'у нужен python-rtmidi (есть только до 3.12), а demucs
# и torch на 3.14 просто нет колёс. Поэтому не требуем, чтобы всё стояло в
# одном питоне, а ищем тот, где стоит нужное.
_PY_CANDIDATES = (
    r"C:\Python312\python.exe",
    r"C:\Python311\python.exe",
    r"C:\Python310\python.exe",
)
_demucs_py_cache: list | None = None


def _python_has(exe: str, module: str, timeout: float = 25.0) -> bool:
    try:
        r = subprocess.run([exe, "-c", f"import {module}"], capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def demucs_python() -> str | None:
    """Интерпретатор, в котором РЕАЛЬНО есть demucs, или None.

    Раньше здесь безусловно стоял sys.executable, и на машине, где бэкенд
    крутится под Python 3.14, а demucs поставился в 3.12, разделение
    падало 65 раз подряд с одним и тем же «No module named demucs» — по
    разу на трек. Ошибка была верная, а причина невидимая."""
    global _demucs_py_cache
    if _demucs_py_cache is not None:
        return _demucs_py_cache[0]
    found = None
    if has_module("demucs"):
        found = sys.executable
    else:
        cands = list(_PY_CANDIDATES)
        for name in ("python3.12", "python3.11", "python3.10", "python3", "python"):
            w = shutil.which(name)
            if w:
                cands.append(w)
        if os.name == "nt":
            local = os.environ.get("LOCALAPPDATA", "")
            for v in ("312", "311", "310"):
                cands.append(os.path.join(local, "Programs", "Python", f"Python{v}", "python.exe"))
        seen = set()
        for exe in cands:
            if exe in seen or not exe:
                continue
            seen.add(exe)
            if os.path.exists(exe) and exe != sys.executable and _python_has(exe, "demucs"):
                found = exe
                break
    _demucs_py_cache = [found]
    return found


def device_info() -> dict:
    """Есть ли GPU. От этого зависит, час считать библиотеку или сутки."""
    try:
        import torch
    except Exception:
        return {"torch": False, "cuda": False, "name": None,
                "demucs": has_module("demucs"), "audio_separator": has_module("audio_separator"),
                "note": "torch не установлен: pip install -r requirements-stems.txt"}
    try:
        cuda = bool(torch.cuda.is_available())
        name = torch.cuda.get_device_name(0) if cuda else None
    except Exception:
        cuda, name = False, None
    return {
        "torch": True, "cuda": cuda, "name": name,
        "demucs": bool(demucs_python()),
        "demucs_python": demucs_python(),
        "audio_separator": has_module("audio_separator"),
        "note": ("GPU: примерно 2-4 минуты на трек в максимальном качестве" if cuda
                 else "только CPU: 20-40 минут на трек в максимальном качестве"),
    }


def pick_device(device: str | None = None) -> str | None:
    """Куда считать. По умолчанию — на видеокарту, если она есть.

    Раньше device=None означало «пусть demucs решает сам», и demucs решал
    правильно... когда torch собран с CUDA. На машине с RTX 4060 и
    CPU-сборкой torch это молча превращалось в 20-40 минут на трек вместо
    2-4, и понять это можно было только по времени. Теперь устройство
    выбирается явно и попадает в лог."""
    if device:
        return device
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return None


def resolve_backend(backend: str = "auto") -> str:
    """Лучшее из того, что реально установлено."""
    if backend != "auto":
        return backend
    py = demucs_python()
    if py and (has_module("audio_separator")
               or (py != sys.executable and _python_has(py, "audio_separator"))):
        return "roformer"
    if py:
        return "demucs"
    return "hpss"


def backend_problem(backend: str) -> str | None:
    """Почему выбранный способ не сработает — ДО того, как начать перебор.

    Проверяем один раз и вслух. Молча пробовать 65 раз подряд и получать
    одну и ту же ошибку — это не «устойчивость к сбоям», это спам."""
    if backend == "hpss":
        return None
    if not demucs_python():
        return ("demucs не установлен ни в одном найденном Python. "
                "Поставьте его: запустите setup_stems.ps1 из папки DARAVE — "
                "он найдёт подходящий Python и поставит всё сам. "
                "Быстрый способ посмотреть без модели: --backend hpss.")
    if backend == "roformer":
        py = demucs_python()
        if not (has_module("audio_separator")
                or (py != sys.executable and _python_has(py, "audio_separator"))):
            return ("audio-separator не установлен — роформер на вокал недоступен. "
                    "Считаю через htdemucs_ft (--backend demucs): четыре слоя, "
                    "вокал чуть хуже роформера, но заметно лучше базовой htdemucs.")
    return None


# ------------------------------------------------------------------- кэш

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
    """{'drums':…, 'bass':…, 'other':…, 'vocals':…} или None, если полного
    комплекта нет. Неполный комплект — это не «частично готово», а «нечем
    собрать деку»: сумма слоёв обязана давать исходный трек, иначе на
    переходе поедет громкость.

    Готовность определяет meta.json с complete=true, а не наличие файлов.
    Проверено на своей же ошибке: прерванный проход оставил четыре файла,
    из которых последний был дописан наполовину — по размеру он проходил
    любую проверку, а звучал обрывом. Метка пишется ПОСЛЕДНЕЙ, поэтому
    оборванный проход просто не считается сделанным и пересчитается."""
    d = stem_dir_for(track_path)
    if not (d / "meta.json").exists():
        return None
    try:
        if not json.loads((d / "meta.json").read_text(encoding="utf-8")).get("complete"):
            return None
    except (OSError, ValueError):
        return None
    out = {}
    for part in PARTS:
        for ext in (".mp3", ".wav", ".flac"):
            f = d / f"{part}{ext}"
            if f.exists() and f.stat().st_size > 1024:
                out[part] = str(f)
                break
    return out if len(out) == len(PARTS) else None


def stem_meta(track_path: str) -> dict:
    """Чем именно посчитаны стемы этого трека — чтобы «качественные» не
    оказались вчерашним HPSS-черновиком."""
    f = stem_dir_for(track_path) / "meta.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def has_vocals(track_path: str) -> bool:
    """Есть ли у трека НАСТОЯЩИЙ вокальный слой.

    У HPSS его нет и быть не может: метод делит спектр на ударное и
    гармоническое, голос живёт в гармонической части целиком. Слой
    записывается пустым сознательно — класть туда гармонию значило бы
    обмануть техники, которые на голос рассчитывают. Но тогда «Акапелла
    поверх» на таких стемах выдаёт тишину и выглядит как поломка рендера,
    а не как отсутствие данных. Поэтому спрашивать надо здесь и заранее."""
    m = stem_meta(track_path)
    return bool(m.get("complete")) and m.get("backend") not in (None, "", "hpss")


def _write_meta(dest: Path, **kw) -> None:
    """Метка готовности. Пишется ТОЛЬКО когда все слои на месте."""
    kw.setdefault("complete", True)
    (dest / "meta.json").write_text(json.dumps(kw, ensure_ascii=False, indent=1),
                                    encoding="utf-8")


def _write_part(dest: Path, part: str, buf, sr: int, fmt: str) -> None:
    """Один слой на диск. mp3 320 через LAME, как и весь остальной вывод
    DARAVE: libsndfile отдаёт 74 кбит/с и битрейт ему не задать."""
    import numpy as np
    import soundfile as sf

    data = np.asarray(buf)
    if data.ndim == 1:
        data = data[:, None]
    if fmt == "mp3":
        try:
            import tempfile

            import set_export
            # Промежуточный WAV кладём во ВРЕМЕННУЮ папку системы, а не
            # рядом с результатом: слой длинного трека это ~80 МБ, и если
            # его не удалось убрать (сетевой диск, права, прерванный
            # проход), в кэше стемов навсегда остаётся мусор вчетверо
            # больше самого кэша.
            with tempfile.TemporaryDirectory(prefix="darave_stem_") as td:
                tmp = Path(td) / f"{part}.wav"
                sf.write(str(tmp), data.astype("float32"), sr, subtype="PCM_16")
                out = set_export._write_mp3(str(tmp), data, sr, 320)
                shutil.copyfile(str(out), str(dest / f"{part}.mp3"))
            return
        except Exception:
            pass
    sf.write(str(dest / f"{part}.wav"), data.astype("float32"), sr, subtype="PCM_16")


# ------------------------------------------------------- разделение: hpss

def separate_hpss(track_path: str, sr: int = 44100, margin: float = 4.0,
                  fmt: str = "mp3") -> dict:
    """Быстрое разделение без модели: гармоника против ударных.

    Даёт три слоя из четырёх и честно кладёт пустой вокал: HPSS вокал не
    выделяет в принципе, а класть туда гармонику значило бы обмануть
    техники, которые на вокал рассчитывают. Бас берётся полосой ниже
    180 Гц из гармонической части — это ровно то приближение, из-за
    которого настоящие стемы и нужны, но для «послушать, стоит ли овчинка
    выделки» годится.
    """
    import librosa
    import numpy as np
    import soundfile as sf
    from scipy.signal import butter, sosfiltfilt

    dest = stem_dir_for(track_path)
    have = stem_paths(track_path)
    if have:
        return {"ok": True, "paths": have, "seconds": 0.0, "cached": True}
    dest.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    try:
        y, _sr = librosa.load(track_path, sr=sr, mono=False)
    except Exception as exc:
        return {"ok": False, "error": f"не читается: {exc}"}
    y = np.atleast_2d(y)
    if y.shape[0] == 1:
        y = np.vstack([y, y])
    if y.shape[1] < sr:
        return {"ok": False, "error": "слишком короткий"}

    # margin > 1 делает разделение жёстче: то, что не уверенно ударное и
    # не уверенно гармоническое, уходит в остаток и не звучит дважды.
    #
    # Маску считаем по МОНО-сумме и применяем к обоим каналам: полный HPSS
    # на каждый канал вдвое дороже и на длинном треке это минуты, а
    # разница в маске между каналами меньше, чем собственная погрешность
    # метода. Стереокартина при этом сохраняется — маска применяется к
    # спектру каждого канала отдельно.
    mono = y.mean(axis=0)
    S = librosa.stft(mono)
    H, P = librosa.decompose.hpss(S, margin=(1.0, margin))
    mag = np.abs(H) + np.abs(P) + 1e-9
    mask_p = np.abs(P) / mag
    perc = np.stack([librosa.istft(librosa.stft(y[c]) * mask_p, length=y.shape[1])
                     for c in range(y.shape[0])])
    rest = y - perc
    sos = butter(4, 180.0 / (sr / 2), btype="lowpass", output="sos")
    bass = sosfiltfilt(sos, rest, axis=1)
    other = rest - bass
    vocals = np.zeros_like(other)

    for part, buf in (("drums", perc), ("bass", bass), ("other", other), ("vocals", vocals)):
        _write_part(dest, part, buf.T, sr, fmt)
    _write_meta(dest, source=track_path, backend="hpss", model="hpss", format=fmt,
                parts=list(PARTS), vocals="пустой: HPSS вокал не выделяет",
                seconds=round(time.time() - t0, 1))
    paths = stem_paths(track_path)
    return ({"ok": True, "paths": paths, "seconds": round(time.time() - t0, 1)} if paths
            else {"ok": False, "error": "не записалось"})


# ----------------------------------------------------- разделение: demucs

def _run(cmd: list[str], timeout: float) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"не уложился в {timeout / 60:.0f} мин"
    except OSError as exc:
        return False, str(exc)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return False, " / ".join(t.strip() for t in tail)
    return True, ""


def _demucs_four(track_path: str, dest: Path, model: str, fmt: str,
                 device: str | None, timeout: float) -> tuple[bool, str]:
    """Четыре слоя demucs прямо в dest."""
    tmp = dest / "_work"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    py = demucs_python() or sys.executable
    cmd = [py, "-m", "demucs", "-n", model, "-o", str(tmp)]
    if fmt == "mp3":
        cmd += ["--mp3", "--mp3-bitrate", "320"]
    if device:
        cmd += ["-d", device]
    cmd.append(track_path)

    ok, err = _run(cmd, timeout)
    if not ok:
        shutil.rmtree(tmp, ignore_errors=True)
        return False, "demucs: " + err

    moved = 0
    for part in PARTS:
        found = sorted(tmp.rglob(f"{part}.*"))
        if found:
            shutil.move(str(found[0]), str(dest / found[0].name))
            moved += 1
    shutil.rmtree(tmp, ignore_errors=True)
    if moved < len(PARTS):
        return False, f"demucs отдал {moved} слоёв из {len(PARTS)}"
    return True, ""


# --------------------------------------------------- разделение: roformer

def _roformer_vocals(track_path: str, work: Path, model_name: str,
                     timeout: float) -> Path | None:
    """Вокал роформером. Возвращает файл вокала или None."""
    work.mkdir(parents=True, exist_ok=True)
    py = demucs_python() or sys.executable
    cmd = [py, "-m", "audio_separator.utils.cli", track_path,
           "--model_filename", model_name, "--output_dir", str(work),
           "--output_format", "WAV"]
    ok, _err = _run(cmd, timeout)
    if not ok:
        # у пакета две точки входа в разных версиях — пробуем консольную
        ok, _err = _run(["audio-separator", track_path, "--model_filename", model_name,
                         "--output_dir", str(work), "--output_format", "WAV"], timeout)
    if not ok:
        return None
    cands = [f for f in work.glob("*.wav") if "vocal" in f.name.lower()
             and "instrument" not in f.name.lower()]
    return cands[0] if cands else None


def _swap_in_roformer_vocals(dest: Path, track_path: str, voc_file: Path,
                             fmt: str, sr: int = 44100) -> bool:
    """Подменяет вокал роформерным и ПЕРЕСЧИТЫВАЕТ гармонию.

    Просто положить чужой вокал рядом нельзя: сумма слоёв перестанет
    давать исходный трек, и на переходе поедет громкость — тем сильнее,
    чем громче вокал. Поэтому other := микс − барабаны − бас − вокал.
    Так сумма сходится по построению, а не по удаче.
    """
    import numpy as np
    import soundfile as sf

    def rd(p) -> np.ndarray | None:
        try:
            y, _ = sf.read(str(p), always_2d=True, dtype="float32")
            return y
        except Exception:
            try:
                import librosa
                y, _ = librosa.load(str(p), sr=sr, mono=False)
                y = np.atleast_2d(y)
                return y.T if y.shape[0] <= 2 else y
            except Exception:
                return None

    mix = rd(track_path)
    voc = rd(voc_file)
    if mix is None or voc is None:
        return False
    layers = {}
    for part in ("drums", "bass"):
        for ext in (".mp3", ".wav", ".flac"):
            f = dest / f"{part}{ext}"
            if f.exists():
                layers[part] = rd(f)
                break
    if any(layers.get(p) is None for p in ("drums", "bass")):
        return False

    n = min(len(mix), len(voc), *(len(v) for v in layers.values()))
    if n < sr:
        return False

    def fix(a):
        a = a[:n]
        return np.repeat(a, 2, axis=1) if a.shape[1] == 1 else a[:, :2]

    mix, voc = fix(mix), fix(voc)
    other = mix - fix(layers["drums"]) - fix(layers["bass"]) - voc

    ext = ".mp3" if fmt == "mp3" else ".wav"
    for part, buf in (("vocals", voc), ("other", other)):
        for old in dest.glob(f"{part}.*"):
            old.unlink(missing_ok=True)
        if ext == ".mp3":
            try:
                import set_export
                set_export._write_mp3(str(dest / f"{part}.wav"), buf, sr, 320)
                Path(dest / f"{part}.wav").unlink(missing_ok=True)
                continue
            except Exception:
                pass
        sf.write(str(dest / f"{part}.wav"), buf, sr, subtype="PCM_16")
    return True


# ------------------------------------------------- живой формат: stem.mp4

def stem_mp4_for(track_path: str) -> Path | None:
    """Готовый .stem.mp4 рядом с треком, если он есть и не устарел."""
    if stem_mp4 is None:
        return None
    out = stem_mp4.stem_mp4_path(track_path)
    if not out.exists():
        return None
    try:
        if out.stat().st_mtime < os.path.getmtime(track_path):
            return None  # трек заменили — стем устарел
    except OSError:
        return None
    return out


def build_live_stems(track_path: str, force: bool = False,
                     bitrate: str = "256k") -> dict:
    """Собирает .stem.mp4 рядом с треком из уже посчитанных слоёв.

    Это и есть ответ на «почему живые стемы нельзя». Можно: Mixxx 2.6+
    читает формат NI STEMS (провайдер «STEM with FFmpeg» регистрируется
    на stem.mp4 и stem.m4a), и всё, чего не хватало DARAVE, — упаковать
    четыре посчитанных слоя в один файл с манифестом. Отдельные wav'ы в
    кэше Mixxx не видит и увидеть не может.

    Файл кладётся В ПАПКУ С ТРЕКОМ, а не в кэш DARAVE: медиатека Mixxx
    работает с файлами на диске, и стем в служебной папке для неё не
    существует."""
    if stem_mp4 is None:
        return {"ok": False, "error": "модуль stem_mp4 не найден"}
    paths = stem_paths(track_path)
    if not paths:
        return {"ok": False, "error": "слои ещё не посчитаны"}
    out = stem_mp4.stem_mp4_path(track_path)
    if out.exists() and not force:
        try:
            if out.stat().st_mtime >= os.path.getmtime(track_path):
                bad = stem_mp4.verify(out)
                if not bad:
                    return {"ok": True, "path": str(out), "cached": True}
        except OSError:
            pass
    try:
        tags = _read_tags(track_path)
        return stem_mp4.build_stem_mp4(
            track_path, {k: paths[k] for k in stem_mp4.STEM_ORDER}, out,
            bitrate=bitrate, title=tags.get("title"), artist=tags.get("artist"))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _read_tags(track_path: str) -> dict:
    """Название и исполнитель из исходника — чтобы стем не появился в
    медиатеке Mixxx безымянным."""
    try:
        from mutagen import File as MutagenFile

        m = MutagenFile(track_path, easy=True)
        if m:
            return {"title": (m.get("title") or [None])[0],
                    "artist": (m.get("artist") or [None])[0]}
    except Exception:
        pass
    stem = Path(track_path).stem
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return {"title": title.strip(), "artist": artist.strip()}
    return {"title": stem, "artist": None}


# ------------------------------------------------------------ точка входа

def separate_track(track_path: str, fmt: str = "mp3", model: str | None = None,
                   device: str | None = None, timeout: float = 3600.0,
                   backend: str = "auto", live: bool = True) -> dict:
    """Считает четыре слоя одного трека. {'ok', 'paths'|'error', 'seconds'}.

    live=True (по умолчанию) — сразу собрать .stem.mp4 рядом с треком,
    чтобы слои были доступны не только рендеру, но и Mixxx на живом
    выступлении."""
    if not os.path.exists(track_path):
        return {"ok": False, "error": "файл не найден"}
    backend = resolve_backend(backend)
    device = pick_device(device)
    if backend == "hpss":
        r = separate_hpss(track_path, fmt=fmt)
        if r.get("ok") and live:
            r["live"] = build_live_stems(track_path)
        return r

    have = stem_paths(track_path)
    if have:
        out = {"ok": True, "paths": have, "seconds": 0.0, "cached": True,
               "backend": stem_meta(track_path).get("backend")}
        if live:
            out["live"] = build_live_stems(track_path)
        return out

    model = model or (FAST_MODEL if backend == "fast" else MODEL)
    dest = stem_dir_for(track_path)
    dest.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    ok, err = _demucs_four(track_path, dest, model, fmt, device, timeout)
    if not ok:
        return {"ok": False, "error": err}

    vocals_from = model
    if backend == "roformer":
        work = dest / "_rof"
        got = None
        for name in ROFORMER_MODELS:
            got = _roformer_vocals(track_path, work, name, timeout)
            if got is not None:
                if _swap_in_roformer_vocals(dest, track_path, got, fmt):
                    vocals_from = name
                break
        shutil.rmtree(work, ignore_errors=True)

    paths = stem_paths(track_path)
    if not paths:
        return {"ok": False, "error": "стемы не собрались"}
    _write_meta(dest, source=track_path, backend=backend, model=model,
                vocals_model=vocals_from, format=fmt, parts=list(PARTS),
                device=device or "cpu", seconds=round(time.time() - t0, 1))
    out = {"ok": True, "paths": paths, "backend": backend, "device": device or "cpu",
           "vocals_model": vocals_from, "seconds": round(time.time() - t0, 1)}
    if live:
        out["live"] = build_live_stems(track_path)
    return out


def separate_library(track_paths: list[str], fmt: str = "mp3", model: str | None = None,
                     device: str | None = None, progress=None, log=print,
                     stop_after: float | None = None, backend: str = "auto",
                     live: bool = True) -> dict:
    """Считает стемы для всей библиотеки, пропуская уже посчитанные."""
    backend = resolve_backend(backend)
    problem = backend_problem(backend)
    if problem and backend == "roformer":
        log("ВНИМАНИЕ: " + problem)
        backend = "demucs"
        problem = backend_problem(backend)
    if problem:
        log("НЕ ЗАПУСКАЮ: " + problem)
        return {"total": len(track_paths), "already": 0, "done": 0,
                "failed": [], "seconds": 0.0, "backend": backend,
                "error": problem, "device": device_info()}
    device = pick_device(device)
    todo = [p for p in track_paths if not stem_paths(p)]
    log(f"стемы: {len(track_paths) - len(todo)} уже есть, считать {len(todo)}")
    info = device_info()
    if live and stem_mp4 is None:
        log("ВНИМАНИЕ: модуль stem_mp4 не найден — .stem.mp4 собран не будет")
        live = False
    if live and not stem_mp4.ffmpeg_exe():
        log("ВНИМАНИЕ: не найден ffmpeg — .stem.mp4 собран не будет. "
            "Поставьте: winget install Gyan.FFmpeg")
        live = False
    if backend == "hpss":
        log("быстрое разделение (HPSS): без модели и GPU, вокала не будет")
    else:
        log(f"устройство: {'CUDA ' + str(info.get('name')) if info.get('cuda') else 'CPU'} — {info.get('note')}")
        log(f"способ: {backend}" + (" (htdemucs_ft + роформер на вокал)" if backend == "roformer" else ""))

    done, failed = 0, []
    t0 = time.time()
    for i, p in enumerate(todo):
        if stop_after and time.time() - t0 > stop_after:
            failed.append({"path": p, "error": "остановлено по времени"})
            break
        r = separate_track(p, fmt=fmt, model=model, device=device, backend=backend,
                           live=live)
        if r.get("ok"):
            done += 1
            log(f"  [{i + 1}/{len(todo)}] {os.path.basename(p)[:48]} — {r.get('seconds', 0):.0f}с")
        else:
            failed.append({"path": p, "error": r.get("error")})
            log(f"  [{i + 1}/{len(todo)}] {os.path.basename(p)[:48]} — ОШИБКА: {r.get('error')}")
        if progress:
            progress(i + 1, len(todo), done, len(failed))

    # Треки, у которых слои были посчитаны раньше, но живого файла ещё нет:
    # догоняем их без пересчёта модели — это секунды на трек.
    live_built, live_failed = 0, []
    if live:
        for p in track_paths:
            if p in todo or stem_mp4_for(p) or not stem_paths(p):
                continue
            r = build_live_stems(p)
            if r.get("ok"):
                live_built += 1
            else:
                live_failed.append({"path": p, "error": r.get("error")})
        if live_built:
            log(f"собрано .stem.mp4 из готовых слоёв: {live_built}")
        for f in live_failed[:5]:
            log(f"  .stem.mp4 не собрался: {os.path.basename(f['path'])[:48]} — {f['error']}")

    return {"total": len(track_paths), "already": len(track_paths) - len(todo),
            "done": done, "failed": failed, "seconds": round(time.time() - t0, 1),
            "backend": backend, "device": info,
            "live_mp4": sum(1 for p in track_paths if stem_mp4_for(p)),
            "live_failed": live_failed}


def library_coverage(track_paths: list[str]) -> dict:
    have = [p for p in track_paths if stem_paths(p)]
    kinds: dict[str, int] = {}
    for p in have:
        kinds[stem_meta(p).get("backend", "?")] = kinds.get(stem_meta(p).get("backend", "?"), 0) + 1
    live = [p for p in track_paths if stem_mp4_for(p)]
    return {"tracks": len(track_paths), "with_stems": len(have),
            "share": round(len(have) / len(track_paths), 3) if track_paths else 0.0,
            "by_backend": kinds,
            "live_mp4": len(live),
            "live_share": round(len(live) / len(track_paths), 3) if track_paths else 0.0}


def main() -> int:
    ap = argparse.ArgumentParser(description="DARAVE — разделение треков на четыре слоя")
    ap.add_argument("--track", help="один файл")
    ap.add_argument("--dir", help="папка с музыкой")
    ap.add_argument("--format", default="mp3", choices=["mp3", "wav"])
    ap.add_argument("--device", default=None, help="cuda | cpu (по умолчанию — что найдёт demucs)")
    ap.add_argument("--backend", default="auto",
                    choices=["auto", "roformer", "demucs", "fast", "hpss"],
                    help="auto — лучшее из установленного; roformer — максимум качества; "
                         "demucs — htdemucs_ft; fast — базовая htdemucs; hpss — без модели")
    ap.add_argument("--info", action="store_true", help="только показать окружение")
    ap.add_argument("--no-live", action="store_true",
                    help="не собирать .stem.mp4 (по умолчанию собирается рядом с треком)")
    ap.add_argument("--only-live", action="store_true",
                    help="ничего не считать, только собрать .stem.mp4 из готовых слоёв")
    ap.add_argument("--rebuild-live", action="store_true",
                    help="пересобрать .stem.mp4, даже если он уже есть")
    ap.add_argument("--mp4-bitrate", default="256k",
                    help="битрейт каждой дорожки .stem.mp4 (по умолчанию 256k)")
    args = ap.parse_args()

    if args.info:
        info = device_info()
        info["backend_auto"] = resolve_backend("auto")
        info["device_auto"] = pick_device() or "cpu"
        info["ffmpeg"] = (stem_mp4.ffmpeg_exe() if stem_mp4 else None)
        info["live_stems"] = bool(info["ffmpeg"]) and stem_mp4 is not None
        if not info["live_stems"]:
            info["live_note"] = ("для .stem.mp4 нужен ffmpeg: "
                                 "winget install Gyan.FFmpeg")
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    paths = []
    if args.track:
        paths = [args.track]
    elif args.dir:
        for root, _d, files in os.walk(args.dir):
            for f in files:
                low = f.lower()
                # сами стемовые файлы — не исходники, иначе разделим стем стема
                if low.endswith((".stem.mp4", ".stem.m4a")):
                    continue
                if low.endswith(AUDIO_EXT):
                    paths.append(os.path.join(root, f))
    if not paths:
        print("Укажите --track или --dir")
        return 1

    if args.only_live:
        built, failed = 0, []
        for p in paths:
            r = build_live_stems(p, force=args.rebuild_live, bitrate=args.mp4_bitrate)
            if r.get("ok"):
                built += 1
                print(f"  {os.path.basename(p)[:56]} — "
                      + ("уже был" if r.get("cached") else f"{r.get('bytes', 0) / 1e6:.0f} МБ"))
            elif r.get("error") != "слои ещё не посчитаны":
                failed.append((p, r.get("error")))
                print(f"  {os.path.basename(p)[:56]} — ОШИБКА: {r.get('error')}")
        print(json.dumps({"live_mp4": built, "failed": len(failed)},
                         ensure_ascii=False, indent=2))
        return 0

    r = separate_library(paths, fmt=args.format, device=args.device,
                         backend=args.backend, live=not args.no_live)
    print(json.dumps({k: v for k, v in r.items() if k != "failed"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
