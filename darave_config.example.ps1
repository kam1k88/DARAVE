# darave_config.example.ps1 — шаблон конфига автозапуска.
#
# start_darave.bat при первом запуске сам скопирует этот файл в
# darave_config.ps1. darave_config.ps1 в .gitignore — ключ не попадёт в
# архив при следующих обновлениях DARAVE.
#
# ВАЖНО: пути к loopMIDI/Mixxx/Python скрипт ищет САМ (типичные места +
# реестр). Заполняйте их, только если в логе написано, что не нашёл.

$Global:DaraveConfig = @{

    # "" = папка этого скрипта.
    DaraveDir     = ""

    # Код комнаты — тот же, что в браузере (?room=...).
    RoomCode      = "my-room"
    Port          = 8765

    # ---- LLM ----
    # "auto"   — рекомендуется: есть ключ Gemini -> Gemini; нет ключа, но
    #            запущена Ollama -> Ollama.
    # "gemini" — только облако (ключ + интернет без гео-блока).
    # "ollama" — только локальная модель (бесплатно, без интернета).
    LlmProvider   = "auto"

    GeminiApiKey  = "PASTE_YOUR_GEMINI_KEY_HERE"   # https://aistudio.google.com
    OllamaModel   = "llama3.1"                     # ollama pull llama3.1
    OllamaHost    = "http://localhost:11434"

    # ---- Python ----
    # "" = 'python' из PATH. ВАЖНО: companion'у нужен python-rtmidi, а его
    # сборок под Windows нет новее Python 3.12. Если основной Python новее,
    # скрипт сам поищет 3.12 через 'py -3.12'; если нашли путь — впишите:
    #   PythonExe = "C:\Users\<вы>\AppData\Local\Programs\Python\Python312\python.exe"
    PythonExe     = ""

    # ---- Пути ("" = искать автоматически) ----
    LoopMidiPath  = ""
    MixxxPath     = ""

    RecordingsDir = "$Env:USERPROFILE\Documents\Mixxx\Recordings"
    MidiPortName  = "DARAVE Virtual Controller"
}
