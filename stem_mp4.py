"""
Сборка и чтение .stem.mp4 — формата Native Instruments STEMS.

Зачем отдельный модуль. DARAVE считал стемы в четыре отдельных файла
(drums.wav, bass.wav, other.wav, vocals.wav) — этого достаточно, чтобы
рендерить микс офлайн, и совершенно недостаточно, чтобы играть стемами
живьём: Mixxx умеет читать слои только из ОДНОГО файла формата NI STEMS.

Что такое .stem.mp4 по факту (проверено по исходникам Mixxx,
src/sources/soundsourcestem.cpp и src/track/steminfoimporter.cpp):

* обычный MP4 c РОВНО пятью аудиодорожками, все стерео, все одним кодеком
  и с одной частотой дискретизации. Итого 10 каналов — то, что и просил
  диджей: 4 стерео-слоя плюс мастер;
* дорожка 0 — готовый мастер (Mixxx её НЕ играет: у мастера свой
  лимитер-компрессор, а анализ считался по сумме слоёв, поэтому
  replaygain разошёлся бы). Дорожки 1..4 — слои;
* в moov/udta лежит атом `stem` с JSON-манифестом: версия, имена и цвета
  слоёв. БЕЗ этого атома файл остаётся обычным mp4, и Mixxx пишет в лог
  «No stem manifest found in the file». Именно атом, а не расширение
  имени, делает файл стемовым.

Почему moov остаётся в конце файла. Атом манифеста дописывается уже в
готовый mp4, то есть moov растёт на длину манифеста. Если moov стоит
ПЕРЕД mdat (режим faststart), сдвигается начало mdat, и все смещения
кусков в stco/co64 становятся неверными — файл перестаёт играть. Пока
moov в конце, дописывание в него не двигает ничего. Поэтому ffmpeg
вызывается без +faststart, и это не потеря: faststart нужен для
прогрессивной загрузки по сети, а файл лежит на диске.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

# Порядок слоёв в файле. Он же порядок кнопок в Mixxx (Stem1..Stem4),
# он же порядок в манифесте — три места, которые обязаны совпадать.
STEM_ORDER = ("drums", "bass", "other", "vocals")

# Названия и цвета, которые диджей увидит на деке. Цвета — те же, что
# Mixxx подставляет по умолчанию (colorblind-safe палитра Окабэ–Ито),
# чтобы файлы DARAVE не выбивались из общего вида.
STEM_LABELS = {
    "drums": ("Drums", "#009E73"),
    "bass": ("Bass", "#D55E00"),
    "other": ("Music", "#CC79A7"),
    "vocals": ("Vocals", "#56B4E9"),
}

MANIFEST_VERSION = 1
DEFAULT_BITRATE = "256k"
DEFAULT_SAMPLERATE = 44100


class StemMp4Error(RuntimeError):
    pass


# ------------------------------------------------------------- ffmpeg

def ffmpeg_exe() -> str | None:
    """ffmpeg из PATH, а если его там нет — из папки Mixxx.

    У диджея Mixxx стоит собранным с ffmpeg, но кладёт он туда только
    DLL, без ffmpeg.exe. Поэтому сначала PATH, потом типовые места
    установки — и только потом честный отказ."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    for cand in (
        r"C:\Mixxx\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
    ):
        if cand and os.path.exists(cand):
            return cand
    return None


def ffprobe_exe() -> str | None:
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    f = ffmpeg_exe()
    if f:
        cand = Path(f).with_name("ffprobe" + Path(f).suffix)
        if cand.exists():
            return str(cand)
    return None


# ------------------------------------------------------- разбор боксов

def _iter_boxes(buf: bytes, start: int, end: int):
    """Проход по боксам MP4 на одном уровне. Отдаёт
    (тип, начало_бокса, начало_данных, конец_бокса)."""
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", buf[pos:pos + 4])[0]
        typ = buf[pos + 4:pos + 8]
        head = 8
        if size == 1:
            if pos + 16 > end:
                break
            size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
            head = 16
        elif size == 0:
            size = end - pos
        if size < head or pos + size > end:
            break
        yield typ, pos, pos + head, pos + size
        pos += size


def _find_box(buf: bytes, path: tuple[bytes, ...], start: int = 0,
              end: int | None = None) -> tuple[int, int, int] | None:
    """Ищет вложенный бокс по пути. Возвращает (начало, начало_данных, конец)."""
    end = len(buf) if end is None else end
    for typ, box_start, data_start, box_end in _iter_boxes(buf, start, end):
        if typ != path[0]:
            continue
        if len(path) == 1:
            return box_start, data_start, box_end
        return _find_box(buf, path[1:], data_start, box_end)
    return None


def _box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def _grow_size(buf: bytearray, box_start: int, delta: int) -> None:
    """Увеличивает поле size бокса. 64-битные боксы здесь не встречаются:
    так помечают только mdat больше 4 ГБ, а мы правим moov и udta."""
    size = struct.unpack(">I", buf[box_start:box_start + 4])[0]
    if size == 1:
        raise StemMp4Error("расширенный (64-битный) бокс — править не берусь")
    struct.pack_into(">I", buf, box_start, size + delta)


def inject_manifest(mp4_path: str | Path, manifest: dict) -> None:
    """Дописывает атом moov/udta/stem в готовый mp4.

    Если udta уже есть — атом кладётся внутрь неё, если нет — udta
    создаётся. Размеры moov (и udta) увеличиваются ровно на длину
    вставки. Смещения кусков не трогаются: см. докстринг модуля."""
    p = Path(mp4_path)
    buf = bytearray(p.read_bytes())

    moov = _find_box(bytes(buf), (b"moov",))
    if not moov:
        raise StemMp4Error("в файле нет moov — это не MP4")
    moov_start, moov_data, moov_end = moov

    payload = json.dumps(manifest, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    stem_box = _box(b"stem", payload)

    udta = _find_box(bytes(buf), (b"udta",), moov_data, moov_end)
    if udta:
        udta_start, _udta_data, udta_end = udta
        # уже стемовый файл — старый манифест выкидываем, чтобы не
        # накапливать по атому на каждый пересчёт
        old = _find_box(bytes(buf), (b"stem",), _udta_data, udta_end)
        if old:
            o_start, _o_data, o_end = old
            removed = o_end - o_start
            del buf[o_start:o_end]
            _grow_size(buf, udta_start, -removed)
            _grow_size(buf, moov_start, -removed)
            udta_end -= removed
        buf[udta_end:udta_end] = stem_box
        _grow_size(buf, udta_start, len(stem_box))
        _grow_size(buf, moov_start, len(stem_box))
    else:
        udta_box = _box(b"udta", stem_box)
        buf[moov_end:moov_end] = udta_box
        _grow_size(buf, moov_start, len(udta_box))

    p.write_bytes(bytes(buf))


def read_manifest(mp4_path: str | Path) -> dict | None:
    """Манифест из готового файла — тем же путём, каким его читает Mixxx."""
    buf = Path(mp4_path).read_bytes()
    found = _find_box(buf, (b"moov", b"udta", b"stem"))
    if not found:
        return None
    _start, data, end = found
    raw = buf[data:end].rstrip(b"\0")
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------- сборка

def stem_mp4_path(track_path: str | Path) -> Path:
    """Путь стемового файла рядом с исходным треком.

    Mixxx видит стемы только как файл в медиатеке, поэтому класть их в
    кэш DARAVE бессмысленно: Track.mp3 -> Track.stem.mp4 в той же папке.
    Двойное расширение обязательно — по нему Mixxx выбирает провайдер
    STEM, когда MIME определить не удалось."""
    p = Path(track_path)
    return p.with_suffix("").with_name(p.stem + ".stem.mp4")


def playable_path(track_path: str | Path) -> str:
    """Какой файл этого трека надо ЗАВОДИТЬ НА ДЕКУ.

    Анализ, планирование и офлайн-рендер работают с исходным .mp3 — там
    лежат теги, и слои читаются из кэша DARAVE отдельными файлами. Но на
    деку заводить нужно .stem.mp4, иначе стемовых фейдеров у деки нет и
    половина приёмов вырождается в обычный кроссфейд.

    Проверено кросс-корреляцией на четырёх треках: мастер .stem.mp4
    совпадает с .mp3 сэмпл в сэмпл, поэтому все точки и метки переносятся
    один в один и пересчитывать ничего не нужно."""
    if not track_path:
        return ""
    try:
        sp = stem_mp4_path(track_path)
        if sp.exists():
            return str(sp)
    except OSError:
        pass
    return str(track_path)


def _replace_with_retry(tmp: Path, out: Path, attempts: int = 10,
                        delay: float = 0.4) -> None:
    """tmp.replace(out), но с повтором.

    Ровно та же болезнь Windows, что была с переносом слоёв demucs:
    только что дописанный файл на 40 МБ ещё секунду-другую держит
    антивирус (реалтайм-скан свежих медиафайлов), а иногда не до конца
    отпустил ffmpeg. Первая же попытка переименования падала с
    PermissionError [WinError 32], и .stem.mp4 не появлялся — при том
    что сам файл был уже собран и проверен. Ждём и повторяем: до 10 раз
    с растущей паузой, суммарно около 20 секунд."""
    import time

    last: OSError | None = None
    for i in range(attempts):
        try:
            tmp.replace(out)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(delay * (i + 1))
        except OSError as exc:
            last = exc
            break
    # Не вышло переименовать — пробуем скопировать содержимое: цель
    # может быть занята чужим процессом (например, открыта в Mixxx), и
    # тогда запись поверх работает там, где переименование нет.
    try:
        shutil.copyfile(str(tmp), str(out))
        tmp.unlink(missing_ok=True)
        return
    except OSError:
        pass
    tmp.unlink(missing_ok=True)
    raise last if last else OSError("не смог положить .stem.mp4 на место")


def build_stem_mp4(master_path: str | Path,
                   parts: dict[str, str | Path],
                   out_path: str | Path,
                   bitrate: str = DEFAULT_BITRATE,
                   samplerate: int = DEFAULT_SAMPLERATE,
                   title: str | None = None,
                   artist: str | None = None,
                   timeout: float = 900.0) -> dict:
    """Собирает .stem.mp4: мастер + четыре слоя, 10 каналов.

    master_path — исходный трек (он и есть мастер-дорожка),
    parts — {'drums': файл, 'bass': ..., 'other': ..., 'vocals': ...}.
    """
    ff = ffmpeg_exe()
    if not ff:
        raise StemMp4Error(
            "не найден ffmpeg — без него .stem.mp4 не собрать. "
            "Поставьте: winget install Gyan.FFmpeg (или scoop install ffmpeg)")

    missing = [k for k in STEM_ORDER if k not in parts or not Path(parts[k]).exists()]
    if missing:
        raise StemMp4Error("нет слоёв: " + ", ".join(missing))
    if not Path(master_path).exists():
        raise StemMp4Error(f"нет мастера: {master_path}")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp возвращает ОТКРЫТЫЙ дескриптор, и раньше отсюда брали
    # только путь — файл так и оставался открыт нашим же процессом. На
    # Linux переименование поверх открытого файла работает, поэтому в
    # тестах это не всплывало; Windows так не умеет, и .stem.mp4 падал с
    # PermissionError [WinError 32] на самом последнем шаге, когда файл
    # был уже собран и проверен. Дескриптор надо закрыть.
    _fd, _tmp_name = tempfile.mkstemp(suffix=".mp4", dir=str(out.parent))
    os.close(_fd)
    tmp = Path(_tmp_name)

    cmd = [ff, "-y", "-loglevel", "error", "-i", str(master_path)]
    for part in STEM_ORDER:
        cmd += ["-i", str(parts[part])]
    for idx in range(5):
        cmd += ["-map", f"{idx}:a:0"]
    cmd += [
        "-c:a", "aac", "-b:a", bitrate,
        "-ar", str(samplerate), "-ac", "2",
        # Mixxx требует, чтобы все пять дорожек были стерео, одним кодеком
        # и с одной частотой — иначе tryOpen() отказывает целиком.
        "-map_metadata", "0",
    ]
    if title:
        cmd += ["-metadata", f"title={title}"]
    if artist:
        cmd += ["-metadata", f"artist={artist}"]
    cmd += ["-f", "mp4", str(tmp)]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        raise StemMp4Error(f"ffmpeg не уложился в {timeout / 60:.0f} мин")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-4:]
        tmp.unlink(missing_ok=True)
        raise StemMp4Error("ffmpeg: " + " / ".join(t.strip() for t in tail))

    manifest = {
        "mastering_dsp": {"compressor": {"enabled": False},
                          "limiter": {"enabled": False}},
        "stems": [{"name": STEM_LABELS[p][0], "color": STEM_LABELS[p][1]}
                  for p in STEM_ORDER],
        "version": MANIFEST_VERSION,
    }
    inject_manifest(tmp, manifest)

    # Проверяем ДО подмены итогового файла: битый стем хуже отсутствующего,
    # потому что Mixxx на него молча отказывается и диджей узнаёт об этом
    # посреди сета.
    problem = verify(tmp)
    if problem:
        tmp.unlink(missing_ok=True)
        raise StemMp4Error(problem)

    out.unlink(missing_ok=True)
    _replace_with_retry(tmp, out)
    return {"ok": True, "path": str(out), "bytes": out.stat().st_size,
            "streams": 5, "channels": 10, "manifest": manifest}


def verify(mp4_path: str | Path) -> str | None:
    """Проверяет файл ровно теми условиями, которые проверяет Mixxx.
    None — всё в порядке, иначе строка с причиной отказа."""
    man = read_manifest(mp4_path)
    if not man:
        return "нет атома moov/udta/stem — Mixxx не увидит стемы"
    if int(man.get("version") or 0) <= 0:
        return "в манифесте нет версии"
    if not isinstance(man.get("stems"), list) or len(man["stems"]) != 4:
        return "в манифесте не четыре слоя"

    probe = ffprobe_exe()
    if not probe:
        return None  # без ffprobe проверяем только манифест
    try:
        raw = subprocess.run(
            [probe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index,channels,sample_rate,codec_name",
             "-of", "json", str(mp4_path)],
            capture_output=True, text=True, timeout=120).stdout
        streams = json.loads(raw).get("streams", [])
    except Exception as exc:
        return None if not raw else f"ffprobe не смог прочитать файл: {exc}"

    if len(streams) != 5:
        return f"дорожек {len(streams)}, а Mixxx ждёт ровно 5 (мастер + 4 слоя)"
    if any(int(s.get("channels") or 0) != 2 for s in streams):
        return "не все дорожки стерео"
    if len({s.get("codec_name") for s in streams}) != 1:
        return "дорожки разными кодеками"
    if len({s.get("sample_rate") for s in streams}) != 1:
        return "у дорожек разная частота дискретизации"
    return None


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Проверка .stem.mp4")
    ap.add_argument("path")
    args = ap.parse_args()
    bad = verify(args.path)
    print(json.dumps(read_manifest(args.path), ensure_ascii=False, indent=1))
    print("ОК" if not bad else "ПРОБЛЕМА: " + bad)
