"""
scripts/core/dj_techniques.py — Complete catalog of 20 DJ transition techniques for Drum & Bass.

Each technique is a dataclass describing:
  - What it does and when to use it
  - Which DSP effects are needed
  - BPM/key/energy constraints
  - Step-by-step implementation instructions
  - Tunable parameters (min/max/default) for the Solo Mode UI

Usage:
    from scripts.core.dj_techniques import TECHNIQUES, get_technique
    t = get_technique("DNB-07")
    print(t.name, t.difficulty, t.effects_used)
    for p in t.parameters:
        print(f"  {p.name}: {p.default} ({p.min_val}-{p.max_val} {p.unit})")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)


@dataclass
class TechniqueParam:
    """A single tunable parameter for a DJ technique."""
    name: str           # machine-readable key: "swap_start_bar"
    label: str          # human-readable: "Точка начала замены"
    type: str           # "int" | "float" | "select"
    min_val: float      # minimum value (ignored for select)
    max_val: float      # maximum value (ignored for select)
    default: float      # default value
    unit: str           # "тактов" | "dB" | "%" | "x" | ""
    options: List[str] = field(default_factory=list)  # for type="select"


@dataclass
class DJTechnique:
    """A single DJ transition technique."""

    id: str                          # "DNB-07"
    name: str                        # "Echo Cut"
    category: str                    # "cut" | "eq" | "filter" | "echo" | "combo" | "loop" | "effect" | "stem" | "structural" | "ambient" | "pitch"
    difficulty: int                  # 1-5
    level: str                       # "beginner" | "intermediate" | "advanced" | "pro" | "experimental"
    description: str                 # What it does
    best_for: str                    # "DnB, Liquid, Neuro"
    when_to_use: str                 # Conditions for using this technique
    steps: List[str]                 # Step-by-step instructions
    effects_used: List[str]          # DSP effects needed: ["echo", "filter", "none"]
    bpm_range: Tuple[int, int]       # Optimal BPM range (160, 180) for DnB
    key_compatibility: str           # "any" | "compatible" | "clashing" | "tritone"
    energy_delta: str                # "low_to_high" | "high_to_low" | "same" | "any"
    transition_bars: int             # Recommended bar count
    frequency_focus: str             # Which frequencies are affected
    description_ru: str = ""         # Russian description
    description_cn: str = ""         # Chinese description
    parameters: List[TechniqueParam] = field(default_factory=list)  # Tunable params


# ---------------------------------------------------------------------------
# Catalog of 20 DnB techniques
# ---------------------------------------------------------------------------

TECHNIQUES: List[DJTechnique] = [
    # ═══════════════════════════════════════════════════════════════════════
    # LEVEL 1: BEGINNER
    # ═══════════════════════════════════════════════════════════════════════

    DJTechnique(
        id="DNB-01",
        name="Double Drop",
        category="cut",
        difficulty=2,
        level="beginner",
        description="Simultaneous launch of two track drops at the same point for maximum energy impact.",
        description_ru="Одновременный запуск дропов двух треков в одной точке для максимального энергетического эффекта.",
        description_cn="同时在同一点启动两首曲目的 Drop，以获得最大能量冲击。",
        best_for="DnB, Jump Up, Neurofunk",
        when_to_use="When both tracks have compatible keys and same BPM. Maximum energy moment.",
        steps=[
            "Find two tracks with the same BPM and compatible Camelot keys (difference 0, ±1, ±3).",
            "Locate the drop point in both tracks (high energy + high spectral centroid).",
            "Synchronize both tracks so drops align on the bar grid.",
            "Launch both tracks simultaneously (or 1-2 bars apart for question-answer effect).",
            "Use EQ to separate frequencies: Bass on Track A, Highs on Track B (or vice versa).",
        ],
        effects_used=["none"],
        bpm_range=(160, 180),
        key_compatibility="compatible",
        energy_delta="same",
        transition_bars=4,
        frequency_focus="all",
        parameters=[
            TechniqueParam(name="phase_offset", label="Смещение фаз", type="int", min_val=0, max_val=4, default=0, unit="тактов"),
            TechniqueParam(name="vol_a", label="Громкость трека A", type="float", min_val=0.0, max_val=1.0, default=1.0, unit=""),
            TechniqueParam(name="vol_b", label="Громкость трека B", type="float", min_val=0.0, max_val=1.0, default=1.0, unit=""),
        ],
    ),

    DJTechnique(
        id="DNB-02",
        name="Bass Swap",
        category="eq",
        difficulty=2,
        level="beginner",
        description="Smooth bassline replacement from one track to another using EQ crossfade.",
        description_ru="Плавная замена басовой линии одного трека на другой с помощью EQ-кроссфейда.",
        description_cn="使用 EQ 交叉淡化平滑地将一首曲目的低音线替换为另一首。",
        best_for="Liquid DnB, Deep DnB",
        when_to_use="When tracks have compatible basslines and harmonic keys. For long, smooth transitions (32+ bars).",
        steps=[
            "Load Track B with Low EQ at 0% (no bass).",
            "Gradually raise Low on Track B while simultaneously lowering Low on Track A.",
            "The crossover point should be at the midpoint of the transition.",
            "Keep Mid and High stable during the bass swap.",
            "Ensure basslines are harmonically compatible (Camelot distance ≤ 2).",
        ],
        effects_used=["none"],
        bpm_range=(160, 180),
        key_compatibility="compatible",
        energy_delta="same",
        transition_bars=32,
        frequency_focus="low",
        parameters=[
            TechniqueParam(name="swap_start_bar", label="Точка начала замены", type="int", min_val=1, max_val=32, default=8, unit="тактов"),
            TechniqueParam(name="swap_speed", label="Скорость замены", type="float", min_val=0.1, max_val=1.0, default=0.5, unit="x"),
        ],
    ),

    DJTechnique(
        id="DNB-03",
        name="Filter Sweep",
        category="filter",
        difficulty=1,
        level="beginner",
        description="Using high-pass/low-pass filter to smoothly bring in or fade out a track.",
        description_ru="Использование HP/LP-фильтра для плавного появления или затухания трека.",
        description_cn="使用高通/低通滤波器平滑引入或淡出曲目。",
        best_for="Liquid DnB, Atmospheric DnB",
        when_to_use="For atmospheric, gentle transitions. Works with any key combination.",
        steps=[
            "On incoming Track B: engage High-Pass Filter, sweep from max to min (track appears from 'radio effect').",
            "On outgoing Track A: engage Low-Pass Filter, sweep from min to max (track 'sinks').",
            "Do this slowly over 16-32 bars.",
            "Keep the filter sweep in sync with the bar grid.",
        ],
        effects_used=["filter"],
        bpm_range=(160, 180),
        key_compatibility="any",
        energy_delta="any",
        transition_bars=16,
        frequency_focus="all",
        parameters=[
            TechniqueParam(name="filter_type", label="Тип фильтра", type="select", min_val=0, max_val=0, default="HP", unit="", options=["HP", "LP"]),
            TechniqueParam(name="sweep_bars", label="Скорость открытия/закрытия", type="int", min_val=4, max_val=32, default=16, unit="тактов"),
        ],
    ),

    DJTechnique(
        id="DNB-04",
        name="Quick Cut",
        category="cut",
        difficulty=1,
        level="beginner",
        description="Sharp track switch at the drop point without overlap.",
        description_ru="Резкое переключение трека на точке дропа без наложения.",
        description_cn="在 Drop 点快速切换曲目，无重叠。",
        best_for="Jump Up, Neurofunk, Techstep",
        when_to_use="When you need a sudden mood change. Perfect for conflicting keys or different BPMs.",
        steps=[
            "At the end of Track A's breakdown (4-8 bars before the drop), start Track B.",
            "At the exact moment Track A's drop would hit, cut Track A to 0 and bring in Track B.",
            "No overlap — clean cut.",
            "Works even with conflicting keys since there's no harmonic overlap.",
        ],
        effects_used=["none"],
        bpm_range=(160, 180),
        key_compatibility="any",
        energy_delta="any",
        transition_bars=4,
        frequency_focus="all",
        parameters=[
            TechniqueParam(name="intro_bars", label="За сколько тактов до дропа включается второй трек", type="int", min_val=2, max_val=8, default=4, unit="тактов"),
        ],
    ),

    # ═══════════════════════════════════════════════════════════════════════
    # LEVEL 2: INTERMEDIATE
    # ═══════════════════════════════════════════════════════════════════════

    DJTechnique(
        id="DNB-05",
        name="Delay Out",
        category="echo",
        difficulty=3,
        level="intermediate",
        description="Using echo/delay effect to 'wash away' the old track while the new one enters.",
        description_ru="Использование эхо/задержки для «смывания» старого трека при входе нового.",
        description_cn="使用回声/延迟效果「冲走」旧曲目，同时新曲目进入。",
        best_for="Liquid DnB, Atmospheric DnB, Neurofunk",
        when_to_use="For dramatic, atmospheric transitions between tracks of different energy levels.",
        steps=[
            "4 bars before the end of the section, activate Echo/Delay on Track A.",
            "Immediately cut Track A's volume fader to 0.",
            "Echo continues to sound, creating a 'fog' effect.",
            "Under the echo, bring in Track B.",
            "Let the echo naturally fade out or manually reduce the wet level.",
        ],
        effects_used=["echo"],
        bpm_range=(160, 180),
        key_compatibility="any",
        energy_delta="any",
        transition_bars=8,
        frequency_focus="effect",
        parameters=[
            TechniqueParam(name="delay_duration", label="Длительность эха", type="float", min_val=0.1, max_val=2.0, default=0.5, unit="сек"),
            TechniqueParam(name="delay_wet", label="Громкость эха", type="float", min_val=0.0, max_val=1.0, default=0.7, unit=""),
            TechniqueParam(name="trigger_bar", label="Точка включения в тактах до конца", type="int", min_val=1, max_val=8, default=4, unit="тактов"),
        ],
    ),

    DJTechnique(
        id="DNB-06",
        name="EQ Roller",
        category="eq",
        difficulty=3,
        level="intermediate",
        description="Gradual replacement of all frequency bands (Low, Mid, High) between two tracks.",
        description_ru="Постепенная замена всех частотных полос (Low, Mid, High) между двумя треками.",
        description_cn="在两首曲目之间逐步替换所有频段（低、中、高）。",
        best_for="Liquid DnB, Deep DnB",
        when_to_use="For maximum smoothness. Tracks must be harmonically compatible.",
        steps=[
            "Load Track B with all EQ bands at 0.",
            "After 8 bars: swap Low (bass).",
            "After 8 more bars: swap Mid (harmony).",
            "After 8 more bars: swap High (tops).",
            "Track A gradually 'disappears' piece by piece.",
            "Total transition: 24-32 bars.",
        ],
        effects_used=["none"],
        bpm_range=(160, 180),
        key_compatibility="compatible",
        energy_delta="same",
        transition_bars=32,
        frequency_focus="all",
        parameters=[
            TechniqueParam(name="eq_order", label="Порядок замены", type="select", min_val=0, max_val=0, default="Low→Mid→High", unit="", options=["Low→Mid→High", "High→Mid→Low", "Mid→Low→High"]),
            TechniqueParam(name="interval_bars", label="Интервал между этапами", type="int", min_val=4, max_val=16, default=8, unit="тактов"),
        ],
    ),

    DJTechnique(
        id="DNB-07",
        name="Echo Cut",
        category="combo",
        difficulty=4,
        level="intermediate",
        description="Combination of Echo, EQ masking, and sharp cut for transitions between clashing keys (±6 Camelot).",
        description_ru="Комбинация эхо, EQ-маскировки и резкого среза для переходов между конфликтующими тональностями (±6 Камелот).",
        description_cn="结合回声、EQ 掩蔽和快速切换，用于冲突调性（±6 Camelot）之间的过渡。",
        best_for="DnB, Neurofunk, Dark DnB",
        when_to_use="When Camelot shows ±6 difference (tritone / 'devil's interval'). When you need to preserve atmosphere but change energy sharply.",
        steps=[
            "Synchronize both tracks by BPM.",
            "On Track B: cut Mid frequency to -∞ (mids = harmony, which would clash).",
            "Introduce Track B 16-32 bars early (only Drums + Highs audible).",
            "At the final note/vocal of Track A, press Echo.",
            "Sharply cut Track A's fader to 0.",
            "At Track B's drop moment, sharply open Mid on Track B.",
            "Let the echo fade or manually reduce it.",
        ],
        effects_used=["echo", "filter"],
        bpm_range=(160, 180),
        key_compatibility="tritone",
        energy_delta="any",
        transition_bars=16,
        frequency_focus="mid",
        parameters=[
            TechniqueParam(name="delay_duration", label="Длительность эха", type="float", min_val=0.1, max_val=2.0, default=0.5, unit="сек"),
            TechniqueParam(name="delay_wet", label="Громкость эха", type="float", min_val=0.0, max_val=1.0, default=0.7, unit=""),
            TechniqueParam(name="trigger_bar", label="Точка включения", type="int", min_val=1, max_val=8, default=4, unit="тактов"),
            TechniqueParam(name="mid_open_speed", label="Скорость открытия Mid", type="float", min_val=0.1, max_val=1.0, default=0.5, unit="x"),
        ],
    ),

    DJTechnique(
        id="DNB-08",
        name="Phrase Match",
        category="structural",
        difficulty=3,
        level="intermediate",
        description="Mixing tracks by aligning their musical phrases (16 or 32 bar blocks).",
        description_ru="Миксование треков путём выравнивания музыкальных фраз (блоки по 16 или 32 такта).",
        description_cn="通过对齐音乐乐句（16 或 32 小节块）来混音曲目。",
        best_for="All DnB subgenres",
        when_to_use="For structured sets with clear dramaturgy. Requires phrase analysis.",
        steps=[
            "Detect phrase boundaries in both tracks (analysis.py already does this).",
            "Start Track B so its phrases align with Track A's (1st phrase with 1st, etc.).",
            "Make a smooth EQ crossfade during the aligned phrases.",
            "Use the bar grid for precise timing.",
            "Typical transition: 16 or 32 bars.",
        ],
        effects_used=["none"],
        bpm_range=(160, 180),
        key_compatibility="compatible",
        energy_delta="same",
        transition_bars=16,
        frequency_focus="all",
        parameters=[
            TechniqueParam(name="phrase_size", label="Длительность фразы", type="select", min_val=0, max_val=0, default="16", unit="", options=["8", "16", "32"]),
            TechniqueParam(name="sync_point", label="Точка синхронизации", type="select", min_val=0, max_val=0, default="start", unit="", options=["start", "drop", "break"]),
        ],
    ),

    DJTechnique(
        id="DNB-09",
        name="A Cappella Overlay",
        category="stem",
        difficulty=4,
        level="intermediate",
        description="Overlaying vocals (a cappella) from one track onto the instrumentals of another.",
        description_ru="Наложение вокала (а-капелла) одного трека на инструментал другого.",
        description_cn="将一首曲目的人声（清唱）叠加到另一首曲目的伴奏上。",
        best_for="Liquid DnB, Vocal DnB, Liquid Funk",
        when_to_use="When you have stems (vocals separated). Creates unique mashup effects.",
        steps=[
            "Separate stems for both tracks (Demucs gives vocals + instrumentals).",
            "Load Track B's vocal stem over Track A's instrumental.",
            "Crossfade vocals from Track A to Track B while keeping instrumentals stable.",
            "Ensure vocal keys are compatible or same.",
            "Blend at the phrase boundary for maximum impact.",
        ],
        effects_used=["stems"],
        bpm_range=(160, 180),
        key_compatibility="compatible",
        energy_delta="same",
        transition_bars=16,
        frequency_focus="mid",
        parameters=[
            TechniqueParam(name="vocal_vol", label="Громкость вокала", type="float", min_val=0.0, max_val=1.0, default=0.8, unit=""),
            TechniqueParam(name="vocal_eq", label="EQ вокала", type="select", min_val=0, max_val=0, default="clean", unit="", options=["clean", "telephone", "radio"]),
            TechniqueParam(name="entry_bar", label="Точка входа", type="int", min_val=1, max_val=16, default=8, unit="тактов"),
        ],
    ),

    # ═══════════════════════════════════════════════════════════════════════
    # LEVEL 3: ADVANCED
    # ═══════════════════════════════════════════════════════════════════════

    DJTechnique(
        id="DNB-10",
        name="Triple Drop",
        category="cut",
        difficulty=5,
        level="advanced",
        description="Simultaneous drop of three tracks for a massive wall of sound.",
        description_ru="Одновременный дроп трёх треков для создания массивной звуковой стены.",
        description_cn="同时 Drop 三首曲目，形成巨大的音墙。",
        best_for="DnB, Neurofunk, Jump Up (peak time)",
        when_to_use="For the finale of a set or climax moment. Maximum energy.",
        steps=[
            "Find three tracks with same BPM and compatible keys.",
            "Separate each track's frequencies: Track A = Bass only, Track B = Mids, Track C = Highs.",
            "Synchronize all three so drops align on the bar grid.",
            "Launch all three simultaneously.",
            "The result is a massive wall of sound with full frequency coverage.",
        ],
        effects_used=["none"],
        bpm_range=(160, 180),
        key_compatibility="compatible",
        energy_delta="same",
        transition_bars=4,
        frequency_focus="all",
        parameters=[
            TechniqueParam(name="phase_a", label="Смещение фаз A", type="int", min_val=0, max_val=4, default=0, unit="тактов"),
            TechniqueParam(name="phase_b", label="Смещение фаз B", type="int", min_val=0, max_val=4, default=0, unit="тактов"),
            TechniqueParam(name="phase_c", label="Смещение фаз C", type="int", min_val=0, max_val=4, default=1, unit="тактов"),
        ],
    ),

    DJTechnique(
        id="DNB-11",
        name="Loop & Roll",
        category="loop",
        difficulty=4,
        level="advanced",
        description="Looping a section of the track with simultaneous filter sweep for buildup.",
        description_ru="Зацикливание фрагмента трека с одновременным протяжением фильтра для нарастания.",
        description_cn="循环播放曲目片段，同时扫动滤波器以营造紧张感。",
        best_for="DnB, Jump Up, Neurofunk",
        when_to_use="To build energy before a drop. Creates tension and anticipation.",
        steps=[
            "Find a cool percussive/vocal fragment in the track.",
            "Activate Loop on 1/4 or 1/2 bar.",
            "Start sweeping High-Pass Filter from min to max (creates acceleration effect).",
            "At the right moment, deactivate Loop and trigger the drop.",
            "The filter sweep + loop creates maximum tension.",
        ],
        effects_used=["loop", "filter"],
        bpm_range=(160, 180),
        key_compatibility="any",
        energy_delta="low_to_high",
        transition_bars=8,
        frequency_focus="all",
        parameters=[
            TechniqueParam(name="loop_size", label="Размер лупа в тактах", type="select", min_val=0, max_val=0, default="1/2", unit="", options=["1/4", "1/2", "1"]),
            TechniqueParam(name="filter_speed", label="Скорость вращения фильтра", type="float", min_val=0.1, max_val=1.0, default=0.5, unit="x"),
        ],
    ),

    DJTechnique(
        id="DNB-12",
        name="Key Jump",
        category="eq",
        difficulty=4,
        level="advanced",
        description="Changing key by +1 or -1 on Camelot wheel to raise or lower energy.",
        description_ru="Смена тональности на +1 или -1 по колесу Камелот для повышения или понижения энергии.",
        description_cn="在 Camelot 轮上将调性升高或降低 +1/-1 以提升或降低能量。",
        best_for="All DnB subgenres",
        when_to_use="To control the crowd's mood: +1 raises energy (before climax), -1 lowers (before atmospheric moment).",
        steps=[
            "Find two tracks with Camelot difference of +1 (e.g., 4A → 5A) or -1 (4A → 3A).",
            "Make a smooth EQ crossfade between them.",
            "In the middle of the transition, add Echo or Filter to smooth the key change.",
            "The audience will feel the music has 'risen' or 'fallen' emotionally.",
        ],
        effects_used=["echo"],
        bpm_range=(160, 180),
        key_compatibility="any",
        energy_delta="any",
        transition_bars=16,
        frequency_focus="all",
        parameters=[
            TechniqueParam(name="target_key", label="Целевая тональность", type="select", min_val=0, max_val=0, default="+1", unit="", options=["+1", "-1"]),
            TechniqueParam(name="transition_bars", label="Длительность перехода", type="int", min_val=8, max_val=32, default=16, unit="тактов"),
        ],
    ),

    DJTechnique(
        id="DNB-13",
        name="Reverse Drop",
        category="effect",
        difficulty=5,
        level="advanced",
        description="Playing a track segment backwards before the drop for an intriguing effect.",
        description_ru="Воспроизведение фрагмента трека задом наперёд перед дропом для интригующего эффекта.",
        description_cn="在 Drop 之前反向播放曲目片段，制造神秘效果。",
        best_for="Experimental DnB, Artstep, Atmospheric DnB",
        when_to_use="For experimental, 'musical' sets. Creates anticipation and surprise.",
        steps=[
            "Identify the drop segment in Track B (2-4 bars before the drop).",
            "Reverse this segment (np.flip on the audio samples).",
            "Play the reversed segment for 4 bars before the drop.",
            "At the drop moment, switch to normal forward playback.",
            "The reverse sound is unusual and intriguing.",
        ],
        effects_used=["reverse"],
        bpm_range=(160, 180),
        key_compatibility="any",
        energy_delta="any",
        transition_bars=8,
        frequency_focus="all",
        parameters=[
            TechniqueParam(name="reverse_bars", label="Длительность реверса", type="int", min_val=1, max_val=4, default=2, unit="тактов"),
            TechniqueParam(name="entry_bar", label="Точка входа", type="int", min_val=1, max_val=8, default=4, unit="тактов"),
        ],
    ),

    DJTechnique(
        id="DNB-14",
        name="Fader FX Series",
        category="combo",
        difficulty=4,
        level="advanced",
        description="Sequential application of multiple effects on one track using crossfader panning.",
        description_ru="Последовательное применение нескольких эффектов к одному треку с помощью панорамирования кроссфейдера.",
        description_cn="使用交叉推子平移依次对同一曲目应用多种效果。",
        best_for="Atmospheric DnB, Liquid DnB, Intelligent DnB",
        when_to_use="For atmospheric, psychedelic sections. Creates a sense of movement.",
        steps=[
            "Assign Echo to the left channel, Reverb to the right channel.",
            "Slowly pan the crossfader from center to left, then to right.",
            "This creates a 'panoramic' sound movement effect.",
            "Effects must be synchronized by BPM.",
            "Duration: 16-32 bars.",
        ],
        effects_used=["echo", "reverb"],
        bpm_range=(160, 180),
        key_compatibility="any",
        energy_delta="any",
        transition_bars=16,
        frequency_focus="effect",
        parameters=[
            TechniqueParam(name="fx_sequence", label="Тип эффектов", type="select", min_val=0, max_val=0, default="Echo→Reverb", unit="", options=["Echo→Reverb", "Filter→Echo", "Reverb→Filter"]),
            TechniqueParam(name="fader_speed", label="Скорость движения фейдера", type="float", min_val=0.1, max_val=1.0, default=0.3, unit="x"),
        ],
    ),

    DJTechnique(
        id="DNB-15",
        name="Mashup Transition",
        category="stem",
        difficulty=5,
        level="advanced",
        description="Creating a live mashup from two tracks that were never mixed together before.",
        description_ru="Создание лайв-мэшапа из двух треков, которые раньше никогда не миксовались вместе.",
        description_cn="从两首从未一起混音过的曲目创建现场 Mashup。",
        best_for="All DnB subgenres (for WOW effect)",
        when_to_use="When you want to create a unique, memorable moment. Requires preparation.",
        steps=[
            "Separate stems for both tracks (Demucs).",
            "Start Track A's instrumental.",
            "Fade in Track B's vocal stem over Track A.",
            "At the phrase boundary, crossfade: Track A's vocal fades out, Track B's instrumental fades in.",
            "Result: a live mashup with unique vocal/instrumental combinations.",
        ],
        effects_used=["stems"],
        bpm_range=(160, 180),
        key_compatibility="compatible",
        energy_delta="same",
        transition_bars=16,
        frequency_focus="all",
        parameters=[
            TechniqueParam(name="mix_ratio", label="Уровни микса", type="float", min_val=0.0, max_val=1.0, default=0.5, unit=""),
            TechniqueParam(name="eq_balance", label="EQ каждого трека", type="select", min_val=0, max_val=0, default="A-vocals-B-instr", unit="", options=["A-vocals-B-instr", "B-vocals-A-instr", "custom"]),
        ],
    ),

    # ═══════════════════════════════════════════════════════════════════════
    # LEVEL 4: EXPERIMENTAL
    # ═══════════════════════════════════════════════════════════════════════

    DJTechnique(
        id="DNB-16",
        name="Time Stretch Glitch",
        category="effect",
        difficulty=5,
        level="experimental",
        description="Extreme time stretching or compression with glitch artifacts.",
        description_ru="Экстремальное растяжение или сжатие времени с артефактами глитча.",
        description_cn="极端的时间拉伸或压缩，产生故障伪影。",
        best_for="Breakbeat DnB, Experimental, Glitch Hop",
        when_to_use="For breakbeat experiments. Use sparingly on short sections.",
        steps=[
            "Take a 2-4 bar segment of Track A.",
            "Apply extreme time stretch (e.g., 174 BPM → 140 BPM or vice versa).",
            "This creates digital artifacts and 'glitch' sounds.",
            "Use the glitched segment as a transition element.",
            "Cut to Track B at the drop point.",
        ],
        effects_used=["glitch"],
        bpm_range=(160, 180),
        key_compatibility="any",
        energy_delta="any",
        transition_bars=4,
        frequency_focus="all",
        parameters=[
            TechniqueParam(name="stretch_factor", label="Степень растяжения", type="float", min_val=0.3, max_val=2.0, default=0.5, unit="x"),
            TechniqueParam(name="glitch_depth", label="Глубина глитча", type="float", min_val=0.0, max_val=1.0, default=0.5, unit=""),
        ],
    ),

    DJTechnique(
        id="DNB-17",
        name="Stutter Effect",
        category="loop",
        difficulty=4,
        level="experimental",
        description="Rapid repetition of a short fragment (1/4 → 1/8 → 1/16) with shrinking loop size.",
        description_ru="Быстрое повторение короткого фрагмента (1/4 → 1/8 → 1/16) с уменьшающимся размером лупа.",
        description_cn="快速重复短片段（1/4 → 1/8 → 1/16），循环长度递减。",
        best_for="DnB, Neurofunk, Techstep",
        when_to_use="Before a powerful drop. Creates a 'revving engine' effect.",
        steps=[
            "Activate Loop on 1/4 bar.",
            "After 2 repetitions, shrink to 1/8 bar.",
            "After 2 more repetitions, shrink to 1/16 bar.",
            "On the last repetition, cut the loop and trigger the drop.",
            "The shrinking loop sounds like an engine revving up.",
        ],
        effects_used=["loop"],
        bpm_range=(160, 180),
        key_compatibility="any",
        energy_delta="low_to_high",
        transition_bars=4,
        frequency_focus="all",
        parameters=[
            TechniqueParam(name="initial_loop", label="Начальный размер лупа", type="select", min_val=0, max_val=0, default="1/4", unit="", options=["1/4", "1/2"]),
            TechniqueParam(name="repetitions", label="Количество повторений", type="int", min_val=2, max_val=8, default=4, unit="раз"),
        ],
    ),

    DJTechnique(
        id="DNB-18",
        name="Texture Layering",
        category="ambient",
        difficulty=4,
        level="experimental",
        description="Layering atmospheric textures (noise, ambience, field recordings) over the track.",
        description_ru="Наложение атмосферных текстур (шум, эмбиент, записанные звуки) поверх трека.",
        description_cn="在曲目上叠加氛围纹理（噪声、环境音、实地录音）。",
        best_for="Liquid DnB, Deep DnB, Intelligent DnB",
        when_to_use="To create a unique atmosphere. Works best in breakdown/intro sections.",
        steps=[
            "Generate or load an atmospheric texture (synthesized noise, rain, city ambience).",
            "Apply High-Pass Filter at 500 Hz (to avoid clashing with bass).",
            "Layer the texture over the main track at low volume.",
            "The texture should be in the same key or atonal (noise).",
            "Creates a stereo 'picture' effect.",
        ],
        effects_used=["texture", "filter"],
        bpm_range=(160, 180),
        key_compatibility="any",
        energy_delta="any",
        transition_bars=16,
        frequency_focus="high",
        parameters=[
            TechniqueParam(name="texture_vol", label="Громкость текстуры", type="float", min_val=0.0, max_val=0.5, default=0.15, unit=""),
            TechniqueParam(name="highpass_hz", label="Частотная обрезка", type="int", min_val=200, max_val=2000, default=500, unit="Hz"),
        ],
    ),

    DJTechnique(
        id="DNB-19",
        name="Scratch In",
        category="effect",
        difficulty=5,
        level="experimental",
        description="Introducing a track via digital scratch simulation (reverse + pitch ramp).",
        description_ru="Введение трека через цифровой скретч-симуляцию (реверс + нарастание тона).",
        description_cn="通过数字搓盘模拟引入曲目（反向 + 音高爬升）。",
        best_for="Jump Up, Crosshair, Neurofunk (hip-hop vibe)",
        when_to_use="For a hard-hitting, old-school vibe. Simulates vinyl scratching.",
        steps=[
            "Take the first beat of Track B.",
            "Create a scratch effect: short reverse segment + pitch ramp.",
            "Repeat the scratch 2-4 times with decreasing intensity.",
            "On the last scratch, release into normal playback.",
            "The result sounds like a DJ scratching a record.",
        ],
        effects_used=["scratch"],
        bpm_range=(160, 180),
        key_compatibility="any",
        energy_delta="any",
        transition_bars=4,
        frequency_focus="all",
        parameters=[
            TechniqueParam(name="scratch_style", label="Стиль скретча", type="select", min_val=0, max_val=0, default="baby", unit="", options=["baby", "chirp", "transform"]),
            TechniqueParam(name="scratch_bars", label="Длительность", type="int", min_val=1, max_val=4, default=2, unit="тактов"),
        ],
    ),

    DJTechnique(
        id="DNB-20",
        name="Tone Play",
        category="pitch",
        difficulty=5,
        level="experimental",
        description="Artificial key shifting using pitch bend during the mix.",
        description_ru="Искусственное смещение тональности с помощью пич-бенда во время микса.",
        description_cn="在混音过程中使用音高弯曲进行人工调性偏移。",
        best_for="All DnB subgenres (experimental moments)",
        when_to_use="To create a 'music flying/falling' effect. Use only at phrase endings.",
        steps=[
            "Enable Master Tempo (to preserve BPM during pitch shift).",
            "Gradually bend pitch up or down by 1-3 semitones.",
            "This creates the effect of music 'rising' or 'falling'.",
            "Use only at the end of a phrase — otherwise it sounds off-key.",
            "Combine with filter sweep for maximum effect.",
        ],
        effects_used=["pitch_bend"],
        bpm_range=(160, 180),
        key_compatibility="any",
        energy_delta="any",
        transition_bars=8,
        frequency_focus="all",
        parameters=[
            TechniqueParam(name="pitch_shift", label="Смещение тона", type="float", min_val=-3.0, max_val=3.0, default=2.0, unit="полутона"),
            TechniqueParam(name="bend_bars", label="Длительность", type="int", min_val=2, max_val=8, default=4, unit="тактов"),
        ],
    ),
]

# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

_TECHNIQUE_MAP = {t.id: t for t in TECHNIQUES}


def get_technique(technique_id: str) -> Optional[DJTechnique]:
    """Get a technique by its ID (e.g., 'DNB-07')."""
    return _TECHNIQUE_MAP.get(technique_id)


def get_techniques_by_category(category: str) -> List[DJTechnique]:
    """Get all techniques in a category."""
    return [t for t in TECHNIQUES if t.category == category]


def get_techniques_by_difficulty(max_difficulty: int) -> List[DJTechnique]:
    """Get all techniques up to a difficulty level."""
    return [t for t in TECHNIQUES if t.difficulty <= max_difficulty]


def get_techniques_for_keys(key_compat: str) -> List[DJTechnique]:
    """Get techniques suitable for a key compatibility type."""
    return [t for t in TECHNIQUES if t.key_compatibility in (key_compat, "any")]


def list_techniques() -> List[dict]:
    """Return all techniques as dicts for API response."""
    return [
        {
            "id": t.id,
            "name": t.name,
            "category": t.category,
            "difficulty": t.difficulty,
            "level": t.level,
            "description": t.description,
            "description_ru": t.description_ru,
            "description_cn": t.description_cn,
            "best_for": t.best_for,
            "when_to_use": t.when_to_use,
            "effects_used": t.effects_used,
            "bpm_range": list(t.bpm_range),
            "key_compatibility": t.key_compatibility,
            "energy_delta": t.energy_delta,
            "transition_bars": t.transition_bars,
            "frequency_focus": t.frequency_focus,
            "steps": t.steps,
            "parameters": [
                {
                    "name": p.name,
                    "label": p.label,
                    "type": p.type,
                    "min_val": p.min_val,
                    "max_val": p.max_val,
                    "default": p.default,
                    "unit": p.unit,
                    "options": p.options,
                }
                for p in t.parameters
            ],
        }
        for t in TECHNIQUES
    ]
