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
        _hold(0, "reverse_hold", source, hold_beats),
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
        events.append(_discrete(t, "loop_activate", source, {"beats": step}))
        events.append(_discrete(t + step * 0.5, "loop_exit", source))
    events.append(_discrete(repeats * step, "sync", target))
    events.append(_discrete(repeats * step, "play_from_cue", target))
    events.append(_ramp(repeats * step, "crossfade", source, 4, 0.0, 1.0))
    return events


def _build_backspin_transition(source: str, target: str, bpm: float, p: dict) -> list[dict]:
    return [
        _hold(0, "reverse_hold", source, p["spin_beats"]),
        _discrete(p["spin_beats"], "play_toggle", source),  # стоп на "выезде" бэкспина
        _discrete(p["spin_beats"], "play_toggle", target),
        _ramp(p["spin_beats"], "crossfade", source, 0.1, 0.0, 1.0),  # мгновенный рез на target
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
    raise NotImplementedError(
        "A Cappella Overlay требует живого доступа к стемам (вокал отдельно) — "
        "сейчас DARAVE умеет только офлайн-разделение (stems.py), живого "
        "независимого канала под вокал на деке ещё нет (см. README 'Что дальше')."
    )


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
    description="Вокал одного трека поверх инструментала другого. Требует живого доступа к стемам — пока не реализовано (см. README).",
    bpm_delta_max=6, key_rule="compatible", energy_direction="any", requires_stems=True, requires_decks=2,
    params=[],
    steps=["(недоступно) Нужен отдельный канал под вокальный стем — сейчас DARAVE умеет только офлайн Demucs, не живое разделение на деке."],
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
        TechniqueParam("blend_bars", "Длина сведения", 8, 4, 16, "тактов"),
        TechniqueParam("entry_delay_bars", "Пауза перед входом нового", 0, 0, 4, "тактов"),
        TechniqueParam("entry_ramp_beats", "Ввод нового за", 2, 1, 16, "долей"),
        TechniqueParam("out_bars", "Из них на вывод старого", 4, 2, 8, "тактов"),
        TechniqueParam("under_level", "Насколько слышен новый под старым", 0.40, 0.15, 0.6),
        TechniqueParam("mid_duck", "Убрать середину у нового, пока играют оба", 0.0, 0.0, 0.8),
        TechniqueParam("swap_bars", "Обмен низом за", 1, 0.5, 2, "тактов"),
        TechniqueParam("loop_bars", "Луп последней фразы старого", 4, 0, 8, "тактов"),
        TechniqueParam("echo", "Эхо на уходе старого", 0.55, 0.0, 1.0),
        TechniqueParam("reverse_beats", "Реверс перед обменом", 0, 0, 4, "долей"),
    ],
    steps=["Sync, запустить новый трек с его интро.",
           "Низ у нового снят полностью — играет один бас, старого.",
           "4-12 тактов они идут вместе — фраза или две, не полторы минуты.",
           "Последняя фраза старого — в луп, чтобы обмен попал на границу фразы.",
           "На границе фразы — один обмен низом за такт.",
           "Старый уводится фильтром и уходит в эхо; хвост дилея склеивает стык."],
), _build_classic)


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
