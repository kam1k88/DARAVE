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
    third = p.get("third_deck", "C")
    align = p["align_bars"] * 4
    return [
        _discrete(0, "sync", target),
        _discrete(0, "sync", third),
        _ramp(0, "crossfade", source, align, 0.0, 0.66),
        _ramp(align, "crossfade", source, 2, 0.66, 1.0),
        _discrete(align + 2, "loop_exit", source),
    ]


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
    params=[TechniqueParam("align_bars", "Такты совмещения", 8, 4, 16, "тактов")],
    steps=["Sync target и третью деку.", "Свести громкости до 2/3.", "На точке совпадения дропов — докроссфейдить, выйти из петли source."],
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


def build_plan(technique_id: str, plan_id: str, source: str, target: str, bpm: float,
                overrides: dict | None = None) -> dict:
    if technique_id not in TECHNIQUES:
        raise ValueError(f"Unknown technique: {technique_id}")
    technique = TECHNIQUES[technique_id]
    params = technique.param_defaults()
    if overrides:
        params.update({k: v for k, v in overrides.items() if k in params})
    events = _BUILDERS[technique_id](source, target, bpm, params)
    return {
        "plan_id": plan_id,
        "bpm": bpm,
        "anchor_lead_seconds": 1.0,
        "events": events,
    }
