# DARAVE

<img width="119" height="118" alt="DARAVE" src="https://github.com/user-attachments/assets/573d6515-baaf-4ab7-b7fb-3aabd4388139" />

**Digital Audio Rave — an environment for studying DJing, analyzing music, and experimenting with algorithmic mixing.**

DARAVE is a research platform for studying DJing as a **structured, adaptive mixing problem**.

The current system combines conventional harmonic mixing with adaptive FX techniques and selective STEMS processing. The long-term goal is to move beyond hard compatibility rules and develop a system capable of creating musical transitions between **any tracks, regardless of KEY, BPM, or genre**.

The central idea is not to replace traditional DJ techniques, but to build a system that can determine **which technique is appropriate for a particular transition**.

---

# Concept

Traditional DJ mixing can be represented as:

```text
TRACK A ────────────────┐
                         ├── MIX
TRACK B ────────────────┘
```

For compatible tracks, DARAVE follows the same fundamental approach:

```text
TRACK A
   │
   ├── BPM
   ├── KEY
   ├── Phrase
   └── Energy
          │
          ▼
    EQ + Camelot
          │
          ▼
         MIX
          ▲
          │
    EQ + Camelot
          │
          │
TRACK B
```

The difference is that DARAVE can analyze the relationship between the tracks and choose a different transition strategy when conventional harmonic mixing is insufficient.

```text
                    TRACK A + TRACK B
                           │
                           ▼
                    MUSICAL ANALYSIS
                           │
                           ▼
                 COMPATIBILITY ANALYSIS
                           │
              ┌────────────┼────────────┐
              │            │            │
              ▼            ▼            ▼
             EQ           FX          STEMS
          Camelot      Advanced       Conflict
           Mixing      Techniques     Removal
              │            │            │
              └────────────┼────────────┘
                           ▼
                          MIX
```

### The current paradigm

**EQ / Camelot** is the standard method for clean harmonic mixing.

**FX** are used for non-standard transitions and for bridging tracks that cannot be mixed cleanly using conventional techniques.

**STEMS** are used selectively to remove conflicting musical layers.

Therefore, DARAVE is **not currently a "STEMS mixing system"**.

STEMS are a tool inside a broader adaptive mixing architecture.

---

# Mixing Hierarchy

DARAVE uses the least invasive technique that can produce a musically acceptable transition.

```text
              TRANSITION
                   │
                   ▼
          Can EQ + Camelot work?
             │             │
            YES            NO
             │             │
             ▼             ▼
        EQ / Camelot       FX
             │             │
             │             ▼
             │       Can the conflict
             │         be resolved?
             │          │       │
             │         YES      NO
             │          │       │
             │          ▼       ▼
             │        STEMS   Advanced
             │        removal techniques
             │          │       │
             └──────────┴───────┘
                        │
                        ▼
                       MIX
```

The objective is not to make every transition complicated.

The objective is to select the **simplest effective technique** for the musical situation.

---

# 1. Standard Mixing — EQ + Camelot

The first and most important DARAVE technique is conventional harmonic DJ mixing.

Compatible tracks can be mixed using:

- BPM synchronization
- KEY compatibility
- Camelot Wheel
- phrase alignment
- EQ
- standard crossfading

```text
TRACK A
   │
   │ EQ / Camelot
   ▼
  MIX
   ▲
   │ EQ / Camelot
   │
TRACK B
```

If two tracks already work together harmonically, there is no reason to introduce unnecessary processing.

DARAVE therefore preserves traditional DJ practice as the baseline.

---

# 2. FX — Non-Standard Mixing

When conventional harmonic mixing is insufficient, DARAVE can use FX and other transition techniques.

FX can be used to:

- create transitional space
- mask incompatible material
- reshape energy
- bridge different musical sections
- create rhythmic transitions
- move between otherwise incompatible tracks
- support structural transitions

Conceptually:

```text
Compatible
    │
    ▼
EQ + Camelot
    │
    ▼
Clean Mix
```

while:

```text
Incompatible
    │
    ▼
FX / Transition Technique
    │
    ▼
Controlled Transition
    │
    ▼
Mix
```

FX are therefore not merely decorative effects.

Within DARAVE they are part of the **transition toolkit**.

---

# 3. STEMS — Conflict Removal

STEMS are currently used as a **conflict-resolution mechanism**.

A track can be separated into:

```text
S1 = Drums
S2 = Bass
S3 = Vocals
S4 = Other
```

For example, two tracks may have compatible structure but contain conflicting bass or vocal content.

Instead of processing the entire track, DARAVE can selectively remove the problematic layer.

```text
TRACK A
 ├── Drums
 ├── Bass       ← conflict
 ├── Vocals
 └── Other
```

can become:

```text
TRACK A'
 ├── Drums
 ├── Vocals
 └── Other
```

STEMS are therefore not the default mixing paradigm.

Their current purpose is:

> **Remove the layer that prevents two musical elements from working together.**

---

# Track Analysis

Each track goes through the DARAVE analysis pipeline.

The analysis produces a unified musical representation:

```text
TRACK
 │
 ├── Duration
 ├── BPM
 ├── KEY
 ├── Energy
 ├── Genre
 ├── Style
 ├── Phrases
 ├── Cue Points
 ├── Vocal Detection
 │
 └── STEMS
      ├── Drums
      ├── Bass
      ├── Vocals
      └── Other
```

The analysis is used to determine the relationship between tracks and to select an appropriate mixing strategy.

### Analysis parameters

| Feature | Purpose |
|---|---|
| Duration | Track and section timing |
| BPM | Tempo relationship |
| KEY | Harmonic relationship |
| Energy | Energy progression and transition selection |
| Genre | Style context |
| Style | Detailed musical classification |
| Phrases | Musical structure and transition timing |
| Cue Points | Important transition positions |
| Vocal Detection | Vocal-aware transitions |
| STEMS | Selective conflict removal |

---

# Unified Musical Mapping

DARAVE combines track analysis into a single mapping rather than treating each feature independently.

```text
TRACK
  │
  ├── BPM
  ├── KEY
  ├── ENERGY
  ├── PHRASE
  ├── GENRE / STYLE
  ├── VOCALS
  ├── CUES
  └── STEMS
          │
          ▼
   UNIFIED MAPPING
          │
          ▼
   TRANSITION MODEL
```

This mapping provides the foundation for automatic transition planning.

---

# Current Architecture

DARAVE currently operates as a control and research layer on top of Mixxx.

```text
┌──────────────────────────────┐
│            DARAVE            │
│                              │
│ Track Analysis               │
│ Musical Mapping              │
│ Mixing Logic                 │
│ Transition Planning          │
│ Adaptive Techniques          │
└──────────────┬───────────────┘
               │
             MIDI
               │
        rtmidi / SysEx
               │
               ▼
┌──────────────────────────────┐
│            MIXXX             │
│                              │
│ Audio Engine                 │
│ Decks                        │
│ Playback                     │
│ Mixing                       │
└──────────────────────────────┘
```

### Current stack

- **Audio engine:** Mixxx
- **Control:** Virtual MIDI controller
- **Transport:** `rtmidi`
- **Protocol:** MIDI / SysEx
- **Analysis:** DARAVE analysis pipeline

The Mixxx implementation provides the environment in which DARAVE's mixing logic can be developed and tested before moving to a dedicated audio environment.

---

# Adaptive Mixing

The long-term DARAVE mixing engine is based on **adaptive transition selection**.

Instead of applying the same transition template to every pair of tracks, DARAVE evaluates their musical relationship.

Possible parameters include:

```text
KEY
BPM
ENERGY
PHRASE
VOCAL ACTIVITY
STEM CONFLICTS
TRACK STRUCTURE
GENRE
STYLE
```

The system can then choose between:

```text
EQ
 │
 ├── Camelot / harmonic mixing
 │
 ├── FX
 │
 ├── STEM conflict removal
 │
 ├── Loops
 │
 ├── Crossfade
 │
 └── Advanced transition techniques
```

The important principle is:

> **The track relationship determines the transition technique.**

---

# Auto-Mix Planning and Assembly

DARAVE can automatically plan and assemble mixes using analysis-driven transition techniques.

The conceptual pipeline is:

```text
TRACK A
   +
TRACK B
   │
   ▼
ANALYSIS
   │
   ▼
COMPATIBILITY MODEL
   │
   ▼
CANDIDATE TRANSITIONS
   │
   ▼
TRANSITION SCORING
   │
   ├── KEY
   ├── BPM
   ├── ENERGY
   ├── PHRASE
   ├── VOCALS
   ├── STEM CONFLICTS
   └── STYLE
        │
        ▼
TECHNIQUE SELECTION
        │
        ├── EQ
        ├── Camelot
        ├── FX
        ├── STEMS
        ├── LOOP
        ├── CROSSFADE
        └── ADVANCED TECHNIQUE
             │
             ▼
          MIX PLAN
             │
             ▼
        MIX ASSEMBLY
```

The goal is to answer two questions:

> **Which track comes next?**

and:

> **How should the transition between the two tracks be performed?**

---

# Breaking the KEY Constraint

Traditional harmonic mixing uses KEY compatibility as a major constraint.

DARAVE's future objective is to make KEY a **parameter of transition planning rather than a hard limitation**.

Instead of:

```text
Compatible KEY
      ↓
Mix

Incompatible KEY
      ↓
Do not mix
```

the future model becomes:

```text
KEY relationship
      ↓
Transition analysis
      ↓
Technique selection
      ↓
Make the transition musically acceptable
```

Possible techniques can include:

- harmonic compatibility when available
- selective stem removal
- FX transitions
- phrase-based transitions
- loops
- structural transitions
- other adaptive techniques

The objective is not necessarily to make two tracks share the same harmonic relationship.

The objective is:

> **Find a transition that works despite the difference.**

---

# Breaking the BPM Constraint

The same principle applies to BPM.

Traditional DJ mixing works best when tracks have similar tempos.

DARAVE aims to treat BPM as a **transition parameter rather than an absolute restriction**.

```text
Small BPM difference
        ↓
Beatmatching / normal mixing
```

```text
Moderate BPM difference
        ↓
Tempo adaptation
+
phrase-aware transition
```

```text
Large BPM difference
        ↓
FX / loop / breakdown
+
structural transition
```

The future system should be able to determine how to move between very different tempos instead of simply rejecting the pair.

The target is:

> **BPM should influence the transition strategy, not decide whether a transition is possible at all.**

---

# Breaking the Genre Constraint

Genre is also treated as a feature rather than an absolute compatibility rule.

Instead of:

```text
House → House
Techno → Techno
Drum & Bass → Drum & Bass
```

DARAVE can evaluate the actual musical properties of the material:

```text
Genre
+
Style
+
BPM
+
KEY
+
Energy
+
Rhythm
+
Structure
+
Vocals
+
STEMS
        │
        ▼
Transition Model
```

A large genre difference does not automatically mean that a transition is impossible.

The system can search for an appropriate bridge between the two musical states.

---

# Universal Mixing

The long-term goal of DARAVE is to progressively remove the traditional compatibility restrictions.

Current DJ logic:

```text
KEY
+
BPM
+
Genre
+
Phrase
        ↓
Can these tracks be mixed?
```

Future DARAVE logic:

```text
TRACK A
   +
TRACK B
   │
   ▼
ANALYSIS
   │
   ▼
What prevents a clean transition?
   │
   ├── KEY?
   ├── BPM?
   ├── Energy?
   ├── Rhythm?
   ├── Structure?
   ├── Vocals?
   └── Layer conflict?
        │
        ▼
Which technique can overcome it?
        │
        ▼
Adaptive Transition
        │
        ▼
MIX
```

The ultimate objective is:

> **To make it possible to mix arbitrary tracks across arbitrary genres, without requiring matching KEY or BPM.**

This is the central long-term research direction of DARAVE.

---

# HYPERSTEMS

HYPERSTEMS are a future extension of the DARAVE control architecture.

They are not a replacement for the current EQ/FX/STEMS mixing hierarchy.

Instead, HYPERSTEMS provide a multidimensional control model for future advanced transitions.

The planned architecture is:

```text
4 decks
×
4 stems
×
4 transitions
×
4 loops
=
4⁴
=
256
```

The four dimensions are:

| Axis | Meaning |
|---|---|
| Deck | D1, D2, D3, D4 |
| Stem | Drums, Bass, Vocals, Other |
| Transition | Transition path / position |
| Loop | Loop / cycle position |

This produces:

```text
256 logical states
```

while maintaining:

```text
4 decks × 4 stems
=
16 physical audio streams
```

The distinction is fundamental:

```text
PHYSICAL AUDIO
4 decks × 4 stems
= 16 streams

LOGICAL CONTROL
4 decks × 4 stems × 4 transitions × 4 loops
= 256 states
```

HYPERSTEMS therefore describe a **logical control space**, not 256 simultaneous audio channels.

---

# Stem Rotation

One planned HYPERSTEMS mechanism is **Stem Rotation**.

The four decks form a ring:

```text
D1 → D2 → D3 → D4
↑                 ↓
└─────────────────┘
```

Individual stems can move through this topology instead of replacing an entire track at once.

## Step-by-Step Rotation

The recommended conceptual transition step is one bar.

```text
Step 1:
Drums   D1 → D2

Step 2:
Bass    D1 → D2

Step 3:
Vocals  D1 → D2

Step 4:
Other   D1 → D2
```

After four steps, all four stems have moved one deck position.

This creates a gradual musical morph rather than an abrupt full-track replacement.

---

# Transition Modes

Planned HYPERSTEMS transition modes include:

| Mode | Description | Character |
|---|---|---|
| All 4 at once | All stems transition simultaneously | Drop / section change |
| One by one | One stem per step | Smooth / hypnotic |
| Two by two | Two stems per step | Fast but controlled |
| Probabilistic | Stem selection determined dynamically | Generative / live |

The transition mode can eventually be selected automatically from musical context.

For example:

```text
LOW ENERGY DIFFERENCE
        ↓
ONE-BY-ONE
        ↓
SMOOTH MORPH
```

or:

```text
HIGH ENERGY DIFFERENCE
        ↓
ALL-AT-ONCE
        ↓
STRONG SECTION CHANGE
```

---

# Track Replacement

A four-deck ring normally has no empty position.

Future HYPERSTEMS architecture therefore supports track replacement through controlled layer transitions.

## Method 1 — Buffer Deck

A fifth deck can act as a temporary buffer:

```text
ring   = [D1, D2, D3, D4]
buffer = D5
```

Conceptually:

```text
for stem in [Drums, Bass, Vocals, Other]:

    wait until old_stem reaches ring[3]

    crossfade(
        old_stem @ ring[3]
        →
        new_stem @ buffer
    )

    ring[3], buffer = buffer, ring[3]
```

After four transitions, the original track has been completely replaced.

## Method 2 — A/B Layers

For the planned MIDI 2.0 architecture, each stem slot can contain two logical layers:

```text
Stem Slot

A = old material
B = new material
```

At the swap point:

```text
A ────────────╲
               ╲
                ╲
                 ╲
B ────────────────╱
```

A controlled:

```text
A → B
```

crossfade can replace the old material without requiring a permanent fifth physical deck.

---

# Autonomous Presets

DARAVE is planned to generate autonomous presets directly from the existing music library.

The library becomes a source of musical building blocks.

```text
LIBRARY
   │
   ▼
ANALYSIS
   │
   ├── BPM
   ├── KEY
   ├── ENERGY
   ├── PHRASES
   ├── STEMS
   └── CUES
        │
        ▼
FRAGMENT SELECTION
        │
        ▼
AUTONOMOUS PRESET
```

No manual sample search is required.

---

# Auto-Sampling

DARAVE can select fragments according to analysis-driven rules.

Possible criteria include:

- phrases
- stems
- energy
- cue points
- BPM
- KEY
- genre
- style

The objective is not simply to extract random samples.

The system should select fragments that are compatible with the intended musical context.

---

# Autonomous Preset Example

A preset can be assembled from different tracks:

```text
Preset: "Warehouse 4AM"

Drums
  ← Track A

Bass
  ← Track B

Vocals
  ← Track C

Other
  ← Track D
```

For example:

```text
Track A
128 BPM
Am

Track B
128 BPM
Am

Track C
128 BPM
Am

Track D
128 BPM
Am
```

The result is a composite musical object:

```text
             PRESET
                │
       ┌────────┼────────┐
       ▼        ▼        ▼
     Track A  Track B  Track C  Track D
       │        │        │        │
     Drums     Bass     Vox      Other
       └────────┼────────┘
                ▼
           HYPERSTEMS
```

These presets are planned to participate in the same advanced transition and rotation system as regular tracks.

---

# MIDI 2.0 Architecture

The planned dedicated DARAVE environment will use **MIDI 2.0** and **UMP (Universal MIDI Packet)** as the primary control architecture.

The logical control space is organized as:

```text
16 groups × 16 channels
=
256 logical addresses
```

The first level maps deck and stem:

```text
D1.S1   D1.S2   D1.S3   D1.S4
D2.S1   D2.S2   D2.S3   D2.S4
D3.S1   D3.S2   D3.S3   D3.S4
D4.S1   D4.S2   D4.S3   D4.S4
```

Where:

```text
S1 = Drums
S2 = Bass
S3 = Vocals
S4 = Other
```

Each group contains 16 logical channels:

```text
P1K1  P1K2  P1K3  P1K4
P2K1  P2K2  P2K3  P2K4
P3K1  P3K2  P3K3  P3K4
P4K1  P4K2  P4K3  P4K4
```

Where:

```text
P = transition position
K = loop position
```

Every logical address can therefore be represented as:

```text
(deck, stem, transition, loop)
```

or:

```text
(D, S, P, K)
```

---

# DARAVE 4D

DARAVE 4D is the long-term architecture for multidimensional control of musical transitions.

```text
EQ
 ↓
STEMS
 ↓
HYPERSTEMS
 ↓
DARAVE 4D
```

Conceptually:

```text
EQ          → 3-band control
STEMS       → 4 musical layers
HYPERSTEMS  → 16 physical streams
DARAVE 4D   → 256 logical states
```

The number 256 is not the objective by itself.

The objective is to create a system where a small physical controller can navigate a much larger virtual state space.

```text
              DARAVE 4D
                  │
         ┌────────┼────────┐
         │        │        │
       Scenes   Macros  Automation
         │        │        │
         └────────┼────────┘
                  │
                  ▼
             Audio States
```

---

# 4D State Model

The planned DARAVE 4D state can be represented as:

```text
State = (Deck, Stem, Transition, Loop)
```

with:

```text
Deck       ∈ {1,2,3,4}
Stem       ∈ {1,2,3,4}
Transition ∈ {1,2,3,4}
Loop       ∈ {1,2,3,4}
```

Total logical state space:

```text
|S| = 4 × 4 × 4 × 4
    = 256
```

Physical audio:

```text
16 audio streams
```

Logical control:

```text
256 states
```

This separation between **physical audio topology** and **logical control topology** is a central architectural principle of DARAVE.

---

# From Controller to State Machine

A conventional DJ controller exposes physical parameters:

```text
Faders
EQ
Knobs
Pads
Jog wheels
```

DARAVE can eventually expose higher-level operations:

```text
SCENE
MACRO
ROTATE
MORPH
REPLACE
LOOP
TRANSITION
ENERGY
```

The controller becomes an interface to the underlying transition and state system.

```text
Physical Controller
        │
        ▼
     MIDI 2.0
        │
        ▼
  DARAVE State Model
        │
        ▼
    HYPERSTEMS
        │
        ▼
      AUDIO
```

A small controller can therefore control a much larger virtual space.

---

# Roadmap

## Phase 1 — Current Mixxx Integration

Current foundation:

```text
DARAVE
   ↓
rtmidi / SysEx
   ↓
Mixxx
```

Purpose:

- validate analysis
- develop mixing logic
- test adaptive techniques
- test automatic mix planning
- experiment with FX strategies
- experiment with selective STEMS processing

---

## Phase 2 — MIDI 2.0

Planned:

- MIDI 2.0
- UMP
- 16 groups
- 16 channels per group
- 256 logical addresses
- higher-level controller abstraction

Target:

**2026 Q4**

---

## Phase 3 — Dedicated DAW

DARAVE will eventually move from a Mixxx-based control layer to its own audio environment.

Target:

**2027 Q1**

Planned technologies include:

- Web Audio
- WebAssembly
- browser-based DSP
- ASIO-oriented integration where applicable
- MIDI 2.0 / UMP

The objective is to create an audio engine designed around DARAVE's mixing model rather than adapting a conventional DJ application.

---

## Phase 4 — Autonomous Presets

Target:

**2027 Q1**

Development of:

- library-driven auto-sampling
- autonomous presets
- phrase-aware fragment selection
- stem compatibility analysis
- KEY/BPM/Energy matching
- automatic preset generation

---

## Phase 5 — HYPERSTEMS / DARAVE 4D

Target:

**2027**

Development of:

- four-deck stem topology
- stem rotation
- multidimensional transitions
- 256 logical states
- scenes
- macros
- automation
- autonomous preset integration

---

# Research Direction

DARAVE investigates whether DJ mixing can be represented as a structured computational problem.

Traditional DJing asks:

> **Can these two tracks be mixed?**

DARAVE aims to ask:

> **What prevents these two tracks from being mixed, and what technique can overcome that limitation?**

The research model is:

```text
TRACK A
   +
TRACK B
   │
   ▼
ANALYSIS
   │
   ▼
MUSICAL RELATIONSHIP
   │
   ▼
CONSTRAINT DETECTION
   │
   ├── KEY
   ├── BPM
   ├── ENERGY
   ├── RHYTHM
   ├── STRUCTURE
   ├── VOCALS
   └── LAYER CONFLICT
        │
        ▼
TECHNIQUE SEARCH
        │
        ├── EQ / Camelot
        ├── FX
        ├── STEMS
        ├── LOOP
        ├── STRUCTURAL TRANSITION
        └── ADVANCED TECHNIQUES
             │
             ▼
       ADAPTIVE TRANSITION
             │
             ▼
            MIX
```

The central research questions include:

- How can musical compatibility be represented computationally?
- How should KEY and BPM influence transition selection without becoming hard restrictions?
- How can phrase structure be incorporated into automatic mixing?
- How can STEMS be used selectively to resolve conflicts?
- How can FX be selected according to musical context?
- How can transitions be represented as changes between musical states?
- How can a controller navigate a multidimensional transition space?
- How can existing tracks become autonomous musical building blocks?
- How far can DJ mixing be automated while preserving musical intent?

---

# Architecture Overview

The long-term DARAVE architecture can be summarized as:

```text
                         DARAVE
                            │
                ┌───────────┴───────────┐
                │                       │
            ANALYSIS                 LIBRARY
                │                       │
                └───────────┬───────────┘
                            │
                            ▼
                    UNIFIED MAPPING
                            │
                            ▼
                  COMPATIBILITY MODEL
                            │
                            ▼
                   TRANSITION ENGINE
                            │
              ┌─────────────┼─────────────┐
              │             │             │
              ▼             ▼             ▼
             EQ            FX           STEMS
          Camelot      Techniques     Conflict
           Mixing                    Removal
              │             │             │
              └─────────────┼─────────────┘
                            │
                            ▼
                    ADAPTIVE MIXING
                            │
                            ▼
                       HYPERSTEMS
                            │
                            ▼
                       DARAVE 4D
                            │
                            ▼
                    256 LOGICAL STATES
                            │
                            ▼
                      MIDI 2.0 / UMP
                            │
                            ▼
                    DARAVE AUDIO ENGINE
                            │
                            ▼
                           AUDIO
```

---

# Current vs. Target Architecture

## Current

```text
Music Library
     ↓
DARAVE Analysis
     ↓
Unified Mapping
     ↓
Mix Planning
     ↓
EQ / Camelot
     +
FX
     +
Selective STEMS
     ↓
MIDI / SysEx
     ↓
rtmidi
     ↓
Mixxx
     ↓
Audio
```

## Target

```text
Music Library
     ↓
DARAVE Analysis
     ↓
Unified Musical Mapping
     ↓
Compatibility / Constraint Analysis
     ↓
Transition Search
     ↓
Adaptive Technique Selection
     ↓
EQ / FX / STEMS / Advanced Techniques
     ↓
HYPERSTEMS
     ↓
DARAVE 4D State Machine
     ↓
MIDI 2.0 / UMP
     ↓
DARAVE Audio Engine
     ↓
Web Audio / WebAssembly
     ↓
Audio
```

---

# Philosophy

DARAVE is not about replacing DJing with STEMS.

It is about evolving the **logic of compatibility**.

```text
TRADITIONAL
───────────

KEY + BPM
    ↓
Camelot
    ↓
EQ
    ↓
MIX
```

```text
DARAVE — CURRENT
─────────────────

KEY + BPM + Energy + Phrase
            ↓
    Compatibility Analysis
            ↓
       ┌────┼────┐
       ▼    ▼    ▼
      EQ   FX  STEMS
       │    │    │
       └────┼────┘
            ▼
           MIX
```

```text
DARAVE — FUTURE
───────────────

ANY TRACK
    +
ANY TRACK
    │
    ▼
ANALYSIS
    │
    ▼
WHAT MAKES THEM INCOMPATIBLE?
    │
    ▼
HOW CAN THE CONFLICT BE OVERCOME?
    │
    ▼
ADAPTIVE TECHNIQUE
    │
    ▼
MIX
```

The fundamental shift is:

```text
"Can these tracks be mixed?"
              ↓
"How can these tracks be made mixable?"
```

The ultimate objective is a DJ system capable of transitioning between **any musical material**, regardless of:

```text
KEY
BPM
GENRE
STYLE
```

while using the appropriate combination of conventional mixing, FX, selective STEMS processing, structural techniques, and future multidimensional control.

---

# Status

| Component | Status |
|---|---|
| Mixxx + rtmidi + SysEx | **Working** |
| Track analysis — KEY | **Done** |
| Track analysis — BPM | **Done** |
| Track analysis — Energy | **Done** |
| Track analysis — Genre / Style | **Done** |
| Track analysis — Phrases / Cues | **Done** |
| Vocal detection | **Done** |
| STEMS analysis / separation | **Done** |
| Auto-mix planning and assembly | **Done** |
| Adaptive mixing / DJing theory research | **In development** |
| MIDI 2.0 / UMP | **Planned — 2026 Q4** |
| Own DAW | **Planned — 2027 Q1** |
| Autonomous presets / auto-sampling | **Planned — 2027 Q1** |
| HYPERSTEMS / DARAVE 4D | **Planned — 2027** |

---

# Project Status

DARAVE is currently a **research and development project**.

The Mixxx implementation provides a working environment for developing and validating the core concepts.

The long-term objective is a dedicated system in which:

```text
analysis
+
musical structure
+
compatibility analysis
+
transition selection
+
EQ
+
FX
+
selective STEMS
+
automation
+
MIDI 2.0
+
multidimensional control
+
audio processing
```

form one integrated architecture.

---

# License

TBD

---

# Links

- [Mixxx](https://mixxx.org/)
- [MIDI 2.0](https://midi.org/midi-2-0)
- [rtmidi](https://github.com/thestk/rtmidi)
