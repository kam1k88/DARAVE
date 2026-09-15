"""
library_analysis.py — что именно нужно досчитать в библиотеке и в каком порядке.

## Зачем отдельный модуль

Во вкладке «Библиотека» стояло пять кнопок: сканировать, стемы, метки в
Mixxx, перемерить темп, убрать мусор. Порядок между ними был неписаным —
стемы после скана, карты вокала после стемов, метки после всего, — и
нарушение порядка не ругалось, а молча давало неверный результат. Хуже
того, каждая кнопка считала СВОЁ заново целиком: добавить в папку один
трек стоило ровно столько же, сколько первый проход по всей библиотеке.

Здесь собрано то, что отвечает на два вопроса — «в каком порядке» и
«чего не хватает». Ровно эти две вещи и надо было проверять тестами, а
они жили внутри server.py, который в самопроверке не поднимается (он
тянет за собой FastAPI и живого LLM-провайдера). Поэтому логика — тут,
а в server.py остались только запуск подпроцессов и рассылка в UI.

ВАЖНО про пропуск. Пропуск шага — это нормальный исход, а не ошибка:
«слои есть у всех 70 треков» и «не смог посчитать слои» должны выглядеть
по-разному, иначе диджей перестаёт читать отчёт вообще.
"""
from __future__ import annotations

import json
import os

# Порядок принудительный: он и есть та самая неписаная память, которую
# кнопки требовали от диджея. Клиент присылает НАБОР шагов, а не их
# последовательность — см. order_steps.
ANALYZE_STEPS = ("scan", "bpm", "stems", "vocals", "catalog", "cleanup", "cues")

ANALYZE_TITLES = {
    "scan": "Разбор треков",
    "bpm": "Темп и карты",
    "stems": "Разделение на слои",
    "vocals": "Карты вокала",
    "catalog": "Стили из каталогов",
    "cleanup": "Уборка мусора",
    "cues": "Метки в Mixxx",
}

ANALYZE_WHY = {
    "scan": "темп, тональность, энергия, структура — только новые и изменившиеся файлы",
    "bpm": "перемер темпа и карты барабанов/энергии там, где они врут или их нет",
    "stems": "разделение на слои и сборка .stem.mp4 — без них половина техник падает на кроссфейд",
    "vocals": "где в треке звучит голос — флаг vocal у секций",
    "catalog": "поджанр из MusicBrainz и Discogs там, где своего нет",
    "cleanup": "демо, сеты, записи и оборванные папки слоёв",
    "cues": "горячие метки в базу Mixxx. Mixxx должен быть ЗАКРЫТ",
}

# Всё, кроме меток: метки пишут в ЧУЖУЮ базу (Mixxx), и делать это без
# спроса нельзя — запущенный Mixxx перезапишет её при выходе, и метки
# исчезнут. Шаг есть, но включается галочкой.
ANALYZE_DEFAULT = ("scan", "bpm", "stems", "vocals", "catalog", "cleanup")

PROGRESS_PREFIX = "[ПРОГРЕСС] "


def order_steps(want) -> list[str]:
    """Набор шагов -> наша последовательность.

    Клиент присылает, ЧТО делать; в каком порядке — решаем здесь. Иначе
    порядок снова оказывается на совести того, кто нажимает кнопку."""
    chosen = set(want or ())
    return [k for k in ANALYZE_STEPS if k in chosen]


def parse_progress_line(line: str) -> dict | None:
    """Машинная строка прогресса от stems.py (см. stems.emit_progress).

    Прогресс идёт машинной строкой, а не разбором человеческого вывода:
    подпроцесс печатает и «[3/13] имя — 41с», и предупреждения, и JSON
    отчёта, и регулярка по ним ломалась бы от каждой правки текста."""
    if not isinstance(line, str) or not line.startswith(PROGRESS_PREFIX):
        return None
    try:
        d = json.loads(line[len(PROGRESS_PREFIX):])
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or "total" not in d or "i" not in d:
        return None
    return d


def blank_status(steps: list[str]) -> dict:
    return {
        "running": True, "current": None, "finished_at": None,
        "steps": [{"key": k, "title": ANALYZE_TITLES.get(k, k),
                   "why": ANALYZE_WHY.get(k, ""), "state": "wait",
                   "done": 0, "total": 0, "note": "", "name": ""}
                  for k in steps],
    }


def common_root(paths) -> str:
    """Общий корень путей библиотеки — папка, с которой диджей работает.

    Нужен там, где папку не у кого спросить: кнопка в скине Mixxx полей
    ввода не имеет, а шаг «стемы» обязан знать, что считать."""
    real = [p for p in (paths or []) if p]
    if not real:
        return ""
    try:
        return str(os.path.commonpath(real))
    except (ValueError, TypeError):
        return ""


def missing_report(tracks, has_stems=None, vocal_map=None) -> dict:
    """Чего не хватает библиотеке — по одному числу на шаг.

    Считается ДО запуска и показывается на кнопке: «анализ нужен вот
    этим 12 трекам» честнее, чем «анализ» без числа, после которого
    непонятно, будет это минута или ночь.

    has_stems/vocal_map передаются снаружи (из stems.py), чтобы модуль не
    тянул за собой numpy и torch ради трёх счётчиков."""
    tracks = list(tracks or [])
    paths = [t.get("path") for t in tracks if t.get("path")]
    out = {"tracks": len(tracks)}

    # Темп-заглушка: одинаковый BPM у многих треков — это не измерение.
    bpms = [round(float(t.get("bpm") or 0), 1) for t in tracks if t.get("bpm")]
    same = max((bpms.count(b) for b in set(bpms)), default=0)
    out["bpm"] = same if same > 1 and len(bpms) > 2 else 0
    out["maps"] = sum(1 for t in tracks
                      if not ((t.get("structure") or {}).get("drum_map"))
                      or not ((t.get("structure") or {}).get("energy_map")))

    if has_stems is not None:
        no_stems = [p for p in paths if not has_stems(p)]
        out["stems"] = len(no_stems)
        if vocal_map is not None:
            out["vocals"] = sum(1 for p in paths
                                if has_stems(p) and vocal_map(p) is None)
    out["catalog"] = sum(1 for t in tracks
                         if not ((t.get("meta") or {}).get("sources")))
    return out
