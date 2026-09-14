"""
mixxx_settings.py — настройки самого Mixxx, от которых зависит, звучит ли
живое сведение так же, как посчитанное демо.

## Зачем отдельный модуль

DARAVE считает переход офлайн и он же исполняет его живьём. Разойтись
эти два результата могут не только из-за техники: часть решений принимает
САМ Mixxx, по своим настройкам, и никакой контроллер их не переопределит.
Самая дорогая из них — форма кроссфейдера. Пока она стоит «аддитивной»,
две деки в середине хода идут почти на полной громкости и складываются
с горбом около +3 dB; DARAVE же строит переход как кроссфейд РАВНОЙ
МОЩНОСТИ (cos/sin), потому что именно он держит громкость ровной. То
есть демо и живой микс расходятся ещё до того, как прозвучал приём.

Поэтому здесь: список настроек, которые должны быть определённого вида,
причина по каждой и уровень — «обязательно», «желательно», «просто
знайте». Правка идёт в mixxx.cfg и ТОЛЬКО при закрытом Mixxx: он держит
конфиг в памяти и перезаписывает его при выходе, стирая всё, что положили
снаружи. Ту же грабку мы уже проходили с метками и с очередью Auto DJ.

## Что НЕ трогаем и почему

* `keylock` выключен на всех деках — и правильно: DARAVE подгоняет темп
  питч-фейдером, как живой диджей, а не растяжением. Замер показал, что
  фазовый вокодер режет резкость атак вдвое (см. claude/status.md).
* «Точка загрузки трека» — не важна: автопилот сам ставит иглу и метку
  CUE там, где велит план.
* ReplayGain оставляем на усмотрение диджея: он статичен (один
  коэффициент на трек) и живому сведению не мешает, но означает, что
  громкость в Mixxx и в офлайн-рендере разная — сравнивать запись с демо
  по уровню нельзя.
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from mixxx_profile import mixxx_settings_dir

# (секция, ключ, нужное значение, уровень, зачем)
#   must  — без этого живое сведение звучит иначе, чем демо
#   nice  — заметно, но не ломает
#   note  — ничего не правим, просто рассказываем
RULES: list[tuple[str, str, str | None, str, str]] = [
    ("[Mixer Profile]", "xFaderMode", "1", "must",
     "Кривая кроссфейдера. DARAVE строит переход как кроссфейд равной мощности; "
     "аддитивный режим держит обе деки почти на полной громкости и даёт горб "
     "около +3 dB ровно в середине сведения — там, где его слышно больше всего."),
    ("[Mixer Profile]", "xFaderCurve", "1.0", "nice",
     "При постоянной мощности ход фейдера должен быть линейным: изгиб кривой "
     "здесь складывается с формой, которую уже задаёт техника."),
    ("[Mixer Profile]", "xFaderReverse", "0", "nice",
     "Обратный кроссфейдер (Hamster) переворачивает стороны, а DARAVE считает, "
     "что нечётные деки слева, чётные справа."),
    ("[Controls]", "AllowTrackLoadToPlayingDeck", "1", "must",
     "Автопилот сам останавливает деку перед загрузкой, но при запрете Mixxx "
     "отвергает загрузку молча — и сет встаёт без внятной причины."),
    ("[Mixer Profile]", "EqAutoReset", "1", "nice",
     "Новый трек не должен приезжать в EQ прошлого сведения."),
    ("[Controls]", "RateDir", "0", "note",
     "«Вниз — увеличение скорости» переворачивает питч-фейдер. Техники темп "
     "ведут через sync, так что на сведение это не влияет — но ручная команда "
     "«питч на 60%» из чата сдвинет темп в другую сторону."),
    ("[ReplayGain]", "ReplayGainEnabled", None, "note",
     "Mixxx подгоняет громкость каждого трека к -18 LUFS, а DARAVE выравнивает "
     "уровни по своему замеру тела трека. Живому сведению это не мешает "
     "(коэффициент статичен), но громкость записи из Mixxx и офлайн-рендера "
     "сравнивать нельзя."),
]

# Деки: quantize обязателен, но его выставляет companion при старте, и в
# конфиге он нужен лишь чтобы Mixxx не стартовал без него.
DECK_RULES = [(f"[Channel{n}]", "quantize", "1", "nice",
               "Без квантования нажатия и метки встают не на долю, и sync "
               "разъезжается на слух даже при точной бит-сетке.")
              for n in (1, 2, 3, 4)]


def _parse(text: str) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    section = ""
    for line in text.splitlines():
        line = line.rstrip()
        if line.startswith("["):
            section = line.strip()
        elif line.strip() and " " in line.strip():
            key, _, value = line.strip().partition(" ")
            out[(section, key)] = value.strip()
    return out


def _cfg_path(mixxx_dir: str | None = None) -> Path | None:
    d = mixxx_settings_dir(mixxx_dir)
    if d is None:
        return None
    p = Path(d) / "mixxx.cfg"
    return p if p.exists() else None


def check(mixxx_dir: str | None = None) -> dict:
    """Что сейчас стоит и что стоило бы поменять."""
    path = _cfg_path(mixxx_dir)
    if path is None:
        return {"ok": False, "error": "не нашёл mixxx.cfg — Mixxx на этой машине не настроен"}
    cur = _parse(path.read_text(encoding="utf-8", errors="replace"))

    findings = []
    for section, key, want, level, why in RULES + DECK_RULES:
        have = cur.get((section, key))
        good = want is None or (have is not None and have.strip() == want)
        findings.append({
            "section": section, "key": key, "have": have, "want": want,
            "level": level, "why": why, "ok": bool(good),
        })
    problems = [f for f in findings if not f["ok"] and f["level"] in ("must", "nice")]
    return {
        "ok": True,
        "path": str(path),
        "findings": findings,
        "to_fix": problems,
        "must": [f for f in problems if f["level"] == "must"],
        "summary": (f"{len(problems)} настроек стоит поменять"
                    if problems else "настройки Mixxx в порядке"),
    }


def apply(mixxx_dir: str | None = None, only_must: bool = False, log=print) -> dict:
    """Прописать нужные значения в mixxx.cfg.

    Mixxx должен быть ЗАКРЫТ: он держит конфиг в памяти и при выходе
    перезаписывает его целиком, стирая правки, сделанные снаружи."""
    path = _cfg_path(mixxx_dir)
    if path is None:
        return {"ok": False, "error": "не нашёл mixxx.cfg"}

    text = path.read_text(encoding="utf-8", errors="replace")
    cur = _parse(text)
    wanted = [(s, k, w, lv) for s, k, w, lv, _why in RULES + DECK_RULES
              if w is not None and lv in (("must",) if only_must else ("must", "nice"))]

    changed: list[str] = []
    lines = text.splitlines()
    section = ""
    for i, line in enumerate(lines):
        if line.startswith("["):
            section = line.strip()
            continue
        stripped = line.strip()
        if not stripped or " " not in stripped:
            continue
        key = stripped.split(" ", 1)[0]
        for s, k, w, _lv in wanted:
            if s == section and k == key and cur.get((s, k)) != w:
                lines[i] = f"{key} {w}"
                changed.append(f"{s} {k}: {cur.get((s, k))} -> {w}")
    # ключей, которых в файле нет вовсе, Mixxx и не ждёт — дописывать их
    # в чужую секцию опаснее, чем оставить настройку по умолчанию
    if not changed:
        return {"ok": True, "changed": [], "note": "менять нечего"}

    backup = path.with_name(path.name + time.strftime(".bak-%Y%m%d-%H%M%S"))
    shutil.copy2(path, backup)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for c in changed:
        log(f"mixxx.cfg: {c}")
    return {"ok": True, "changed": changed, "backup": str(backup),
            "note": "Mixxx должен быть закрыт — иначе он перезапишет конфиг при выходе"}


def main() -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Настройки Mixxx под DARAVE")
    ap.add_argument("--apply", action="store_true", help="прописать нужные значения")
    ap.add_argument("--must-only", action="store_true")
    ap.add_argument("--mixxx-dir", default=None)
    args = ap.parse_args()

    if args.apply:
        print(json.dumps(apply(args.mixxx_dir, args.must_only), ensure_ascii=False, indent=2))
        return 0
    out = check(args.mixxx_dir)
    if not out.get("ok"):
        print(out["error"])
        return 1
    print(out["path"])
    for f in out["findings"]:
        mark = ("  ok " if f["ok"] else
                {"must": "ЧИНИТЬ", "nice": "стоит ", "note": " инфо "}[f["level"]])
        print(f"{mark} {f['section']} {f['key']} = {f['have']}"
              + (f"  (нужно {f['want']})" if not f["ok"] and f["want"] else ""))
    print("\n" + out["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
