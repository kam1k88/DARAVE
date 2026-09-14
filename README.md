# DARAVE

<img width="119" height="118" alt="DARAVE" src="https://github.com/user-attachments/assets/573d6515-baaf-4ab7-b7fb-3aabd4388139" />

**Digital Audio Rave — an environment for studying DJing, analyzing music, and experimenting with algorithmic mixing.**

DARAVE is a research platform for exploring DJing as a **multidimensional audio-control problem**.

Instead of treating a track as a single audio object, DARAVE decomposes it into structured musical layers and describes them through a unified mapping of:

* BPM
* KEY
* Energy
* Genre / Style
* Phrases
* Cue points
* Vocal presence
* STEMS

The resulting mapping is used to plan, control, and eventually automate transitions between musical states.

The long-term goal of DARAVE is to move from traditional:

```text
Track → EQ → Transition
```

towards:

```text
Track
  ↓
Analysis
  ↓
STEMS + Musical Structure
  ↓
Unified Mapping
  ↓
Transition States
  ↓
Multidimensional Mixing
  ↓
DARAVE 4D
```

---

# Concept

Traditional DJing primarily operates on complete tracks and a relatively small set of controls.

DARAVE treats the mix as a system of independently controllable musical components.

```text
Traditional DJ
──────────────

TRACK A ────────────────┐
                         ├── MIX
TRACK B ────────────────┘


DARAVE
──────

TRACK A
 ├── Drums
 ├── Bass
 ├── Vocals
 └── Other

TRACK B
 ├── Drums
 ├── Bass
 ├── Vocals
 └── Other
        │
        ▼
   STATE SPACE
        │
        ▼
     MIXING
```

This makes it possible to transition between tracks **stem by stem**, generate combinations of material from different tracks, and eventually construct transitions automatically from musical analysis.

---

# Track Analysis

Every track is processed by the DARAVE analysis pipeline.

The analysis produces a unified representation of the track:

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

This information is not treated as independent metadata.

It becomes part of a **single musical mapping** used by the mixing system.

### Analysis parameters

| Feature       | Purpose                                     |
| ------------- | ------------------------------------------- |
| Duration      | Track and section timing                    |
| BPM           | Tempo compatibility                         |
| KEY           | Harmonic compatibility                      |
| Energy        | Energy progression and transition selection |
| Genre         | Style compatibility                         |
| Style         | More detailed musical classification        |
| Phrases       | Musical structure and transition timing     |
| Cues          | Important positions in the track            |
| Vox Detection | Vocal presence and vocal-aware transitions  |
| STEMS         | Independent musical layers                  |

---

# Current Architecture

DARAVE currently operates as a control and research layer on top of Mixxx.

```text
┌──────────────────────────────┐
│           DARAVE             │
│                              │
│ Analysis                     │
│ Mapping                      │
│ Mixing Logic                 │
│ Transition Algorithms       │
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

* **Audio engine:** Mixxx
* **Control:** Virtual MIDI controller
* **Transport:** `rtmidi`
* **Protocol:** MIDI / SysEx
* **Analysis:** DARAVE analysis pipeline

At the current stage, DARAVE is intentionally implemented as a layer above an existing DJ engine.

The purpose is to develop and test the mixing logic before moving to a dedicated audio environment.

---

# DARAVE Mixing Model

The fundamental abstraction in DARAVE is the **musical state**.

A state is not simply:

```text
Track A
```

It can contain:

```text
Track A
 ├── Drums
 ├── Bass
 ├── Vocals
 └── Other

+
Track B
 ├── Drums
 ├── Bass
 ├── Vocals
 └── Other
```

The system can therefore transform:

```text
STATE A
   ↓
stem transition
   ↓
STATE B
```

instead of performing a conventional full-track crossfade.

---

# STEMS

DARAVE uses four primary stem categories:

```text
S1 = Drums
S2 = Bass
S3 = Vocals
S4 = Other
```

This produces four independently controllable layers per deck.

With four decks:

```text
4 decks × 4 stems = 16 physical audio streams
```

```text
D1 ── S1 S2 S3 S4
D2 ── S1 S2 S3 S4
D3 ── S1 S2 S3 S4
D4 ── S1 S2 S3 S4
```

The important distinction is:

> **16 physical audio streams do not mean 16 independent tracks.**

They are the physical audio representation of a larger logical control system.

---

# HYPERSTEMS

**HYPERSTEMS** extend the conventional four-stem model into a multidimensional transition system.

The architecture is based on:

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

| Dimension  | Meaning                     |
| ---------- | --------------------------- |
| Deck       | D1, D2, D3, D4              |
| Stem       | Drums, Bass, Vocals, Other  |
| Transition | Transition path             |
| Loop       | Repetition / cycle position |

This produces:

```text
256 logical states
```

while maintaining:

```text
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

HYPERSTEMS therefore describe a **control space**, not 256 simultaneous audio channels.

---

# DARAVE 4D

DARAVE 4D is the long-term architecture in which DJing becomes multidimensional sound control.

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

The objective is to create a system where a relatively small physical controller can navigate a much larger musical state space.

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

# Stem Rotation

One of the core DARAVE transition mechanisms is **Stem Rotation**.

The four decks form a circular topology:

```text
D1 → D2 → D3 → D4
↑                 ↓
└─────────────────┘
```

Instead of replacing an entire track at once, individual stems move through the ring.

---

## Step-by-Step Rotation

The recommended transition step is **one bar**.

Only one stem changes at each step.

```text
Step 1
Drums   D1 → D2

Step 2
Bass    D1 → D2

Step 3
Vocals  D1 → D2

Step 4
Other   D1 → D2
```

After four steps, all four stems have moved one deck position.

```text
D1
 ↓
D2
 ↓
D3
 ↓
D4
 ↓
D1
```

This creates a gradual musical morph instead of an abrupt track replacement.

---

# Transition Modes

DARAVE supports several conceptual transition modes.

| Mode          | Description                           | Character             |
| ------------- | ------------------------------------- | --------------------- |
| All 4 at once | All stems transition simultaneously   | Drop / section change |
| One by one    | One stem per step                     | Smooth / hypnotic     |
| Two by two    | Two stems per step                    | Fast but controlled   |
| Probabilistic | Stem selection determined dynamically | Generative / live     |

The transition mode can eventually be selected automatically based on the musical context.

For example:

```text
LOW ENERGY DIFFERENCE
        ↓
one-by-one
        ↓
smooth morph


HIGH ENERGY DIFFERENCE
        ↓
all-at-once
        ↓
strong section change
```

---

# Track Replacement

A four-deck ring normally has no empty position.

Therefore, replacing a track is performed **stem by stem**.

The old track gradually leaves the ring while the new track enters it.

---

## Method 1 — Buffer Deck

A fifth deck can act as a temporary buffer.

```text
ring   = [D1, D2, D3, D4]
buffer = D5
```

The conceptual algorithm:

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

The ring never needs to become empty.

---

## Method 2 — A/B Layers

For the future MIDI 2.0 architecture, each stem slot can contain two logical layers:

```text
Stem Slot

A = old material
B = new material
```

At the replacement point:

```text
A ────────────╲
               ╲
                ╲
                 ╲
B ────────────────╱
```

The system performs a controlled:

```text
A → B
```

crossfade.

This allows the four-deck architecture to perform replacement without requiring a permanent fifth physical deck.

---

# Autonomous Presets

DARAVE is designed to eventually generate presets directly from the user's existing music library.

This removes the need to manually search for individual samples.

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

---

# Auto-Sampling

DARAVE can select fragments according to analysis-driven rules.

Possible selection criteria include:

* phrases
* stems
* energy
* cue points
* BPM
* KEY
* genre
* style

The objective is not simply to extract random samples.

The system searches for fragments that are musically compatible.

---

# Autonomous Presets

A preset can be assembled from material belonging to several different tracks.

Example:

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

Example compatibility:

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

The result is a new composite musical object:

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

These presets can participate in the same rotation system as conventional tracks.

---

# Auto-Mix Planning

The next level is automatic transition planning.

DARAVE analyzes the available musical material and determines how a transition should be constructed.

Conceptually:

```text
TRACK A
   │
   ▼
ANALYSIS
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
   ├── STEM RELATION
   └── STYLE
        │
        ▼
TECHNIQUE SELECTION
        │
        ├── EQ
        ├── STEM
        ├── FX
        ├── LOOP
        ├── CROSSFADE
        └── ROTATION
             │
             ▼
          MIX PLAN
             │
             ▼
        MIX ASSEMBLY
```

The system should eventually determine not only:

> **Which track comes next?**

but also:

> **Which parts of that track should enter, when should they enter, and which transition technique should be used?**

---

# Adaptive Mixing

DARAVE's long-term mixing engine is based on adaptive transition techniques.

Instead of applying the same transition template to every pair of tracks, the system evaluates the musical context.

Possible parameters include:

```text
KEY
BPM
ENERGY
PHRASE
VOCAL ACTIVITY
STEM DENSITY
TRACK STRUCTURE
GENRE
STYLE
```

The transition engine can then select an appropriate strategy.

For example:

```text
compatible key
+
similar BPM
+
similar energy
+
phrase boundary
        ↓
smooth stem rotation
```

while:

```text
large energy difference
+
section boundary
        ↓
short transition
+
FX
+
simultaneous stem replacement
```

This is the direction of DARAVE's adaptive DJing research.

---

# MIDI 2.0 Architecture

The planned dedicated DARAVE environment will use **MIDI 2.0** and **UMP (Universal MIDI Packet)** as the primary control architecture.

The logical control space is organized as:

```text
16 groups × 16 channels
=
256 logical addresses
```

The first level maps the deck and stem:

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

Each group contains 16 logical channels representing:

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

Thus every logical address can be represented as:

```text
(deck, stem, transition, loop)
```

or:

```text
(D, S, P, K)
```

---

# 4D State Space

The DARAVE 4D state can therefore be represented as:

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

Total state space:

```text
|S| = 4 × 4 × 4 × 4
    = 256
```

The physical system remains:

```text
16 audio streams
```

while the control layer can navigate:

```text
256 logical states
```

This separation between **audio topology** and **control topology** is one of the central ideas of DARAVE.

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

DARAVE can instead expose higher-level operations:

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

The controller becomes an interface to the underlying state machine.

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

# Architecture Roadmap

## Phase 1 — Mixxx Integration

Current foundation:

```text
DARAVE
   ↓
rtmidi / SysEx
   ↓
Mixxx
```

Purpose:

* validate analysis
* test stem control
* develop transition algorithms
* experiment with rotation
* test automatic mixing logic

---

## Phase 2 — MIDI 2.0

Planned:

* MIDI 2.0
* UMP
* 16 groups
* 16 channels per group
* 256 logical addresses
* higher-level controller abstraction

Target:

**2026 Q4**

---

## Phase 3 — Dedicated DAW

DARAVE will eventually move from a Mixxx-based control layer to its own audio environment.

Target:

**2027 Q1**

The planned architecture is based on:

* Web Audio
* WebAssembly
* browser-based DSP
* ASIO-oriented integration where applicable
* MIDI 2.0 / UMP

The objective is to create an audio engine designed around DARAVE's multidimensional mixing model rather than adapting a conventional DJ application.

---

## Phase 4 — Autonomous Presets

Target:

**2027 Q1**

Development of:

* library-driven auto-sampling
* autonomous presets
* phrase-aware fragment selection
* stem compatibility analysis
* KEY/BPM/Energy matching
* automatic preset generation

---

## Phase 5 — HYPERSTEMS / DARAVE 4D

Target:

**2027**

Development of:

* four-deck stem topology
* stem rotation
* multidimensional transitions
* 256 logical states
* scenes
* macros
* automation
* autonomous preset integration

---

# Research Direction

DARAVE is not intended to be only another DJ application.

The project investigates whether DJ mixing can be represented as a structured computational problem.

Instead of:

```text
DJ
 ↓
manually selects track
 ↓
manually mixes track
```

DARAVE explores:

```text
Music Library
      ↓
Analysis
      ↓
Musical Representation
      ↓
Candidate States
      ↓
Transition Search
      ↓
Transition Scoring
      ↓
Technique Selection
      ↓
Mix Generation
```

The research questions include:

* How can musical compatibility be represented computationally?
* How should KEY, BPM and ENERGY influence transition selection?
* How can phrase structure be incorporated into automatic mixing?
* How should STEMS be combined without destroying musical coherence?
* How can transitions be represented as state changes?
* How can a controller navigate a multidimensional state space?
* How can existing tracks become autonomous musical building blocks?
* How far can DJ mixing be automated while preserving musical intent?

---

# Architecture Overview

The complete long-term DARAVE architecture can be summarized as:

```text
                    DARAVE
                       │
              ┌────────┴────────┐
              │                 │
          ANALYSIS          LIBRARY
              │                 │
              └────────┬────────┘
                       │
                       ▼
               UNIFIED MAPPING
                       │
          ┌────────────┼────────────┐
          │            │            │
        TRACKS       STEMS       PRESETS
          │            │            │
          └────────────┼────────────┘
                       │
                       ▼
                MIXING ENGINE
                       │
          ┌────────────┼────────────┐
          │            │            │
       ROTATION      MORPH       REPLACE
          │            │            │
          └────────────┼────────────┘
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
                    AUDIO
```

---

# Philosophy

DARAVE evolves DJing through several abstraction levels:

```text
2 channels + EQ
        ↓
4 STEMS
        ↓
16 physical audio streams
        ↓
256 logical states
        ↓
DARAVE 4D
```

The important transition is not simply:

```text
more channels
```

but:

```text
more dimensions of control
```

The physical interface can remain small.

The complexity exists inside the software model.

```text
Small Controller
      ↓
Scenes
      ↓
Macros
      ↓
Automation
      ↓
4D State Space
      ↓
Musical Transformation
```

**Four dimensions.
Sixteen physical streams.
Two hundred fifty-six logical states.
One rave.**

---

# Status

| Component                               | Status                |
| --------------------------------------- | --------------------- |
| Mixxx + rtmidi + SysEx                  | **Working**           |
| Track analysis — KEY                    | **Done**              |
| Track analysis — BPM                    | **Done**              |
| Track analysis — Energy                 | **Done**              |
| STEMS analysis / separation             | **Done**              |
| Auto-mix planning and assembly          | **Done**              |
| Adaptive mixing / DJing theory research | **In development**    |
| MIDI 2.0 / UMP                          | **Planned — 2026 Q4** |
| Own DAW                                 | **Planned — 2027 Q1** |
| Autonomous presets / auto-sampling      | **Planned — 2027 Q1** |
| HYPERSTEMS / DARAVE 4D                  | **Planned — 2027**    |

---

# Current vs. Target Architecture

```text
CURRENT
───────

Music Library
     ↓
DARAVE Analysis
     ↓
DARAVE Logic
     ↓
MIDI / SysEx
     ↓
rtmidi
     ↓
Mixxx
     ↓
Audio


TARGET
──────

Music Library
     ↓
DARAVE Analysis
     ↓
Unified Musical Mapping
     ↓
Mix Planning
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

# Project Status

DARAVE is currently a **research and development project**.

The existing Mixxx implementation provides a working environment for validating the core concepts.

The long-term objective is a dedicated system where:

```text
analysis
+
musical structure
+
STEMS
+
transition logic
+
automation
+
MIDI 2.0
+
audio processing
```

form one integrated architecture.

---

# License

TBD

---

# Links

* [Mixxx](https://mixxx.org/)
* [MIDI 2.0](https://midi.org/midi-2-0)
* [rtmidi](https://github.com/thestk/rtmidi)

