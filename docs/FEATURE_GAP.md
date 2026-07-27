# Feature Gap: Backend → Frontend

What the backend computes/exposes vs. what the React frontend actually renders.

> Last verified: 2026-07-27

---

## Summary

| # | Feature | Backend | Frontend | Status |
|---|---------|---------|----------|--------|
| 1 | EQ strategy | `transition_intel.py` computes `default / bass_heavy / vocal_priority / aggressive` | `RemixControls.tsx:168` select dropdown | ✅ Exposed |
| 2 | Crossfade type | `transition_intel.py` computes `extended / standard / sharp` | `RemixControls.tsx:180` select dropdown | ✅ Exposed |
| 3 | LUFS target | `master_mix()` accepts `target_lufs` param | `RemixControls.tsx:148` range slider (−16…−6) | ✅ Exposed (default −8) |
| 4 | Demucs model | `StemSplitRequest.model` (`htdemucs / htdemucs_ft / mdx_extra`) | `LibraryAtlas.tsx:516` state var, **no UI control** | ❌ Hardcoded |
| 5 | Stem enhancement | `StemSplitRequest.enhance` | `LibraryAtlas.tsx:517` state var, **no UI control** | ❌ Hardcoded |
| 6 | Smart transitions | `DJChainRequest.smart_transitions` | `SetBuilder.tsx:282` **not sent in chain request** | ❌ Missing |
| 7 | Phrase boundaries | `TrackStructure.phrase_boundaries` | `TrackStructureView.tsx:61,181` rendered as SVG markers | ✅ Rendered |

---

## Details

### 1. EQ Strategy — ✅ Exposed

**Backend** (`transition_intel.py:47`):
```python
eq_strategy: str  # "default" | "bass_heavy" | "vocal_priority" | "aggressive"
```
Computed per-transition based on priority rules (tritone → default, energy drop → bass_heavy, experimental → aggressive).

**API** (`schemas.py:251`):
```python
eq_strategy: str = Field("auto", description="EQ strategy: 'auto' | 'default' | 'bass_heavy' | 'vocal_priority' | 'aggressive'")
```

**Frontend** (`RemixControls.tsx:168-177`):
```tsx
<select value={value.eq_strategy} onChange={...}>
  {EQ_STRATEGIES.map(s => <option key={s} value={s}>{label(s)}</option>)}
</select>
```
Options: `auto, default, bass_heavy, vocal_priority, aggressive`.

**Used in**: MixDeck (`launchRemix`), SetBuilder (`launchChain`).

---

### 2. Crossfade Type — ✅ Exposed

**Backend** (`transition_intel.py:45`):
```python
crossfade_type: str  # "extended" | "standard" | "sharp"
```
Controls the crossfade envelope shape in `dj_engine.py`.

**API** (`schemas.py:255`):
```python
crossfade_type: str = Field("auto", description="Crossfade shape: 'auto' | 'standard' | 'extended' | 'sharp'")
```

**Frontend** (`RemixControls.tsx:180-189`):
```tsx
<select value={value.crossfade_type} onChange={...}>
  {CROSSFADE_TYPES.map(t => <option key={t} value={t}>{label(t)}</option>)}
</select>
```
Options: `auto, standard, extended, sharp`.

---

### 3. LUFS Target — ✅ Exposed

**Backend** (`mastering.py:322`):
```python
def master_mix(audio, sr, target_lufs: float = -14.0, ...)
```

**API** (`schemas.py:246`):
```python
target_lufs: float = Field(-8.0, ge=-16.0, le=-6.0)
```

**Frontend** (`RemixControls.tsx:148-165`):
```tsx
<input type="range" min={-16} max={-6} step={1} value={value.target_lufs} />
```
Default: −8 (loud DJ mix). Range: −16 (quiet/streaming) to −6 (loud).

---

### 4. Demucs Model — ❌ No UI Control

**Backend** (`schemas.py:203`):
```python
model: str = Field("htdemucs", description="htdemucs | htdemucs_ft | mdx_extra")
```

**Frontend** (`LibraryAtlas.tsx:516`):
```tsx
const [stemModel, setStemModel] = useState('htdemucs')
```
State exists but **no dropdown/toggle renders it**. Always sends `htdemucs`.

**What's missing**: A `<select>` in the stems expansion row or a global stems config panel.

---

### 5. Stem Enhancement — ❌ No UI Control

**Backend** (`schemas.py:202`):
```python
enhance: bool = Field(True, description="Run audio enhancement chain before Demucs")
```

**Frontend** (`LibraryAtlas.tsx:517`):
```tsx
const [stemEnhance, setStemEnhance] = useState(true)
```
State exists but **no toggle renders it**. Always sends `true`.

**What's missing**: A toggle switch next to the stem split button.

---

### 6. Smart Transitions — ❌ Not Sent

**Backend** (`schemas.py:280`):
```python
smart_transitions: bool = Field(True, description="Use AI to select optimal technique per transition")
```

**Frontend** (`SetBuilder.tsx:282-292`):
```tsx
const res = await remixApi.chain(set.map(e => e.name), {
  transition_bars: chainOpts.transition_bars,
  preset: chainOpts.preset,
  // ... other fields ...
  // smart_transitions is MISSING from this object
})
```
The field exists in `DJChainRequest` but SetBuilder **never includes it** in the API call. Backend defaults to `True`.

**What's missing**: A toggle in SetBuilder UI + passing it in the chain request.

---

### 7. Phrase Boundaries — ✅ Rendered

**Backend** (`dj_analysis.py`): Computed as part of `TrackStructure`.

**Frontend types** (`types/index.ts:247`):
```ts
phrase_boundaries: number[]
```

**Rendered** (`TrackStructureView.tsx:61,181`):
```tsx
const phraseBoundaries = structure.phrase_boundaries || []
// ... SVG markers rendered at each boundary position
```

---

## Migration Notes

When adding UI controls for the missing features:

1. **Demucs model + Enhancement** — Add to `LibraryAtlas.tsx` near the stem split button (line ~180). Use a small `<select>` for model and a toggle for enhance.

2. **Smart transitions** — Add to `RemixControls.tsx` as a toggle, or create a separate `ChainControls` component. Pass the value in `SetBuilder.tsx:282`.

3. **Consider**: Moving stem config to a shared context/store so it persists across sessions (currently resets to defaults on page reload).
