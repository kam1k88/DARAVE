"""
techniques.py — библиотека техник сведения (изначально DNB, но структура
универсальна) + сборка MixPlan под конкретную технику.

Три роли одних и тех же данных:
  1. Контекст для LLM (agent.py передаёт TECHNIQUES в system-промпт/tool-схему,
     чтобы модель выбирала технику по имени, а не изобретала MIDI-параметры).
  2. Данные для web UI ("Техника" — карточки с описанием/параметрами/шагами,
     см. server.py::GET /api/techniques и static/chat.html).
  3. Исполняемая логика — build_plan() строит реальный MixPlan (в midi_mapping.py
     терминах) под выбранную технику, с учётом structure-анализа трека
     (drops/breakdowns из track_analysis.py), если он есть.

Часть техник (A Cappella Overlay) требует стемов — им requires_stems=True,
build_plan для них поднимает NotImplementedError с понятным сообщением,
а не притворяется, что что-то сделала (см. README "Что дальше").
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class TechniqueParam:
    key: str
    label: str
    default: float
    min: float
    max: float
    unit: str = ""


@dataclass
class Technique:
    id: str
    name: str
    category: str  # "dnb" | "universal"
    difficulty: int  # 1..5
    description: str
    bpm_delta_max: float | None  # None = не важно; иначе макс. |bpm1-bpm2| в % для применимости
    key_rule: str  # "any" | "compatible" | "clash" (техника ИМЕННО для конфликтующих тональностей)
    energy_direction: str  # "up" | "down" | "any"
    requires_stems: bool
    requires_decks: int
    params: list[TechniqueParam] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    # Техника РАССЧИТЫВАЕТ УСЛЫШАТЬ голос отдельно, а не просто ведёт его
    # фейдером вместе с остальными слоями. Разница важная: обмен
    # барабанами двигает и вокальный слой, но если тот пустой — приём всё
    # равно звучит правильно. Акапелла на пустом слое даёт тишину.
    needs_vocals: bool = False

    def param_defaults(self) -> dict[str, float]:
        return {p.key: p.default for p in self.params}

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


from mixxx_controls import BY_ID as _CONTROLS

# «Штатная единица» ручки EQ в нормализованных 0..1. Диапазон контрола
# Mixxx у EQ — 0..4, поэтому единица это 0.25, а не 1.0 (1.0 = буст в 4x).
# Берём из каталога, чтобы не разъехалось, если диапазон когда-то изменится.
EQ_UNITY = _CONTROLS["eq_low"].neutral


# --- вспомогательные конструкторы событий (см. mixplan.py::MixEvent) ---

def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _discrete(beat_offset: float, action: str, deck: str, params: dict | None = None) -> dict:
    return {"beat_offset": beat_offset, "action": action, "deck": deck, "kind": "discrete", "params": params or {}}


def _ramp(beat_offset: float, action: str, deck: str, duration_beats: float,
          value_from: float, value_to: float, curve: str = "linear", params: dict | None = None) -> dict:
    return {
        "beat_offset": beat_offset, "action": action, "deck": deck, "kind": "ramp",
        "duration_beats": duration_beats, "value_from": value_from, "value_to": value_to,
        "curve": curve, "params": params or {},
    }


def _hold(beat_offset: float, action: str, deck: str, duration_beats: float) -> dict:
    return {"beat_offset": beat_offset, "action": action, "deck": deck, "kind": "hold", "duration_beats": duration_beats}


# --- сборщики MixPlan-событий по технике ---
# Сигнатура единая: (source, target, bpm, p: dict-параметров-с-дефолтами) -> list[event-dict]
# beat_offset=0 — точка "решения" (обычно последняя фраза source перед свипом/дропом).

def _build_long_blend(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    dur = p["sweep_bars"] * 4
    return [
        _ramp(0, "filter_sweep", source, dur, 0.0, 1.0, "ease_in"),
        # Входящая дека: поставить в темп, залочить синхрон на весь бленд и
        # реально ЗАПУСТИТЬ её с cue-точки (см. play_from_cue).
        _discrete(dur, "sync", target),
        _hold(dur, "sync_lock", target, dur * 2),
        _discrete(dur, "play_from_cue", target),
        # Бас-своп — то, как длинный бленд в DnB и делается: два низа
        # одновременно = каша, поэтому низ уводим с source и одновременно
        # поднимаем на target. 1.0 = штатный уровень, 0.0 = низ убран.
        _ramp(dur, "eq_low", target, dur, 0.0, EQ_UNITY),
        _ramp(dur, "eq_low", source, dur, EQ_UNITY, 0.0),
        _ramp(dur, "crossfade", source, dur * 2, 0.0, 1.0),
        _discrete(dur * 3, "loop_exit", source),
        # Вернуть низ ушедшей деки в штатное положение, иначе она останется
        # с убитым басом до следующего раза.
        _ramp(dur * 3, "eq_low", source, 2, 0.0, EQ_UNITY),
    ]


def _build_harmonic_blend(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    # Ключи совместимы — фильтр не нужен, просто длинный плавный кроссфейд.
    dur = p["blend_bars"] * 4
    return [
        _discrete(0, "sync", target),
        _hold(0, "sync_lock", target, dur),
        _discrete(0, "play_from_cue", target),
        _ramp(0, "eq_low", target, dur * 0.5, 0.0, EQ_UNITY),
        _ramp(0, "eq_low", source, dur * 0.5, EQ_UNITY, 0.0),
        _ramp(0, "crossfade", source, dur, 0.0, 1.0),
    ]


def _build_quick_cut(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    dur = max(0.25, p["cut_beats"])
    return [
        _discrete(0, "sync", target),
        _ramp(0, "crossfade", source, dur, 0.0, 1.0),
    ]


def _build_bass_swap(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    swap_at = p["swap_bar"] * 4
    return [
        _discrete(0, "sync", target),
        _discrete(0, "eq_kill_low", target),  # у target бас выключен, пока играет source
        _ramp(0, "crossfade", source, swap_at, 0.0, 0.5),
        _discrete(swap_at, "eq_kill_low", source),   # выключить бас источника
        _discrete(swap_at, "eq_kill_low", target),   # включить бас цели (toggle — см. примечание в README)
        _ramp(swap_at, "crossfade", source, swap_at, 0.5, 1.0),
        _discrete(swap_at * 2, "loop_exit", source),
    ]


def _build_filter_sweep(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    dur = p["sweep_bars"] * 4
    return [
        _ramp(0, "filter_sweep", source, dur, 0.0, 1.0, "ease_in"),
        _discrete(dur, "sync", target),
        _ramp(dur, "crossfade", source, 4, 0.0, 1.0),
    ]


def _build_delay_out(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    tail = p["tail_beats"]
    return [
        # Без fx_enable юнит не подключён к каналу деки, и крутилка meta
        # меняет параметр эффекта, который никуда не выведен — раньше эхо
        # именно поэтому и не было слышно. Держим юнит включённым ровно на
        # время техники и отпускаем, чтобы эффект не остался висеть.
        _hold(0, "fx_enable", source, tail + 6),
        _ramp(0, "fx_mix", source, tail, 0.0, p["echo_mix"]),
        _ramp(0, "fx_meta", source, tail, 0.0, p["echo_mix"]),
        _discrete(tail, "eq_kill_mid", source),  # убираем мид, остаётся только хвост дилея
        _discrete(tail, "sync", target),
        _discrete(tail, "play_from_cue", target),
        _ramp(tail, "crossfade", source, 4, 0.0, 1.0),
        _ramp(tail + 4, "fx_mix", source, 2, p["echo_mix"], 0.0),
        _ramp(tail + 4, "fx_meta", source, 2, p["echo_mix"], 0.0),
    ]


def _build_eq_roller(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    dur = p["roll_bars"] * 4
    return [
        _ramp(0, "filter_sweep", source, dur, 0.2, 0.8, "sine", {"cycles": p["cycles"]}),
        _discrete(dur, "sync", target),
        _ramp(dur, "crossfade", source, 4, 0.0, 1.0),
        _discrete(dur + 4, "loop_exit", source),
    ]


def _build_echo_cut(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Рецепт 1-в-1 со скриншотом Echo Cut: kill mid у target заранее, ввод
    target на глухих барабанах, на финальной ноте source — эхо, резкий cut
    фейдера source в 0, резкое открытие mid у target на дропе."""
    intro_at = -p["intro_bars_early"] * 4  # target заходит раньше anchor'а
    echo_at = 0
    cut_at = p["echo_duration_beats"]
    return [
        _discrete(intro_at, "eq_kill_mid", target),
        _discrete(intro_at, "sync", target),
        _discrete(intro_at, "play_from_cue", target),
        _ramp(intro_at, "crossfade", source, -intro_at, 0.0, 0.3),
        _hold(echo_at, "fx_enable", source, p["echo_duration_beats"] + 2),
        _ramp(echo_at, "fx_mix", source, p["echo_duration_beats"], 0.0, p["echo_mix"]),
        _ramp(echo_at, "fx_meta", source, p["echo_duration_beats"], 0.0, p["echo_mix"]),
        _ramp(cut_at, "crossfade", source, 0.25, 0.3, 1.0),  # резкий рез фейдера в 0 (на target)
        _discrete(cut_at, "eq_kill_mid", target),  # снять килл — резко открыть mid на дропе
    ]


def _build_phrase_match(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    # Обёртка над Long Blend, но с фразоопределяющей длительностью (кратно 8/16
    # тактам) — сам расчёт "куда именно" делает mix_strategist.py по structure.
    return _build_long_blend(source, target, bpm, {"sweep_bars": p["phrase_bars"]})


def _build_double_drop(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    align = p["align_bars"] * 4
    return [
        _discrete(0, "sync", target),
        _ramp(0, "crossfade", source, align, 0.0, 0.5),
        # оба трека звучат вместе align тактов, дроп синхронизирован structure-анализом
        # (mix_strategist.py подставляет align_bars так, чтобы дропы совпали)
        _ramp(align, "crossfade", source, 2, 0.5, 1.0),
        _discrete(align + 2, "loop_exit", source),
    ]


def _build_triple_drop(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Три дропа в один удар. Третья дека здесь настоящая, а не для вида.

    Прежняя версия слала на третью деку один `sync` и больше ничего: дека
    не запускалась, громкость не поднималась, низ не разводился. То есть
    «поддержка трёх дек» состояла в том, что третьей деке сообщали темп, а
    играли по-прежнему вдвоём.

    Главная опасность приёма — не темп и не тональность, а НИЗ. Три баса
    одновременно не складываются ни в какой музыкальный результат: это
    просто перегруз в 30-80 Гц, и слышно его как гудение, а не как мощь.
    Поэтому бас играет РОВНО ОДИН, и это входящая дека B; у C низ снят на
    всё время приёма и не возвращается, а у A уходит в момент дропа. Так
    же поступают и живьём: дабл- и трипл-дроп держат на одном сабе.

    Обе входящие деки заводятся ЗАРАНЕЕ, за align тактов до совпадения, —
    их дропы обязаны прийти в один и тот же удар, а не подъехать по
    очереди. Точку старта считает mix_points (`pre_drop`), здесь мы только
    выдерживаем расстояние."""
    third = p.get("third_deck") or "C"
    align = float(p["align_bars"]) * 4
    hold = float(p.get("hold_beats", 16))
    unity = EQ_UNITY
    ev = [
        # обе входящие деки: темп, запуск, низ снят
        _discrete(0, "sync", target),
        _discrete(0, "sync", third),
        _hold(0, "sync_lock", target, align + hold + 8),
        _hold(0, "sync_lock", third, align + hold + 8),
        _ramp(0, "eq_low", target, 1, 0.0, 0.0),
        _ramp(0, "eq_low", third, 1, 0.0, 0.0),
        _discrete(0, "play_from_cue", target),
        _discrete(0, "play_from_cue", third),
        # заходят тихо и вместе — до дропа их не должно быть слышно как
        # отдельные треки
        _ramp(0, "volume_ramp", target, align, 0.0, 0.55),
        _ramp(0, "volume_ramp", third, align, 0.0, 0.45),
    ]
    # МОМЕНТ ДРОПА: три трека звучат вместе, бас один — у входящей B.
    ev += [
        _ramp(align, "volume_ramp", target, 2, 0.55, 1.0),
        _ramp(align, "volume_ramp", third, 2, 0.45, 0.85),
        _ramp(align, "eq_low", target, 2, 0.0, unity),
        _ramp(align, "eq_low", source, 2, unity, 0.0),
        # у третьей деки низ так и остаётся снятым — она добавляет
        # барабаны и гармонию, но не бас
    ]
    # Держим совмещение, потом остаётся одна дека.
    out = align + hold
    ev += [
        _ramp(out, "crossfade", source, 4, 0.0, 1.0),
        _ramp(out, "volume_ramp", third, 8, 0.85, 0.0),
        _discrete(out + 4, "loop_exit", source),
        _ramp(out + 4, "eq_low", source, 2, 0.0, unity),
        _ramp(out + 8, "eq_low", third, 2, 0.0, unity),
    ]
    ev.sort(key=lambda e: e["beat_offset"])
    return ev


def _build_loop_and_roll(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    loop_bars = p["loop_bars"] * 4
    return [
        # beats обязателен: без него beatloop_activate зациклит на той длине,
        # что СЕЙЧАС выставлена в Mixxx руками, а не на заказанной техникой.
        _discrete(0, "loop_activate", source, {"beats": loop_bars}),
        _discrete(loop_bars, "sync", target),
        _discrete(loop_bars, "play_from_cue", target),
        _ramp(loop_bars, "crossfade", source, 4, 0.0, 1.0),
        _discrete(loop_bars, "loop_exit", source),
    ]


def _build_key_jump(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    dur = p["jump_bars"] * 4
    shift = 0.5 + p["semitones"] / 12.0  # по конвенции 0.5=0, см. midi_mapping.py
    return [
        _ramp(0, "key_shift", target, dur, 0.5, max(0.0, min(1.0, shift))),
        _discrete(0, "sync", target),
        _ramp(dur, "crossfade", source, 4, 0.0, 1.0),
    ]


def _build_reverse_drop(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    hold_beats = p["reverse_beats"]
    return [
        # reverse_hold привязан к `reverseroll` Mixxx — это censor, то есть
        # реверс СО СЛИПОМ: после отпускания трек идёт там же, где шёл бы.
        {"beat_offset": 0, "action": "reverse_play", "deck": source, "kind": "hold",
         "duration_beats": hold_beats, "params": {"slip": 1}},
        _discrete(hold_beats, "sync", target),
        _ramp(hold_beats, "crossfade", source, 1, 0.0, 1.0),
    ]


def _build_fader_fx_series(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    pulses = int(p["pulses"])
    step = p["pulse_beats"]
    events = []
    last_value = 0.0
    for i in range(pulses):
        t = i * step
        value_from = 0.0 if i % 2 == 0 else 1.0
        value_to = 1.0 if i % 2 == 0 else 0.0
        events.append(_ramp(t, "crossfade", source, step / 2, value_from, value_to))
        last_value = value_to
    events.append(_discrete(pulses * step, "sync", target))
    events.append(_ramp(pulses * step, "crossfade", source, 2, last_value, 1.0))
    return events



# --- Резовые техники: то, как драм-н-бейс реально сводят на фейдере ---
# Диджей не всегда «плавно перетекает». В DnB и джангле классика — рез:
# фейдер быстро уходит на новый трек, потом обратно на старый, и так
# несколько раз, пока не остаётся новый. Ухо слышит не «переход», а
# диалог двух треков. Ниже три разных характера такого реза.

CUT_BEATS = 0.03  # «щелчок» фейдера: не мгновенный скачок (щелчок в буфере),
                  # но и не слышимый как плавность — примерно 10 мс на 174 BPM


def _cut(beat: float, source: str, value_from: float, value_to: float) -> dict:
    """Резкий, но не щёлкающий бросок кроссфейдера."""
    return _ramp(beat, "crossfade", source, CUT_BEATS, value_from, value_to)


def _build_fader_chop(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Перекидываем фейдер между треками целыми тактами, N раз, потом
    остаёмся на новом. Каждый трек успевает сказать музыкальную фразу —
    в этом отличие от статтера, где слышны только огрызки."""
    swaps = int(p["swaps"])
    span = float(p["bars_per_swap"]) * 4
    events = [_discrete(0, "sync", target), _discrete(0, "play_from_cue", target)]
    pos = 0.0
    on_target = False
    for _ in range(swaps):
        pos += span
        events.append(_cut(pos, source, 1.0 if on_target else 0.0, 0.0 if on_target else 1.0))
        on_target = not on_target
    if not on_target:                       # заканчиваем обязательно на новом треке
        pos += span
        events.append(_cut(pos, source, 0.0, 1.0))
    events.append(_discrete(pos + span, "loop_exit", source))
    return events


def _build_drop_teaser(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Новый трек «выглядывает» короткими вспышками на границах фраз, а
    потом заходит совсем. Приём для дропа: ухо уже узнало трек, и когда
    он приходит по-настоящему, это читается как обещанное событие."""
    teases = int(p["teases"])
    every = float(p["every_bars"]) * 4
    length = float(p["tease_beats"])
    events = [_discrete(0, "sync", target), _discrete(0, "play_from_cue", target)]
    pos = 0.0
    for _ in range(teases):
        pos += every
        events.append(_cut(pos, source, 0.0, 1.0))
        events.append(_cut(pos + length, source, 1.0, 0.0))
    pos += every
    events.append(_cut(pos, source, 0.0, 1.0))          # финальный заход
    events.append(_discrete(pos + 4, "loop_exit", source))
    return events


def _build_bar_switch(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Джангловый обмен такт за тактом с разведением низа: пока играет
    один трек, у второго снят низ. Без этого два баса складываются в кашу
    ровно в тот момент, когда рез должен звучать чисто."""
    rounds = int(p["rounds"])
    span = float(p["bars_per_round"]) * 4
    events = [_discrete(0, "sync", target), _discrete(0, "play_from_cue", target)]
    unity = EQ_UNITY
    pos = 0.0
    on_target = False
    for _ in range(rounds):
        pos += span
        on_target = not on_target
        events.append(_cut(pos, source, 0.0 if on_target else 1.0, 1.0 if on_target else 0.0))
        # низ уходит вместе с фейдером — у деки, которая сейчас молчит
        events.append(_ramp(pos, "eq_low", source, CUT_BEATS, unity if on_target else 0.0,
                            0.0 if on_target else unity))
        events.append(_ramp(pos, "eq_low", target, CUT_BEATS, 0.0 if on_target else unity,
                            unity if on_target else 0.0))
    if not on_target:
        pos += span
        events.append(_cut(pos, source, 0.0, 1.0))
        events.append(_ramp(pos, "eq_low", target, CUT_BEATS, 0.0, unity))
    events.append(_discrete(pos + span, "loop_exit", source))
    return events



def _build_classic(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """То, как драм-н-бейс сводят на самом деле. Одно сведение, один обмен.

    Раньше здесь был каталог из 24 приёмов, и план выбирал из них по
    очкам. Это была ошибка постановки задачи: у диджея нет двадцати
    четырёх способов: есть ОДИН основной ход, который он делает почти
    всегда, и несколько акцентов на особый случай. Основной ход такой:

      1. Новый трек заводится СВОИМ ИНТРО под ещё играющий старый. Интро
         для этого и написано разреженным — там нет барабанов, и оно не
         спорит со старым треком ни за низ, ни за середину.
      2. Низ у нового снят полностью. Играет один бас — старого.
      3. Так они идут 16-32 такта. Слушатель уже слышит новый трек, но
         ещё не заметил перехода.
      4. Последняя фраза старого ставится в ЛУП — тогда обмен приходится
         ровно на границу фразы, а не туда, где трек случайно кончился.
      5. На границе фразы — ОДИН обмен низом за такт.
      6. Старый уводится фильтром и уходит в ЭХО — хвост дилея тянется
         поверх нового трека и склеивает стык. Без этого фейдер просто
         закрывается, и переход слышен как обрыв.

    Ключ к тому, чтобы это звучало, — п.1 и п.2. Когда обе деки играют
    полным спектром, 180-500 Гц и 500-2000 Гц складываются с перебором
    в 2-3 dB, и получается каша, которую нельзя вычистить эквалайзером:
    обеим дорожкам эти полосы нужны. Интро под старый трек — это способ
    НЕ создавать эту сумму вообще.
    """
    # Длина всего сведения — ОДИН параметр, а не сумма трёх. Раньше здесь
    # стояло «24 такта под старым + 8 на вывод» = 32 такта, это 44 секунды
    # при 174 BPM. Живой диджей столько не тянет: сведение в драм-н-бейсе
    # — фраза или две, 4-12 тактов. Длинное наложение и звучит вяло, и
    # съедает время трека, которого при 50 треках в 90 минут и так мало.
    blend = float(p.get("blend_bars", 8))
    out_bars = min(float(p.get("out_bars", 4)), max(1.0, blend / 2.0))
    under_bars = max(1.0, blend - out_bars)

    intro = under_bars * 4                   # долей: сколько новый идёт под старым
    out = out_bars * 4
    # обмен низом не может быть длиннее самого вывода
    swap = _clamp(float(p["swap_bars"]) * 4, 2.0, out)
    under = float(p["under_level"])          # насколько слышен новый под старым
    # луп не длиннее того куска, что новый идёт под старым, иначе
    # loop_activate уезжает в отрицательное время
    loop_bars = min(float(p.get("loop_bars", 4)), under_bars)
    echo = float(p.get("echo", 0.55))
    rev = float(p.get("reverse_beats", 0))
    unity = EQ_UNITY

    # Пауза перед входом нового трека. Диджей не бросает новую деку ровно
    # в тот же удар, где уходящий провалился в брейкдаун: он даёт паузе
    # прозвучать такт, и только потом заводит. Без этого вход слышен как
    # «слишком рано», хотя формально всё в такт.
    #
    # ВАЖНО, почему тактами, а не долями: сдвиг на две доли поставил бы
    # «раз» нового трека на третью долю старого. Это ровно то расхождение
    # на 1-2 доли, которое чинили в прошлый раз, — рисунки барабанов
    # разъезжаются, бочка садится на снейр. Целый такт даёт ту же паузу на
    # слух (1.4 с при 174 BPM), но фазу не ломает.
    delay = max(0.0, float(p.get("entry_delay_bars", 0))) * 4

    swap_at = intro
    # Ввод нового — короткий и решительный. Раньше он занимал половину
    # всего наложения (до двух тактов), и трек «вползал»: пока он
    # набирает уровень, обе дорожки долго спорят серединой, а вокал
    # входящего наползает на вокал уходящего. Диджей так не делает — он
    # открывает фейдер за долю-две, потому что низ у входящего уже снят и
    # бояться нечего.
    ramp_in = _clamp(float(p.get("entry_ramp_beats", 2)), 1.0, max(1.0, intro))
    ev = [
        _discrete(0, "sync", target),
        _hold(0, "sync_lock", target, intro + out),
        _discrete(0, "play_from_cue", target),
        # новый заходит под старый, низ снят
        _ramp(0, "eq_low", target, 1.0, 0.0, 0.0),
        _ramp(0, "crossfade", source, ramp_in, 0.0, under),
    ]
    if swap_at > ramp_in:
        ev.append(_ramp(ramp_in, "crossfade", source, swap_at - ramp_in, under, under))

    # последняя фраза старого — в луп, чтобы обмен попал на границу фразы
    if loop_bars > 0 and swap_at > loop_bars * 4:
        ev.append(_discrete(swap_at - loop_bars * 4, "loop_activate", source,
                            {"beats": loop_bars * 4}))

    # короткий реверс прямо перед обменом — «вдох» перед сменой трека
    if rev > 0:
        # реверс не может начаться раньше самого сведения: при 32 долях и
        # коротком заходе точка ухода уехала бы в отрицательное время
        rev = min(rev, max(0.0, swap_at - 1.0))
        if rev > 0:
            ev.append(_hold(swap_at - rev, "reverse_hold", source, rev))

    # Середина входящего, пока обе деки идут барабанами. Снятого низа тут
    # мало: он разводит бочки, а «коняшки» дают ТЕЛА барабанов — 300-3000
    # Гц, где живут снейр и рабочий барабан. Два кита в этой полосе
    # складываются в спотыкание, и эквалайзером его потом не вычистить,
    # потому что полоса нужна обоим.
    #
    # Величину задаёт план: он единственный знает по картам барабанов,
    # играют ли обе деки одновременно. Если входящий заводится своим интро
    # без барабанов, глушить середину нельзя — подложка станет ватной,
    # поэтому по умолчанию 0.
    duck = _clamp(float(p.get("mid_duck", 0.0)), 0.0, 0.8)
    if duck > 0.01:
        ev += [
            _ramp(delay, "eq_mid", target, 1.0, unity, unity * (1.0 - duck)),
            _ramp(delay + 1.0, "eq_mid", target, max(1.0, swap_at - delay - 1.0),
                  unity * (1.0 - duck), unity * (1.0 - duck)),
            # к обмену середина возвращается вместе с низом
            _ramp(swap_at, "eq_mid", target, swap, unity * (1.0 - duck), unity),
        ]

    ev += [
        # обмен низом — один, за такт, на границе фразы
        _ramp(swap_at, "eq_low", source, swap, unity, 0.0),
        _ramp(swap_at, "eq_low", target, swap, 0.0, unity),
        # старый уводится
        _ramp(swap_at, "crossfade", source, out, under, 1.0),
        _ramp(swap_at, "filter_sweep", source, out, 0.0, 1.0, "ease_in"),
        _discrete(swap_at, "loop_exit", source),
    ]

    # ... и уходит в эхо: хвост дилея тянется поверх нового трека
    if echo > 0:
        ev += [
            _discrete(swap_at, "fx_enable", source),
            _ramp(swap_at, "fx_mix", source, out * 0.6, 0.0, echo),
            _ramp(swap_at + out * 0.6, "fx_mix", source, out * 0.4, echo, 0.0),
        ]

    ev.append(_discrete(swap_at + out, "loop_exit", source))

    if delay > 0:
        # сдвигаем ВСЮ технику: уходящий столько же играет один, а
        # взаимное положение событий и, главное, фаза такта не меняются
        for e in ev:
            e["beat_offset"] = round(float(e["beat_offset"]) + delay, 4)
    return ev


def _build_mashup_transition(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    overlap = p["overlap_bars"] * 4
    return [
        _discrete(0, "sync", target),
        _ramp(0, "volume_ramp", target, overlap, 0.0, 0.85),
        _ramp(overlap, "crossfade", source, 4, 0.0, 1.0),
        _discrete(overlap + 4, "loop_exit", source),
    ]


def _build_stutter_effect(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    repeats = int(p["repeats"])
    step = p["repeat_beats"]
    events = []
    for i in range(repeats):
        t = i * step
        # статтер — это РОЛЛ: со слипом, иначе каждый повтор уводил бы трек
        # назад и к концу серии он отставал бы на всю их сумму
        events.append({"beat_offset": t, "action": "loop_roll", "deck": source,
                       "kind": "hold", "duration_beats": step,
                       "params": {"beats": step * 0.5, "slip": 1}})
    events.append(_discrete(repeats * step, "sync", target))
    events.append(_discrete(repeats * step, "play_from_cue", target))
    events.append(_ramp(repeats * step, "crossfade", source, 4, 0.0, 1.0))
    return events


def _build_backspin_transition(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    # Раньше это был reverse_hold + play_toggle: реверс на полной скорости
    # и мьют. Бэкспин — другое: обороты стартуют в разы выше нормы и гаснут
    # трением, вместе с ними уезжает высота. Сама остановка встроена в
    # spinback, глушить деку отдельно не нужно.
    return [
        {"beat_offset": 0, "action": "spinback", "deck": source, "kind": "hold",
         "duration_beats": p["spin_beats"], "params": {"rate": -9.0}},
        _discrete(p["spin_beats"], "play_toggle", target),
        _ramp(p["spin_beats"], "crossfade", source, 0.1, 0.0, 1.0),
    ]


def _build_beatmatch_cut(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    return [
        _discrete(0, "sync", target),
        _ramp(0, "crossfade", source, 0.1, 0.0, 1.0),
    ]


def _build_tension_riser(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    dur = p["riser_bars"] * 4
    return [
        _ramp(0, "filter_sweep", target, dur, 0.0, 1.0, "ease_in"),
        _ramp(0, "volume_ramp", target, dur, 0.0, 1.0, "ease_in"),
        _discrete(dur, "sync", target),
        _ramp(dur, "crossfade", source, 2, 0.0, 1.0),
    ]


def _build_bass_cut_swap(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    beat = p["swap_beats"]
    return [
        _discrete(0, "sync", target),
        _discrete(0, "eq_kill_low", target),
        _discrete(beat, "eq_kill_low", source),
        _discrete(beat, "eq_kill_low", target),
        _discrete(beat * 2, "eq_kill_low", source),
        _discrete(beat * 2, "eq_kill_low", target),
        _ramp(0, "crossfade", source, beat * 3, 0.0, 1.0),
    ]


def _build_a_cappella_overlay(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Историческое имя приёма «Акапелла поверх» (см. ST-01).

    Здесь стояла заглушка с NotImplementedError: пока стемов не было,
    честнее было отказаться, чем притвориться. Теперь слои считаются
    офлайн, и приём собирается из тех же событий, что ST-01 — параметры
    переводим в его словарь, чтобы не держать два разных рецепта одного
    и того же."""
    return _build_acappella_over(source, target, bpm, {
        "hold_bars": float(p.get("overlay_bars", p.get("hold_bars", 8))),
        "tail_beats": float(p.get("tail_beats", 4)),
    })


# --- реестр техник ---

TECHNIQUES: dict[str, Technique] = {}


def _register(t: Technique, builder) -> None:
    TECHNIQUES[t.id] = t
    _BUILDERS[t.id] = builder


_BUILDERS: dict[str, "callable"] = {}

_register(Technique(
    id="DNB-00", name="Long Blend", category="dnb", difficulty=1,
    description="Базовый плавный переход: filter sweep + sync + долгий кроссфейд. Подходит почти всегда.",
    bpm_delta_max=6, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[TechniqueParam("sweep_bars", "Длительность свипа", 8, 4, 32, "тактов")],
    steps=["Синхронизировать BPM.", "Свип фильтра на source за N тактов.", "Sync target.", "Долгий кроссфейд 2N тактов.", "Выйти из петли source."],
), _build_long_blend)

_register(Technique(
    id="DNB-01", name="Harmonic Blend", category="dnb", difficulty=2,
    description="Долгий кроссфейд без фильтра — для полностью совместимых тональностей (Camelot ±0/±1).",
    bpm_delta_max=4, key_rule="compatible", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[TechniqueParam("blend_bars", "Длительность блендa", 16, 8, 32, "тактов")],
    steps=["Проверить совместимость тональностей.", "Sync target.", "Плавный кроссфейд N тактов без фильтра — оба трека звучат гармонично вместе."],
), _build_harmonic_blend)

_register(Technique(
    id="DNB-02", name="Quick Cut", category="dnb", difficulty=1,
    description="Мгновенный резкий кроссфейд на границе фразы — для несовместимых BPM-типов, когда бленд звучит грязно.",
    bpm_delta_max=None, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[TechniqueParam("cut_beats", "Длительность реза", 0.5, 0.1, 2, "долей")],
    steps=["Дождаться границы фразы source.", "Sync target.", "Резкий кроссфейд < 1 доли."],
), _build_quick_cut)

_register(Technique(
    id="DNB-03", name="Bass Swap", category="dnb", difficulty=2,
    description="Обмен басом между декам на границе такта — классика DnB, две басовые линии не должны звучать одновременно.",
    bpm_delta_max=5, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[TechniqueParam("swap_bar", "Точка обмена", 4, 1, 16, "тактов")],
    steps=["Sync target, бас target выключен.", "Кроссфейд до середины.", "На точке обмена — killLow source, unkill target.", "Докроссфейдить, выйти из петли."],
), _build_bass_swap)

_register(Technique(
    id="DNB-04", name="Filter Sweep", category="dnb", difficulty=1,
    description="Классический свип фильтра вверх на source перед резким переходом — короче Long Blend.",
    bpm_delta_max=8, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[TechniqueParam("sweep_bars", "Длительность свипа", 4, 2, 16, "тактов")],
    steps=["Свип фильтра ease-in на source.", "Sync target.", "Кроссфейд 1 такт."],
), _build_filter_sweep)

_register(Technique(
    id="DNB-05", name="Delay Out", category="dnb", difficulty=3,
    description="Хвост source уходит в дилей/эхо, мид глушится, остаётся только затухающий хвост — мягкий выход без резкого обрыва.",
    bpm_delta_max=None, key_rule="any", energy_direction="down", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("tail_beats", "Длина хвоста", 4, 2, 16, "долей"),
        TechniqueParam("echo_mix", "Громкость эха", 0.6, 0.1, 1.0),
    ],
    steps=["Включить fx-посыл (delay) на source, нарастить за N долей.", "Убрать mid на source — остаётся только эхо-хвост.", "Sync/кроссфейд target.", "Выключить fx после перехода."],
), _build_delay_out)

_register(Technique(
    id="DNB-06", name="EQ Roller", category="dnb", difficulty=3,
    description="Ритмичная LFO-модуляция фильтра (колебание, не монотонный свип) перед переходом — эффект 'подкатывающегося' звука.",
    bpm_delta_max=None, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("roll_bars", "Длительность роллa", 8, 4, 16, "тактов"),
        TechniqueParam("cycles", "Число колебаний", 4, 1, 12),
    ],
    steps=["Колебание фильтра source (sine, N периодов) за roll_bars.", "Sync target.", "Кроссфейд.", "Выйти из петли."],
), _build_eq_roller)

_register(Technique(
    id="DNB-07", name="Echo Cut", category="dnb", difficulty=4,
    description="Комбинация эха, EQ-маскировки и резкого реза — для переходов между конфликтующими тональностями (±6 Camelot).",
    bpm_delta_max=None, key_rule="clash", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("intro_bars_early", "Точка включения", 4, 2, 16, "тактов"),
        TechniqueParam("echo_duration_beats", "Длительность эха", 2, 1, 8, "долей"),
        TechniqueParam("echo_mix", "Громкость эха", 0.7, 0.1, 1.0),
    ],
    steps=["Синхронизировать BPM.", "На target — killMid (мид = гармония, которая конфликтует).", "Ввести target за N тактов (только бас+хай).", "На финальной ноте source — эхо.", "Резко срезать фейдер source в 0.", "На дропе target — резко открыть mid.", "Дать эху затухнуть."],
), _build_echo_cut)

_register(Technique(
    id="DNB-08", name="Phrase Match", category="dnb", difficulty=2,
    description="Long Blend, но переход строго на границе фразы (8/16 тактов) — таймингом занимается mix_strategist.py по structure-анализу.",
    bpm_delta_max=6, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[TechniqueParam("phrase_bars", "Длина фразы", 16, 8, 32, "тактов")],
    steps=["Найти ближайшую границу фразы (structure-анализ трека).", "Long Blend, приведённый к этой границе."],
), _build_phrase_match)

_register(Technique(
    id="DNB-09", name="A Cappella Overlay", category="dnb", difficulty=4,
    description="Вокал одного трека поверх инструментала другого. Историческое имя приёма «Акапелла поверх» (ST-01) — собирается теми же событиями.",
    bpm_delta_max=6, key_rule="compatible", energy_direction="any", requires_stems=True, requires_decks=2,
    needs_vocals=True,
    params=[
        TechniqueParam("overlay_bars", "Голос над новым треком", 8, 2, 32, "тактов"),
        TechniqueParam("tail_beats", "Уход голоса в эхо", 4, 1, 16, "долей"),
    ],
    steps=["Завести новый инструменталом, его вокал выключен.",
           "У старого погасить всё, кроме голоса.",
           "Голос поёт над новым треком.",
           "Голос уходит в эхо, у нового возвращается свой вокал."],
), _build_a_cappella_overlay)

_register(Technique(
    id="DNB-10", name="Triple Drop", category="dnb", difficulty=5,
    description="Три трека сведены так, что их дропы совпадают одновременно — нужна 3-я дека.",
    bpm_delta_max=4, key_rule="compatible", energy_direction="up", requires_stems=False, requires_decks=3,
    params=[
        TechniqueParam("align_bars", "За сколько тактов до дропа заводить обе деки", 8, 2, 32, "тактов"),
        TechniqueParam("hold_beats", "Сколько держать все три вместе", 16, 4, 64, "долей"),
    ],
    steps=["Обе входящие деки завести за N тактов до их дропов, низ снят у обеих.",
           "В момент дропа — низ возвращается ТОЛЬКО у одной деки: три баса не складываются.",
           "Держать совмещение, потом увести старую и третью, оставить одну."],
), _build_triple_drop)

_register(Technique(
    id="DNB-11", name="Loop & Roll", category="dnb", difficulty=2,
    description="Зациклить break source, ввести target поверх петли, затем довести кроссфейд.",
    bpm_delta_max=6, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[TechniqueParam("loop_bars", "Длина петли", 4, 2, 8, "тактов")],
    steps=["Активировать петлю на source.", "Sync target поверх петли.", "Кроссфейд.", "Выйти из петли."],
), _build_loop_and_roll)

_register(Technique(
    id="DNB-12", name="Key Jump", category="dnb", difficulty=3,
    description="Временное транспонирование target к тональности source перед переходом (pitch_adjust, BPM не трогается).",
    bpm_delta_max=6, key_rule="clash", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("jump_bars", "Длительность", 4, 2, 8, "тактов"),
        TechniqueParam("semitones", "Сдвиг", -2, -6, 6, "полутонов"),
    ],
    steps=["Сдвинуть pitch_adjust target на N полутонов.", "Sync и кроссфейд.", "(опционально) вернуть pitch после перехода вручную."],
), _build_key_jump)

_register(Technique(
    id="DNB-13", name="Reverse Drop", category="dnb", difficulty=4,
    description="Реверс (+slip) source прямо перед дропом target — эффект 'засасывания' в переход.",
    bpm_delta_max=None, key_rule="any", energy_direction="up", requires_stems=False, requires_decks=2,
    params=[TechniqueParam("reverse_beats", "Длительность реверса", 2, 1, 8, "долей")],
    steps=["Зажать reverseroll на source на N долей.", "Отпустить точно на дропе target.", "Sync/кроссфейд."],
), _build_reverse_drop)

_register(Technique(
    id="DNB-14", name="Fader FX Series", category="dnb", difficulty=3,
    description="Серия быстрых щелчков кроссфейдером в такт — эффект гейта/статтера на переходе.",
    bpm_delta_max=8, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("pulses", "Число щелчков", 4, 2, 8),
        TechniqueParam("pulse_beats", "Доль на щелчок", 0.5, 0.25, 2),
    ],
    steps=["Серия коротких кроссфейд-импульсов source/target в такт.", "Финальный докроссфейд на target."],
), _build_fader_fx_series)

_register(Technique(
    id="DNB-15", name="Mashup Transition", category="dnb", difficulty=3,
    description="Оба трека играют одновременно N тактов (совместимые key+bpm), затем один остаётся.",
    bpm_delta_max=3, key_rule="compatible", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[TechniqueParam("overlap_bars", "Длительность наложения", 16, 8, 32, "тактов")],
    steps=["Sync target.", "Поднять громкость target до 85%, не убирая source.", "После overlap_bars — докроссфейдить, выйти из петли."],
), _build_mashup_transition)

_register(Technique(
    id="DNB-16", name="Double Drop", category="dnb", difficulty=4,
    description="Дропы source и target синхронизированы по времени — structure-анализ (drops) обоих треков используется для расчёта align_bars.",
    bpm_delta_max=4, key_rule="compatible", energy_direction="up", requires_stems=False, requires_decks=2,
    params=[TechniqueParam("align_bars", "Такты совмещения", 8, 4, 16, "тактов")],
    steps=["Sync target.", "Свести громкости до 50/50 к точке совпадения дропов.", "На дропе — докроссфейдить, выйти из петли source."],
), _build_double_drop)

_register(Technique(
    id="DNB-17", name="Stutter Effect", category="dnb", difficulty=3,
    description="Рваное повторение короткого фрагмента source (гейтед-лупинг) перед переходом.",
    bpm_delta_max=None, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("repeats", "Число повторов", 4, 2, 8),
        TechniqueParam("repeat_beats", "Доль на повтор", 0.5, 0.25, 1),
    ],
    steps=["Быстрые loop_activate/loop_exit на source в такт.", "Sync/кроссфейд target."],
), _build_stutter_effect)

_register(Technique(
    id="DNB-18", name="Backspin Transition", category="dnb", difficulty=3,
    description="Классический винтажный бэкспин: реверс с ускорением 'назад', резкая остановка, старт target.",
    bpm_delta_max=None, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[TechniqueParam("spin_beats", "Длительность спина", 1, 0.5, 4, "долей")],
    steps=["Зажать reverseroll на source.", "Резко остановить source, запустить target.", "Резкий кроссфейд на target."],
), _build_backspin_transition)

_register(Technique(
    id="DNB-19", name="Beatmatch Cut", category="dnb", difficulty=1,
    description="Простейший резкий рез после бит-матчинга — базовая техника, всегда доступна как fallback.",
    bpm_delta_max=None, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[],
    steps=["Sync target.", "Мгновенный кроссфейд."],
), _build_beatmatch_cut)

_register(Technique(
    id="DNB-20", name="Tension Riser", category="dnb", difficulty=2,
    description="Нарастание фильтра+громкости на target перед дропом — усиливает ощущение подъёма энергии в сете.",
    bpm_delta_max=6, key_rule="any", energy_direction="up", requires_stems=False, requires_decks=2,
    params=[TechniqueParam("riser_bars", "Длительность подъёма", 8, 4, 16, "тактов")],
    steps=["Нарастить фильтр+громкость target ease-in за N тактов.", "Sync.", "Короткий докроссфейд на дропе."],
), _build_tension_riser)

_register(Technique(
    id="DNB-21", name="Bass Cut Swap", category="dnb", difficulty=2,
    description="Быстрый 'вопрос-ответ' басами: source/target попеременно врубают и глушат бас на каждый такт.",
    bpm_delta_max=5, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[TechniqueParam("swap_beats", "Долей между обменами", 4, 2, 8)],
    steps=["Sync target, бас target выключен.", "Каждые N долей — попеременный killLow source/target.", "Кроссфейд параллельно обменам."],
), _build_bass_cut_swap)


def available_techniques(source_deck_count: int = 2) -> list[Technique]:
    return [t for t in TECHNIQUES.values() if t.requires_decks <= source_deck_count]



_register(Technique(
    id="DNB-22", name="Fader Chop", category="dnb", difficulty=3,
    description="Фейдер перекидывается между треками целыми тактами несколько раз, потом остаётся на новом. То, как драм-н-бейс режут вживую.",
    bpm_delta_max=4, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("swaps", "Сколько раз перекинуть", 4, 2, 8),
        TechniqueParam("bars_per_swap", "Тактов на трек", 1, 0.5, 4, "тактов"),
    ],
    steps=["Sync и запуск нового трека с точки входа.",
           "Бросок фейдера на новый трек ровно на границе такта.",
           "Обратно на старый через такт. Повторить N раз.",
           "Последний бросок — остаёмся на новом."],
), _build_fader_chop)

_register(Technique(
    id="DNB-23", name="Drop Teaser", category="dnb", difficulty=4,
    description="Новый трек выглядывает короткими вспышками на границах фраз, а потом заходит совсем — ухо уже узнало его, и дроп читается как обещанное событие.",
    bpm_delta_max=4, key_rule="compatible", energy_direction="up", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("teases", "Число вспышек", 3, 1, 6),
        TechniqueParam("every_bars", "Через сколько тактов", 2, 1, 8, "тактов"),
        TechniqueParam("tease_beats", "Длина вспышки", 2, 0.5, 8, "долей"),
    ],
    steps=["Sync и запуск нового трека.",
           "Короткая вспышка нового трека на границе фразы.",
           "Назад на старый. Повторить.",
           "Финальный заход — новый остаётся."],
), _build_drop_teaser)

_register(Technique(
    id="DNB-24", name="Bar Switch", category="dnb", difficulty=3,
    description="Джангловый обмен такт за тактом: низ уходит вместе с фейдером, поэтому два баса никогда не звучат вместе.",
    bpm_delta_max=4, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("rounds", "Число обменов", 4, 2, 8),
        TechniqueParam("bars_per_round", "Тактов на обмен", 2, 1, 4, "тактов"),
    ],
    steps=["Sync и запуск нового трека.",
           "Обмен: фейдер и низ уходят на новый трек.",
           "Через N тактов — обратно.",
           "Последний обмен оставляет новый трек."],
), _build_bar_switch)



_register(Technique(
    id="DNB-25", name="Классика", category="dnb", difficulty=1,
    description="Основной ход: новый заводится своим интро под старый со снятым низом, фразу-две они идут вместе, потом один обмен низом и старый уводится фильтром с эхом. Длина сведения — 4-12 тактов, как у живого диджея.",
    bpm_delta_max=4, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[
        # Диапазоны нарочно шире, чем нужно «по умолчанию»: значения по
        # умолчанию — это то, как сводят обычно, а границы — предел, за
        # которым техника перестаёт быть собой. Ручку, упёртую в край, слышно
        # как ограничение инструмента, а не как решение.
        TechniqueParam("blend_bars", "Длина сведения", 8, 2, 32, "тактов"),
        TechniqueParam("entry_delay_bars", "Пауза перед входом нового", 0, 0, 8, "тактов"),
        TechniqueParam("entry_ramp_beats", "Ввод нового за", 2, 1, 32, "долей"),
        TechniqueParam("out_bars", "Из них на вывод старого", 4, 1, 16, "тактов"),
        TechniqueParam("under_level", "Насколько слышен новый под старым", 0.40, 0.0, 1.0),
        TechniqueParam("mid_duck", "Убрать середину у нового, пока играют оба", 0.0, 0.0, 1.0),
        TechniqueParam("swap_bars", "Обмен низом за", 1, 0.25, 8, "тактов"),
        TechniqueParam("loop_bars", "Луп последней фразы старого", 4, 0, 32, "тактов"),
        TechniqueParam("echo", "Эхо на уходе старого", 0.55, 0.0, 1.0),
        TechniqueParam("reverse_beats", "Реверс перед обменом", 0, 0, 32, "долей"),
    ],
    steps=["Sync, запустить новый трек с его интро.",
           "Низ у нового снят полностью — играет один бас, старого.",
           "4-12 тактов они идут вместе — фраза или две, не полторы минуты.",
           "Последняя фраза старого — в луп, чтобы обмен попал на границу фразы.",
           "На границе фразы — один обмен низом за такт.",
           "Старый уводится фильтром и уходит в эхо; хвост дилея склеивает стык."],
), _build_classic)



# --- Приёмы вертушки: то, чем диджеи реально «рвут» сет ---
#
# Раньше в библиотеке они были только по названию: Backspin делался
# действием reverse_hold, а оно в офлайн-рендере просто переворачивало
# кусок буфера — реверс на ПОЛНОЙ скорости, без падения оборотов и без
# падения высоты. То есть ровно того, по чему бэкспин и тейп-стоп узнаются
# на слух, там не было вовсе.
#
# Теперь это отдельный словарь действий (см. transport.py), который двигает
# ПОЗИЦИЮ ИГЛЫ, а не обрабатывает звук:
#   brake        — выбег до остановки (тейп-стоп, «Stop» на Technics);
#   spinback     — рука кидает пластинку назад в N крат, трение гасит;
#   soft_start   — мотор разгоняется с нуля до рабочей скорости;
#   reverse_play — реверс; со слипом это censor / reverse roll;
#   loop_roll    — моментарный луп со слипом (после отпускания трек НЕ сбит);
#   loop_activate/loop_exit — обычный луп;
#   beatjump     — прыжок по сетке.
#
# Где эти приёмы уместны — не вкусовщина, а жанровая норма:
#   * DnB / джангл / дабстеп — спинбэк и ревайнд это часть культуры
#     («wheel-up»), спинбэком закругляют сведение;
#   * хип-хоп / фанк — vinyl stop, бэкспин, рез, эхо;
#   * хаус / транс — почти никогда: там длинный бленд и EQ;
#   * техно — лупы и FX, но не вертушечные трюки;
#   * открытый формат — всё сразу, и именно трюком делают прыжок
#     между жанрами и темпами.
# Поэтому автоматический выбор трюки НЕ ставит (кроме случая, когда темпы
# объективно не сводятся), но они доступны пресетом и вручную.

def _transport(beat_offset: float, action: str, deck: str, duration_beats: float,
               params: dict | None = None) -> dict:
    """Событие, двигающее иглу. duration_beats — сколько ДЛИТСЯ приём,
    а не сколько материала он проматывает: бэкспин укладывается в одну
    долю, но уводит трек на три-четыре доли назад."""
    return {"beat_offset": beat_offset, "action": action, "deck": deck, "kind": "hold",
            "duration_beats": duration_beats, "params": params or {}}


def _land_on_bar(beats: float) -> float:
    """Ближайшая ГРАНИЦА ТАКТА не раньше, чем через beats долей.

    Приём вертушки не «начинается» на границе фразы — он на ней
    ЗАКАНЧИВАЕТСЯ: бэкспин делают на последней доле перед «разом», а не
    после него. Поэтому техника считает, где приём должен приземлиться, и
    отступает назад на его длительность. Иначе между смертью старого трека
    и стартом нового зияет дырка в три доли — ровно то, чего живой диджей
    не допускает никогда."""
    import math
    return 4.0 * max(1.0, math.ceil(beats / 4.0))


def _build_spinback_cut(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Классический бэкспин: последняя доля фразы — пластинку назад,
    трек умирает, новый входит на «раз» следующего такта."""
    spin = p["spin_beats"]
    land = _land_on_bar(spin)
    return [
        _discrete(0, "sync", target),
        _transport(land - spin, "spinback", source, spin, {"rate": -abs(p["spin_rate"])}),
        _cut(land, source, 0.0, 1.0),
        _discrete(land, "play_from_cue", target),
    ]


def _build_tape_stop(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Плёнка/пластинка тормозит до полной остановки: темп и высота уезжают
    вниз вместе — это одно движение, а не эффект. Новый трек заходит с
    границы такта после остановки."""
    brake = p["brake_beats"]
    land = _land_on_bar(brake)
    start = land - brake
    return [
        _discrete(0, "sync", target),
        _transport(start, "brake", source, brake, {"curve": p["curve"]}),
        _cut(land, source, 0.0, 1.0),
        _discrete(land, "play_from_cue", target),
    ]


def _build_power_off(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """«Выдернули из розетки»: очень долгий выбег, когда сет надо
    переломить целиком. Это одноразовый приём — не способ сводить."""
    coast = p["coast_beats"]
    land = _land_on_bar(coast)
    start = land - coast
    return [
        _discrete(0, "sync", target),
        _transport(start, "brake", source, coast, {"curve": 1.15}),
        _ramp(start, "fx_meta", source, coast, 0.0, 0.45),
        _cut(land, source, 0.0, 1.0),
        _discrete(land, "play_from_cue", target),
    ]


def _build_reverse_roll_in(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Reverse roll (censor) на уходящем: последний такт идёт задом наперёд,
    но благодаря слипу трек НЕ сбивается — фраза кончается там же, где
    кончилась бы. Под ним уже играет новый, и на «раз» они меняются."""
    under = p["under_bars"] * 4
    rev = min(p["reverse_beats"], max(1.0, under - 2))
    level = 0.4          # насколько слышен новый, пока идёт под старым
    return [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        # низ у нового снят: играет один бас, старого
        _ramp(0, "eq_low", target, 1, 0.0, 0.0),
        # Слышимость входящей деки здесь ведёт КРОССФЕЙДЕР, а не
        # volume_ramp: в рендере громкости дек сводятся равной мощностью
        # именно по нему (см. demo_render, _equal_power(cf_env)), и дека,
        # у которой фейдер на нуле, не зазвучит ни от какой громкости.
        _ramp(0, "crossfade", source, 2, 0.0, level),
        _ramp(2, "crossfade", source, under - rev - 2, level, level),
        _transport(under - rev, "reverse_play", source, rev, {"slip": 1}),
        _ramp(under - rev, "fx_meta", source, rev, 0.0, 0.5),
        _cut(under, source, level, 1.0),
        _ramp(under, "eq_low", target, 2, 0.0, EQ_UNITY),
    ]


def _build_loop_roll_build(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Лупролл, который вдвое короче на каждом шаге: 4 -> 2 -> 1 -> 1/2 ->
    1/4. Ухо читает сжимающийся луп как разгон, и на «раз» после него
    новый трек садится сам собой. Ролл идёт со слипом: если бы это был
    обычный луп, трек уехал бы назад на всю сумму повторов."""
    steps = int(p["steps"])
    beats = float(p["start_beats"])
    t = 0.0
    events: list[dict] = [_discrete(0, "sync", target)]
    for _ in range(max(1, steps)):
        # луп надо УСЛЫШАТЬ повторённым: держим его хотя бы два оборота,
        # иначе четырёхдольный «ролл» длиной в четыре доли — это просто
        # трек, играющий сам себя, и ничего не происходит
        hold = max(beats * 2.0, 1.0)
        events.append(_transport(t, "loop_roll", source, hold, {"beats": beats, "slip": 1}))
        t += hold
        beats = max(0.125, beats / 2.0)
    land = _land_on_bar(t)
    events += [
        _cut(land, source, 0.0, 1.0),
        _discrete(land, "play_from_cue", target),
    ]
    return events


def _build_loop_out(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Уходящий уходит в луп на последней фразе и висит там, пока новый
    заходит целиком. Луп — единственный способ дождаться фразы нового
    трека, не обрывая старый на полуслове."""
    loop_beats = p["loop_bars"] * 4
    hold = max(p["hold_bars"] * 4, loop_beats + 4)
    level = 0.4
    return [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        _discrete(0, "loop_activate", source, {"beats": loop_beats}),
        _ramp(0, "eq_low", target, 1, 0.0, 0.0),
        _ramp(0, "crossfade", source, 2, 0.0, level),
        _ramp(2, "crossfade", source, hold - 6, level, level),
        _ramp(hold * 0.5, "filter_sweep", source, hold * 0.5, 0.0, 0.85, "ease_in"),
        _ramp(hold - 4, "eq_low", source, 4, EQ_UNITY, 0.0),
        _ramp(hold - 4, "eq_low", target, 4, 0.0, EQ_UNITY),
        _discrete(hold, "loop_exit", source),
        _ramp(hold - 4, "crossfade", source, 8, level, 1.0),
    ]


def _build_echo_spinback(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Эхо-хвост и бэкспин поверх него. Эхо держит гармонию последней
    фразы, пока трека уже нет, — поэтому приём работает даже там, где
    тональности спорят и темпы не сводятся."""
    echo = p["echo_beats"]
    spin = p["spin_beats"]
    land = _land_on_bar(echo + spin)
    open_at = max(0.0, land - spin - echo)
    return [
        _discrete(0, "sync", target),
        # Посыл на дилей — это НЕ плавное нарастание с начала фразы.
        # Диджей открывает его в конце, на последних долях: линия задержки
        # должна забрать ПОСЛЕДНЮЮ фразу, ту, что будет звенеть над новым
        # треком. Ручка, открытая с начала, забирает начало — ровно то,
        # что было слышно как «эхо берёт первую половину».
        _ramp(open_at, "fx_meta", source, 0.5, 0.0, 0.95),
        _ramp(open_at + 0.5, "fx_meta", source, max(0.5, echo - 0.5), 0.95, 0.95),
        _transport(land - spin, "spinback", source, spin, {"rate": -abs(p["spin_rate"])}),
        _cut(land, source, 0.0, 1.0),
        _discrete(land, "play_from_cue", target),
    ]


def _build_loop_choke_spin(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Луп делится пополам до предела, потом бэкспин.

    То, что диджеи делают руками чаще всего: взять луп на последней фразе
    и жать «половину» — 4 доли, 2, 1, 1/2, 1/4 — пока трек не свернётся в
    гудящую точку. Ухо читает удвоение частоты повторов как разгон, и на
    пределе разгон разрешается бэкспином.

    Каждая ступень держится ОДИНАКОВОЕ время, а не одинаковое число
    повторов: только тогда частота повторов удваивается на каждом шаге, и
    получается разгон. Держать «по два оборота» значило бы, что каждая
    следующая ступень вдвое короче предыдущей, и весь приём кончился бы,
    не начавшись."""
    step = float(p["step_beats"])
    beats = float(p["start_beats"])
    floor = float(p["min_beats"])
    t = 0.0
    ev: list[dict] = [_discrete(0, "sync", target)]
    while True:
        ev.append(_transport(t, "loop_roll", source, step, {"beats": beats, "slip": 1}))
        t += step
        if beats <= floor + 1e-6:
            break
        beats = max(floor, beats / 2.0)
    spin = float(p["spin_beats"])
    land = _land_on_bar(t + spin)
    return ev + [
        _transport(land - spin, "spinback", source, spin, {"rate": -abs(p["spin_rate"])}),
        _cut(land, source, 0.0, 1.0),
        _discrete(land, "play_from_cue", target),
    ]


def _build_rewind(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Ревайнд / wheel-up: пластинку уводят далеко назад, и новый трек
    поднимается с нуля до рабочей скорости — так дроп читается как
    объявленный. В джангле, DnB и дабстепе это часть культуры, а не трюк
    ради трюка."""
    spin = p["spin_beats"]
    ramp = p["start_bars"] * 4
    land = _land_on_bar(spin)
    return [
        _discrete(0, "sync", target),
        _transport(land - spin, "spinback", source, spin, {"rate": -abs(p["spin_rate"])}),
        _cut(land, source, 0.0, 1.0),
        _discrete(land, "play_from_cue", target),
        _transport(land, "soft_start", target, ramp),
    ]



_register(Technique(
    id="TT-01", name="Спинбэк в рез", category="turntable", difficulty=3,
    description="Пластинку кидают назад на последней доле фразы: обороты и высота улетают вверх и гаснут, трек умирает, новый входит на «раз». Темпы и тональности при этом могут не сходиться вовсе — их ничто не накладывает.",
    bpm_delta_max=None, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("spin_beats", "Длительность спина", 1, 0.25, 4, "долей"),
        TechniqueParam("spin_rate", "Сила броска", 9, 3, 16, "крат"),
    ],
    steps=["Довести уходящий до последней доли фразы.",
           "Бросок пластинки назад — обороты гаснут за долю.",
           "Фейдер на новый трек, новый стартует с «раза» следующего такта."],
), _build_spinback_cut)

_register(Technique(
    id="TT-02", name="Тейп-стоп", category="turntable", difficulty=2,
    description="Выбег до полной остановки: темп и высота уезжают вниз ВМЕСТЕ, потому что это одно движение, а не эффект. Работает как знак препинания между кусками сета и как единственный честный способ сменить темп.",
    bpm_delta_max=None, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("brake_beats", "Длительность выбега", 2, 0.5, 16, "долей"),
        TechniqueParam("curve", "Характер торможения", 1.6, 1.0, 3.0),
    ],
    steps=["На границе фразы — тормоз уходящей деки.",
           "Обороты падают за N долей, высота падает вместе с ними.",
           "Новый трек — с «раза» следующего такта."],
), _build_tape_stop)

_register(Technique(
    id="TT-03", name="Выключение мотора", category="turntable", difficulty=2,
    description="Очень долгий выбег с эхом — «выдернули из розетки». Приём на один раз за сет: им ломают сет пополам или закрывают его.",
    bpm_delta_max=None, key_rule="any", energy_direction="down", requires_stems=False, requires_decks=2,
    params=[TechniqueParam("coast_beats", "Длительность выбега", 8, 4, 32, "долей")],
    steps=["Поднять эхо на уходящем.", "Отпустить мотор — трек вязнет и умирает.",
           "Новый трек с границы такта."],
), _build_power_off)

_register(Technique(
    id="TT-04", name="Реверс-ролл", category="turntable", difficulty=3,
    description="Последний такт уходящего идёт задом наперёд, но со слипом: фраза кончается ровно там же, где кончилась бы. Новый трек уже играет под ним и забирает низ на «раз».",
    bpm_delta_max=6, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("under_bars", "Новый идёт под старым", 8, 2, 32, "тактов"),
        TechniqueParam("reverse_beats", "Длина реверса", 4, 1, 16, "долей"),
    ],
    steps=["Завести новый под играющим со снятым низом.",
           "За N долей до границы фразы — реверс уходящего (censor).",
           "На «раз» — фейдер и низ уходят новому."],
), _build_reverse_roll_in)

_register(Technique(
    id="TT-05", name="Лупролл-билд", category="turntable", difficulty=4,
    description="Лупролл, вдвое короче на каждом шаге: 4 → 2 → 1 → ½ → ¼. Сжимающийся луп ухо читает как разгон, и рез после него звучит как разрешение. Ролл идёт со слипом — трек не сбивается.",
    bpm_delta_max=None, key_rule="any", energy_direction="up", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("start_beats", "Стартовая длина лупа", 4, 1, 16, "долей"),
        TechniqueParam("steps", "Сколько раз ужать", 4, 2, 6),
    ],
    steps=["Взять луп на последней фразе уходящего.",
           "Ужимать вдвое каждый шаг, оставаясь на сетке.",
           "На «раз» после последнего ролла — рез на новый трек."],
), _build_loop_roll_build)

_register(Technique(
    id="TT-06", name="Луп-хвост", category="turntable", difficulty=2,
    description="Уходящий уходит в луп на последней фразе и висит там, пока новый заходит целиком. Единственный способ дождаться фразы нового трека, не обрывая старый на полуслове — и то, чем чинят «у нового слишком длинное интро».",
    bpm_delta_max=6, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("loop_bars", "Длина лупа", 4, 1, 16, "тактов"),
        TechniqueParam("hold_bars", "Сколько держать", 8, 4, 32, "тактов"),
    ],
    steps=["Луп на последней фразе уходящего.",
           "Новый заходит под ним со снятым низом.",
           "Свип фильтра на уходящем, обмен низом за такт до конца.",
           "Выход из лупа и фейдер на новый."],
), _build_loop_out)

_register(Technique(
    id="TT-07", name="Эхо-хвост + спинбэк", category="turntable", difficulty=3,
    description="Эхо забирает последнюю фразу, поверх хвоста — бэкспин. Эхо держит гармонию, когда трека уже нет, поэтому приём работает и на спорящих тональностях, и на несводимых темпах.",
    bpm_delta_max=None, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("echo_beats", "Длина эха", 4, 2, 16, "долей"),
        TechniqueParam("spin_beats", "Длительность спина", 1, 0.25, 4, "долей"),
        TechniqueParam("spin_rate", "Сила броска", 9, 3, 16, "крат"),
    ],
    steps=["Поднять эхо на уходящем к границе фразы.",
           "Бэкспин поверх хвоста эха.",
           "Новый трек с «раза» следующего такта."],
), _build_echo_spinback)

_register(Technique(
    id="TT-08", name="Ревайнд", category="turntable", difficulty=4,
    description="Wheel-up: пластинку уводят далеко назад, и новый трек поднимается с нуля до рабочей скорости. В джангле, DnB и дабстепе это часть культуры — так объявляют дроп.",
    bpm_delta_max=None, key_rule="any", energy_direction="up", requires_stems=False, requires_decks=2,
    params=[
        # Ревайнд — не бэкспин. Бэкспин укладывается в долю и звучит как
        # точка; ревайнд тянут ЦЕЛЫЙ ТАКТ, и слышно, как пластинка едет.
        # Раньше здесь стояло 2 доли на 12 крат: пулей, и за одну и ту же
        # промотку назад. Теперь такт на шести кратах — та же дистанция,
        # но её слышно.
        TechniqueParam("spin_beats", "Длительность ревайнда", 4, 1, 16, "долей"),
        TechniqueParam("spin_rate", "Сила броска", 6, 2, 20, "крат"),
        TechniqueParam("start_bars", "Разгон нового", 1, 0.25, 4, "тактов"),
    ],
    steps=["Ревайнд уходящего — далеко назад.",
           "Новый стартует с нуля и разгоняется до рабочей скорости.",
           "Дроп попадает ровно на выход разгона."],
), _build_rewind)



# --- Техники по слоям (стемам) ---
#
# Всё, что выше, работает с готовым стерео-мастером: эквалайзер, фильтр,
# фейдер. Этого достаточно, пока накладываются интро и хвост, и перестаёт
# хватать ровно там, где начинается интересное — когда обе деки играют с
# битом. Измерено на реальных переходах: 180-500 Гц и 500-2000 Гц дают
# перебор +2..+3 dB над громчайшим из двух, и вычистить это эквалайзером
# НЕЛЬЗЯ, потому что обеим дорожкам эти полосы нужны.
#
# Слои снимают задачу целиком: барабаны одного трека и гармония другого
# не спорят, потому что их больше не складывают. Отсюда приёмы, которых
# без разделения не существует в принципе:
#   * акапелла поверх чужого инструментала;
#   * обмен барабанами (то, что делает Traktor Stems);
#   * честный обмен БАСОМ — именно инструментом, а не полосой ниже 180 Гц,
#     в которую заодно попадает бочка и низ пэда;
#   * дабл-дроп, в котором ритм-секция берётся у одного трека, а гармония
#     у другого — и он не превращается в кашу.
#
# Слои: drums, bass, other (гармония), vocals. Действие stem_gain(слой)
# работает как отдельный фейдер на каждый — так же, как на пульте со
# стемами, ДО эквалайзера и фильтра канала.

STEMS = ("drums", "bass", "other", "vocals")


def _stem(beat_offset: float, deck: str, stem: str, duration_beats: float,
          value_from: float, value_to: float, curve: str = "linear") -> dict:
    """Фейдер одного слоя. 1.0 — слой звучит целиком, 0.0 — его нет."""
    return {"beat_offset": beat_offset, "action": "stem_gain", "deck": deck,
            "kind": "ramp", "duration_beats": max(0.01, duration_beats),
            "value_from": value_from, "value_to": value_to, "curve": curve,
            "params": {"stem": stem}}


def _stems_at(beat: float, deck: str, levels: dict, ramp_beats: float = 0.5) -> list[dict]:
    """Выставить сразу несколько слоёв деки. Не мгновенно, а за полдоли:
    мгновенный обрыв слоя щёлкает так же, как мгновенный фейдер."""
    return [_stem(beat, deck, k, ramp_beats, v, v) for k, v in levels.items()]


def _build_acappella_over(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Вокал уходящего поверх инструментала входящего.

    Приём открытого формата и главная причина, ради которой вокал вообще
    выделяют отдельным слоем. Порядок такой: новый заходит инструменталом
    (вокала у него нет — он выключен), у старого постепенно гаснет всё,
    кроме голоса, и какое-то время голос старого поёт над новым треком.
    Потом голос уходит в эхо, а у нового возвращается свой вокал."""
    hold = p["hold_bars"] * 4
    tail = p["tail_beats"]
    ramp = 4.0
    ev = [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        _ramp(0, "crossfade", source, ramp, 0.0, 0.5),
        # у входящего вокала пока нет: два голоса разом — единственный
        # клэш, который слышат вообще все
        *_stems_at(0, target, {"vocals": 0.0}),
    ]
    # у уходящего гасим всё, кроме голоса
    for part in ("drums", "bass", "other"):
        ev.append(_stem(ramp, source, part, ramp, 1.0, 0.0, "ease_out"))
    ev += [
        _ramp(ramp, "crossfade", source, ramp, 0.5, 0.35),
        # голос ещё поёт над новым треком
        _ramp(hold - tail, "fx_meta", source, tail, 0.0, 0.9, "ease_in"),
        _stem(hold - tail, source, "vocals", tail, 1.0, 0.0, "ease_out"),
        _cut(hold, source, 0.35, 1.0),
        _stem(hold, target, "vocals", ramp, 0.0, 1.0),
    ]
    return ev


def _build_drum_swap(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Обмен барабанами: то, что делает Traktor Stems.

    Новый трек заходит ОДНИМИ барабанами под гармонию старого — их некуда
    складывать, потому что барабаны старого в этот момент выключены. Ухо
    слышит смену ритма раньше, чем смену трека, и это самый плавный из
    возможных переходов между разными по настроению вещами."""
    under = p["under_bars"] * 4
    swap = p["swap_beats"]
    return [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        _ramp(0, "crossfade", source, 2, 0.0, 0.5),
        # входящий — только барабаны
        *_stems_at(0, target, {"bass": 0.0, "other": 0.0, "vocals": 0.0}),
        # у уходящего барабаны уходят ровно в тот же момент
        _stem(0, source, "drums", swap, 1.0, 0.0, "ease_out"),
        # через фразу возвращаем новому всё остальное, у старого забираем
        _stem(under, target, "bass", swap, 0.0, 1.0),
        _stem(under, target, "other", swap, 0.0, 1.0),
        _stem(under, target, "vocals", swap, 0.0, 1.0),
        _stem(under, source, "bass", swap, 1.0, 0.0),
        _stem(under, source, "other", swap, 1.0, 0.0, "ease_out"),
        _stem(under, source, "vocals", swap, 1.0, 0.0, "ease_out"),
        _ramp(under, "crossfade", source, swap * 2, 0.5, 1.0),
    ]


def _build_bass_handover(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Честный обмен басом — инструментом, а не полосой.

    Обмен низом эквалайзером всегда забирает не только бас: ниже 180 Гц
    живут ещё бочка и тело пэда, и поэтому «обмен низом» слышен как
    короткий провал. Со слоями меняется ровно бас, а бочка обеих дек
    остаётся на месте."""
    under = p["under_bars"] * 4
    swap = p["swap_beats"]
    return [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        _ramp(0, "crossfade", source, 2, 0.0, p["under_level"]),
        *_stems_at(0, target, {"bass": 0.0}),
        _stem(under, target, "bass", swap, 0.0, 1.0),
        _stem(under, source, "bass", swap, 1.0, 0.0),
        _ramp(under, "crossfade", source, swap * 2, p["under_level"], 1.0),
        _ramp(under + swap * 2, "fx_meta", source, 4, 0.0, 0.5),
    ]


def _build_stem_double_drop(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Дабл-дроп, который не превращается в кашу.

    Обычный дабл-дроп — это два полных мастера разом, и звучит он ровно
    так, как звучат два полных мастера разом. Со слоями ритм-секция
    берётся у ОДНОГО трека, а гармония и голос у другого: слышно оба
    трека, а складывать нечего."""
    hold = p["hold_bars"] * 4
    ramp = 2.0
    return [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        _ramp(0, "crossfade", source, ramp, 0.0, 0.5),
        # ритм — у уходящего, гармония и голос — у входящего
        *_stems_at(0, target, {"drums": 0.0, "bass": 0.0}),
        *_stems_at(0, source, {"other": 0.0, "vocals": 0.0}),
        # к концу совмещения ритм отдаётся входящему
        _stem(hold, target, "drums", ramp, 0.0, 1.0),
        _stem(hold, target, "bass", ramp, 0.0, 1.0),
        _stem(hold, source, "drums", ramp, 1.0, 0.0),
        _stem(hold, source, "bass", ramp, 1.0, 0.0),
        _ramp(hold, "crossfade", source, ramp * 2, 0.5, 1.0),
    ]


def _build_vocal_echo_out(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Уходящий разбирается на части: сначала уходит ритм, потом гармония,
    последним остаётся голос — и уходит в эхо.

    Это способ закончить трек, когда следующий совсем другой: к моменту
    смены от старого остаётся один голос, и он не спорит ни с чем."""
    step = p["step_bars"] * 4
    tail = p["tail_beats"]
    land = step * 3 + tail
    return [
        _discrete(0, "sync", target),
        _stem(0, source, "drums", step, 1.0, 0.0, "ease_out"),
        _stem(step, source, "bass", step, 1.0, 0.0, "ease_out"),
        _stem(step * 2, source, "other", step, 1.0, 0.0, "ease_out"),
        _ramp(step * 3, "fx_meta", source, tail, 0.0, 0.95, "ease_in"),
        _stem(step * 3, source, "vocals", tail, 1.0, 0.0, "ease_out"),
        _cut(land, source, 0.0, 1.0),
        _discrete(land, "play_from_cue", target),
    ]



_register(Technique(
    id="ST-01", name="Акапелла поверх", category="stems", difficulty=4,
    description="Голос уходящего поёт над инструменталом входящего: у нового вокал выключен, у старого выключено всё, кроме голоса. Приём открытого формата и главная причина выделять вокал отдельным слоем.",
    bpm_delta_max=6, key_rule="compatible", energy_direction="any", requires_stems=True, requires_decks=2,
    needs_vocals=True,
    params=[
        TechniqueParam("hold_bars", "Голос над новым треком", 8, 2, 32, "тактов"),
        TechniqueParam("tail_beats", "Уход голоса в эхо", 4, 1, 16, "долей"),
    ],
    steps=["Завести новый инструменталом, его вокал выключен.",
           "У старого погасить барабаны, бас и гармонию — остаётся голос.",
           "Голос поёт над новым треком заданное число тактов.",
           "Голос уходит в эхо, у нового возвращается свой вокал."],
), _build_acappella_over)

_register(Technique(
    id="ST-02", name="Обмен барабанами", category="stems", difficulty=3,
    description="Новый заходит ОДНИМИ барабанами под гармонию старого — складывать нечего, потому что барабаны старого в этот момент выключены. Ухо слышит смену ритма раньше, чем смену трека.",
    bpm_delta_max=4, key_rule="any", energy_direction="any", requires_stems=True, requires_decks=2,
    params=[
        TechniqueParam("under_bars", "Барабаны нового под старым", 8, 2, 32, "тактов"),
        TechniqueParam("swap_beats", "Длина обмена", 2, 0.5, 8, "долей"),
    ],
    steps=["Завести новый: только барабаны, остальное выключено.",
           "У старого барабаны уходят в тот же момент.",
           "Через фразу вернуть новому бас, гармонию и голос, у старого забрать."],
), _build_drum_swap)

_register(Technique(
    id="ST-03", name="Обмен басом по слою", category="stems", difficulty=2,
    description="Меняется ровно бас — инструмент, а не полоса ниже 180 Гц. Обмен низом эквалайзером всегда уносит с собой бочку и тело пэда, и поэтому слышен как провал; здесь бочка обеих дек остаётся на месте.",
    bpm_delta_max=4, key_rule="compatible", energy_direction="any", requires_stems=True, requires_decks=2,
    params=[
        TechniqueParam("under_bars", "Новый под старым", 8, 2, 32, "тактов"),
        TechniqueParam("swap_beats", "Длина обмена", 2, 0.5, 8, "долей"),
        TechniqueParam("under_level", "Уровень нового под старым", 0.4, 0.2, 0.7),
    ],
    steps=["Завести новый с выключенным басом.",
           "Держать фразу-две.", "На границе фразы обменять бас за N долей.",
           "Вывести старого фейдером, хвост закрыть эхом."],
), _build_bass_handover)

_register(Technique(
    id="ST-04", name="Дабл-дроп по слоям", category="stems", difficulty=5,
    description="Ритм-секция от одного трека, гармония и голос от другого. Слышно оба трека сразу, а складывать нечего — то, чего обычный дабл-дроп из двух полных мастеров не умеет.",
    bpm_delta_max=3, key_rule="compatible", energy_direction="up", requires_stems=True, requires_decks=2,
    params=[TechniqueParam("hold_bars", "Сколько держать совмещение", 8, 2, 32, "тактов")],
    steps=["Совместить дропы обоих треков.",
           "Барабаны и бас — у уходящего, гармония и голос — у входящего.",
           "К концу совмещения отдать ритм входящему."],
), _build_stem_double_drop)

_register(Technique(
    id="ST-05", name="Разбор на слои", category="stems", difficulty=3,
    description="Уходящий разбирается по частям: сначала уходят барабаны, потом бас, потом гармония, последним остаётся голос — и уходит в эхо. Способ закончить трек, когда следующий совсем другой.",
    bpm_delta_max=None, key_rule="any", energy_direction="down", requires_stems=True, requires_decks=2,
    params=[
        TechniqueParam("step_bars", "Шаг разбора", 2, 1, 8, "тактов"),
        TechniqueParam("tail_beats", "Хвост голоса в эхе", 4, 1, 16, "долей"),
    ],
    steps=["Убрать барабаны.", "Через N тактов убрать бас.",
           "Ещё через N — гармонию, остаётся голос.",
           "Голос в эхо, новый трек с границы такта."],
), _build_vocal_echo_out)


# =====================================================================
# Слои + эффекты, и слои + обычный эквалайзер
# =====================================================================
#
# Первая волна стемовых приёмов (ST-01..ST-08) делала со слоями ровно
# одно: включала и выключала их. Этого мало, и диджей прав, когда
# говорит, что «стемы реализованы плохо». В реальной работе слои почти
# никогда не работают в одиночку — они дают ДОСТУП к материалу, а форму
# переходу задают эффекты и обычный эквалайзер:
#
#   * эхо и реверб на ОДНОМ слое звучат совсем не так, как на всём
#     миксе: эхо от голоса не размазывает барабаны, реверб на гармонии
#     не заливает бочку. Ради этого слои и разделяют;
#   * луп из одного слоя (обычно из барабанов) держит пульс, пока
#     остальное меняется. Луп на полном миксе так не умеет: он тащит
#     за собой мелодию и она начинает спорить с новой;
#   * а собственно СВЕДЕНИЕ — то есть разведение двух треков по
#     частотам — по-прежнему делает эквалайзер, а не стем-фейдеры.
#
# Почему последнее важно, и почему появился ST-09 «Соло-слой + EQ».
# Стем-фейдер и ручка эквалайзера решают РАЗНЫЕ задачи. Разделение
# никогда не бывает идеальным: в слое баса остаётся тело бочки, в
# барабанах — призвук баса, в гармонии — сибилянты голоса. Пока слой
# играет ОДИН, этих остатков не слышно — их не с чем сравнить. А вот
# когда двумя стем-фейдерами разводят две деки (у одной убрали бас, у
# другой добавили), остатки складываются с оригиналами, и получается
# та самая «мутная» середина, которой у честного EQ-обмена нет.
#
# Отсюда приём, который спрашивал диджей: слои используются ТОЛЬКО в
# режиме соло — один слой одной деки, — а разводятся деки обычным
# трёхполосным эквалайзером. Названия у производителей разные (в
# rekordbox это Part ISO и Active Part, в Serato и VirtualDJ — Stem
# solo/mute), общего документа с описанием самой техники найти не
# удалось: в руководствах описаны кнопки, а не приёмы. Поэтому здесь
# она собрана из причины, а не переписана из источника, — и причина
# выше проверяема на слух: сравните ST-03 (обмен басом по слоям) и
# ST-09 на одной и той же паре.


def _stem_crossfade(deck_out: str, deck_in: str, at_beat: float, span_beats: float,
                    order: tuple = ("drums", "bass", "other", "vocals"),
                    stagger_beats: float = 0.0) -> list[dict]:
    """Кроссфейдер ПО СЛОЯМ: каждый слой уходит у одной деки ровно тогда,
    когда приходит у другой.

    Зачем отдельная функция. Обычный кроссфейдер двигает деки целиком, и
    в середине хода обе играют полностью — там и живёт вся каша. Резкий
    обмен слоями (снять всё у одного, включить всё у другого в один
    удар) кашу убирает, но звучит склейкой: диджей слышит, что уходящий
    уже без баса и барабанов, а входящий появляется вдруг.
    Пересечение по слоям — это середина: в любой момент времени каждый
    слой звучит СУММАРНО на единицу, поэтому плотность не проваливается
    и не удваивается, а смена всё равно происходит слой за слоем.

    stagger_beats сдвигает слои друг относительно друга: сначала
    переходят барабаны, потом бас, потом гармония, последним голос. Так
    это делают руками, и так переход читается как решение, а не как
    случайность."""
    ev: list[dict] = []
    span = max(0.25, span_beats)
    for i, part in enumerate(order):
        start = at_beat + stagger_beats * i
        ev.append(_stem(start, deck_out, part, span, 1.0, 0.0, "ease_out"))
        ev.append(_stem(start, deck_in, part, span, 0.0, 1.0, "ease_in"))
    return ev


def _build_stem_crossfader(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Кроссфейдер по слоям — то, чем стем-дека отличается от обычной.

    Кроссфейдер стоит посередине и не двигается: громкость дек не
    меняется вообще. Меняется СОСТАВ: барабаны переходят первыми, за
    ними бас, потом гармония, последним голос. Каждая пара слоёв
    пересекается, поэтому провала нет, а суммирования двух ударных
    установок нет по построению."""
    span = float(p["span_beats"])
    stagger = float(p["stagger_bars"]) * 4
    return [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        # Кроссфейдер сразу посередине: работу делают слои, а не он.
        _ramp(0, "crossfade", source, 1, 0.0, 0.5),
        *_stems_at(0, target, {k: 0.0 for k in STEMS}, 0.25),
        *_stem_crossfade(source, target, 4.0, span, stagger_beats=stagger),
        # Уходящая дека к этому моменту уже молчит всеми слоями —
        # кроссфейдер доводится только чтобы освободить канал.
        _ramp(4.0 + stagger * 3 + span, "crossfade", source, 4, 0.5, 1.0),
    ]


def _build_stem_solo_eq(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Соло-слой + обычный частотный эквалайзер.

    Слои здесь работают ровно в одном режиме — соло. Входящий заходит
    ОДНИМ слоем (по умолчанию гармонией: она узнаётся, но не спорит с
    ритмом), всё остальное у него выключено. Сведение при этом делает
    обычный трёхполосный эквалайзер: низ уходящего уступает низу
    входящего, середина расходится, верх сходится. Стем-фейдерами тут не
    разводят НИЧЕГО — и именно поэтому середина остаётся чистой (см.
    комментарий к блоку выше).

    Когда весь обмен по частотам сделан, входящему возвращаются
    остальные слои — по одному, с шагом в такт."""
    solo = p.get("solo_stem") or "other"
    if isinstance(solo, (int, float)):          # ползунок из UI: 0..3
        solo = STEMS[int(_clamp(float(solo), 0, len(STEMS) - 1))]
    blend = float(p["blend_bars"]) * 4
    back = float(p["return_bars"]) * 4
    others = [s for s in STEMS if s != solo]
    ev = [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        # Входящий: соло одного слоя. Остальные — в ноль, и это
        # единственное, что делают стем-фейдеры за весь приём.
        *_stems_at(0, target, {k: (1.0 if k == solo else 0.0) for k in STEMS}, 0.5),
        _ramp(0, "crossfade", source, 4, 0.0, 0.5),
        # Низ входящего до обмена закрыт: соло-слой всё равно почти без
        # низа, а открытый низ на двух деках — это то самое, что
        # эквалайзер и должен развести.
        _ramp(0, "eq_low", target, 1, 0.0, 0.0),
        # --- собственно сведение: только эквалайзер ---
        _ramp(blend * 0.5, "eq_high", source, blend * 0.5, EQ_UNITY, EQ_UNITY * 0.4, "ease_out"),
        _ramp(blend * 0.5, "eq_mid", source, blend * 0.5, EQ_UNITY, EQ_UNITY * 0.5, "ease_out"),
        _ramp(blend, "eq_low", source, 4, EQ_UNITY, 0.0, "ease_out"),
        _ramp(blend, "eq_low", target, 4, 0.0, EQ_UNITY, "ease_in"),
        _ramp(blend, "crossfade", source, 8, 0.5, 1.0),
    ]
    # Слои возвращаются входящему по одному — по такту на каждый.
    for i, part in enumerate(others):
        ev.append(_stem(blend + back * (i + 1), target, part, 2.0, 0.0, 1.0, "ease_in"))
    return ev


def _build_stem_echo_out(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Эхо на ОДНОМ слое: уходящий сжимается до голоса (или до мелодии),
    и уходит именно этот слой — в эхо, поверх уже играющего нового.

    На полном миксе тот же посыл эха мажет барабаны и превращает конец
    фразы в грязь. На одном слое эхо слышно как приём: повторяется
    фраза, а не весь трек."""
    keep = p.get("keep_stem") or "vocals"
    if isinstance(keep, (int, float)):
        keep = STEMS[int(_clamp(float(keep), 0, len(STEMS) - 1))]
    strip = float(p["strip_bars"]) * 4
    tail = float(p["tail_beats"])
    ev = [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        _ramp(0, "crossfade", source, 4, 0.0, 0.45),
        # Входящий заходит ритм-секцией — верх свободен под эхо.
        *_stems_at(0, target, {"other": 0.0, "vocals": 0.0}, 0.5),
    ]
    # Уходящий сжимается до одного слоя.
    for part in STEMS:
        if part != keep:
            ev.append(_stem(0, source, part, strip, 1.0, 0.0, "ease_out"))
    ev += [
        # Посыл открывается В КОНЦЕ фразы: в эхо попадает последняя
        # фраза слоя, а не всё, что он играл до этого.
        _fx(strip, source, "echo", 0.5, 0.0, 0.9, "linear",
            time_beats=p["echo_beats"], feedback=p["feedback"]),
        _fx(strip + 0.5, source, "echo", tail, 0.9, 0.9),
        _stem(strip, source, keep, tail, 1.0, 0.0, "ease_out"),
        _cut(strip + tail, source, 0.45, 1.0),
        # Хвост эха ещё звенит, а новый уже разворачивается целиком.
        _stem(strip + tail, target, "other", 4, 0.0, 1.0, "ease_in"),
        _stem(strip + tail, target, "vocals", 4, 0.0, 1.0, "ease_in"),
    ]
    return ev


def _build_stem_loop_bed(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Луп из барабанов уходящего как подложка под новый трек.

    Обычный луп на полном миксе тащит за собой мелодию, и она спорит с
    входящим. Здесь в луп уходит трек, у которого оставлены ТОЛЬКО
    барабаны, — получается ритмическая подложка без гармонии, спорить
    нечему. Под неё входящий поднимается гармонией и басом, а на выходе
    из лупа возвращает себе барабаны."""
    bed = float(p["bed_bars"]) * 4
    beats = float(p["loop_beats"])
    keep = "drums"
    ev = [
        _discrete(0, "sync", target),
        # Уходящий сводится к барабанам и зацикливается.
        *[_stem(0, source, part, 2.0, 1.0, 0.0, "ease_out")
          for part in STEMS if part != keep],
        _discrete(2.0, "loop_activate", source, {"beats": beats}),
        _discrete(2.0, "play_from_cue", target),
        _ramp(2.0, "crossfade", source, 4, 0.0, 0.5),
        # Входящий поднимается ПОД барабанами: сначала гармония, тактом
        # позже бас, барабаны последними — иначе две установки разом.
        *_stems_at(2.0, target, {k: 0.0 for k in STEMS}, 0.25),
        _stem(4.0, target, "other", 8, 0.0, 1.0, "ease_in"),
        _stem(4.0, target, "vocals", 8, 0.0, 1.0, "ease_in"),
        _stem(bed * 0.5, target, "bass", 8, 0.0, 1.0, "ease_in"),
        # Выход из лупа — на границе такта, вместе с приходом барабанов.
        _discrete(bed, "loop_exit", source),
        _stem(bed, target, "drums", 1.0, 0.0, 1.0),
        _stem(bed, source, keep, 2.0, 1.0, 0.0, "ease_out"),
        _ramp(bed, "crossfade", source, 4, 0.5, 1.0),
    ]
    return ev


def _build_stem_reverb_wash(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Реверб-заливка по слоям: гармония уходящего уходит в комнату, а
    ритм-секция в это же время передаётся входящему.

    Разделение здесь решает старую проблему реверба на переходе: посыл
    с полного микса заливает и барабаны, и всё превращается в туман, в
    котором не слышно доли. Гармония в комнате, барабаны сухие — туман
    есть, а доля на месте."""
    wash = float(p["wash_bars"]) * 4
    span = float(p["swap_beats"])
    return [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        _ramp(0, "crossfade", source, 4, 0.0, 0.5),
        *_stems_at(0, target, {k: 0.0 for k in STEMS}, 0.25),
        # Гармония и голос уходящего — в комнату, и оттуда уже не
        # возвращаются: посыл открыт, слой гаснет, хвост звенит.
        _fx(0, source, "reverb", 1.0, 0.0, 0.9, "ease_in", decay_beats=p["decay_beats"]),
        _fx(1.0, source, "reverb", wash, 0.9, 0.9),
        _stem(0, source, "other", wash, 1.0, 0.0, "ease_out"),
        _stem(0, source, "vocals", wash, 1.0, 0.0, "ease_out"),
        # Ритм-секция передаётся честным пересечением слоёв.
        *_stem_crossfade(source, target, wash * 0.5, span, order=("drums", "bass")),
        # Верх входящего приходит последним — когда комната уже стихла.
        _stem(wash, target, "other", 8, 0.0, 1.0, "ease_in"),
        _stem(wash, target, "vocals", 8, 0.0, 1.0, "ease_in"),
        _fx(wash, source, "reverb", 4, 0.9, 0.0, "ease_out"),
        _ramp(wash, "crossfade", source, 8, 0.5, 1.0),
    ]


def _build_stem_eq_hybrid(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """STEMS EQ: бас меняется слоем, всё остальное — эквалайзером.

    Самый практичный из гибридов и, вероятно, самый частый приём на
    стем-пультах вообще. Разделение выигрывает ровно в одном месте — в
    басу: ручка низа забирает вместе с басом ещё и бочку, и низ пэда, а
    слой забирает бас. Всё, что выше, слои делят хуже, чем эквалайзер, —
    и туда их не пускают."""
    hold = float(p["hold_bars"]) * 4
    swap = float(p["swap_beats"])
    return [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        # Единственное, что делают слои: у входящего выключен бас.
        *_stems_at(0, target, {"bass": 0.0}, 0.5),
        _ramp(0, "crossfade", source, 4, 0.0, 0.45),
        # Середина и верх разводятся эквалайзером, как в обычном сведении.
        _ramp(0, "eq_mid", target, 4, EQ_UNITY * 0.35, EQ_UNITY * 0.35),
        _ramp(hold * 0.5, "eq_mid", source, hold * 0.5, EQ_UNITY, EQ_UNITY * 0.45, "ease_out"),
        _ramp(hold * 0.5, "eq_mid", target, hold * 0.5, EQ_UNITY * 0.35, EQ_UNITY, "ease_in"),
        # Бас — слоем, ровно на границе такта и обеими деками сразу.
        _stem(hold, source, "bass", swap, 1.0, 0.0, "ease_out"),
        _stem(hold, target, "bass", swap, 0.0, 1.0, "ease_in"),
        _ramp(hold, "eq_high", source, 4, EQ_UNITY, EQ_UNITY * 0.3, "ease_out"),
        _ramp(hold, "crossfade", source, 8, 0.45, 1.0),
    ]


_register(Technique(
    id="ST-09", name="Соло-слой + обычный EQ", category="stems", difficulty=3,
    description=(
        "Слои работают ТОЛЬКО в режиме соло: входящий заходит одним слоем "
        "(по умолчанию гармонией), а разводит деки обычный трёхполосный "
        "эквалайзер. Стем-фейдерами не разводят ничего — поэтому середина "
        "остаётся чистой: остатки разделения слышны, только когда двумя "
        "стем-фейдерами сводят две деки сразу."),
    bpm_delta_max=6, key_rule="compatible", energy_direction="any",
    requires_stems=True, requires_decks=2,
    params=[
        TechniqueParam("solo_stem", "Какой слой в соло (0 барабаны, 1 бас, 2 гармония, 3 голос)", 2, 0, 3, ""),
        TechniqueParam("blend_bars", "Длина сведения", 8, 2, 32, "тактов"),
        TechniqueParam("return_bars", "Шаг возврата слоёв", 1, 1, 8, "тактов"),
    ],
    steps=["У входящего включён ОДИН слой, остальные в ноль.",
           "Низ входящего закрыт эквалайзером.",
           "Верх и середина уходящего уступают — ручками, не слоями.",
           "Обмен низом эквалайзером на границе фразы.",
           "Слои входящему возвращаются по одному, по такту."],
), _build_stem_solo_eq)

_register(Technique(
    id="ST-10", name="Кроссфейдер по слоям", category="stems", difficulty=2,
    description=(
        "Громкость дек не меняется вообще: кроссфейдер стоит посередине, а "
        "переходят слои — сначала барабаны, за ними бас, потом гармония, "
        "последним голос. Каждая пара слоёв пересекается, поэтому нет ни "
        "провала, ни удвоения ритм-секции."),
    bpm_delta_max=4, key_rule="compatible", energy_direction="any",
    requires_stems=True, requires_decks=2,
    params=[
        TechniqueParam("span_beats", "Пересечение слоя", 8, 1, 32, "долей"),
        TechniqueParam("stagger_bars", "Сдвиг между слоями", 2, 0, 8, "тактов"),
    ],
    steps=["Кроссфейдер посередине, у входящего все слои в нуле.",
           "Барабаны переходят первыми — пересечением, не резом.",
           "Через N тактов бас, потом гармония, последним голос.",
           "Кроссфейдер доводится, когда у уходящего уже нечему звучать."],
), _build_stem_crossfader)

_register(Technique(
    id="SF-01", name="Эхо на слое", category="stems", difficulty=3,
    description=(
        "Уходящий сжимается до одного слоя (голос или мелодия), и в эхо "
        "уходит именно он — поверх уже играющей ритм-секции нового. Эхо с "
        "полного микса мажет барабаны; эхо с одного слоя слышно как приём."),
    bpm_delta_max=6, key_rule="compatible", energy_direction="any",
    requires_stems=True, requires_decks=2,
    params=[
        TechniqueParam("keep_stem", "Какой слой уводить в эхо (0 барабаны, 1 бас, 2 гармония, 3 голос)", 3, 0, 3, ""),
        TechniqueParam("strip_bars", "За сколько сжать уходящий", 4, 1, 16, "тактов"),
        TechniqueParam("tail_beats", "Хвост в эхе", 8, 2, 32, "долей"),
        TechniqueParam("echo_beats", "Время эха", 0.75, 0.125, 4, "доли"),
        TechniqueParam("feedback", "Обратная связь", 0.55, 0.0, 0.9, ""),
    ],
    steps=["Входящий заходит ритм-секцией, верх у него выключен.",
           "У уходящего гаснут все слои, кроме выбранного.",
           "На границе фразы открывается посыл эха.",
           "Слой гаснет, хвост звенит поверх нового трека.",
           "Новому возвращаются гармония и голос."],
), _build_stem_echo_out)

_register(Technique(
    id="SF-02", name="Луп из барабанов", category="stems", difficulty=3,
    description=(
        "У уходящего остаются одни барабаны и уходят в луп — получается "
        "ритмическая подложка без гармонии, с которой новому треку нечем "
        "спорить. Под неё входящий поднимается гармонией и басом, а на "
        "выходе из лупа забирает барабаны себе."),
    bpm_delta_max=4, key_rule="any", energy_direction="any",
    requires_stems=True, requires_decks=2,
    params=[
        TechniqueParam("bed_bars", "Длина подложки", 8, 2, 32, "тактов"),
        TechniqueParam("loop_beats", "Длина лупа", 4, 1, 16, "долей"),
    ],
    steps=["У уходящего гаснет всё, кроме барабанов.",
           "Барабаны уходят в луп — это подложка.",
           "Входящий поднимается гармонией, тактом позже басом.",
           "Выход из лупа на границе такта — барабаны у нового."],
), _build_stem_loop_bed)

_register(Technique(
    id="SF-03", name="Реверб-заливка по слоям", category="stems", difficulty=4,
    description=(
        "Гармония и голос уходящего уходят в комнату, ритм-секция в это же "
        "время передаётся входящему пересечением слоёв. Реверб с полного "
        "микса заливает барабаны и съедает долю; здесь барабаны сухие, а "
        "туман есть."),
    bpm_delta_max=6, key_rule="compatible", energy_direction="down",
    requires_stems=True, requires_decks=2,
    params=[
        TechniqueParam("wash_bars", "Длина заливки", 8, 2, 32, "тактов"),
        TechniqueParam("swap_beats", "Пересечение ритм-секции", 8, 1, 32, "долей"),
        TechniqueParam("decay_beats", "Длина хвоста комнаты", 8, 1, 32, "долей"),
    ],
    steps=["Входящий стартует со всеми слоями в нуле.",
           "Посыл реверба открывается на гармонии уходящего.",
           "Барабаны и бас переходят пересечением слоёв.",
           "Верх входящего приходит, когда комната стихла."],
), _build_stem_reverb_wash)

_register(Technique(
    id="SE-01", name="STEMS EQ: бас слоем, верх ручками", category="stems", difficulty=2,
    description=(
        "Гибрид, который на стем-пультах используют чаще всего: слоем "
        "меняется только бас (ручка низа забирает вместе с ним бочку и низ "
        "пэда, а слой — ровно бас), всё остальное разводится обычным "
        "эквалайзером, который делит верх и середину лучше, чем разделение."),
    bpm_delta_max=4, key_rule="compatible", energy_direction="any",
    requires_stems=True, requires_decks=2,
    params=[
        TechniqueParam("hold_bars", "До обмена басом", 8, 2, 32, "тактов"),
        TechniqueParam("swap_beats", "Пересечение баса", 2, 0.5, 16, "долей"),
    ],
    steps=["У входящего выключен только бас — больше слои не трогаем.",
           "Середина расходится эквалайзером.",
           "Обмен басом слоем, на границе такта.",
           "Верх уходящего убирается, кроссфейдер доводится."],
), _build_stem_eq_hybrid)


# ---------------------------------------------------------------------
# Слой как отдельный канал: свой эквалайзер, своя фаза, свой питч,
# свой посыл на эффект
# ---------------------------------------------------------------------
#
# До этого места слой умел ровно одно — звучать или не звучать. Этого
# достаточно для обмена барабанами, но не для того, ради чего стемы
# вообще нужны: трек перестаёт быть пластинкой и становится набором
# деталей, а с деталями работают как в студии.
#
# Что добавляют события ниже и почему каждое из них НЕЛЬЗЯ заменить
# ручкой на канале:
#
#   stem_eq   — срез полосы у ОДНОГО слоя. Главный случай — две бочки
#     разом. Складывать их нельзя не потому, что «громко», а потому что
#     две бочки в разной фазе гасят друг друга: вместе они звучат ТИШЕ
#     и пустее, чем каждая порознь. Ручка низа на канале срежет низ у
#     всего трека; stem_eq срезает его у одной бочки, оставляя щелчок,
#     и обе установки складываются без потери низа.
#
#   stem_phase — то же лекарство с другой стороны: перевернуть фазу
#     слоя. Иногда этого достаточно, и резать ничего не нужно.
#
#   stem_pitch — сдвиг одного слоя на полутона. Мелодия, поднятая на
#     октаву, перестаёт спорить с чужим басом (они больше не в одном
#     регистре) и работает как атмосферный верх — так пара «не в
#     тональности» становится сводимой.
#
#   stem_fx   — посыл на эффект С ОДНОГО СЛОЯ. Эхо с полного микса
#     размазывает барабаны, и конец фразы превращается в грязь. Посыл с
#     вокального слоя оставляет барабаны сухими: голос растворяется, а
#     доля бьёт до последнего такта.
#
#   sidechain — чужая бочка продавливает эту деку. Это то, что склеивает
#     дабл-дроп: барабаны одного трека и мелодия другого звучат как две
#     записи ровно до тех пор, пока мелодия не начинает приседать на
#     каждый удар чужой бочки.
#
# Про уровни. Когда одновременно открыты четыре слоя с разных дек,
# сумма легко уходит в клип. Поэтому в приёмах ниже слои включаются НЕ
# на единицу, а на 0.55-0.7 (это примерно -3..-5 дБ) — не «на всякий
# случай», а потому что четыре источника вместо одного дают ровно
# такой прирост.

STEM_BANDS = ("low", "mid", "high")


def _stem_eq(beat_offset: float, deck: str, stem: str, band: str,
             duration_beats: float, value_from: float, value_to: float,
             curve: str = "linear") -> dict:
    """Полосовая ручка ОДНОГО слоя. 1.0 — полоса на месте, 0.0 — убрана."""
    if band not in STEM_BANDS:
        raise ValueError(f"нет такой полосы: {band}")
    return {"beat_offset": beat_offset, "action": "stem_eq", "deck": deck,
            "kind": "ramp", "duration_beats": max(0.01, duration_beats),
            "value_from": value_from, "value_to": value_to, "curve": curve,
            "params": {"stem": stem, "band": band}}


def _stem_phase(beat_offset: float, deck: str, stem: str, invert: bool = True) -> dict:
    """Инверсия фазы слоя с этого момента и до конца приёма."""
    return {"beat_offset": beat_offset, "action": "stem_phase", "deck": deck,
            "kind": "discrete", "duration_beats": 0.0,
            "value_from": 0.0, "value_to": 1.0 if invert else 0.0,
            "params": {"stem": stem}}


def _stem_pitch(beat_offset: float, deck: str, stem: str, semitones: float,
                duration_beats: float) -> dict:
    """Сдвиг одного слоя на полутона (+12 — октава вверх)."""
    return {"beat_offset": beat_offset, "action": "stem_pitch", "deck": deck,
            "kind": "ramp", "duration_beats": max(0.01, duration_beats),
            "value_from": semitones, "value_to": semitones, "curve": "linear",
            "params": {"stem": stem}}


def _stem_fx(beat_offset: float, deck: str, stem: str, unit: str,
             duration_beats: float, value_from: float, value_to: float,
             curve: str = "linear", **params) -> dict:
    """Посыл на эффект с одного слоя. unit: echo | reverb."""
    if unit not in ("echo", "reverb"):
        raise ValueError(f"со слоя можно послать только echo или reverb, не {unit}")
    pr = {"stem": stem, "unit": unit}
    pr.update({k: v for k, v in params.items() if v is not None})
    return {"beat_offset": beat_offset, "action": "stem_fx", "deck": deck,
            "kind": "ramp", "duration_beats": max(0.01, duration_beats),
            "value_from": value_from, "value_to": value_to, "curve": curve,
            "params": pr}


def _sidechain(beat_offset: float, deck: str, duration_beats: float,
               depth: float = 0.6, from_deck: str | None = None) -> dict:
    """Эту деку продавливает бочка ДРУГОЙ деки. depth 0..1."""
    return {"beat_offset": beat_offset, "action": "sidechain", "deck": deck,
            "kind": "ramp", "duration_beats": max(0.01, duration_beats),
            "value_from": depth, "value_to": depth, "curve": "linear",
            "params": {"from_deck": from_deck} if from_deck else {}}


def _kick_layer_fix(deck: str, beat: float, cut_bars: float = 32.0) -> list[dict]:
    """Две бочки одновременно: у ЭТОЙ срезаем низ, оставляем щелчок.

    Без этого две бочки гасят друг друга по фазе, и дабл-дроп звучит
    тише и пустее, чем каждый трек порознь, — самая частая и самая
    незаметная ошибка при наслоении."""
    return [
        _stem_eq(beat, deck, "drums", "low", cut_bars * 4, 1.0, 0.0),
        _stem_eq(beat, deck, "drums", "high", cut_bars * 4, 1.0, 1.25),
    ]


def _build_stem_collage(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Сборка из трёх дек: барабаны от одного, бас от второго, гармония
    и голос от третьего.

    Это уже не переход, а живой ремикс: одновременно открыты слои трёх
    треков. Чтобы это не превратилось в кашу, работают три правила и все
    три здесь выполнены:
      * каждый слой берётся ровно с ОДНОЙ деки — складывать нечего;
      * у гармонии срезан низ, чтобы место внизу осталось басу;
      * уровни слоёв подрезаны (0.6-0.7): три источника вместо одного.

    Тональность обязательна: бас одного трека и мелодия другого
    работают вместе только в совместимых тональностях (±0/±1 по кругу
    Камелота) — иначе это не коллаж, а фальшь."""
    third = p.get("third_deck") or "C"
    hold = float(p["hold_bars"]) * 4
    lvl = float(p["layer_level"])
    ev = [
        _discrete(0, "sync", target),
        _discrete(0, "sync", third),
        _discrete(0, "play_from_cue", target),
        _discrete(0, "play_from_cue", third),
        _ramp(0, "crossfade", source, 2, 0.0, 0.5),
        # source — только барабаны
        *_stems_at(0, source, {"bass": 0.0, "other": 0.0, "vocals": 0.0}, 0.5),
        # target — только бас
        *_stems_at(0, target, {"drums": 0.0, "other": 0.0, "vocals": 0.0}, 0.5),
        _stem(0, target, "bass", 2.0, 0.0, lvl, "ease_in"),
        # third — гармония и голос
        *_stems_at(0, third, {"drums": 0.0, "bass": 0.0}, 0.5),
        _stem(0, third, "other", 2.0, 0.0, lvl, "ease_in"),
        _stem(0, third, "vocals", 2.0, 0.0, lvl, "ease_in"),
        # место внизу — басу: у гармонии низ срезан на всё время коллажа
        _stem_eq(0, third, "other", "low", hold, 1.0, 0.15),
        # и наоборот: бас не лезет в середину
        _stem_eq(0, target, "bass", "mid", hold, 1.0, 0.4),
        # мелодия приседает на бочку — это и склеивает три записи
        _sidechain(0, third, hold, depth=float(p["pump"]), from_deck=source),
        _sidechain(0, target, hold, depth=float(p["pump"]) * 0.6, from_deck=source),
    ]
    # выход: коллаж сворачивается в трек target
    ev += [
        _stem(hold, source, "drums", 8, 1.0, 0.0, "ease_out"),
        _stem(hold, third, "other", 8, lvl, 0.0, "ease_out"),
        _stem(hold, third, "vocals", 8, lvl, 0.0, "ease_out"),
        _stem(hold, target, "drums", 8, 0.0, 1.0, "ease_in"),
        _stem(hold, target, "bass", 8, lvl, 1.0, "ease_in"),
        _stem(hold, target, "other", 8, 0.0, 1.0, "ease_in"),
        _ramp(hold, "crossfade", source, 8, 0.5, 1.0),
    ]
    return ev


def _build_sidechain_double_drop(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Дабл-дроп, склеенный сайдчейном.

    Барабаны берутся у уходящего, бас и мелодия у входящего — до сюда
    это ST-04. Разница в двух вещах, и обе слышны:
      * мелодия и бас входящего приседают на каждый удар бочки
        уходящего. Два трека перестают быть двумя записями и начинают
        дышать в одном ритме — это тот самый «насос»;
      * если оба барабанных слоя всё же открыты, у входящего срезан низ
        бочки: иначе две бочки гасят друг друга по фазе."""
    hold = float(p["hold_bars"]) * 4
    pump = float(p["pump"])
    ev = [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        _ramp(0, "crossfade", source, 2, 0.0, 0.5),
        # ритм у уходящего, гармония и бас у входящего
        *_stems_at(0, target, {"drums": 0.0}, 0.5),
        *_stems_at(0, source, {"other": 0.0, "vocals": 0.0}, 0.5),
        _stem(0, source, "bass", 2.0, 1.0, 0.0, "ease_out"),
        # мелодия и бас входящего дышат чужой бочкой
        _sidechain(0, target, hold, depth=pump, from_deck=source),
    ]
    if float(p["layer_kicks"]) > 0.5:
        # обе установки разом — только с срезанным низом у одной
        ev.append(_stem(0, target, "drums", 4, 0.0, 0.7, "ease_in"))
        ev += _kick_layer_fix(target, 0, cut_bars=p["hold_bars"])
    ev += [
        _stem(hold, target, "drums", 4, (0.7 if float(p["layer_kicks"]) > 0.5 else 0.0), 1.0),
        _stem_eq(hold, target, "drums", "low", 4, 0.0, 1.0),
        _stem(hold, source, "drums", 4, 1.0, 0.0, "ease_out"),
        _ramp(hold, "crossfade", source, 8, 0.5, 1.0),
    ]
    return ev


def _build_built_breakdown(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Брейкдаун, собранный руками: у уходящего в брейке гаснут
    барабаны, а грув держит перкуссия входящего.

    Зачем. Короткий брейк — это дыра в энергии: атмосфера есть, а
    танцевать не подо что. Здесь атмосфера остаётся у уходящего
    (гармония и голос играют), а пульс приходит от входящего — одними
    барабанами, с срезанным низом, чтобы не было второй бочки под ещё
    не начавшийся дроп. К концу брейка входящий добирает бас, и его
    собственный дроп приходит уже подготовленным."""
    brk = float(p["break_bars"]) * 4
    return [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        _ramp(0, "crossfade", source, 4, 0.0, 0.4),
        # уходящий в брейке: без барабанов и без баса
        _stem(0, source, "drums", 4, 1.0, 0.0, "ease_out"),
        _stem(0, source, "bass", 4, 1.0, 0.0, "ease_out"),
        # входящий — только барабаны, и только верх: держим грув, а не бочку
        *_stems_at(0, target, {"bass": 0.0, "other": 0.0, "vocals": 0.0}, 0.5),
        _stem(0, target, "drums", 4, 0.0, float(p["perc_level"]), "ease_in"),
        _stem_eq(0, target, "drums", "low", brk, 1.0, 0.1),
        # к концу брейка низ возвращается, и входящий встаёт на ноги
        _stem_eq(brk - 8, target, "drums", "low", 8, 0.1, 1.0, "ease_in"),
        _stem(brk - 8, target, "drums", 8, float(p["perc_level"]), 1.0, "ease_in"),
        _stem(brk - 4, target, "bass", 4, 0.0, 1.0, "ease_in"),
        _stem(brk, target, "other", 4, 0.0, 1.0, "ease_in"),
        _stem(brk, target, "vocals", 4, 0.0, 1.0, "ease_in"),
        _stem(brk, source, "other", 8, 1.0, 0.0, "ease_out"),
        _stem(brk, source, "vocals", 8, 1.0, 0.0, "ease_out"),
        _ramp(brk, "crossfade", source, 8, 0.4, 1.0),
    ]


def _build_vocal_echo_dry_beat(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Вокал растворяется, барабаны бьют сухими.

    Отличие от SF-01 (там уходящий сжимался до одного слоя) в том, что
    здесь ВСЁ продолжает играть. Посыл открывается только на вокальном
    слое — барабаны и бас его не видят вовсе. Ухо слышит студийный
    приём: голос уходит в огромную комнату, а доля остаётся сухой и
    чёткой до последнего такта. На полном миксе так не бывает: посыл
    забирает барабаны вместе с голосом."""
    lead = float(p["lead_bars"]) * 4
    tail = float(p["tail_beats"])
    unit = "reverb" if float(p["use_reverb"]) > 0.5 else "echo"
    kw = ({"decay_beats": p["size_beats"]} if unit == "reverb"
          else {"delay_beats": p["size_beats"], "feedback": 0.6})
    return [
        _discrete(0, "sync", target),
        # посыл ТОЛЬКО с вокала — за 16 тактов до конца
        _stem_fx(0, source, "vocals", unit, 4, 0.0, 0.95, "ease_in", **kw),
        _stem_fx(4, source, "vocals", unit, lead, 0.95, 0.95, **kw),
        # сам вокальный слой уходит, хвост остаётся в комнате
        _stem(lead * 0.5, source, "vocals", lead * 0.5, 1.0, 0.0, "ease_out"),
        # барабаны и бас — сухие и на месте до конца
        _discrete(lead, "play_from_cue", target),
        *_stems_at(lead, target, {"other": 0.0, "vocals": 0.0}, 0.5),
        _ramp(lead, "crossfade", source, 8, 0.0, 0.5),
        _stem(lead, source, "other", 8, 1.0, 0.0, "ease_out"),
        _stem(lead + 8, source, "drums", 8, 1.0, 0.0, "ease_out"),
        _stem(lead + 8, source, "bass", 8, 1.0, 0.0, "ease_out"),
        _stem(lead + 8, target, "other", 8, 0.0, 1.0, "ease_in"),
        _stem(lead + 8, target, "vocals", 8, 0.0, 1.0, "ease_in"),
        _stem_fx(lead + tail, source, "vocals", unit, 4, 0.95, 0.0, "ease_out", **kw),
        _ramp(lead + 8, "crossfade", source, 8, 0.5, 1.0),
    ]


def _build_octave_melody(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Мелодия входящего поднимается на октаву — и пара «не в
    тональности» становится сводимой.

    Смысл не в том, чтобы подогнать тональность (октава её не меняет), а
    в том, чтобы увести мелодию из регистра, где она спорит. Поднятая на
    +12 мелодия перестаёт пересекаться с басом уходящего и читается как
    атмосферный верх — колокольчики, «звоны», подклад. Опущенная на -12
    работает наоборот: превращается в саб-гармонику под чужую бочку.

    Это честный обходной путь, а не замена гармоничной пары: если
    тональности совместимы (±0/±1), приём не нужен."""
    semis = float(p["semitones"])
    hold = float(p["hold_bars"]) * 4
    return [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        _ramp(0, "crossfade", source, 4, 0.0, 0.45),
        # входящий заходит ОДНОЙ мелодией, сдвинутой на октаву
        *_stems_at(0, target, {"drums": 0.0, "bass": 0.0, "vocals": 0.0}, 0.5),
        _stem(0, target, "other", 4, 0.0, float(p["layer_level"]), "ease_in"),
        _stem_pitch(0, target, "other", semis, hold),
        # низ у сдвинутой мелодии не нужен: место занято чужим басом
        _stem_eq(0, target, "other", "low", hold, 1.0, 0.1),
        # к концу сдвиг снимается, и трек входит уже собой
        _stem(hold, target, "other", 8, float(p["layer_level"]), 1.0, "ease_in"),
        _stem_eq(hold, target, "other", "low", 8, 0.1, 1.0, "ease_in"),
        _stem(hold, target, "drums", 4, 0.0, 1.0, "ease_in"),
        _stem(hold, target, "bass", 4, 0.0, 1.0, "ease_in"),
        _stem(hold, target, "vocals", 8, 0.0, 1.0, "ease_in"),
        _stem(hold, source, "drums", 8, 1.0, 0.0, "ease_out"),
        _stem(hold, source, "bass", 8, 1.0, 0.0, "ease_out"),
        _stem(hold, source, "other", 8, 1.0, 0.0, "ease_out"),
        _ramp(hold, "crossfade", source, 8, 0.45, 1.0),
    ]


def _build_piecewise_intro(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Ввод по частям: перкуссия — бас — барабаны, с шагом в фразу.

    Мозгу нужно время. Рывком новый трек воспринимается как смена
    пластинки; по частям — как развитие того же самого. Порядок именно
    такой: сначала приходит верх ударных (он ни с чем не спорит), потом
    меняется бас (главный носитель тональности), последними — барабаны
    (после них трек уже новый). Каждая замена происходит на границе
    фразы, поэтому ни одна из них не слышна как событие."""
    step = float(p["step_bars"]) * 4
    return [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        _ramp(0, "crossfade", source, 4, 0.0, 0.5),
        # 1) только верх ударных входящего, зациклен как хэт-подложка
        *_stems_at(0, target, {"bass": 0.0, "other": 0.0, "vocals": 0.0}, 0.5),
        _stem(0, target, "drums", 4, 0.0, float(p["perc_level"]), "ease_in"),
        _stem_eq(0, target, "drums", "low", step * 2, 1.0, 0.0),
        _stem_eq(0, target, "drums", "mid", step * 2, 1.0, 0.35),
        # 2) через фразу — бас меняется целиком
        _stem(step, source, "bass", 4, 1.0, 0.0, "ease_out"),
        _stem(step, target, "bass", 4, 0.0, 1.0, "ease_in"),
        # 3) ещё через фразу — барабаны
        _stem_eq(step * 2, target, "drums", "low", 4, 0.0, 1.0, "ease_in"),
        _stem_eq(step * 2, target, "drums", "mid", 4, 0.35, 1.0, "ease_in"),
        _stem(step * 2, target, "drums", 4, float(p["perc_level"]), 1.0, "ease_in"),
        _stem(step * 2, source, "drums", 4, 1.0, 0.0, "ease_out"),
        # 4) последней приходит гармония — трек стал новым
        _stem(step * 3, target, "other", 8, 0.0, 1.0, "ease_in"),
        _stem(step * 3, target, "vocals", 8, 0.0, 1.0, "ease_in"),
        _stem(step * 3, source, "other", 8, 1.0, 0.0, "ease_out"),
        _stem(step * 3, source, "vocals", 8, 1.0, 0.0, "ease_out"),
        _ramp(step * 3, "crossfade", source, 8, 0.5, 1.0),
    ]


_register(Technique(
    id="ST-11", name="Коллаж из трёх дек", category="stems", difficulty=5,
    description=(
        "Барабаны с одной деки, бас со второй, гармония и голос с третьей — "
        "живой ремикс вместо перехода. Каждый слой берётся ровно с одной "
        "деки, у гармонии срезан низ (место басу), уровни подрезаны, а "
        "верхние деки приседают на бочку нижней. Тональности обязаны быть "
        "совместимы: бас одного и мелодия другого работают только в ±0/±1."),
    bpm_delta_max=3, key_rule="compatible", energy_direction="any",
    requires_stems=True, requires_decks=3,
    params=[
        TechniqueParam("hold_bars", "Длина коллажа", 16, 4, 64, "тактов"),
        TechniqueParam("layer_level", "Уровень чужих слоёв", 0.65, 0.3, 1.0, ""),
        TechniqueParam("pump", "Глубина насоса", 0.5, 0.0, 0.9, ""),
    ],
    steps=["Дека A — только барабаны.", "Дека B — только бас, середина подрезана.",
           "Дека C — гармония и голос, низ срезан.",
           "B и C приседают на бочку A.",
           "Коллаж сворачивается в трек B."],
), _build_stem_collage)

_register(Technique(
    id="ST-12", name="Дабл-дроп с сайдчейном", category="stems", difficulty=4,
    description=(
        "Барабаны у уходящего, бас и мелодия у входящего — и мелодия "
        "приседает на каждый удар чужой бочки. Насос связывает две записи "
        "ритмически; без него дабл-дроп слышен как два трека разом. Если "
        "открыть обе установки, у входящей бочки срезается низ: иначе две "
        "бочки гасят друг друга по фазе и звучат тише, чем порознь."),
    bpm_delta_max=2, key_rule="compatible", energy_direction="up",
    requires_stems=True, requires_decks=2,
    params=[
        TechniqueParam("hold_bars", "Длина совмещения", 8, 2, 32, "тактов"),
        TechniqueParam("pump", "Глубина насоса", 0.6, 0.0, 0.9, ""),
        TechniqueParam("layer_kicks", "Обе установки разом (0/1)", 0, 0, 1, ""),
    ],
    steps=["Входящий заходит басом и мелодией, барабаны выключены.",
           "Сайдчейн: входящий приседает на бочку уходящего.",
           "При наслоении бочек — низ у входящей срезан.",
           "На границе фразы барабаны переходят входящему."],
), _build_sidechain_double_drop)

_register(Technique(
    id="ST-13", name="Собранный брейкдаун", category="stems", difficulty=3,
    description=(
        "Короткий брейк растягивается вручную: у уходящего в брейке гаснут "
        "барабаны и бас, а грув держит перкуссия входящего — с срезанным "
        "низом, чтобы не было второй бочки. Атмосфера остаётся у старого "
        "трека, пульс приходит от нового, дыры в энергии нет."),
    bpm_delta_max=4, key_rule="compatible", energy_direction="down",
    requires_stems=True, requires_decks=2,
    params=[
        TechniqueParam("break_bars", "Длина брейка", 16, 4, 64, "тактов"),
        TechniqueParam("perc_level", "Уровень перкуссии", 0.6, 0.2, 1.0, ""),
    ],
    steps=["Уходящий входит в брейк — снимаем барабаны и бас.",
           "Входящий даёт барабаны с срезанным низом.",
           "За 2 фразы до конца низ возвращается.",
           "Входящий добирает бас и гармонию — это его дроп."],
), _build_built_breakdown)

_register(Technique(
    id="SF-04", name="Эхо-вокал без потери бита", category="stems", difficulty=3,
    description=(
        "Посыл на эхо или огромный реверб открывается ТОЛЬКО на вокальном "
        "слое: голос растворяется в воздухе, а барабаны и бас продолжают "
        "бить сухими до последнего такта. На полном миксе тот же посыл "
        "забирает барабаны вместе с голосом и превращает конец фразы в грязь."),
    bpm_delta_max=6, key_rule="any", energy_direction="any",
    requires_stems=True, requires_decks=2, needs_vocals=True,
    params=[
        TechniqueParam("lead_bars", "За сколько до конца открыть посыл", 16, 4, 32, "тактов"),
        TechniqueParam("tail_beats", "Хвост", 16, 4, 64, "долей"),
        TechniqueParam("size_beats", "Размер эффекта", 6, 0.5, 32, "долей"),
        TechniqueParam("use_reverb", "Реверб вместо эха (0/1)", 1, 0, 1, ""),
    ],
    steps=["Посыл открывается на вокальном слое, барабаны сухие.",
           "Вокальный слой гаснет, хвост живёт в комнате.",
           "Новый трек заходит ритм-секцией под хвост.",
           "Уходящий убирается по слоям, посыл закрывается."],
), _build_vocal_echo_dry_beat)

_register(Technique(
    id="ST-14", name="Мелодия на октаву", category="stems", difficulty=4,
    description=(
        "Мелодия входящего сдвигается на ±12 полутонов и заходит поверх "
        "чужого баса. Октава не меняет тональность — она уводит мелодию из "
        "регистра, где та спорит: вверх получается атмосферный верх, вниз — "
        "саб-гармоника. Обходной путь для пар, которые иначе не свести; при "
        "совместимых тональностях приём не нужен."),
    bpm_delta_max=4, key_rule="clash", energy_direction="any",
    requires_stems=True, requires_decks=2,
    params=[
        TechniqueParam("semitones", "Сдвиг мелодии", 12, -12, 12, "полутонов"),
        TechniqueParam("hold_bars", "Сколько держать сдвиг", 8, 2, 32, "тактов"),
        TechniqueParam("layer_level", "Уровень мелодии", 0.7, 0.3, 1.0, ""),
    ],
    steps=["Входящий заходит одной мелодией, сдвинутой на октаву.",
           "У сдвинутой мелодии срезан низ — там чужой бас.",
           "На границе фразы сдвиг снимается.",
           "Входящий добирает барабаны, бас и голос."],
), _build_octave_melody)

_register(Technique(
    id="SF-05", name="Ввод по частям", category="stems", difficulty=3,
    description=(
        "Новый трек вводится тремя шагами по фразе: сначала верх его "
        "ударных как подложка, потом целиком меняется бас, потом барабаны, "
        "последней приходит гармония. Рывком трек читается как смена "
        "пластинки, по частям — как развитие того же самого."),
    bpm_delta_max=3, key_rule="compatible", energy_direction="any",
    requires_stems=True, requires_decks=2,
    params=[
        TechniqueParam("step_bars", "Шаг ввода", 8, 2, 32, "тактов"),
        TechniqueParam("perc_level", "Уровень перкуссии", 0.6, 0.2, 1.0, ""),
    ],
    steps=["Верх ударных входящего — подложка, низ срезан.",
           "Через фразу меняется бас.",
           "Ещё через фразу — барабаны, низ возвращается.",
           "Последней приходит гармония."],
), _build_piecewise_intro)





_register(Technique(
    id="TT-09", name="Луп в бэкспин", category="turntable", difficulty=4,
    description="Луп делится пополам до предела — 4 доли, 2, 1, ½, ¼ — пока трек не свернётся в гудящую точку, и на пределе разгон разрешается бэкспином. То, что диджеи делают руками чаще всего.",
    bpm_delta_max=None, key_rule="any", energy_direction="up", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("start_beats", "Стартовая длина лупа", 4, 1, 16, "долей"),
        TechniqueParam("min_beats", "До какой длины делить", 0.25, 0.0625, 2, "долей"),
        TechniqueParam("step_beats", "Сколько держать ступень", 2, 0.5, 8, "долей"),
        TechniqueParam("spin_beats", "Длительность спина", 1, 0.25, 4, "долей"),
        TechniqueParam("spin_rate", "Сила броска", 9, 3, 16, "крат"),
    ],
    steps=["Взять луп на последней фразе уходящего.",
           "Делить пополам, держа каждую ступень одинаковое время.",
           "На пределе — бэкспин.",
           "Новый трек с «раза» следующего такта."],
), _build_loop_choke_spin)



def _build_acappella_in(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Входящий заходит ОДНОЙ акапеллой — приём на понижение энергии.

    Зеркало ST-01, и путать их нельзя: там голос УХОДЯЩЕГО поёт над
    инструменталом нового, здесь наоборот — голос НОВОГО появляется над
    ещё играющим старым треком. Разница не косметическая, а
    драматургическая: ST-01 провожает трек, а этот приём объявляет
    следующий и при этом роняет энергию — то, что нужно после нескольких
    жёстких треков подряд, чтобы зал отдышался.

    Порядок: голос нового появляется с ревербом поверх старого; старый
    разбирается по слоям (барабаны, бас, гармония) и уходит; какое-то
    время звучит почти голая акапелла нового; потом к ней возвращаются
    его собственные слои — и трек начинается уже «изнутри»."""
    lead = p["lead_bars"] * 4          # сколько голос висит над старым
    naked = p["naked_bars"] * 4        # сколько держим почти голую акапеллу
    ramp = 4.0
    ev = [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        # Новый заходит ТОЛЬКО голосом: остальные слои сняты, иначе это
        # обычный бленд, а не акапелла.
        *_stems_at(0, target, {"drums": 0.0, "bass": 0.0, "other": 0.0, "vocals": 1.0}),
        _ramp(0, "volume_ramp", target, ramp, 0.0, 0.85, "ease_in"),
        # Реверб на голос — он висит один и без пространства звучит голо.
        _ramp(0, "fx_meta", target, ramp, 0.0, 0.45, "ease_in"),
    ]
    # Старый разбирается по слоям, пока голос уже слышен.
    for i, part in enumerate(("drums", "bass", "other")):
        ev.append(_stem(lead * 0.35 + i * ramp, source, part, ramp, 1.0, 0.0, "ease_out"))
    ev += [
        _ramp(lead, "crossfade", source, ramp, 0.0, 1.0, "ease_out"),
        # Почти голая акапелла: энергия на дне, зал отдыхает.
        # Держим кроссфейдер на месте рампой из значения в себя же.
        # _hold здесь был ошибкой: HOLD — это «нажать и отпустить кнопку»,
        # а кроссфейдер не кнопка. Живой планировщик честно падал на
        # «Unknown discrete action: crossfade», то есть ST-06 нельзя было
        # отыграть на пульте вообще — только услышать в демо.
        _ramp(lead + ramp, "crossfade", source, naked, 1.0, 1.0),
        # Реверб убираем ДО того, как войдут барабаны, иначе они размажутся.
        _ramp(lead + naked, "fx_meta", target, ramp, 0.45, 0.0, "ease_out"),
        # Возврат слоёв нового: сначала гармония, потом бас, последними
        # барабаны — так вход читается как развитие, а не как включение.
        _stem(lead + naked, target, "other", ramp * 2, 0.0, 1.0, "ease_in"),
        _stem(lead + naked + ramp * 2, target, "bass", ramp, 0.0, 1.0, "ease_in"),
        _stem(lead + naked + ramp * 4, target, "drums", ramp, 0.0, 1.0, "ease_in"),
        _ramp(lead + naked + ramp * 4, "volume_ramp", target, ramp, 0.85, 1.0),
    ]
    return ev


def _build_loop_mill(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Луп-мельница: из лупа делают мелодию, а не просто дробят его.

    Отличие от TT-09 («Луп в бэкспин») существенное. Там луп делится
    пополам и разрешается бэкспином — приём про разгон. Здесь тот же
    луп становится ИНСТРУМЕНТОМ: на каждой ступени деления меняется
    высота (питч-фейдером, как это и делают руками), а эффект открывается
    не ровной рампой, а толчками на разных долях — не подряд. Ухо
    перестаёт слышать «кусок трека по кругу» и начинает слышать мотив.

    Родина приёма — house и техно, где переход длинный и его нечем
    заполнить; в драм-н-бейсе он тоже работает, но короче."""
    beats = float(p["start_beats"])
    step = float(p["step_beats"])
    floor_beats = float(p["min_beats"])
    pitch = float(p["pitch_range"])
    ev: list[dict] = [_discrete(0, "sync", target), _discrete(0, "play_from_cue", target)]

    pos = 0.0
    rung = 0
    # Ступени деления. Каждая держится ОДИНАКОВОЕ время, а не одинаковое
    # число повторов: только тогда частота повторов удваивается на каждом
    # шаге и получается разгон (то же правило, что в TT-09).
    while beats >= floor_beats - 1e-9:
        ev.append(_discrete(pos, "loop_activate", source, {"beats": beats}))
        # Питч ходит по ступеням вверх-вниз, а не ползёт в одну сторону:
        # мелодия — это чередование, а монотонный подъём слышен как брак
        # синхронизации.
        up = 0.5 + pitch * (0.5 if rung % 2 == 0 else -0.5)
        ev.append(_ramp(pos, "key_shift", source, step * 0.5, 0.5, up, "ease_in"))
        ev.append(_ramp(pos + step * 0.5, "key_shift", source, step * 0.5, up, 0.5))
        # Эффект толчками по НЕПОДРЯД идущим долям внутри ступени.
        # Именно неравномерность и превращает повтор в фразу.
        for k in (0.0, 0.75, 0.25):
            if k * step < step:
                ev.append(_ramp(pos + k * step, "fx_meta", source,
                                min(0.5, step * 0.4), 0.0, 0.8 - 0.2 * rung % 1.0, "ease_in"))
        pos += step
        rung += 1
        beats /= 2.0

    ev += [
        _discrete(pos, "loop_exit", source),
        _ramp(pos, "key_shift", source, 2.0, 0.5, 0.5),
        # Новый вступает «с раза» следующего такта — луп для того и
        # закручивался, чтобы дроп нового прозвучал разрешением.
        _cut(pos, source, 0.0, 1.0),
        _ramp(pos, "volume_ramp", target, 2.0, 0.6, 1.0, "ease_in"),
        _ramp(pos, "fx_meta", source, 8.0, 0.8, 0.0, "ease_out"),
    ]
    return ev


def _stab_swap(source: str, target: str, bpm: float, p: dict,
               hold: str = "vocals") -> list[dict]:
    """Вокальный рез на дроп: голос уходящего в один такт закрывает шов,
    а ритм-секция за этот же такт целиком меняется на новую.

    Это НЕ ST-06 и не дабл-дроп, хотя ухо слышит его как дабл-дроп.

    * ST-06 — длинная акапелла НА ПОНИЖЕНИЕ: новый объявляет себя голосом,
      энергия падает, зал отдыхает.
    * Здесь наоборот, НА ПОДЪЁМ и быстро: уходящий на своём дропе теряет
      всё, кроме голоса, ровно на такт; входящий в этот же удар входит
      СВОИМ дропом — барабанами и басом. Голос повисает над чужим дропом
      и через такт уходит в эхо.
    * Настоящий дабл-дроп (ST-04) держит оба трека целиком много тактов;
      здесь совмещение длится один такт и держится на голосе.

    Почему нечего складывать. В этот такт у уходящего играет ТОЛЬКО
    вокальный слой, а барабаны и бас уже у входящего — то есть бочка
    одна и бас один по построению, а не потому, что мы их развели
    эквалайзером. Ровно это и делает приём быстрым: разводить нечего,
    можно резать.

    Точка приёма (beat_offset=0) — удар дропа, общий для обоих треков."""
    gap = float(p["gap_bars"]) * 4          # сколько держим голый голос
    tail = float(p["tail_beats"])           # хвост голоса в эхе
    back = float(p["return_bars"]) * 4      # когда входящему вернуть верх
    # Насколько «рез» на самом деле рез. Ноль — мгновенно, как и было
    # задумано. Но на слух этого мало: диджей слышит, что уходящий уже
    # без баса и барабанов, а входящий появляется вдруг, — и это
    # читается как склейка, а не как приём. Полдоли пересечения по
    # слоям убирают шов, не превращая рез в кроссфейд: смена всё равно
    # происходит внутри одной доли.
    #
    # Отдельно: если ощущение «точки разъехались» остаётся и на
    # ненулевом пересечении, дело почти наверняка не здесь, а в темпе
    # трека. При BPM, определённом в 2/3 (174 записано как 116 — см.
    # id3_tags.py), доля длиннее в полтора раза, и ЛЮБОЙ приём встаёт
    # мимо сетки.
    cut = max(0.05, float(p.get("overlap_beats", 0.5)))

    ev = [
        _discrete(0, "sync", target),
        # Входящий стартует со СВОЕГО дропа — приём про совпадение дропов.
        _discrete(0, "play_from_cue", target),
        # Уходящий: всё, кроме голоса, снимается мгновенно.
        *_stems_at(0, source, {k: (1.0 if k == hold else 0.0)
                               for k in ("drums", "bass", "other", "vocals")}, cut),
        # Входящий входит ритм-секцией: барабаны и бас. Гармония и его
        # собственный вокал молчат — иначе два голоса разом, единственный
        # клэш, который слышат все.
        # Входящий входит ритм-секцией. Верхние слои молчат: два голоса
        # (или две мелодии) разом — единственный клэш, который слышат все.
        *_stems_at(0, target, {"drums": 1.0, "bass": 1.0, "other": 0.0, "vocals": 0.0}, cut),
        _cut(0, source, 0.0, 1.0),
        _ramp(0, "volume_ramp", target, cut, 0.0, 1.0),
    ]
    ev += [
        # Голос уходит в эхо — хвост живёт уже поверх нового трека.
        _ramp(gap - tail, "fx_meta", source, tail, 0.0, 0.85, "ease_in"),
        _stem(gap - tail, source, hold, tail, 1.0, 0.0, "ease_out"),
        # Входящему возвращается верх: сначала гармония, потом голос.
        _stem(gap, target, "other", back, 0.0, 1.0, "ease_in"),
        _stem(gap + back, target, "vocals", 4.0, 0.0, 1.0, "ease_in"),
        _ramp(gap + back, "fx_meta", source, 8.0, 0.85, 0.0, "ease_out"),
    ]
    return ev


def _build_vocal_stab_swap(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Шов закрывает ГОЛОС — классический вариант приёма."""
    return _stab_swap(source, target, bpm, p, hold="vocals")


def _build_melody_stab_swap(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Шов закрывает МЕЛОДИЯ — вариант для инструментальных треков.

    Нужен не «на всякий случай»: в библиотеке диджея вокал есть у
    единиц треков, и приём с голосом там честно отказывается работать.
    Гармонический слой держит шов ровно так же — важно ведь не то, что
    звучит голос, а то, что в этот такт у уходящего играет ОДИН слой, и
    ритм-секцию можно резать, а не разводить."""
    return _stab_swap(source, target, bpm, p, hold="other")


_register(Technique(
    id="ST-06", name="Вход с акапеллы", category="stems", difficulty=4,
    description="Новый трек объявляет себя голым голосом поверх ещё играющего старого, старый разбирается по слоям и уходит — и какое-то время звучит почти одна акапелла. Приём НА ПОНИЖЕНИЕ: после нескольких жёстких треков подряд залу нужно отдышаться, и пауза, сделанная голосом, работает лучше, чем просто тихий трек.",
    bpm_delta_max=6, key_rule="compatible", energy_direction="down",
    requires_stems=True, requires_decks=2, needs_vocals=True,
    params=[
        TechniqueParam("lead_bars", "Голос нового над старым", 8, 4, 32, "тактов"),
        TechniqueParam("naked_bars", "Сколько держать голую акапеллу", 8, 2, 32, "тактов"),
    ],
    steps=["Завести новый ОДНИМ вокальным слоем, с ревербом.",
           "Старый разобрать по слоям и увести.",
           "Держать почти голую акапеллу — энергия на дне.",
           "Вернуть новому гармонию, потом бас, последними барабаны."],
), _build_acappella_in)

_register(Technique(
    id="HS-01", name="Луп-мельница", category="universal", difficulty=5,
    description="Луп уходящего делится пополам такт за тактом, но вместе с делением ходит питч, а эффект открывается толчками по НЕПОДРЯД идущим долям. Из повтора получается мотив, а не «кусок трека по кругу». Приём house и техно, где переход длинный и его нечем заполнить.",
    bpm_delta_max=None, key_rule="any", energy_direction="up",
    requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("start_beats", "Стартовая длина лупа", 4, 1, 16, "долей"),
        TechniqueParam("min_beats", "До какой длины делить", 0.5, 0.125, 4, "долей"),
        TechniqueParam("step_beats", "Сколько держать ступень", 4, 1, 16, "долей"),
        TechniqueParam("pitch_range", "Ход питча", 0.3, 0.0, 1.0),
    ],
    steps=["Взять луп на последней фразе уходящего.",
           "Делить пополам, каждую ступень держать одинаковое время.",
           "На каждой ступени двигать питч вверх-вниз, а не в одну сторону.",
           "Эффект открывать толчками по разным долям, не подряд.",
           "Выйти из лупа — новый вступает «с раза» следующего такта."],
), _build_loop_mill)

_register(Technique(
    id="ST-07", name="Вокальный рез на дроп", category="stems", difficulty=5,
    description="Уходящий на своём дропе теряет всё, кроме голоса, ровно на такт — и в этот же удар входящий входит СВОИМ дропом, барабанами и басом. Голос повисает над чужим дропом и уходит в эхо. Ухо слышит дабл-дроп, но складывать нечего: бочка одна и бас один по построению, поэтому можно резать, а не разводить. Быстрый и мощный подъём.",
    bpm_delta_max=3, key_rule="compatible", energy_direction="up",
    requires_stems=True, requires_decks=2, needs_vocals=True,
    params=[
        TechniqueParam("gap_bars", "Голос один, без ритма", 1, 0.5, 4, "тактов"),
        TechniqueParam("tail_beats", "Уход голоса в эхо", 2, 0.5, 8, "долей"),
        TechniqueParam("return_bars", "Когда вернуть верх новому", 2, 1, 8, "тактов"),
        TechniqueParam("overlap_beats", "Пересечение слоёв на резе", 0.5, 0.05, 4, "долей"),
    ],
    steps=["Совместить дроп уходящего с дропом входящего.",
           "В удар дропа снять у уходящего барабаны, бас и гармонию — остаётся голос.",
           "Тем же ударом ввести входящий барабанами и басом.",
           "Через такт голос уходит в эхо.",
           "Вернуть входящему гармонию, следом его собственный вокал."],
), _build_vocal_stab_swap)

_register(Technique(
    id="ST-08", name="Мелодический рез на дроп", category="stems", difficulty=4,
    description="То же, что вокальный рез, но шов закрывает мелодия, а не голос: у уходящего на его дропе остаётся один гармонический слой, входящий тем же ударом входит барабанами и басом. Для инструментальных треков, где вокального слоя просто нет, — а таких в драм-н-бейсе большинство.",
    bpm_delta_max=3, key_rule="compatible", energy_direction="up",
    requires_stems=True, requires_decks=2, needs_vocals=False,
    params=[
        TechniqueParam("gap_bars", "Мелодия одна, без ритма", 1, 0.5, 4, "тактов"),
        TechniqueParam("tail_beats", "Уход мелодии в эхо", 2, 0.5, 8, "долей"),
        TechniqueParam("return_bars", "Когда вернуть верх новому", 2, 1, 8, "тактов"),
        TechniqueParam("overlap_beats", "Пересечение слоёв на резе", 0.5, 0.05, 4, "долей"),
    ],
    steps=["Совместить дроп уходящего с дропом входящего.",
           "В удар дропа снять у уходящего барабаны, бас и вокал — остаётся мелодия.",
           "Тем же ударом ввести входящий барабанами и басом.",
           "Через такт мелодия уходит в эхо.",
           "Вернуть входящему гармонию, следом вокал."],
), _build_melody_stab_swap)


# --- Эффекты как часть техники ---
#
# До сих пор из эффектов техника умела ровно одно — эхо, и то под именем
# fx_meta. Диджей работает не так: у него на пульте юнит с набором
# эффектов, и выбор эффекта — часть приёма, а не отдельная кнопка.
#
# Делятся эффекты на два класса, и это определяет, где эффект стоит:
#   ПОСЫЛ (эхо, реверб) — у них есть ХВОСТ, который обязан пережить увод
#   трека. Считаются отдельной шиной и складываются в микс ПОСЛЕ фейдера.
#   ВСТАВКА (фленджер, фейзер, вобл, перегруз, биткрашер, фильтр) —
#   обрабатывают сигнал деки и живут ДО фейдера: увёл фейдер, эффекта
#   тоже нет.
# Признак простой: есть хвост — посыл, нет — вставка.
#
# Скорость качания задаётся В ДОЛЯХ, а не в герцах: иначе эффект не
# попадает в темп, и это слышно сразу.

FX_UNITS = ("echo", "reverb", "flanger", "phaser", "wobble", "distortion",
            "bitcrush", "filter")


def _fx(beat_offset: float, deck: str, unit: str, duration_beats: float,
        value_from: float, value_to: float, curve: str = "linear", **params) -> dict:
    """Эффект юнита на деке. value — dry/wet 0..1."""
    if unit not in FX_UNITS:
        raise ValueError(f"нет такого эффекта: {unit}")
    p = {"unit": unit}
    p.update({k: v for k, v in params.items() if v is not None})
    return {"beat_offset": beat_offset, "action": "fx", "deck": deck, "kind": "ramp",
            "duration_beats": max(0.01, duration_beats), "value_from": value_from,
            "value_to": value_to, "curve": curve, "params": p}


def _build_flanger_out(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Фленджер уводит старый трек: гребёнка съедает середину, и место
    для нового освобождается само, без эквалайзера."""
    sweep = p["sweep_bars"] * 4
    return [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        _ramp(0, "eq_low", target, 1, 0.0, 0.0),
        _ramp(0, "crossfade", source, 2, 0.0, 0.4),
        _fx(0, source, "flanger", sweep, 0.0, 0.9, "ease_in",
            rate_beats=p["rate_beats"], depth_ms=p["depth_ms"]),
        _ramp(sweep - 4, "eq_low", source, 4, EQ_UNITY, 0.0),
        _ramp(sweep - 4, "eq_low", target, 4, 0.0, EQ_UNITY),
        _ramp(sweep - 4, "crossfade", source, 8, 0.4, 1.0),
    ]


def _build_phaser_rise(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Фейзер на ВХОДЯЩЕМ: новый трек поднимается из-под старого сквозь
    движущиеся вырезы. Слушатель слышит, что что-то приближается, задолго
    до того, как поймёт, что именно."""
    rise = p["rise_bars"] * 4
    return [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        _ramp(0, "eq_low", target, 1, 0.0, 0.0),
        _fx(0, target, "phaser", rise, 0.95, 0.0, "ease_out", rate_beats=p["rate_beats"]),
        _ramp(0, "crossfade", source, rise, 0.0, 0.5),
        _ramp(rise, "eq_low", source, 4, EQ_UNITY, 0.0),
        _ramp(rise, "eq_low", target, 4, 0.0, EQ_UNITY),
        _ramp(rise, "crossfade", source, 8, 0.5, 1.0),
    ]


def _build_wobble_swap(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Вобл качает фильтр в темп, и под это качание меняются треки.
    То, на чём стоит половина бас-музыки: обмен прячется в движении."""
    hold = p["hold_bars"] * 4
    return [
        _discrete(0, "sync", target),
        _discrete(0, "play_from_cue", target),
        _ramp(0, "eq_low", target, 1, 0.0, 0.0),
        _ramp(0, "crossfade", source, 2, 0.0, 0.45),
        _fx(0, source, "wobble", hold, 0.0, 1.0, "ease_in", rate_beats=p["rate_beats"]),
        _fx(hold, target, "wobble", 8, 1.0, 0.0, "ease_out", rate_beats=p["rate_beats"]),
        _ramp(hold, "eq_low", source, 2, EQ_UNITY, 0.0),
        _ramp(hold, "eq_low", target, 2, 0.0, EQ_UNITY),
        _cut(hold, source, 0.45, 1.0),
    ]


def _build_reverb_out(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Реверб-хвост: комната забирает последнюю фразу и звенит поверх
    пустоты, пока новый трек не встанет на ноги. Посыл открывается в
    КОНЦЕ фразы — ручка решает, что попадёт в комнату, а не что из неё
    выйдет."""
    tail = p["tail_beats"]
    land = _land_on_bar(tail + 2)
    open_at = max(0.0, land - tail)
    return [
        _discrete(0, "sync", target),
        _fx(open_at, source, "reverb", 0.5, 0.0, 0.95, "linear",
            decay_beats=p["decay_beats"]),
        _fx(open_at + 0.5, source, "reverb", max(0.5, tail - 0.5), 0.95, 0.95),
        _ramp(land - 2, "eq_low", source, 2, EQ_UNITY, 0.0),
        _cut(land, source, 0.0, 1.0),
        _discrete(land, "play_from_cue", target),
    ]


def _build_crush_break(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Слом: перегруз и биткрашер за пару тактов превращают трек в
    обломок, и на этом обломке происходит рез. Приём на один раз за сет —
    он ломает не переход, а сет."""
    bars = p["break_bars"] * 4
    land = _land_on_bar(bars)
    return [
        _discrete(0, "sync", target),
        _fx(0, source, "distortion", bars * 0.6, 0.0, 0.8, "ease_in", drive=p["drive"]),
        _fx(bars * 0.5, source, "bitcrush", bars * 0.5, 0.0, 0.95, "ease_in",
            bits=p["bits"], downsample=p["downsample"]),
        _ramp(bars * 0.5, "eq_low", source, bars * 0.5, EQ_UNITY, 0.0),
        _cut(land, source, 0.0, 1.0),
        _discrete(land, "play_from_cue", target),
    ]



_register(Technique(
    id="FX-01", name="Фленджер на увод", category="fx", difficulty=2,
    description="Гребёнка фленджера съедает середину уходящего, и место для нового освобождается само — без эквалайзера. Скорость качания задаётся в долях, поэтому эффект всегда в темп.",
    bpm_delta_max=6, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("sweep_bars", "Длительность свипа", 8, 2, 32, "тактов"),
        TechniqueParam("rate_beats", "Период качания", 4, 0.25, 16, "долей"),
        TechniqueParam("depth_ms", "Глубина", 3.5, 0.5, 12, "мс"),
    ],
    steps=["Завести новый со снятым низом.",
           "Поднять фленджер на уходящем за N тактов.",
           "Обмен низом и вывод фейдером."],
), _build_flanger_out)

_register(Technique(
    id="FX-02", name="Фейзер-подъём", category="fx", difficulty=3,
    description="Фейзер на ВХОДЯЩЕМ: новый трек поднимается из-под старого сквозь движущиеся вырезы. Слышно, что что-то приближается, задолго до того, как понятно, что именно.",
    bpm_delta_max=6, key_rule="any", energy_direction="up", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("rise_bars", "Длительность подъёма", 8, 4, 32, "тактов"),
        TechniqueParam("rate_beats", "Период качания", 8, 0.5, 32, "долей"),
    ],
    steps=["Завести новый со снятым низом и фейзером на максимуме.",
           "Постепенно убирать фейзер — трек «проявляется».",
           "На границе фразы обмен низом и вывод."],
), _build_phaser_rise)

_register(Technique(
    id="FX-03", name="Вобл-переход", category="fx", difficulty=3,
    description="Фильтр качается в темп, и под это качание меняются треки: обмен прячется в движении. То, на чём стоит половина бас-музыки.",
    bpm_delta_max=4, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("hold_bars", "Длительность качания", 8, 2, 32, "тактов"),
        TechniqueParam("rate_beats", "Период качания", 1, 0.25, 8, "долей"),
    ],
    steps=["Завести новый со снятым низом.",
           "Раскачать вобл на уходящем.",
           "На «раз» — обмен низом и рез, вобл уезжает уже на новом."],
), _build_wobble_swap)

_register(Technique(
    id="FX-04", name="Реверб-хвост", category="fx", difficulty=1,
    description="Комната забирает последнюю фразу и звенит поверх пустоты, пока новый трек не встанет на ноги. Посыл открывается в КОНЦЕ фразы — ручка решает, что попадёт в комнату, а не что из неё выйдет.",
    bpm_delta_max=None, key_rule="any", energy_direction="down", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("tail_beats", "Что уходит в комнату", 4, 1, 16, "долей"),
        TechniqueParam("decay_beats", "Длина хвоста", 4, 1, 16, "долей"),
    ],
    steps=["На последних долях фразы открыть посыл на реверб.",
           "Убрать низ и увести фейдер — хвост остаётся.",
           "Новый трек с «раза» следующего такта."],
), _build_reverb_out)

_register(Technique(
    id="FX-05", name="Дисторшн-слом", category="fx", difficulty=4,
    description="Перегруз и биткрашер за пару тактов превращают трек в обломок, и на этом обломке происходит рез. Приём на один раз за сет — он ломает не переход, а сет.",
    bpm_delta_max=None, key_rule="any", energy_direction="any", requires_stems=False, requires_decks=2,
    params=[
        TechniqueParam("break_bars", "Длительность слома", 2, 1, 8, "тактов"),
        TechniqueParam("drive", "Перегруз", 8, 2, 20),
        TechniqueParam("bits", "Разрядность", 6, 3, 12, "бит"),
        TechniqueParam("downsample", "Прореживание", 8, 1, 32, "раз"),
    ],
    steps=["Поднять перегруз на уходящем.",
           "Поверх — биткрашер, трек рассыпается.",
           "Рез на границе такта."],
), _build_crush_break)


# --------------------------------------------------------- своя техника
#
# Каталог из 38 приёмов отвечает на вопрос «как это делают». Он не
# отвечает на вопрос «а если вот так»: у каждой техники три-десять
# ползунков, и всё, чего в них нет, недостижимо — хочешь эхо длиннее, а
# обмен низом короче, и чтобы вокал нового заходил раньше барабанов, —
# такой техники в списке нет и не будет, потому что комбинаций больше,
# чем можно перечислить.
#
# «Своя» — это не ещё одна техника, а пульт: один набор ползунков на
# всё, что DARAVE вообще умеет отправить в Mixxx. Эквалайзер обеих дек,
# фильтр, посыл на эффект, четыре слоя на каждой деке, луп, реверс,
# бэкспин, бросок, кроссфейд, пауза перед входом.
#
# Значения по умолчанию — это «Классика» (DNB-25), то есть то, как сводят
# в девяти случаях из десяти. Ползунок, оставленный на месте, ничего не
# меняет; сдвинутый — меняет ровно одно. Так проще услышать, что делает
# каждый, чем начиная с нуля.
#
# Ноль в ползунке всегда означает «этого действия нет», а не «действие с
# нулевой силой»: событие просто не попадает в план. Иначе каждый
# невыключенный приём слал бы в Mixxx рампу из нуля в ноль, и на длинном
# сведении их набирались бы сотни.

CUSTOM_PARAMS = [
    # --- общая форма перехода ---
    TechniqueParam("blend_bars", "Длина сведения", 8, 2, 64, "тактов"),
    TechniqueParam("out_bars", "Из них на вывод старого", 4, 1, 32, "тактов"),
    TechniqueParam("entry_delay_bars", "Пауза перед входом нового", 0, 0, 16, "тактов"),
    TechniqueParam("entry_ramp_beats", "Ввод нового за", 2, 0.25, 32, "долей"),
    TechniqueParam("under_level", "Уровень нового под старым", 0.40, 0.0, 1.0),
    TechniqueParam("hold_bars", "Сколько держать совмещение", 0, 0, 32, "тактов"),
    TechniqueParam("xfade_curve", "Форма кроссфейда: 0 плавно, 1 резко", 0.0, 0.0, 1.0),

    # --- эквалайзер: обмен и подрезка ---
    TechniqueParam("swap_bars", "Длина обмена низом", 1, 0.125, 16, "тактов"),
    TechniqueParam("low_kill_new", "Снять низ у нового", 1.0, 0.0, 1.0),
    TechniqueParam("mid_duck_new", "Убрать середину у нового", 0.0, 0.0, 1.0),
    TechniqueParam("high_duck_new", "Убрать верх у нового", 0.0, 0.0, 1.0),
    TechniqueParam("mid_duck_old", "Убрать середину у старого на выводе", 0.0, 0.0, 1.0),
    TechniqueParam("high_duck_old", "Убрать верх у старого на выводе", 0.0, 0.0, 1.0),

    # --- фильтр и эффекты ---
    TechniqueParam("filter_old", "Свип фильтра на старом", 0.0, 0.0, 1.0),
    TechniqueParam("filter_new", "Свип фильтра на новом (снизу вверх)", 0.0, 0.0, 1.0),
    TechniqueParam("echo", "Сила эха на уходе старого", 0.55, 0.0, 1.0),
    TechniqueParam("echo_beats", "Длина эха", 8, 1, 64, "долей"),
    TechniqueParam("fx_new", "Эффект на новом", 0.0, 0.0, 1.0),

    # --- слои (работают, если на деке .stem.mp4) ---
    TechniqueParam("stem_drums_new", "Барабаны нового под старым", 0.0, 0.0, 1.0),
    TechniqueParam("stem_bass_new", "Бас нового под старым", 0.0, 0.0, 1.0),
    TechniqueParam("stem_other_new", "Гармония нового под старым", 1.0, 0.0, 1.0),
    TechniqueParam("stem_vocals_new", "Вокал нового под старым", 1.0, 0.0, 1.0),
    TechniqueParam("stem_drums_old", "Барабаны старого на выводе", 1.0, 0.0, 1.0),
    TechniqueParam("stem_vocals_old", "Вокал старого на выводе", 1.0, 0.0, 1.0),

    # --- транспорт ---
    TechniqueParam("loop_bars", "Луп последней фразы старого", 4, 0, 32, "тактов"),
    TechniqueParam("reverse_beats", "Реверс перед обменом", 0, 0, 32, "долей"),
    TechniqueParam("spin_beats", "Длительность бэкспина", 0, 0, 16, "долей"),
    TechniqueParam("throw_amount", "Сила броска (лупролл)", 0, 0, 1.0),
    TechniqueParam("throw_beats", "Длина броска", 1, 0.125, 8, "долей"),
]

_STEM_KEYS = (("drums", "stem_drums"), ("bass", "stem_bass"),
              ("other", "stem_other"), ("vocals", "stem_vocals"))


def _build_custom(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    """Собирает переход из ползунков. Ничего не решает за диджея."""
    unity = EQ_UNITY

    blend = max(2.0, float(p.get("blend_bars", 8)))
    out_bars = min(float(p.get("out_bars", 4)), max(1.0, blend / 2.0))
    hold_bars = max(0.0, float(p.get("hold_bars", 0)))
    under_bars = max(1.0, blend - out_bars) + hold_bars

    intro = under_bars * 4          # долей: новый идёт под старым
    out = out_bars * 4              # долей: вывод старого
    delay = max(0.0, float(p.get("entry_delay_bars", 0))) * 4
    swap_at = delay + intro
    end = swap_at + out

    ramp_in = _clamp(float(p.get("entry_ramp_beats", 2)), 0.25, max(0.25, intro))
    under = float(p.get("under_level", 0.40))
    swap = _clamp(float(p.get("swap_bars", 1)) * 4, 0.5, max(0.5, out))
    curve = "linear" if float(p.get("xfade_curve", 0.0)) < 0.5 else "ease_in"

    ev: list[dict] = [
        _discrete(delay, "sync", target),
        _hold(delay, "sync_lock", target, end - delay + 4),
        _discrete(delay, "play_from_cue", target),
    ]

    # --- эквалайзер нового, пока он под старым --------------------------
    # Низ снимается ПЕРЕД вводом фейдера, а не одновременно с ним: иначе
    # первые доли играют два баса, и слышно это отчётливее всего.
    kill = float(p.get("low_kill_new", 1.0))
    ev.append(_ramp(max(0.0, delay - 1), "eq_low", target, 1,
                    unity * (1.0 - kill), unity * (1.0 - kill)))
    for key, action in (("mid_duck_new", "eq_mid"), ("high_duck_new", "eq_high")):
        duck = float(p.get(key, 0.0))
        if duck > 0.01:
            ev.append(_ramp(delay, action, target, ramp_in, unity, unity * (1.0 - duck)))

    # --- слои нового, пока он под старым --------------------------------
    for stem, key in _STEM_KEYS:
        val = p.get(f"{key}_new")
        if val is None or abs(float(val) - 1.0) < 0.01:
            continue  # 1.0 = слой звучит как есть, событие не нужно
        ev.append(_stem(delay, target, stem, max(0.25, ramp_in), 1.0, float(val)))

    # --- ввод нового ----------------------------------------------------
    ev.append(_ramp(delay, "crossfade", source, ramp_in, 0.0, under, curve))

    # --- фильтр и эффект на новом ---------------------------------------
    if float(p.get("filter_new", 0.0)) > 0.01:
        amt = float(p["filter_new"])
        # Снизу вверх: у входящего фильтр открывается, а не закрывается.
        ev.append(_ramp(delay, "filter_sweep", target, intro, -amt, 0.0))
    if float(p.get("fx_new", 0.0)) > 0.01:
        ev.append(_discrete(delay, "fx_enable", target))
        ev.append(_ramp(delay, "fx_mix", target, intro, 0.0, float(p["fx_new"])))
        ev.append(_ramp(end, "fx_mix", target, 2, float(p["fx_new"]), 0.0))

    # --- луп последней фразы старого ------------------------------------
    loop_bars = min(float(p.get("loop_bars", 0)), under_bars)
    if loop_bars >= 1:
        ev.append(_discrete(swap_at - loop_bars * 4, "loop_activate",
                            source, {"beats": loop_bars * 4}))

    # --- приёмы перед обменом -------------------------------------------
    rev = float(p.get("reverse_beats", 0))
    if rev >= 0.25:
        ev.append(_hold(swap_at - rev, "reverse_hold", source, rev))
    spin = float(p.get("spin_beats", 0))
    if spin >= 0.25:
        # Бэкспин кончается сам — отпускать его нельзя (см. midi_mapping),
        # поэтому это _discrete, а не _hold.
        ev.append(_discrete(swap_at, "spinback", source,
                            {"factor": 1.0 + spin / 8.0, "rate": -10.0}))
    throw = float(p.get("throw_amount", 0))
    if throw > 0.01:
        beats = float(p.get("throw_beats", 1))
        ev.append(_hold(swap_at, "loop_roll", target, beats, ))
        ev[-1]["params"] = {"beats": beats}

    # --- обмен низом ----------------------------------------------------
    ev.append(_ramp(swap_at, "eq_low", target, swap, unity * (1.0 - kill), unity))
    ev.append(_ramp(swap_at, "eq_low", source, swap, unity, 0.0))
    # Середина и верх нового возвращаются на место вместе с низом: если
    # оставить их подрезанными, новый трек так и играет глухим.
    for key, action in (("mid_duck_new", "eq_mid"), ("high_duck_new", "eq_high")):
        if float(p.get(key, 0.0)) > 0.01:
            ev.append(_ramp(swap_at, action, target, swap,
                            unity * (1.0 - float(p[key])), unity))
    for stem, key in _STEM_KEYS:
        val = p.get(f"{key}_new")
        if val is None or abs(float(val) - 1.0) < 0.01:
            continue
        ev.append(_stem(swap_at, target, stem, swap, float(val), 1.0))

    # --- вывод старого ---------------------------------------------------
    ev.append(_ramp(swap_at, "crossfade", source, out, under, 1.0, curve))
    if float(p.get("filter_old", 0.0)) > 0.01:
        ev.append(_ramp(swap_at, "filter_sweep", source, out, 0.0,
                        float(p["filter_old"])))
    for key, action in (("mid_duck_old", "eq_mid"), ("high_duck_old", "eq_high")):
        duck = float(p.get(key, 0.0))
        if duck > 0.01:
            ev.append(_ramp(swap_at, action, source, out, unity, unity * (1.0 - duck)))
    for stem, key in (("drums", "stem_drums"), ("vocals", "stem_vocals")):
        val = p.get(f"{key}_old")
        if val is None or abs(float(val) - 1.0) < 0.01:
            continue
        ev.append(_stem(swap_at, source, stem, out, 1.0, float(val)))

    # --- эхо на уходе ----------------------------------------------------
    echo = float(p.get("echo", 0.55))
    if echo > 0.01:
        echo_beats = max(1.0, float(p.get("echo_beats", 8)))
        # Эхо включается ДО конца вывода и тянется ПОСЛЕ него: смысл его в
        # том, что хвост старого звучит уже поверх нового и склеивает стык.
        start = max(swap_at, end - echo_beats / 2.0)
        ev.append(_discrete(start, "fx_enable", source))
        ev.append(_ramp(start, "fx_mix", source, echo_beats / 2.0, 0.0, echo))
        ev.append(_ramp(end, "fx_mix", source, echo_beats, echo, 0.0))

    # --- уборка за собой --------------------------------------------------
    ev.append(_discrete(end, "loop_exit", source))
    ev.append(_ramp(end + 2, "eq_low", source, 2, 0.0, unity))
    for action, key in (("eq_mid", "mid_duck_old"), ("eq_high", "high_duck_old")):
        if float(p.get(key, 0.0)) > 0.01:
            ev.append(_ramp(end + 2, action, source, 2,
                            unity * (1.0 - float(p[key])), unity))
    if float(p.get("filter_old", 0.0)) > 0.01:
        ev.append(_ramp(end + 2, "filter_sweep", source, 2, float(p["filter_old"]), 0.0))
    for stem, key in (("drums", "stem_drums"), ("vocals", "stem_vocals")):
        val = p.get(f"{key}_old")
        if val is not None and abs(float(val) - 1.0) >= 0.01:
            ev.append(_stem(end + 2, source, stem, 2, float(val), 1.0))

    ev.sort(key=lambda e: e["beat_offset"])
    return ev


_register(Technique(
    id="CUSTOM", name="Своя техника", category="custom", difficulty=3,
    description=(
        "Пульт, а не приём: все ползунки EQ, фильтра, эффектов, слоёв и "
        "транспорта сразу. По умолчанию стоит «Классика» — ползунок на "
        "месте ничего не меняет, сдвинутый меняет ровно одно. Слои "
        "действуют, только если на деке лежит .stem.mp4."),
    bpm_delta_max=None, key_rule="any", energy_direction="any",
    requires_stems=False, requires_decks=2,
    params=CUSTOM_PARAMS,
    steps=["Крутите ползунки и слушайте демо — каждая правка проигрывается сразу.",
           "Ползунок на нуле означает «этого действия нет», а не «сила нулевая».",
           "Слои (стемы) работают, если трек загружен как .stem.mp4."],
), _build_custom)


def build_plan(technique_id: str, plan_id: str, source: str, target: str, bpm: float,
                overrides: dict | None = None, third: str | None = None) -> dict:
    """third — третья дека для приёмов с requires_decks=3.

    Раньше её имя жило в `params` со значением по умолчанию "C", а params
    состоит из ЧИСЛОВЫХ ползунков — то есть передать другую деку было
    нечем, и трипл-дроп всегда играл на C, даже если трек лежал на D."""
    if technique_id not in TECHNIQUES:
        raise ValueError(f"Unknown technique: {technique_id}")
    technique = TECHNIQUES[technique_id]
    params = technique.param_defaults()
    if overrides:
        params.update({k: v for k, v in overrides.items() if k in params})
    if technique.requires_decks >= 3:
        if not third:
            raise ValueError(
                f"«{technique.name}» играется на трёх деках — укажите третью "
                f"(source={source}, target={target}). Она должна быть загружена "
                f"и видна в телеметрии.")
        if third in (source, target):
            raise ValueError(f"Третья дека не может совпадать с {source} или {target}")
        params["third_deck"] = third
    events = _BUILDERS[technique_id](source, target, bpm, params)
    return {
        "plan_id": plan_id,
        "bpm": bpm,
        "anchor_lead_seconds": 1.0,
        "events": events,
    }
