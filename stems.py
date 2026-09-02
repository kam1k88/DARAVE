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
import re
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
    # Порядок — по среднему SDR на вокале из таблицы самого audio-separator
    # (models-scores.json), а не по репутации имени. Проверено на
    # установленной 0.47.0: 11.53 / 11.49 / 11.17.
    "mel_band_roformer_kim_ft_unwa.ckpt",
    "vocals_mel_band_roformer.ckpt",
    "melband_roformer_big_beta4.ckpt",
    # Долго был лучшим и остаётся значением по умолчанию у самого CLI, но
    # в списке загрузки 0.47.0 его уже нет. Оставлен последним для других
    # версий пакета; недоступные имена всё равно отсеются на проверке.
    "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
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
    """Есть ли GPU и КАКАЯ у torch сборка.

    Одного `cuda: false` мало: причин две, и лечатся они по-разному.
    Либо torch собран без CUDA вовсе (`torch.version.cuda is None`) — с
    обычного PyPI под Windows приезжает именно такой, и никакие драйверы
    его не оживят, нужна переустановка с индекса PyTorch. Либо сборка
    CUDA-шная, а карта не видна — тогда дело в драйвере.

    Разница дорогая: на CPU трек считается 20-40 минут вместо 2-4, то
    есть библиотека из 48 треков — сутки против часа. Молча уйти в CPU и
    оставить диджея гадать, почему «стемы считаются вечно», нельзя."""
    try:
        import torch
    except Exception:
        return {"torch": False, "cuda": False, "name": None, "torch_build": None,
                "demucs": has_module("demucs"), "audio_separator": has_module("audio_separator"),
                "note": "torch не установлен: pip install -r requirements-stems.txt"}
    build = getattr(getattr(torch, "version", None), "cuda", None)
    try:
        cuda = bool(torch.cuda.is_available())
        name = torch.cuda.get_device_name(0) if cuda else None
    except Exception:
        cuda, name = False, None

    if cuda:
        note = f"GPU {name}: примерно 2-4 минуты на трек в максимальном качестве"
    elif not build:
        note = ("torch собран БЕЗ CUDA (это обычное колесо с PyPI) — считать будет "
                "процессор, 20-40 минут на трек. Переустановите с индекса PyTorch:\n"
                "  pip uninstall -y torch torchaudio\n"
                "  pip install torch --index-url https://download.pytorch.org/whl/cu128\n"
                "Если под вашу версию Python колёс cu* нет, поставьте Python 3.12 и "
                "запустите setup_stems.ps1 — он положит стем-стек туда.")
    else:
        note = (f"torch собран под CUDA {build}, но видеокарта не видна — "
                "проверьте драйвер NVIDIA. Пока считает процессор, 20-40 минут на трек.")

    return {
        "torch": True, "torch_version": getattr(torch, "__version__", "?"),
        "torch_build": build or "cpu",
        "cuda": cuda, "name": name,
        "demucs": bool(demucs_python()),
        "demucs_python": demucs_python(),
        "audio_separator": has_module("audio_separator"),
        "note": note,
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
    """Что считать по умолчанию.

    Раньше «auto» означало «роформер, если он установлен». Опыт показал,
    что это плохой выбор по умолчанию: у роформера слишком много способов
    сломаться молча (не поднялись веса — отдаёт тишину; имя слоя в
    названии модели — подставляется не тот файл), а каждая такая поломка
    стоит прогона по всей библиотеке. У чистого demucs этого класса
    ошибок нет вовсе: четыре слоя приходят из одной модели и складываются
    в исходный трек по построению, без вычитаний и выравниваний.

    Поэтому по умолчанию — demucs, а роформер включается явно
    (`--backend roformer`) и обязан доказать, что работает: вокальный
    слой проверяется и на тишину, и на «это весь микс»."""
    if backend != "auto":
        return backend
    return "demucs" if demucs_python() else "hpss"


# Модули, без которых ветка MDXC (это и есть роформер) не стартует.
# Проверено по установленному пакету 0.47.0: onnxruntime импортируется в
# separator.py на верхнем уровне, ml_collections и rotary_embedding_torch —
# в mdxc_separator.py и самой архитектуре роформера, pydub — в
# common_separator.py. resampy, samplerate и diffq в пакете НЕ используются
# на этом пути, поэтому в список не входят.
ROFORMER_MODULES = ("onnxruntime", "ml_collections", "rotary_embedding_torch", "pydub",
                    "audioread")
_PIP_NAME = {"rotary_embedding_torch": "rotary-embedding-torch"}
_rof_miss_cache: list | None = None


def missing_for_roformer() -> list[str]:
    """Каких модулей не хватает роформеру в том Python, где стоит demucs.

    Спрашиваем ОДИН раз и все сразу: иначе pip-«догонялки» идут по одному
    модулю за прогон, а прогон — это часы."""
    global _rof_miss_cache
    if _rof_miss_cache is not None:
        return list(_rof_miss_cache)
    py = demucs_python() or sys.executable
    miss: list[str] = []

    # 1. Быстрая проверка известных имён — все сразу, а не по одному.
    code = ("import importlib.util as u, json;"
            "print(json.dumps([m for m in %r if u.find_spec(m) is None]))" % (ROFORMER_MODULES,))
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=90)
        if r.returncode == 0:
            miss = json.loads(r.stdout.strip() or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    # 2. И настоящая попытка импорта всей цепочки роформера. Список выше
    #    поддерживать руками бесполезно: пакет ставился с --no-deps, и
    #    недостающие модули всплывали по одному за прогон — сначала
    #    onnxruntime, потом audioread из spec_utils. Импорт находит их
    #    сам, включая те, о которых мы ещё не знаем.
    if not miss:
        probe = ("import audio_separator.separator.architectures.mdxc_separator;"
                 "import audio_separator.separator.uvr_lib_v5.spec_utils;"
                 "print('OK')")
        try:
            r = subprocess.run([py, "-c", probe], capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                err = (r.stderr or "") + (r.stdout or "")
                m = re.search(r"No module named ['\"]([\w.]+)['\"]", err)
                if m:
                    miss = [m.group(1).split(".")[0]]
                else:
                    tail = [ln.strip() for ln in err.strip().splitlines() if ln.strip()][-1:]
                    miss = ["?" + (tail[0][:160] if tail else "не смог импортировать")]
        except (OSError, subprocess.SubprocessError):
            pass

    _rof_miss_cache = list(miss)
    return miss


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
                    "вокал чуть хуже роформера, но заметно лучше базовой htdemucs. "
                    + ROFORMER_INSTALL_HINT)
        miss = missing_for_roformer()
        if miss and miss[0].startswith("?"):
            return ("audio-separator стоит, но не импортируется: "
                    + miss[0][1:]
                    + "\nРоформер без этого не считает вокал.")
        if miss:
            names = " ".join(_PIP_NAME.get(m, m) for m in miss)
            return (f"audio-separator стоит, но не запускается: нет модуля "
                    f"{', '.join(miss)}. Так и было: 40 треков подряд падали с "
                    f"ModuleNotFoundError, потому что пакет ставился с --no-deps. "
                    f"Поставьте одной командой:\n"
                    f'    "{py}" -m pip install {names}\n'
                    f"onnxruntime нужен обычный, не -gpu: роформер считает на torch, "
                    f"а onnxruntime здесь только импортируется — CPU-колесо не "
                    f"замедляет и не требует CUDA-DLL.")
    return None


# Почему обычная установка audio-separator падает и что с этим делать.
#
# `pip install audio-separator` тянет diffq-fixed, а та собирается из
# исходников и на Python 3.13+ падает: её setup.py зовёт Cython на
# bitpack.pyx, которого в sdist нет («'bitpack.pyx' doesn't match any
# files»). Колёс под новые Python у неё тоже нет.
#
# Существенно вот что: **роформеру diffq не нужен**. В audio-separator
# он импортируется только внутри вложенного кода demucs
# (uvr_lib_v5/demucs/states.py и соседние), а архитектуры выбираются
# лениво по имени класса — роформер идёт веткой MDXC и этот модуль не
# загружает. То есть зависимость обязательна для demucs-моделей ВНУТРИ
# пакета, которыми мы не пользуемся: свой demucs у нас стоит отдельно.
#
# Отсюда установка без зависимостей и доставка остальных руками.
ROFORMER_INSTALL_HINT = (
    "Ставить надо БЕЗ зависимостей, иначе pip упрётся в diffq-fixed:\n"
    "  pip install audio-separator --no-deps\n"
    "  pip install \"beartype==0.18.5\" einops julius librosa ml_collections "
    "numpy onnx onnx2torch-py313 pydub pyyaml requests resampy samplerate "
    "scipy six soundfile tqdm rotary-embedding-torch audioop-lts\n"
    "  pip install onnxruntime-gpu\n"
    "Проверено: у всех этих пакетов есть колёса под Python 3.14/Windows, "
    "и только у diffq-fixed их нет."
)


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


# Папка со стемами ищется ДВУМЯ способами, и второй важнее первого.
#
# Быстрый путь — по ключу из пути, времени правки и размера файла. Он
# бесплатный, пока файл не трогали.
#
# Но время правки — плохое основание для «это тот же трек»: его меняет
# любая запись тега. Ровно это и случилось: что-то (Mixxx с включённой
# записью метаданных, теговый редактор — неважно) переписало теги всех
# 64 файлов, mtime у всех стал сегодняшним, и из 64 ключей совпало ДВА.
# Стемы никуда не делись — их просто перестали находить, и в вебе
# колонка STEMS у всей библиотеки стала прочерком. Часы работы GPU
# обесценились от записи тега, не изменившей ни одного отсчёта звука.
#
# Поэтому есть второй путь: индекс по полю `source` из meta.json — там
# записан исходный трек, для которого слои считались. Он переживает и
# правку тегов, и переименование папки кэша. Если файл действительно
# заменили другим, слои пересчитываются флагом --rebuild: молча
# выбрасывать работу по косвенному признаку хуже, чем один раз попросить.

_SRC_TTL = 30.0
_src_index: dict[str, str] = {}
_src_index_at = 0.0


def _norm_src(p) -> str:
    return str(p or "").replace("\\", "/").lower()


def _source_index() -> dict[str, str]:
    """{нормализованный путь исходного трека: имя папки со слоями}."""
    global _src_index_at
    now = time.time()
    if _src_index and now - _src_index_at < _SRC_TTL:
        return _src_index
    idx: dict[str, tuple[float, str]] = {}
    try:
        entries = list(STEM_DIR.iterdir())
    except OSError:
        entries = []
    for d in entries:
        meta = d / "meta.json"
        try:
            if not meta.is_file():
                continue
            j = json.loads(meta.read_text(encoding="utf-8"))
            if not j.get("complete"):
                continue
            src = _norm_src(j.get("source"))
            if not src:
                continue
            ts = meta.stat().st_mtime
        except (OSError, ValueError):
            continue
        # Если у трека несколько папок (пересчитывали) — берём свежую.
        if src not in idx or ts > idx[src][0]:
            idx[src] = (ts, d.name)
    _src_index.clear()
    _src_index.update({k: v[1] for k, v in idx.items()})
    _src_index_at = now
    return _src_index


def stem_dir_for(track_path: str) -> Path:
    fast = STEM_DIR / stem_key(track_path)
    try:
        if (fast / "meta.json").is_file():
            return fast
    except OSError:
        pass
    name = _source_index().get(_norm_src(track_path))
    return STEM_DIR / name if name else fast


# Ответ «есть ли стемы» стоит четырёх обращений к диску, а спрашивают его
# тысячами: подбор шва перебирает сотни пар, и на каждую пару приходится
# по несколько вопросов о слоях. Замер на 65 треках: один подбор делал
# 30 000 stat() и занимал 14 секунд — почти всё время уходило в файловую
# систему. Ответ живёт секунды: разделение идёт минутами, и то, что
# стемов ещё нет, за время одного запроса не изменится.
_PATHS_TTL = 15.0
_paths_cache: dict[str, tuple[float, dict | None]] = {}


def invalidate_stem_cache(track_path: str | None = None) -> None:
    """Забыть кэш готовности — после разделения или удаления слоёв."""
    global _src_index_at
    _src_index_at = 0.0
    if track_path is None:
        _paths_cache.clear()
    else:
        _paths_cache.pop(str(track_path), None)


def stem_paths(track_path: str) -> dict | None:
    key = str(track_path)
    hit = _paths_cache.get(key)
    now = time.time()
    if hit is not None and now - hit[0] < _PATHS_TTL:
        return hit[1]
    out = _stem_paths_uncached(track_path)
    _paths_cache[key] = (now, out)
    return out


def _stem_paths_uncached(track_path: str) -> dict | None:
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
    return _parts_on_disk(d)


def _parts_on_disk(d: Path) -> dict | None:
    """Полный комплект файлов слоёв в папке — БЕЗ проверки meta.json.

    Нужно ровно в одном месте: сразу после разделения, когда слои уже
    записаны, а метка готовности ещё нет (она пишется последней и как раз
    по итогу этой проверки). Всем остальным нужен `stem_paths`, который
    метку требует."""
    out = {}
    for part in PARTS:
        for ext in (".mp3", ".wav", ".flac"):
            f = d / f"{part}{ext}"
            if f.exists() and f.stat().st_size > 1024:
                out[part] = str(f)
                break
    return out if len(out) == len(PARTS) else None


_meta_cache: dict[str, tuple[float, dict]] = {}


def stem_meta(track_path: str) -> dict:
    """Чем именно посчитаны стемы этого трека — чтобы «качественные» не
    оказались вчерашним HPSS-черновиком.

    Кэшируется на те же секунды, что и готовность слоёв, и по той же
    причине: подбор шва спрашивает про вокал тысячи раз за запрос."""
    key = str(track_path)
    hit = _meta_cache.get(key)
    now = time.time()
    if hit is not None and now - hit[0] < _PATHS_TTL:
        return hit[1]
    out = _stem_meta_uncached(track_path)
    _meta_cache[key] = (now, out)
    return out


def _stem_meta_uncached(track_path: str) -> dict:
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
    # Кэш готовности врал бы ещё несколько секунд после разделения —
    # ровно в тот момент, когда веб спрашивает «ну что, готово?».
    invalidate_stem_cache()
    _meta_cache.clear()


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
    have = None if rebuild else stem_paths(track_path)
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

def _move_with_retry(src: Path, dst: Path, attempts: int = 8, delay: float = 0.4) -> None:
    """shutil.move, но с повтором.

    На Windows свежесозданный demucs'ом mp3 иногда ещё секунду-две держит
    антивирус (реалтайм-скан) или не до конца отпускает кодировщик,
    которого demucs дёргает отдельным процессом. Само по себе это не
    ошибка разделения — файл цел, просто он занят чуть дольше, чем
    subprocess.run() ждал завершения demucs. Раньше первая же попытка
    move() валила весь трек с PermissionError [WinError 32], хотя через
    секунду файл уже был свободен."""
    last: PermissionError | None = None
    for i in range(attempts):
        try:
            shutil.move(str(src), str(dst))
            return
        except PermissionError as exc:
            last = exc
            time.sleep(delay * (i + 1))
    raise last


_LAST_RUN_OUTPUT = ""


def _run(cmd: list[str], timeout: float) -> tuple[bool, str]:
    global _LAST_RUN_OUTPUT
    _LAST_RUN_OUTPUT = ""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"не уложился в {timeout / 60:.0f} мин"
    except OSError as exc:
        return False, str(exc)
    _LAST_RUN_OUTPUT = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return False, " / ".join(t.strip() for t in tail)
    return True, ""


def _demucs_four(track_path: str, dest: Path, model: str, fmt: str,
                 device: str | None, timeout: float,
                 overlap: float | None = None) -> tuple[bool, str]:
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
    # Перекрытие окон. По умолчанию demucs берёт 0.25 — это вчетверо
    # больше пересчёта, чем 0.1, а разница на слух невелика. Отдельной
    # ручкой, потому что цена времени тут заметная: на библиотеке из
    # 65 треков это часы.
    if overlap is not None:
        cmd += ["--overlap", str(overlap)]
    cmd.append(track_path)

    ok, err = _run(cmd, timeout)
    if not ok:
        shutil.rmtree(tmp, ignore_errors=True)
        return False, "demucs: " + err

    moved = 0
    for part in PARTS:
        found = sorted(tmp.rglob(f"{part}.*"))
        if found:
            _move_with_retry(found[0], dest / found[0].name)
            moved += 1
    shutil.rmtree(tmp, ignore_errors=True)
    if moved < len(PARTS):
        return False, f"demucs отдал {moved} слоёв из {len(PARTS)}"
    return True, ""


# --------------------------------------------------- разделение: roformer

_rof_avail_cache: list | None = None


def _installed_roformer_models() -> set[str]:
    """Какие .ckpt установленный audio-separator РЕАЛЬНО умеет скачать.

    Имена моделей меняются от версии к версии, и молча просить
    несуществующую — значит каждый раз тихо оставаться на вокале demucs.
    Поэтому спрашиваем сам пакет (его models.json), а не гадаем. На
    установленной 0.47.0 из трёх имён, которые DARAVE просил раньше, в
    списке загрузки было ровно одно."""
    global _rof_avail_cache
    if _rof_avail_cache is not None:
        return set(_rof_avail_cache)
    code = ("import audio_separator, os, json;"
            "p=os.path.join(os.path.dirname(audio_separator.__file__),'models.json');"
            "d=json.load(open(p, encoding='utf-8'));"
            "print(chr(10).join(k for t in d.get('roformer_download_list',{}).values()"
            " for k in t if k.endswith('.ckpt')))")
    names: set[str] = set()
    try:
        r = subprocess.run([demucs_python() or sys.executable, "-c", code],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            names = {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}
    except (OSError, subprocess.SubprocessError):
        pass
    _rof_avail_cache = sorted(names)
    return names


def roformer_candidates() -> list[str]:
    """Желаемые модели, из которых оставлены доступные в этой версии."""
    avail = _installed_roformer_models()
    if not avail:                       # список прочитать не удалось — пробуем всё
        return list(ROFORMER_MODELS)
    return [m for m in ROFORMER_MODELS if m in avail]


def _file_rms_db(path) -> float:
    """Громкость файла в dB — чтобы отличить настоящий вокал от тишины
    ДО того, как она попадёт в слой."""
    buf = _ffmpeg_decode(str(path), 44100, 1)
    if buf is None or len(buf) == 0:
        return -999.0
    return _rms_db(buf)


def _roformer_vocals(track_path: str, work: Path, model_name: str, timeout: float):
    """Вокал роформером. Возвращает (массив вокала или None, причина отказа).

    ПОЧЕМУ НЕ `-m audio_separator.utils.cli`. У пакета в `utils/cli.py`
    нет блока `if __name__ == "__main__"`, и `__main__.py` тоже нет:
    запуск модулем импортирует файл, объявляет main() и выходит с кодом
    0, НИЧЕГО не посчитав. Зовём main() напрямую.

    ПОЧЕМУ ПРОВЕРЯЕМ ГРОМКОСТЬ. Раньше брался первый файл, в имени
    которого есть «vocal», и принимался на веру. На библиотеке диджея это
    дало вокальный слой −80..−128 dB (тишина) на ВСЕХ треках, включая те,
    где вокал очевидно есть, — а сам вокал остался в гармонии. Тишина
    больше не принимается за результат: если рядом лежит инструментал,
    вокал берётся вычитанием (микс − инструментал)."""
    work.mkdir(parents=True, exist_ok=True)
    py = demucs_python() or sys.executable
    args = [track_path, "--model_filename", model_name,
            "--output_dir", str(work), "--output_format", "WAV"]
    launcher = "import sys; from audio_separator.utils.cli import main; sys.exit(main() or 0)"
    ok, err = _run([py, "-c", launcher] + args, timeout)
    if not ok:
        ok, err2 = _run(["audio-separator"] + args, timeout)
        if not ok:
            return None, err or err2

    # На чём он на самом деле считал. Вопрос не праздный: пакет ставит
    # torch на CUDA сразу, как только её видит, а провайдер ONNX остаётся
    # процессорным — и по логу это легко перепутать с «всё на CPU».
    for line in _LAST_RUN_OUTPUT.splitlines():
        if ("Torch device" in line or "hardware acceleration" in line
                or "CPU mode" in line or "acceleration will NOT" in line
                # Загрузка весов: если модель не поднялась, она отдаёт
                # тишину, и это надо видеть, а не гадать по уровню.
                or "Successfully loaded" in line or "implementation failed" in line
                or "Fell back to legacy" in line or "loading stats" in line):
            print("[стемы] роформер: " + line.split(" - ")[-1].strip())

    produced = sorted(work.glob("*.wav"))
    if not produced:
        got = ", ".join(sorted(f.name for f in work.glob("*"))) or "пусто"
        return None, f"вокального файла нет (в папке: {got})"

    loud = {f: _file_rms_db(f) for f in produced}
    print("[стемы] роформер отдал: "
          + "; ".join(f"{f.name} {loud[f]:.1f} dB" for f in produced))

    def stem_of(f: Path) -> str:
        """Имя слоя из «..._(Vocals)_модель.wav».

        Ровно здесь была ошибка, стоившая прогона по всей библиотеке.
        Раньше слой искали подстрокой по ВСЕМУ имени файла — а у модели
        `vocals_mel_band_roformer` слово «vocals» стоит в названии самой
        модели, поэтому её ИНСТРУМЕНТАЛЬНЫЙ файл
        `..._(other)_vocals_mel_band_roformer.wav` считался вокальным. Он
        громкий, поэтому и побеждал. В итоге «вокалом» становился почти
        весь микс, а гармония превращалась в минус бас и барабаны.
        Скобки ставит сам audio-separator, и в них лежит именно слой."""
        m = re.findall(r"_\(([^()]+)\)_", f.name)
        return (m[-1] if m else "").strip().lower()

    def pick(names: tuple[str, ...]):
        best = None
        for f in produced:
            if stem_of(f) in names:
                if best is None or loud[f] > loud[best]:
                    best = f
        return best

    voc_f = pick(("vocals", "vocal"))
    inst_f = pick(("instrumental", "other", "no vocals", "no_vocals"))

    if voc_f is not None and loud[voc_f] > -60.0:
        voc = _ffmpeg_decode(str(voc_f))
        # Вторая проверка, кроме «не тишина»: вокал НЕ должен совпадать с
        # миксом. Если модель не отделила ничего и вернула вход как есть,
        # такой «вокал» вычтется целиком и гармония станет мусором —
        # именно это и случилось, когда слоем ошибочно взяли инструментал.
        mix = _ffmpeg_decode(track_path)
        if voc is not None and mix is not None:
            sh, q = _estimate_shift(mix, voc)
            same_level = abs(_rms_db(voc) - _rms_db(mix)) < 3.0
            if q > 0.9 and same_level:
                return None, (f"{voc_f.name}: это не вокал, а почти весь микс "
                              f"(похожесть {q:.2f}, разница уровней меньше 3 dB)")
        return voc, ""

    # Вокальный файл пуст или его нет — выводим вокал из инструментала.
    if inst_f is not None and loud[inst_f] > -60.0:
        mix = _ffmpeg_decode(track_path)
        inst = _ffmpeg_decode(str(inst_f))
        if mix is not None and inst is not None:
            sh, q = _estimate_shift(mix, inst)
            if q >= 0.3:
                n = min(len(mix), len(inst) + abs(sh))
                voc = mix[:n] - _shift_to(inst, -sh, n)
                lvl = _rms_db(voc)
                was = loud[voc_f] if voc_f is not None else -999.0
                if lvl > -60.0:
                    print(f"[стемы] вокальный файл пуст ({was:.1f} dB) — взял вокал "
                          f"как «микс минус инструментал», {lvl:.1f} dB")
                    return voc, ""
                return None, (f"и вокальный файл, и разность с инструменталом пусты "
                              f"({lvl:.1f} dB) — похоже, в треке действительно нет вокала")
            return None, f"инструментал не выровнялся с миксом (качество {q:.2f})"

    have = "; ".join(f"{f.name} {loud[f]:.1f} dB" for f in produced)
    return None, f"вокал пустой и вывести его не из чего ({have})"


def _ffmpeg_decode(path: str, sr: int = 44100, channels: int = 2):
    """Декодирование через ffmpeg, а не через soundfile.

    Это не придирка к библиотеке, а причина конкретной поломки. У mp3,
    сжатого LAME, в начале стоит служебная задержка кодировщика — 1105
    сэмплов. ffmpeg её снимает по заголовку LAME/Xing, а libsndfile
    отдаёт как есть. Пока слои читались libsndfile, demucs'овы drums и
    bass приезжали сдвинутыми относительно микса, вычитание не сходилось,
    и в other оставались бас и барабаны — те самые «задвоенные со
    смещением». Измерено на треке диджея: слои demucs шли со сдвигом
    −2210 сэмплов (две задержки: одна от исходника, вторая от записи
    слоя), а наш собственный вокал — со сдвигом −1105."""
    import numpy as np

    exe = (stem_mp4.ffmpeg_exe() if stem_mp4 is not None else None) or "ffmpeg"
    proc = subprocess.run(
        [exe, "-v", "error", "-i", str(path), "-f", "f32le",
         "-ac", str(channels), "-ar", str(sr), "-"],
        capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return None
    buf = np.frombuffer(proc.stdout, dtype="<f4").astype("float32")
    if channels > 1:
        buf = buf.reshape(-1, channels)
    return buf


def _estimate_shift(ref, sig, max_lag: int = 8000) -> tuple[int, float]:
    """На сколько сэмплов sig сдвинут относительно ref. (сдвиг, качество).

    Качество — нормированный пик взаимной корреляции: по нему видно,
    можно ли сдвигу верить, или сигналы просто не связаны."""
    import numpy as np

    a = ref.mean(axis=1) if getattr(ref, "ndim", 1) > 1 else ref
    b = sig.mean(axis=1) if getattr(sig, "ndim", 1) > 1 else sig
    n = min(len(a), len(b))
    if n < 44100 * 4:
        return 0, 0.0
    beg = n // 3
    w = min(44100 * 30, n - beg)
    x = a[beg:beg + w].astype("float64"); x -= x.mean()
    y = b[beg:beg + w].astype("float64"); y -= y.mean()
    nfft = 1 << int(np.ceil(np.log2(w * 2)))
    cc = np.fft.irfft(np.fft.rfft(x, nfft) * np.conj(np.fft.rfft(y, nfft)), nfft)
    cc = np.concatenate([cc[-max_lag:], cc[:max_lag + 1]])
    lags = np.arange(-max_lag, max_lag + 1)
    i = int(np.argmax(cc))
    q = float(cc[i] / (np.sqrt((x ** 2).sum() * (y ** 2).sum()) + 1e-12))
    return int(lags[i]), q


def _shift_to(buf, shift: int, n: int):
    """Подвинуть слой на shift и подогнать длину до n."""
    import numpy as np

    if shift > 0:
        pad = np.zeros((shift,) + buf.shape[1:], dtype=buf.dtype)
        buf = np.concatenate([pad, buf])
    elif shift < 0:
        buf = buf[-shift:]
    if len(buf) < n:
        pad = np.zeros((n - len(buf),) + buf.shape[1:], dtype=buf.dtype)
        buf = np.concatenate([buf, pad])
    return buf[:n]


def _rms_db(x) -> float:
    import numpy as np

    return float(20 * np.log10(np.sqrt((x.astype("float64") ** 2).mean()) + 1e-12))


def _layer_file(dest: Path, part: str) -> Path | None:
    for ext in (".mp3", ".wav", ".flac"):
        f = dest / f"{part}{ext}"
        if f.exists():
            return f
    return None


def _write_layer(dest: Path, part: str, buf, sr: int, fmt: str) -> None:
    """Записать слой, убрав прежние файлы этого слоя в любом формате."""
    import soundfile as sf

    for old in dest.glob(f"{part}.*"):
        old.unlink(missing_ok=True)
    if fmt == "mp3":
        try:
            import set_export

            set_export._write_mp3(str(dest / f"{part}.wav"), buf, sr, 320)
            Path(dest / f"{part}.wav").unlink(missing_ok=True)
            return
        except Exception:
            pass
    sf.write(str(dest / f"{part}.wav"), buf, sr, subtype="PCM_16")


def recompute_other(dest: Path, track_path: str, voc_new=None, fmt: str = "mp3",
                    sr: int = 44100) -> dict:
    """Пересчитать гармонию так, чтобы сумма слоёв ДАВАЛА исходный трек.

    Два режима, и разница между ними важна:

    * `voc_new` задан (сразу после разделения) — на диске ещё лежат
      родные слои demucs, и работает тождество
      `other := other_demucs + vocals_demucs − новый вокал`. Микс при
      этом не трогается вовсе, а значит и ошибиться в его выравнивании
      негде: все четыре слоя demucs живут в одной временной сетке.
    * `voc_new` не задан (починка уже посчитанного трека) — родных
      other и vocals уже нет, поэтому считаем от микса:
      `other := микс − drums − bass − vocals`.

    В обоих случаях сдвиг между сеткой demucs и сеткой микса не
    угадывается, а ИЗМЕРЯЕТСЯ по взаимной корреляции, и результат
    проверяется: остаток «микс минус сумма слоёв» обязан быть тихим.
    Раньше проверки не было вовсе — и вычитание молча промахивалось на
    1105 сэмплов, оставляя в other бас и барабаны."""
    import numpy as np

    mix = _ffmpeg_decode(track_path, sr)
    if mix is None:
        return {"ok": False, "error": "не смог декодировать исходный трек"}
    parts = {}
    for part in ("drums", "bass", "other", "vocals"):
        f = _layer_file(dest, part)
        parts[part] = _ffmpeg_decode(str(f), sr) if f else None
    if parts["drums"] is None or parts["bass"] is None:
        return {"ok": False, "error": "нет слоёв drums/bass"}

    # Сетка demucs: сумма его четырёх слоёв обязана давать микс, поэтому
    # по ней сдвиг измеряется надёжнее всего — сигналы совпадают почти
    # целиком, а не «немного похожи».
    before = None
    have_native = voc_new is not None and parts["other"] is not None and parts["vocals"] is not None
    if have_native:
        m = min(len(parts[p]) for p in ("drums", "bass", "other", "vocals"))
        ref = (parts["drums"][:m] + parts["bass"][:m]
               + parts["other"][:m] + parts["vocals"][:m])
    else:
        m = min(len(parts["drums"]), len(parts["bass"]))
        ref = parts["drums"][:m] + parts["bass"][:m]
    shift, quality = _estimate_shift(mix, ref)
    if quality < 0.2:
        return {"ok": False, "error": f"не смог выровнять слои с миксом "
                                      f"(качество совпадения {quality:.2f})"}

    n = m
    mix_a = _shift_to(mix, -shift, n)   # микс в сетке слоёв
    d, b = parts["drums"][:n], parts["bass"][:n]

    if have_native:
        o_nat, v_nat = parts["other"][:n], parts["vocals"][:n]
        before = _rms_db(mix_a - (d + b + o_nat + v_nat))
        # Тождество «other := other + vocals − новый вокал» верно только
        # если четыре слоя на диске действительно дают в сумме микс. Если
        # там лежит результат прежнего кривого вычитания, тождество
        # унаследует его ошибку — тогда честнее считать от микса заново.
        # Так порядок команд перестаёт быть ловушкой.
        if before > -40.0:
            have_native = False
    if have_native:
        voc = _shift_to(np.asarray(voc_new, dtype="float32"), -shift, n)
        if voc.ndim == 1:
            voc = np.repeat(voc[:, None], 2, axis=1)
        other = o_nat + v_nat - voc[:, :2]
    else:
        raw = parts["vocals"]
        if raw is None:
            voc = np.zeros_like(d)
        else:
            # Наш собственный вокал писался mp3 и имеет СВОЮ задержку —
            # её измеряем, а не считаем нулём. Но у почти пустого слоя
            # (инструментал) корреляции нет и быть не может, а сдвигать
            # тишину незачем — тогда просто оставляем как есть.
            vs, vq = _estimate_shift(mix_a, raw)
            silent = _rms_db(raw) < -60.0
            # _shift_to обязателен в любом случае: слой, записанный нами,
            # короче demucs'овых ровно на один MP3-фрейм (1152 сэмпла), и
            # без приведения длины вычитание падало на несовпадении форм.
            voc = _shift_to(raw, -vs if (vq >= 0.2 and not silent) else 0, n)
        if voc_new is not None:
            voc = _shift_to(np.asarray(voc_new, dtype="float32"), -shift, n)[:, :2]
        other = mix_a - d - b - voc

    residual = _rms_db(mix_a - (d + b + voc[:, :2] + other))
    out = {"ok": True, "shift": int(shift), "quality": round(quality, 3),
           "residual_db": round(residual, 1), "other_db": round(_rms_db(other), 1)}
    if before is not None:
        out["native_residual_db"] = round(before, 1)
    _write_layer(dest, "vocals", voc[:, :2], sr, fmt)
    _write_layer(dest, "other", other, sr, fmt)
    return out


def _swap_in_roformer_vocals(dest: Path, track_path: str, voc, fmt: str,
                             sr: int = 44100) -> bool:
    """Подменяет вокал роформерным и ПЕРЕСЧИТЫВАЕТ гармонию.

    Просто положить чужой вокал рядом нельзя: сумма слоёв перестанет
    давать исходный трек, и на переходе поедет громкость — тем сильнее,
    чем громче вокал. Вся арифметика и проверка — в recompute_other."""
    if voc is None:
        return False
    r = recompute_other(dest, track_path, voc_new=voc, fmt=fmt, sr=sr)
    if not r.get("ok"):
        print(f"[стемы] гармония не пересчитана: {r.get('error')}")
        return False
    print(f"[стемы] сумма слоёв сходится: остаток {r['residual_db']} dB, "
          f"сдвиг {r['shift']:+d} сэмплов (качество {r['quality']})")
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
        d = stem_dir_for(track_path)
        have = sorted(f.name for f in d.glob("*")) if d.exists() else []
        return {"ok": False,
                "error": f"слои не найдены (папка {d.name}: "
                         f"{', '.join(have) if have else 'её нет'})"}
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
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


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


def _roformer_pass(track_path: str, dest: Path, fmt: str,
                   timeout: float) -> tuple[str | None, str | None]:
    """Роформерный вокал поверх уже готовых demucs-слоёв в dest.

    Возвращает (имя сработавшей модели, причина отказа). Причина больше не
    выбрасывается: раньше отказ был молчаливым, и «роформер» отличался от
    «demucs» только надписью в meta.json."""
    names = roformer_candidates()
    if not names:
        return None, ("ни одно из имён моделей роформера не известно "
                      "установленному audio-separator")
    work = dest / "_rof"
    err_last = None
    try:
        for name in names:
            got, err = _roformer_vocals(track_path, work, name, timeout)
            if got is None:
                err_last = f"{name}: {err}"
                continue
            if _swap_in_roformer_vocals(dest, track_path, got, fmt):
                return name, None
            return None, f"{name}: вокал посчитан, но подмена слоёв не удалась"
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return None, err_last


def vocals_are_roformer(track_path: str) -> bool:
    """Стоит ли на вокале роформер, а не запасной вокал demucs."""
    return str(stem_meta(track_path).get("vocals_model", "")).endswith(".ckpt")


def redo_vocals(track_path: str, timeout: float = 3600.0, live: bool = True) -> dict:
    """Пересчитать ТОЛЬКО вокал роформером, не запуская demucs заново.

    Понадобилось, когда выяснилось, что роформер не запускался вовсе: в
    meta.json стояло backend=roformer при vocals_model=htdemucs_ft. Гонять
    demucs ради этого не нужно — барабаны и бас уже посчитаны и не
    меняются, а гармония в любом случае ПЕРЕСЧИТЫВАЕТСЯ как микс минус
    drums, bass и новый вокал. Живой .stem.mp4 пересобирается, потому что
    две его дорожки из пяти изменились."""
    paths = stem_paths(track_path)
    if not paths:
        return {"ok": False, "error": "слои ещё не посчитаны"}
    dest = stem_dir_for(track_path)
    meta = dict(stem_meta(track_path))
    t0 = time.time()
    used, err = _roformer_pass(track_path, dest, meta.get("format", "mp3"), timeout)
    if not used:
        return {"ok": False, "error": err or "роформер не отработал"}
    meta.pop("vocals_error", None)
    voc_f = _layer_file(dest, "vocals")
    meta.update(backend="roformer", vocals_model=used,
                vocals_db=round(_file_rms_db(voc_f), 1) if voc_f else None,
                vocals_seconds=round(time.time() - t0, 1))
    _write_meta(dest, **meta)
    out = {"ok": True, "vocals_model": used, "seconds": round(time.time() - t0, 1)}
    if live:
        out["live"] = build_live_stems(track_path, force=True)
    return out


# ------------------------------------------------------------ точка входа

def separate_track(track_path: str, fmt: str = "mp3", model: str | None = None,
                   device: str | None = None, timeout: float = 3600.0,
                   backend: str = "auto", live: bool = True,
                   overlap: float | None = None, rebuild: bool = False) -> dict:
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

    ok, err = _demucs_four(track_path, dest, model, fmt, device, timeout, overlap)
    if not ok:
        return {"ok": False, "error": err}

    vocals_from = model
    vocals_error = None
    if backend == "roformer":
        used, vocals_error = _roformer_pass(track_path, dest, fmt, timeout)
        if used:
            vocals_from = used

    # Порядок здесь важен и однажды уже стоил прогона по всей библиотеке.
    # Готовность стемов определяет meta.json (`complete: true`), и метка
    # пишется ПОСЛЕДНЕЙ — иначе прерванный проход выглядел бы готовым.
    # Но проверять готовность через stem_paths() ЗДЕСЬ нельзя: метки ещё
    # нет по построению, поэтому она возвращала None на каждом треке,
    # и каждый трек падал с «стемы не собрались» — при том что все четыре
    # слоя лежали на диске и видеокарта честно их посчитала.
    paths = _parts_on_disk(dest)
    if not paths:
        have = sorted(p.name for p in dest.glob("*") if p.is_file())
        return {"ok": False,
                "error": "стемы не собрались: в папке " + (", ".join(have) or "пусто")}
    meta_extra = {"vocals_error": vocals_error} if vocals_error else {}
    _write_meta(dest, source=track_path, backend=backend, model=model,
                vocals_model=vocals_from, format=fmt, parts=list(PARTS),
                device=device or "cpu", seconds=round(time.time() - t0, 1),
                **meta_extra)
    out = {"ok": True, "paths": paths, "backend": backend, "device": device or "cpu",
           "vocals_model": vocals_from, "seconds": round(time.time() - t0, 1)}
    if vocals_error:
        out["vocals_error"] = vocals_error
    if live:
        out["live"] = build_live_stems(track_path)
    return out


def fix_other_library(track_paths: list[str], log=print, live: bool = True) -> dict:
    """Починить гармонию у уже посчитанных треков, не запуская модель.

    Нужно после того, как выяснилось, что вычитание промахивалось на
    задержку MP3-кодировщика: drums и bass остаются годными (их писал
    demucs), вокал тоже (его писал роформер), а other пересчитывается из
    них и микса. Это секунды на трек против семи минут полного прохода."""
    todo = [p for p in track_paths if stem_paths(p)]
    if not todo:
        log("чинить нечего: посчитанных стемов нет")
        return {"todo": 0, "done": 0, "failed": 0}
    log(f"пересчёт гармонии: {len(todo)} треков")
    done, failed, worst = 0, 0, None
    for i, p in enumerate(todo):
        dest = stem_dir_for(p)
        meta = dict(stem_meta(p))
        r = recompute_other(dest, p, fmt=meta.get("format", "mp3"))
        name = os.path.basename(p)[:44]
        if not r.get("ok"):
            failed += 1
            log(f"  [{i + 1}/{len(todo)}] {name} — ОШИБКА: {r.get('error')}")
            continue
        done += 1
        meta["sum_residual_db"] = r["residual_db"]
        meta["align_shift"] = r["shift"]
        _write_meta(dest, **meta)
        if live:
            build_live_stems(p, force=True)
        worst = max(worst, r["residual_db"]) if worst is not None else r["residual_db"]
        log(f"  [{i + 1}/{len(todo)}] {name} — сумма сходится: остаток "
            f"{r['residual_db']} dB, сдвиг {r['shift']:+d}, other {r['other_db']} dB")
    return {"todo": len(todo), "done": done, "failed": failed, "worst_residual_db": worst}


def redo_vocals_library(track_paths: list[str], log=print, live: bool = True,
                        force: bool = False) -> dict:
    """Первый этап «всё одной кнопкой»: доложить роформерный вокал к тем
    трекам, у которых слои уже посчитаны, а вокал остался demucs'овым.

    Отдельный этап, а не часть обычного прохода, потому что обычный проход
    такие треки СЧИТАЕТ ГОТОВЫМИ и пропускает — и вокал остался бы
    demucs'овым навсегда. Демукс здесь не запускается: барабаны и бас уже
    есть, а гармония пересчитывается из нового вокала."""
    problem = backend_problem("roformer")
    if problem:
        log("пересчёт вокала пропущен: " + problem)
        return {"todo": 0, "done": 0, "failed": 0, "error": problem}
    # force нужен для случая «метка роформера стоит, а слой пустой»: сама
    # метка ничего не гарантирует, пока не проверена громкость слоя.
    todo = [p for p in track_paths
            if stem_paths(p) and (force or not vocals_are_roformer(p))]
    ready = sum(1 for p in track_paths if vocals_are_roformer(p))
    if not todo:
        log(f"вокал: пересчитывать нечего (роформерных уже {ready})")
        return {"todo": 0, "done": 0, "failed": 0}
    log(f"вокал роформером на готовых слоях: {len(todo)} треков "
        f"(уже роформерных {ready})")
    done, failed, same = 0, 0, None
    for i, p in enumerate(todo):
        r = redo_vocals(p, live=live)
        if r.get("ok"):
            done += 1
            log(f"  [{i + 1}/{len(todo)}] {os.path.basename(p)[:48]} — "
                f"{r.get('seconds', 0):.0f}с, {os.path.splitext(r['vocals_model'])[0][:34]}")
            continue
        failed += 1
        err = str(r.get("error"))
        log(f"  [{i + 1}/{len(todo)}] {os.path.basename(p)[:48]} — ОШИБКА: {err[:200]}")
        # Три одинаковые ошибки подряд — это не «не повезло с треком», это
        # сломанное окружение. Останавливаемся, а не спамим весь список.
        if same == err:
            log("  одна и та же ошибка третий раз — останавливаю пересчёт вокала")
            break
        same = err if failed >= 2 else None
    return {"todo": len(todo), "done": done, "failed": failed}


def separate_library(track_paths: list[str], fmt: str = "mp3", model: str | None = None,
                     device: str | None = None, progress=None, log=print,
                     stop_after: float | None = None, backend: str = "auto",
                     live: bool = True, overlap: float | None = None,
                     rebuild: bool = False) -> dict:
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
    todo = list(track_paths) if rebuild else [p for p in track_paths if not stem_paths(p)]
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
        log(f"способ: {backend} | модель demucs: {model or (FAST_MODEL if backend == 'fast' else MODEL)}"
            + (f" | перекрытие {overlap}" if overlap is not None else "")
            + (" | вокал роформером" if backend == "roformer" else ""))

    done, failed = 0, []
    t0 = time.time()
    for i, p in enumerate(todo):
        if stop_after and time.time() - t0 > stop_after:
            failed.append({"path": p, "error": "остановлено по времени"})
            break
        r = separate_track(p, fmt=fmt, model=model, device=device, backend=backend,
                           live=live, overlap=overlap, rebuild=rebuild)
        if r.get("ok"):
            done += 1
            log(f"  [{i + 1}/{len(todo)}] {os.path.basename(p)[:48]} — {r.get('seconds', 0):.0f}с"
                + (f"  [вокал от {os.path.splitext(str(r.get('vocals_model')))[0][:34]}]"
                   if r.get("vocals_model") and r.get("vocals_model") != model else ""))
            if r.get("vocals_error"):
                log(f"      ВНИМАНИЕ: роформер не отработал, вокал от demucs — {r['vocals_error']}")
            # Сборка живого файла молчала: слои считались часами, а
            # .stem.mp4 не появлялся, и в отчёте стояло live_mp4: 0 без
            # единого слова о причине. Третий молчаливый отказ подряд в
            # этом конвейере — поэтому теперь он говорит.
            lv = r.get("live") or {}
            if live and not lv.get("ok"):
                log(f"      .stem.mp4 НЕ собран: {lv.get('error', 'причина неизвестна')}")
            elif live and lv.get("ok") and not lv.get("cached"):
                log(f"      .stem.mp4 собран: {lv.get('bytes', 0) / 1e6:.0f} МБ")
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
    ap.add_argument("--all", action="store_true", dest="do_all",
                    help="всё одной командой: сперва доложить роформерный вокал к уже "
                         "посчитанным трекам, потом досчитать остальные; .stem.mp4 "
                         "собирается сам")
    ap.add_argument("--rebuild", action="store_true",
                    help="пересчитать треки заново, даже если стемы уже есть "
                         "(нужно после исправлений в самом разделении)")
    ap.add_argument("--demucs-model", default=None, dest="demucs_model",
                    help="модель demucs: htdemucs_ft (по умолчанию, вчетверо дольше) "
                         "или htdemucs — вокал всё равно берётся роформером, "
                         "поэтому дообученная нужна только ради drums/bass/other")
    ap.add_argument("--overlap", type=float, default=None,
                    help="перекрытие окон demucs (по умолчанию его 0.25; "
                         "0.1 заметно быстрее)")
    ap.add_argument("--force-vocals", action="store_true", dest="force_vocals",
                    help="пересчитать вокал даже там, где в meta.json уже стоит "
                         "роформер (метка есть, а слой пустой)")
    ap.add_argument("--fix-other", action="store_true", dest="fix_other",
                    help="пересчитать гармонию (other) у уже посчитанных треков: "
                         "вычитание промахивалось на задержку MP3-кодировщика")
    ap.add_argument("--redo-vocals", action="store_true",
                    help="не считать заново: пересчитать роформером ТОЛЬКО вокал "
                         "у треков, где он остался от demucs")
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

    if args.fix_other:
        r = fix_other_library(paths, live=not args.no_live)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    if args.redo_vocals or args.force_vocals:
        r = redo_vocals_library(paths, live=not args.no_live,
                                force=args.force_vocals)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if not r.get("error") else 1

    if args.do_all:
        problem = backend_problem(resolve_backend(args.backend))
        if problem:
            print("НЕ ЗАПУСКАЮ: " + problem)
            return 1
        v = {"done": 0}
        if resolve_backend(args.backend) == "roformer" and not args.rebuild:
            print("== этап 1: вокал роформером на уже готовых слоях ==")
            v = redo_vocals_library(paths, live=not args.no_live)
            print("== этап 2: остальные треки целиком ==")
        r = separate_library(paths, fmt=args.format, device=args.device,
                             backend=args.backend, live=not args.no_live,
                             model=args.demucs_model, overlap=args.overlap,
                             rebuild=args.rebuild)
        r["vocals_redone"] = v.get("done", 0)
        print(json.dumps({k: v2 for k, v2 in r.items() if k != "failed"},
                         ensure_ascii=False, indent=2))
        return 0

    if args.only_live:
        built, failed = 0, []
        for p in paths:
            r = build_live_stems(p, force=args.rebuild_live, bitrate=args.mp4_bitrate)
            if r.get("ok"):
                built += 1
                print(f"  {os.path.basename(p)[:56]} — "
                      + ("уже был" if r.get("cached") else f"{r.get('bytes', 0) / 1e6:.0f} МБ"))
            elif not str(r.get("error", "")).startswith("слои не найдены"):
                failed.append((p, r.get("error")))
                print(f"  {os.path.basename(p)[:56]} — ОШИБКА: {r.get('error')}")
        print(json.dumps({"live_mp4": built, "failed": len(failed)},
                         ensure_ascii=False, indent=2))
        return 0

    r = separate_library(paths, fmt=args.format, device=args.device,
                         backend=args.backend, live=not args.no_live,
                         model=args.demucs_model, overlap=args.overlap,
                         rebuild=args.rebuild)
    print(json.dumps({k: v for k, v in r.items() if k != "failed"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
