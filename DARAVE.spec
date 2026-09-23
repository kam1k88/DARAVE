# -*- mode: python ; coding: utf-8 -*-
"""Сборка DARAVE.exe. Запускать не напрямую, а через build.ps1.

Три решения здесь неочевидны, и каждое стоило отладки.

1. onedir, не onefile. onefile при каждом запуске распаковывает ~200 МБ
   во временную папку: старт 8-15 секунд вместо одной, и антивирус на
   свежераспакованный exe реагирует куда охотнее. Плюс DARAVE запускает
   сам себя как подпроцесс (скан библиотеки, слои) — в onefile это вторая
   распаковка на каждый запуск.

2. collect_data_files("librosa"). librosa держит в пакете файлы .npz с
   таблицами фильтров и служебный example-манифест и читает их с диска.
   PyInstaller тянет только .py — без этой строки сборка стартует нормально
   и падает при ПЕРВОМ анализе трека, через несколько минут работы.

3. collect_all("scipy"). У scipy расширения грузятся динамически, по имени,
   и статический анализ их не видит. Симптом тот же отложенный: скан
   валится на первом же ресемплинге.

torch и demucs исключены намеренно: они приезжают отдельным пакетом слоёв.
Если они стоят в том же venv, откуда идёт сборка, без excludes они уедут
в дистрибутив и превратят 200 МБ в 3 ГБ.
"""

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

datas = []
binaries = []
hiddenimports = []

# --- ресурсы самой программы ---
datas += [
    ("static", "static"),
    ("mixxx_catalog.json", "."),
    # Установка стем-стека. Раньше этих двух файлов в сборке не было,
    # и человек, поставивший DARAVE установщиком, слои получить не мог
    # НИКАК: скрипта у него нет, а интерфейс отправлял его к кнопке,
    # которой тоже не было. Сам пакет (torch + demucs, 2-3 ГБ) в
    # установщик по-прежнему не входит — скрипт качает его с PyPI.
    ("setup_stems.ps1", "."),
    ("requirements-stems.txt", "."),
]

# --- librosa: таблицы и манифесты лежат файлами внутри пакета ---
datas += collect_data_files("librosa")
hiddenimports += collect_submodules("librosa")

# --- scipy: динамические расширения ---
_s_datas, _s_binaries, _s_hidden = collect_all("scipy")
datas += _s_datas
binaries += _s_binaries
hiddenimports += _s_hidden

# --- soundfile тащит libsndfile отдельным бинарником ---
datas += collect_data_files("soundfile")

# --- google-genai: пространство имён, а не обычный пакет ---
# `from google import genai` — это namespace package: каталога с
# __init__.py у `google` нет, и статический анализ PyInstaller'а такой
# импорт не видит вовсе. Симптом отложенный и оттого злой: программа
# ставится, запускается, работает — и ломается ровно в тот момент, когда
# пользователь вставил ключ и нажал «Подключить».
try:
    _g_datas, _g_binaries, _g_hidden = collect_all("google.genai")
    datas += _g_datas
    binaries += _g_binaries
    hiddenimports += _g_hidden
    hiddenimports += ["google", "google.genai", "google.genai.types"]
except Exception as _exc:      # пакет не установлен в окружении сборки
    print(f"[spec] google-genai не найден ({_exc}) — чат-ассистент в сборке работать не будет")

# --- python-multipart: имя пакета и имя модуля не совпадают ---
# Ставится как python-multipart, импортируется как multipart (в свежих
# версиях ещё и как python_multipart). FastAPI ищет его по имени внутри
# декоратора маршрута, поэтому промах ломает не загрузку файла, а импорт
# server.py целиком.
hiddenimports += ["multipart", "multipart.multipart", "python_multipart"]
for _name in ("multipart", "python_multipart"):
    try:
        _m_datas, _m_binaries, _m_hidden = collect_all(_name)
        datas += _m_datas
        binaries += _m_binaries
        hiddenimports += _m_hidden
    except Exception:
        pass

# --- uvicorn грузит свои реализации по строковому имени ---
hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
]

# --- модули DARAVE, которые запускаются как дочерние (child.py) и на
#     которые нет прямой ссылки import в точке входа ---
hiddenimports += ["track_analysis", "stems", "fix_library_bpm", "autopilot_selftest"]

# fx_presets импортируется ЛЕНИВО, из тела обработчика: статический
# анализ такие импорты обычно находит, но проверять это падением сборки
# на десятой минуте — дорогое удовольствие. Строка стоит копейку.
hiddenimports += ["fx_presets"]

# stem_features — тоже только ленивые импорты: из шага «признаки слоёв»
# в server.py, из style.layer_technique_bias и из audition.pair_layers.
# Ни одного статического import в точке входа, то есть без этой строки
# подбор в собранной программе молча работал бы БЕЗ признаков слоёв —
# самая дорогая поломка из возможных: она не падает, а тихо
# возвращает подбор к прежнему качеству.
# track_pool и stem_features импортируются ЛЕНИВО (внутри функций),
# поэтому PyInstaller их сам не находит — в сборке модуль бы просто
# отсутствовал, а пул треков падал бы уже у диджея.
hiddenimports += ["stem_features", "track_pool"]

excludes = [
    # Пакет слоёв — отдельно, см. requirements-stems.txt.
    "torch", "torchaudio", "demucs", "audio_separator",
    "laion_clap", "panns_inference",
    # Тянутся за librosa/scipy, но в работе не участвуют.
    "matplotlib", "pytest", "IPython", "notebook", "tkinter",
]

a = Analysis(
    ["darave_app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DARAVE",
    debug=False,
    strip=False,
    upx=False,          # UPX ломает расширения numpy/scipy и злит антивирусы
    # console=True, хотя окна быть не должно. Собранный windowed-exe
    # остаётся без stdout, а DARAVE запускает сам себя дочерним процессом
    # и ЧИТАЕТ его вывод — так идёт живой прогресс скана и слоёв. В
    # windowed-сборке print() в дочернем процессе падает на None, прогресс
    # застывает, и снаружи это выглядит как зависший скан. Поэтому консоль
    # есть, а окно прячется при старте (darave_app.hide_console).
    console=True,
    icon="build/darave.ico" if __import__("os").path.exists("build/darave.ico") else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="DARAVE",
)
