# DARAVE
<img width="119" height="118" alt="image" src="https://github.com/user-attachments/assets/573d6515-baaf-4ab7-b7fb-3aabd4388139" />


**Digital Audio Rave — an environment for studying DJing and experimenting with mixes.**

DARAVE is a research platform where tracks are not just blended but broken down into structured layers and controlled as a multidimensional system. It analyzes each track by a set of features and combines them into a unified mapping for building sets.

### Track Analysis and Mapping

Each track goes through a custom analysis algorithm:

- **Duration**
- **KEY**
- **BPM**
- **Energy**
- **Genres / Styles / Phrases**
- **Cues**
- **Vox Detection**
- **STEMS** — split into layers (drums, bass, vocals, other)

All of this is combined into a **single mapping** used to build the mix.

---


## Current Implementation

- **Sound engine:** [Mixxx](https://mixxx.org/) (open-source)
- **Control:** virtual MIDI controller
- **Transport:** `rtmidi` + `SysEX`

At this stage, DARAVE is a layer on top of Mixxx that provides non-standard mixing logic via MIDI.

---

## Roadmap

### 1. Own DAW with MIDI 2.0

Building a dedicated digital audio workstation with full **MIDI 2.0** support.

- DSP with ASIO support not on hardware but in the web  (Web Audio / WebAssembly)
- Communication via **UMP** (Universal MIDI Packet)
- 16 groups × 16 channels = **256 logical channels**

### 2. HYPERSTEMS / 4D STEMS

A unique mixing feature that extends standard STEMS into a four-dimensional system.

**Formula:**

```
4 decks × 4 stems × 4 transitions × 4 loops = 4⁴ = 256
```

| Axis | Meaning |
|------|---------|
| 4 decks | D1, D2, D3, D4 — sound sources |
| 4 stems | Drums, bass, vocals, other |
| 4 transitions | 1→2, 2→3, 3→4, 4→1 |
| 4 loops | The cycle repeats 4 times |

**Result:**
- **16 physical audio streams** (4 decks × 4 stems playing simultaneously)
- **256 logical MIDI channels** (16 groups × 16 channels of MIDI 2.0)

### 3. Autonomous Presets from Library (Auto-Sampling)

Creating autonomous presets based on the existing track library.

**Idea:** the system automatically assembles presets from library material — no manual sampling required.

**What it provides:**

- **Auto-sampling** — DARAVE slices fragments from library tracks by given rules (by phrases, by stems, by energy, by cue points).
- **Autonomous presets** — ready-made sets of stems/loops/phrases collected from different tracks but sounding as a single whole.
- **Analysis-driven** — presets are built with KEY, BPM, Energy, and genre in mind, so they are compatible in tonality and tempo.
- **Library as source** — no need to search for individual samples: DARAVE finds suitable fragments in the existing collection.
- **Live application** — presets are loaded into HYPERSTEMS and participate in 4D rotation alongside regular tracks.

**Example:**

```
Preset "Warehouse 4AM"
├── Drums   → fragment from track A (bpm 128, key Am)
├── Bass    → fragment from track B (bpm 128, key Am)
├── Vocals  → fragment from track C (bpm 128, key Am)
└── Other   → fragment from track D (bpm 128, key Am)
```

All four stems come from different tracks but are compatible in key, tempo, and energy. The preset is ready for rotation in DARAVE 4D.

### 4. Auto-Mix Planning and Assembly

Planning and automatic assembly of a clean mix using dozens of adaptive techniques (EQ/FX/STEMS) selected based on the analysis of the tracks.

**In development.**

---

## DARAVE 4D

**DARAVE 4D** is an architecture where DJing moves from EQ and STEMS to multidimensional sound control.

```
EQ → STEMS → HYPERSTEMS → DARAVE 4D
 3      4         16          256
```

- **EQ** — 3 bands
- **STEMS** — 4 layers
- **HYPERSTEMS** — 16 audio streams
- **DARAVE 4D** — 256 states

---

## Stem Rotation Algorithm

### Circular Shift

Decks form a ring:

```
D1 → D2 → D3 → D4 → D1
```

Stems move around the ring step by step.

### Step-by-Step Transition (Recommended)

Step = 1 bar. Only **one stem** moves per step.

```
Step 1: Drums   D1 → D2
Step 2: Bass    D1 → D2
Step 3: Vocals  D1 → D2
Step 4: Other   D1 → D2
```

In 4 steps, all stems shift by one deck. This creates a smooth, musically coherent morph.

### Transition Modes

| Mode | Description | Character |
|------|-------------|-----------|
| All 4 at once | Sharp shift | Drop, section change |
| One by one | Smooth shift | Hypnotic morph |
| Two by two | Compromise | Fast but not abrupt |
| Probabilistic | Random choice | Generative, live performance |

---

## Track Replacement

In a ring of 4 decks, there are never empty slots. Track replacement is done **stem by stem**.

### Method 1: 5th Deck as Buffer

```
ring = [D1, D2, D3, D4]
buffer = D5

for stem in [Drums, Bass, Vocals, Other]:
    wait until old_stem is on ring[3]
    crossfade(old_stem @ ring[3] → new_stem @ buffer)
    ring[3], buffer = buffer, ring[3]
```

In 4 steps the track is fully replaced without stopping.

### Method 2: A/B Layers (for MIDI 2.0)

Each stem slot has two layers: **A (old)** and **B (new)**. At the swap point, a crossfade A → B is performed. 4 decks are sufficient.

---

## MIDI 2.0 Map (Draft)

**16 groups × 16 channels = 256 addresses.**

Groups = 4 decks × 4 stems:

```
D1.S1  D1.S2  D1.S3  D1.S4
D2.S1  D2.S2  D2.S3  D2.S4
D3.S1  D3.S2  D3.S3  D3.S4
D4.S1  D4.S2  D4.S3  D4.S4
```

Channels within a group = 4 transitions × 4 loops:

```
P1K1  P1K2  P1K3  P1K4
P2K1  P2K2  P2K3  P2K4
P3K1  P3K2  P3K3  P3K4
P4K1  P4K2  P4K3  P4K4
```

Each address is a point in 4D space:

```
(deck, stem, transition, loop)
```

---

## Philosophy

- Before: 2 channels + EQ
- Then: STEMS — 4 layers
- Now: HYPERSTEMS — 256 states in 4D

The controller can be small. The hypercube unfolds through scenes, macros, and automation.
4 dimensions, 16 physically channels, 256 states, one rave.

---

## Status

| Component | Status |
|-----------|--------|
| Mixxx + rtmidi + SysEX | Working |
| Track analysis (KEY, BPM, Energy, STEMS) | **Done** |
| Auto-mix planning and assembly | **Done** |
| Adaptive technicians in mix clipping and djing theory research | in drafting |
| MIDI 2.0 / UMP | Planned 2026 Q4 |
| Own DAW | Planned 2027 Q1 |
| Autonomous presets (auto-sampling) | Planned 2027 Q1 |
| HYPERSTEMS / 4D STEMS | Planned 2027 |



---

## License

TBD

## Links

- [Mixxx](https://mixxx.org/)
- [MIDI 2.0 Specification](https://midi.org/midi-2-0)
- [rtmidi](https://github.com/thestk/rtmidi)
