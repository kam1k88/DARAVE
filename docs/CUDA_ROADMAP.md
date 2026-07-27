# CUDA / GPU Roadmap — DARAVE DSP

Цель: ускорить CPU-bound DSP-пайплайн за счёт расширения `scripts/core/gpu.py` torch-based примитивами. Без отдельного C++/CUDA проекта — всё через PyTorch + torch.compile.

---

## 1. Текущее состояние

### Уже на GPU (`gpu.py`, 375 строк)

| Примитив | Функция | Фолбэк |
|---|---|---|
| STFT / ISTFT | `gpu_stft()` | librosa |
| Time-stretch | `gpu_time_stretch()` | librosa |
| Resample | `gpu_resample()` | librosa |
| FIR-фильтры | `gpu_filter()` (conv1d) | scipy |
| Cosine similarity | `gpu_cosine_similarity()` | numpy |

### Примитивы, не требующие porting (уже в cuDNN через torch)

- `sin`, `cos`, `tanh` (oscillator, LFO, softclipper)
- STFT (через `gpu_stft()`)
- `conv1d` (через `gpu_filter()`)

### GPU-покрытие пайплайна

| Файл | Строк | GPU | CPU-bound瓶颈 |
|---|---|---|---|
| `gpu.py` | 375 | ✅ 100% | — |
| `dj_engine.py` | 1616 | 🟡 2 call-site | `_make_hp_ramp`, `_apply_filter_sweep` |
| `mastering.py` | 751 | ❌ 0% | `_smooth_gain_envelope`, `apply_limiter` |
| `dj_effects.py` | 828 | ❌ 0% | `flanger`, `phaser`, `vinyl_stop` |
| `audio_enhance.py` | 418 | ❌ 0% | `apply_noise_gate`, `apply_compression` |
| `beat_synth.py` | 365 | ❌ 0% | `render_beat` (pattern stamping) |

---

## 2. Боттл-neck'и — детальный разбор

### 2.1 `_smooth_gain_envelope()` — `mastering.py:184-240`

**Суть:** Рекурсивный one-pole envelope follower. Направление (attack/release) определяется на каждом сэмпле.

```
gain[i] = alpha * gain[i-1] + (1 - alpha) * target[i]
```

- **Numba-путь** (строки 219-230): `@njit` цикл, но всё ещё последовательный
- **Python-fallback** (строки 232-238): то же, на порядок медленнее
- **Вызывается из:** `apply_limiter()` (строка 291)

**GPU-стратегия:** Parallel scan (prefix sum) с direction-dependent coefficients. Algoritmy: Blelloch scan или state-space model через `torch.cumsum` + piecewise coefficients.

### 2.2 `apply_noise_gate()` — `audio_enhance.py:130-156`

**Суть:** Per-hop (10 мс) one-pole smoother для gate envelope.

```
for i in range(n_hops):
    gain = release * gain + (1 - release) * target
    out[start:end] *= gain
```

- ~4410 итераций/секунду аудио (при 44100 Гц, hop=10 мс)
- Каждый hop независим по RMS, но envelope — sequential

**GPU-стратегия:** Batched hop-RMS на GPU (vectorized `unfold` → `mean`), затем parallel scan для envelope.

### 2.3 `apply_compression()` — `audio_enhance.py:163-209`

**Суть:** Аналогичный envelope follower с attack/release switch и soft-knee.

```
coeff = attack_c if target_gr > gain_db else release_c
gain_db = coeff * gain_db + (1 - coeff) * target_gr
```

- ~8820 итераций/секунду (hop=5 мс)
- Direction-dependent coefficient (как в `_smooth_gain_envelope`)

**GPU-стратегия:** Та же parallel scan + piecewise coefficients.

### 2.4 `_make_hp_ramp()` — `dj_engine.py:122-148`

**Суть:** 16 чанков, каждый со своим Butterworth-фильтром.

```
for i in range(num_chunks):  # num_chunks=16
    b, a = butter_highpass(freq, sr)
    chunk = lfilter(b, a, chunk)
```

- 16 последовательных `lfilter` с пересчитанными коэффициентами

**GPU-стратегия:** `gpu_filter()` для каждого чанка (параллельно через batched conv1d) или segment-wise STFT filtering.

### 2.5 `effect_flanger()` — `dj_effects.py:291-336`

**Суть:** Delay line с LFO-модуляцией + feedback.

```python
for i in range(n):
    delay_samples = int((lfo[i] * 0.5 + 0.5) * max_delay_s)
    delayed[i + delay_samples] = audio[i]
for i in range(max_delay_s, n + max_delay_s):
    delayed[i] += delayed[i - max_delay_s] * feedback * 0.3
```

- 2 цикла по N сэмплов, feedback dependency: `delayed[i]` зависит от `delayed[i - max_delay_s]`

**GPU-стратегия:** Строго speaking, flanger с feedback невозможно параллелить полностью. Варианты:
1. Parallel delay-line write (без feedback) — fully vectorizable
2. Chunk-based feedback (разбить на чанки, feedback внутри чанка — parallel между чанками)
3. Оставить на CPU если feedback crucial

### 2.6 `effect_phaser()` — `dj_effects.py:339-383`

**Суть:** N poles × allpass recursion.

```python
for pole in range(n_poles):        # 4-6 полюсов
    for i in range(1, n):          # N сэмплов
        y[i] = coeff[i] * y[i-1] + result[i] - coeff[i] * result[i-1]
```

- n_poles × N последовательных операций

**GPU-стратегия:** Все allpass-фильтры (по полюсам) независимы → batched parallel. Внутри каждого allpass — sequential (one-pole), но можно vectorize через `torch.cumsum` для time-varying coefficients.

### 2.7 `effect_vinyl_stop()` — `dj_effects.py:386-433`

**Суть:** Non-linear read position (speed decreases quadratically).

```python
for i in range(stop_samples):
    speed = 1.0 - (i / stop_samples) ** 2
    src_pos = int(start + i * speed)
    result[idx] = audio[src_pos] * (1.0 - progress)
```

- Non-linear index → `torch.gather` / advanced indexing

**GPU-стратегия:** Precompute all `src_pos` как tensor, затем `torch.gather(audio, src_pos)` — fully vectorizable.

### 2.8 `render_beat()` — `beat_synth.py:205-277`

**Суть:** Двойной цикл `bars × pattern_hits`, non-contiguous writes.

```python
for bar in range(bars):
    for inst, step, vel in pattern:
        output[position:end] += sound[:length] * vel
```

- 4-16 баров × 16-32 хита = 64-512 итераций
- Non-contiguous writes в output

**GPU-стратегия:** Precompute all positions/lengths как tensors, batch scatter-add через `torch.index_add_`.

---

## 3. Архитектура решения

### Принцип: расширять `gpu.py`, не создавать `darave_cpp/`

```
scripts/core/gpu.py  ← расширять
    ├── gpu_envelope_follower()    — новый
    ├── gpu_gate()                 — новый
    ├── gpu_compressor()           — новый
    ├── gpu_flanger()              — новый
    ├── gpu_phaser()               — новый
    ├── gpu_vinyl_stop()           — новый
    ├── gpu_beat_stamp()           — новый
    └── gpu_hp_ramp()              — новый
```

Каждая функция:
1. Принимает `np.ndarray`, возвращает `np.ndarray`
2. Автоматически выбирает GPU/CPU (как существующие `gpu_stft` и т.д.)
3. Фолбэк на numpy/scipy если torch недоступен

### Паттерн (повторяет существующий стиль)

```python
def gpu_envelope_follower(
    signal: np.ndarray,
    target: np.ndarray,
    alpha_attack: float,
    alpha_release: float,
    device: str | None = None,
) -> np.ndarray:
    """One-pole envelope follower with parallel scan on GPU."""
    torch = _import_torch()
    if torch is None or device == "cpu":
        return _cpu_envelope_follower(signal, target, alpha_attack, alpha_release)

    dev = torch.device(device or get_device())
    sig_t = to_tensor(signal, device=dev)
    tgt_t = to_tensor(target, device=dev)

    # GPU implementation (parallel scan)
    ...

    return to_numpy(result)
```

---

## 4. Дорожная карта

### Фаза 1: Envelope Follower (приоритет: критический)

**Цель:** Ускорить mastering + audio_enhance (основной боттл-neck для render latency).

| Задача | Файл | Сложность | Время |
|---|---|---|---|
| `gpu_envelope_follower()` | `gpu.py` | Средняя | 2-3 дня |
| `gpu_gate()` | `gpu.py` | Низкая | 0.5 дня |
| `gpu_compressor()` | `gpu.py` | Средняя | 1 день |
| Интеграция в `mastering.py` | `mastering.py` | Низкая | 0.5 дня |
| Интеграция в `audio_enhance.py` | `audio_enhance.py` | Низкая | 0.5 дня |
| Тесты | `tests/` | Средняя | 1 день |

**Критерии приёмки:**
- [ ] `gpu_envelope_follower` проходит все тесты `_smooth_gain_envelope` (numpy-ref match)
- [ ] Ускорение ≥ 5× на GPU vs numba-CPU на аудио > 10 сек
- [ ] Фолбэк на CPU работает без torch
- [ ] Максимальное отклонение от CPU-ref < 1e-6 (absolute)

### Фаза 2: DJ Effects (приоритет: высокий)

**Цель:** Ускорить flanger, phaser, vinyl_stop — основные realtime effects.

| Задача | Файл | Сложность | Время |
|---|---|---|---|
| `gpu_vinyl_stop()` | `gpu.py` | Низкая | 0.5 дня |
| `gpu_flanger()` | `gpu.py` | Высокая | 2 дня |
| `gpu_phaser()` | `gpu.py` | Средняя | 1.5 дня |
| Интеграция в `dj_effects.py` | `dj_effects.py` | Низкая | 0.5 дня |
| Тесты | `tests/` | Средняя | 1 день |

**Критерии приёмки:**
- [ ] `gpu_vinyl_stop`: fully vectorizable, ускорение ≥ 10×
- [ ] `gpu_flanger`: chunk-based feedback, ускорение ≥ 3× (с feedback)
- [ ] `gpu_phaser`: batched allpass, ускорение ≥ 5×
- [ ] Все эффекты проходят ABX-тест vs CPU-ref (SNR > 90 дБ)

### Фаза 3: Filter Sweeps (приоритет: средний)

**Цель:** Ускорить `_make_hp_ramp` и `_apply_filter_sweep`.

| Задача | Файл | Сложность | Время |
|---|---|---|---|
| `gpu_hp_ramp()` | `gpu.py` | Средняя | 1 день |
| Интеграция в `dj_engine.py` | `dj_engine.py` | Низкая | 0.5 дня |
| Тесты | `tests/` | Низкая | 0.5 дня |

**Критерии приёмки:**
- [ ] Batched Butterworth через STFT-domain filtering
- [ ] Ускорение ≥ 3× vs sequential lfilter
- [ ] Phase response совпадает с CPU-ref

### Фаза 4: Beat Synthesis (приоритет: низкий)

**Цель:** Ускорить `render_beat` для batch-RL (1000+ эпизодов).

| Задача | Файл | Сложность | Время |
|---|---|---|---|
| `gpu_beat_stamp()` | `gpu.py` | Средняя | 1.5 дня |
| Интеграция в `beat_synth.py` | `beat_synth.py` | Низкая | 0.5 дня |
| Тесты | `tests/` | Низкая | 0.5 дня |

**Критерии приёмки:**
- [ ] Scatter-add через `torch.index_add_`
- [ ] Ускорение ≥ 10× для bars ≥ 8
- [ ] Результат идентичен CPU-ref

### Фаза 5: torch.compile (приоритет: бонус)

**Цель:** Автоматическое fusion и kernel optimization.

| Задача | Файл | Сложность | Время |
|---|---|---|---|
| `torch.compile` для каждой GPU-функции | `gpu.py` | Низкая | 1 день |
| Benchmark suite | `tests/` | Средняя | 1 день |

**Критерии приёмки:**
- [ ] `torch.compile(mode="reduce-overhead")` применяется ко всем gpu_* функциям
- [ ] Дополнительное ускорение ≥ 1.5× поверх raw torch
- [ ] Compilation time < 30 сек на первый вызов

---

## 5. Общие критерии приёмки

Для каждой GPU-функции:

1. **Корректность:** Максимальное отклонение от CPU-ref < 1e-6 (absolute) или SNR > 90 дБ
2. **Ускорение:** Минимум 3× на GPU vs лучший CPU-path (numba если доступен, иначе numpy)
3. **Фолбэк:** Все функции работают без torch (CPU numpy/scipy path)
4. **API:** Принимают `np.ndarray`, возвращают `np.ndarray`, device auto-detect
5. **Тесты:** Каждая функция имеет unit test + comparison test vs CPU-ref
6. **Линтер:** `ruff check` + `mypy` без ошибок

---

## 6. Risks и Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Feedback-based effects (flanger) не параллелятся | Средний | Chunk-based approach или оставить на CPU |
| `torch.compile` compilation time > 30 сек | Низкий | Lazy compilation, кеширование |
| GPU memory для длинных треков (> 10 мин) | Средний | Chunked processing, streaming |
| Numba fallback может быть быстрее torch на CPU | Низкий | Benchmark before/after, keep numba path |
| Numerical drift между GPU и CPU | Высокий | Strict tolerance tests, deterministic mode |

---

## 7. Timeline

| Фаза | Срок | Зависимости |
|---|---|---|
| Фаза 1: Envelope Follower | Неделя 1 | — |
| Фаза 2: DJ Effects | Неделя 2-3 | Фаза 1 (gate/compressor pattern) |
| Фаза 3: Filter Sweeps | Неделя 3 | — |
| Фаза 4: Beat Synthesis | Неделя 4 | — |
| Фаза 5: torch.compile | Неделя 5 | Фазы 1-4 |

**Общий срок:** ~5 недель

---

## 8. Метрики успеха

| Метрика | Текущее | Цель |
|---|---|---|
| `mastering.py` render time (10 мин трек) | ~8 сек (numba) | < 2 сек |
| `dj_effects.py` flanger (10 мин) | ~3 сек | < 1 сек |
| `dj_effects.py` phaser (10 мин) | ~5 сек | < 1 сек |
| `audio_enhance.py` gate+compress (10 мин) | ~4 сек | < 1 сек |
| `beat_synth.py` render_beat (16 баров) | ~0.5 сек | < 0.05 сек |
| GPU coverage | 2 call-sites | 15+ call-sites |

---

## Приложение A: Сравнение слоёв DARAVE vs Traktor

### Все слои в Traktor (полный стек)

| Тип слоя | Количество | Назначение |
|---|---|---|
| Деки (источники) | 2–4 | Каждый трек — отдельный слой |
| Стерео-каналы cue (наушники) | 2 (стерео) | Предпрослушивание, независимо от мастера |
| Стерео-каналы мастер (Main) | 2 (стерео) | Финальный микс на колонки |
| FX-блоки | До 4 на деку | Каждый эффект — отдельный слой обработки |
| Встроенные фильтры | 1 на канал | LPF/HPF на каждом канале |
| EQ (3-полосный) | 1 на канал | Low, Mid, High — три независимых слоя |
| Кроссфейдер | 1 | Смешивает слои A и B |
| Stem-слои (Traktor Pro 4) | До 4 на трек | Bass, Drums, Vocals, Instruments (AI) |

**Итого для двух дек:** минимум **15+ звуковых слоёв** одновременно.

### Модель слоёв DARAVE

| Слой | Назначение | Аналог в Traktor |
|---|---|---|
| Трек A | Основной источник | Дека A |
| Трек B | Второй источник | Дека B |
| Синтезированный бас | Генеративный слой | **Нет аналога** (инновация) |
| Текстура/атмосфера | Генеративный слой | **Нет аналога** (инновация) |
| Эхо/задержка | FX-слой | FX-блок |
| Фильтр | Частотная обработка | Фильтр на канале |
| Стемы (будущее) | AI-разделение | Stem-слои Traktor Pro 4 |
| Мастер-шина | Сборка всего микса | Main output |

### Вывод

Traktor — живое доказательство: **многолейерность обязательна** для profesional DJ-ПО. DARAVE идёт дальше: генеративные слои (синт, текстуры) делают архитектуру **инновационнее** — Traktor не умеет создавать слои, которых нет в оригинальном треке.
