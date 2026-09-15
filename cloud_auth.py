"""cloud_auth.py — вход через darave.ru для локального клиента.

Схема целиком, чтобы не собирать её по кускам:

    браузер                     darave.ru                 DARAVE.exe
    ───────                     ─────────                 ──────────
    открывает /app        →     отдаёт страницу
    вводит почту/пароль   →     login.php, кука сессии
    просит код спаривания →     pair.php, токен на 5 минут
    POST /pair ──────────────────────────────────────→    принимает токен
                                verify.php    ←────────    проверяет ЧЕЙ он
                                              ────────→    почта, тариф, срок
    дальше все запросы с Bearer-токеном ─────────────→    работает

Три решения, каждое со своей причиной.

**Программа не верит браузеру.** Страница могла бы просто сказать «я
вошёл как Вася» — это сказал бы кто угодно, открыв консоль. Поэтому
браузер приносит бумажку, а программа сама идёт на darave.ru и
спрашивает, настоящая ли она и чья.

**Проверка живёт 30 дней, а не до следующего запуска.** У диджея сеть
пропадает ровно на выступлении, и программа, которая в этот момент
требует интернет, подводит его на сцене. Проверились один раз — работаем
месяц, потом переспрашиваем. Это осознанный размен: отзыв доступа
доезжает не мгновенно, зато выступление не срывается.

**Отпечаток машины — не привязка к железу.** Считается один раз и лежит
в файле рядом с данными. Смена видеокарты или переустановка Windows не
превращают покупателя в вора; при переносе на новый компьютер появится
новая запись, и если машин стало больше, чем позволяет тариф, человек
увидит внятный отказ со списком, а не «доступ запрещён».
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import time
from pathlib import Path

CLOUD_BASE = os.environ.get("DARAVE_CLOUD", "https://darave.ru").rstrip("/")
VERIFY_URL = CLOUD_BASE + "/api/verify.php"

STATE_NAME = "darave_account.json"
DEVICE_NAME = "darave_device.json"

# Сколько ждём ответа от сайта. Мало — потому что это стоит между
# человеком и его программой: лучше честно сказать «сайт не отвечает»,
# чем держать пустое окно полминуты.
HTTP_TIMEOUT = 8.0


def _dir() -> Path:
    try:
        import paths as _p

        return _p.data_dir()
    except Exception:
        return Path(__file__).resolve().parent


def device_id() -> str:
    """Постоянный идентификатор этой установки.

    Именно случайное число в файле, а не серийник диска или MAC. Во-первых,
    железо меняется, и привязка к нему наказывает честного покупателя.
    Во-вторых, серийники — это данные о человеке, которые незачем
    отправлять на сервер, чтобы просто отличить один компьютер от другого.
    """
    path = _dir() / DEVICE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        val = str(data.get("device_id") or "")
        if len(val) >= 32:
            return val
    except (OSError, ValueError):
        pass
    val = secrets.token_hex(32)
    try:
        path.write_text(json.dumps({"device_id": val}), encoding="utf-8")
    except OSError:
        pass
    return val


def device_name() -> str:
    try:
        return platform.node() or "компьютер"
    except Exception:
        return "компьютер"


# --------------------------------------------------------------- состояние

def _state_path() -> Path:
    return _dir() / STATE_NAME


def load_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data: dict) -> None:
    try:
        _state_path().write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    except OSError:
        pass


def clear_state() -> None:
    try:
        _state_path().unlink()
    except OSError:
        pass


def status() -> dict:
    """Кто вошёл и сколько ещё действует проверка."""
    st = load_state()
    if not st.get("token"):
        return {"signed_in": False}
    left = float(st.get("valid_until", 0)) - time.time()
    return {
        "signed_in": left > 0,
        "email": st.get("email"),
        "tier": st.get("tier", "free"),
        "expired_tier": bool(st.get("expired")),
        "days_left": max(0, int(left // 86400)),
        "grace": left <= 0,
    }


def token_ok(token: str) -> bool:
    """Тот ли это токен и не истёк ли срок проверки."""
    if not token:
        return False
    st = load_state()
    saved = str(st.get("token") or "")
    if not saved:
        return False
    # Сравнение с постоянным временем: обычное == по строкам утекает
    # длину совпадения, и подобрать токен байт за байтом становится
    # вопросом терпения.
    if not secrets.compare_digest(saved, token):
        return False
    return float(st.get("valid_until", 0)) > time.time()


# ------------------------------------------------------------- спаривание

class PairError(RuntimeError):
    """Отказ, который НУЖНО показать человеку дословно.

    Сообщения сюда приходят с сервера и объясняют, что делать: «код
    просрочен», «это третий компьютер на тарифе, где их два». Заменять их
    общим «ошибка входа» — значит отправить человека гадать."""


def pair(pair_token: str, app_version: str = "") -> dict:
    """Проверить код спаривания на darave.ru и запомнить вход."""
    import urllib.error
    import urllib.request

    payload = json.dumps({
        "pair_token": pair_token,
        "device_id": device_id(),
        "device_name": device_name(),
        "app_version": app_version,
    }).encode("utf-8")

    req = urllib.request.Request(
        VERIFY_URL, data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": f"DARAVE/{app_version or '1.0'}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        # У отказов сервера тело с внятным текстом — читаем его, а не
        # подменяем кодом ответа.
        try:
            data = json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            raise PairError(f"Сайт ответил ошибкой {exc.code}") from exc
        raise PairError(str(data.get("error") or f"Ошибка {exc.code}"))
    except urllib.error.URLError as exc:
        raise PairError(
            f"Не смог связаться с {CLOUD_BASE}: {exc.reason}. "
            "Проверьте интернет — для входа он нужен один раз."
        ) from exc
    except (ValueError, OSError) as exc:
        raise PairError(f"Непонятный ответ сайта: {exc}") from exc

    if not data.get("ok"):
        raise PairError(str(data.get("error") or "Сайт не подтвердил вход"))

    user = data.get("user") or {}
    days = float(data.get("valid_days") or 30)
    token = secrets.token_hex(32)
    save_state({
        "token": token,
        "email": user.get("email"),
        "tier": user.get("tier", "free"),
        "expired": bool(user.get("expired")),
        "paired_at": time.time(),
        "valid_until": time.time() + days * 86400,
        "device_id": device_id(),
    })
    return {"token": token, **status()}
