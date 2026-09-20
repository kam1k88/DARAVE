<?php
declare(strict_types=1);

require_once __DIR__ . '/_db.php';

const COOKIE_NAME = 'darave_session';

/**
 * Вход в браузере.
 *
 * Своя кука вместо session_start() — потому что сессия PHP живёт файлом
 * на диске хостинга, и её срок жизни задаётся настройкой сервера, а не
 * нами. Здесь срок и отзыв в наших руках: строку из sessions можно
 * удалить, и вход прекратится немедленно на всех устройствах.
 */

function set_session_cookie(string $token, int $days): void
{
    setcookie(COOKIE_NAME, $token, [
        'expires'  => time() + $days * 86400,
        'path'     => '/',
        'secure'   => (bool)(cfg()['cookie_secure'] ?? true),  // только по https
        'httponly' => true,       // JavaScript не прочитает — защита от XSS
        'samesite' => 'Lax',      // кука не уедет на чужой сайт
    ]);
}

function clear_session_cookie(): void
{
    setcookie(COOKIE_NAME, '', [
        'expires' => time() - 3600, 'path' => '/',
        'secure' => (bool)(cfg()['cookie_secure'] ?? true),
        'httponly' => true, 'samesite' => 'Lax',
    ]);
}

/** Текущий пользователь или null. Заодно чистит протухшие сессии. */
function current_user(): ?array
{
    $token = $_COOKIE[COOKIE_NAME] ?? '';
    if ($token === '' || !ctype_xdigit($token)) {
        return null;
    }
    $hash = hash('sha256', $token);
    $st = db()->prepare(
        'SELECT u.id, u.email, u.nickname, u.tier, u.tier_until, u.blocked,
                u.is_admin, u.deleted_at
           FROM sessions s JOIN users u ON u.id = s.user_id
          WHERE s.token_hash = ? AND s.expires_at > NOW()'
    );
    $st->execute([$hash]);
    $row = $st->fetch();
    // Обезличенная учётка — это НЕ пользователь: почта затёрта, пароль
    // невалиден, заказы остались только как документы. Старая сессия
    // такой учётки не должна пускать никуда.
    if (!$row || (int)$row['blocked'] === 1 || $row['deleted_at'] !== null) {
        return null;
    }
    return $row;
}

function require_user(): array
{
    $u = current_user();
    if ($u === null) {
        json_out(['error' => 'Нужно войти'], 401);
    }
    return $u;
}

/**
 * Действует ли тариф. Просроченный НЕ блокирует вход: человек должен
 * попасть внутрь и увидеть, что оплата кончилась, а не биться в форму
 * входа, гадая, что он ввёл не так.
 */
function tier_active(array $user): bool
{
    if ($user['tier'] === 'free') {
        return true;
    }
    if ($user['tier_until'] === null) {
        return true;
    }
    return strtotime((string)$user['tier_until']) > time();
}

function effective_tier(array $user): string
{
    return tier_active($user) ? (string)$user['tier'] : 'free';
}

/**
 * Защита от перебора. Считаем неудачи за 15 минут отдельно по адресу
 * и по IP: первое ловит подбор пароля к конкретному человеку, второе —
 * перебор адресов с одной машины.
 */
function too_many_attempts(string $email, ?string $ip): bool
{
    $st = db()->prepare(
        'SELECT COUNT(*) c FROM login_attempts
          WHERE at > (NOW() - INTERVAL 15 MINUTE) AND email = ?'
    );
    $st->execute([$email]);
    if ((int)$st->fetch()['c'] >= 8) {
        return true;
    }
    if ($ip !== null) {
        $st = db()->prepare(
            'SELECT COUNT(*) c FROM login_attempts
              WHERE at > (NOW() - INTERVAL 15 MINUTE) AND ip = ?'
        );
        $st->execute([$ip]);
        if ((int)$st->fetch()['c'] >= 30) {
            return true;
        }
    }
    return false;
}

function note_attempt(string $email, ?string $ip): void
{
    db()->prepare('INSERT INTO login_attempts (email, ip) VALUES (?, ?)')
        ->execute([$email, $ip]);
    // Лениво подчищаем старое, чтобы таблица не росла вечно.
    if (random_int(1, 50) === 1) {
        db()->exec('DELETE FROM login_attempts WHERE at < (NOW() - INTERVAL 1 DAY)');
    }
}

function valid_email(string $e): bool
{
    return (bool)filter_var($e, FILTER_VALIDATE_EMAIL) && strlen($e) <= 190;
}

function user_public(array $u): array
{
    return [
        'email'      => $u['email'],
        'nickname'   => $u['nickname'] ?? null,
        'tier'       => effective_tier($u),
        'tier_paid'  => $u['tier'],
        'tier_until' => $u['tier_until'],
        'expired'    => !tier_active($u),
        'is_admin'   => (int)($u['is_admin'] ?? 0) === 1,
    ];
}

/**
 * Админ — это флаг на обычной учётной записи, а не вторая система входа.
 *
 * Так вся уже написанная и проверенная машинерия — защищённая кука,
 * защита от перебора паролей, срок сессии, выход — работает и для
 * админки без единой новой строки. Отдельный «вход для админа» означал
 * бы второй логин, второй сброс пароля и второй шанс ошибиться.
 *
 * Цена решения: чужой доступ к вашей учётке — это сразу и админка.
 * Поэтому пароль у неё должен быть отдельный и длинный.
 *
 * Флаг ставится ТОЛЬКО руками в базе (см. db-admin.sql). Через интерфейс
 * его выдать нельзя — иначе первый же зашедший сделал бы админом себя.
 */
function require_admin(): array
{
    $u = require_user();
    if ((int)($u['is_admin'] ?? 0) !== 1) {
        // 404, а не 403: постороннему незачем знать, что админка есть.
        json_out(['error' => 'Not found'], 404);
    }
    return $u;
}

/**
 * След в журнале. Вызывается ПОСЛЕ удачного действия, а не до: запись о
 * том, чего не произошло, хуже её отсутствия.
 */
function admin_log(int $adminId, string $action, ?int $targetId = null, $details = null): void
{
    try {
        db()->prepare('INSERT INTO admin_log (admin_id, action, target_id, details, ip)
                       VALUES (?, ?, ?, ?, ?)')
            ->execute([
                $adminId, $action, $targetId,
                $details === null ? null : json_encode($details, JSON_UNESCAPED_UNICODE),
                substr((string)($_SERVER['REMOTE_ADDR'] ?? ''), 0, 45),
            ]);
    } catch (Throwable $e) {
        // Журнал не должен ронять само действие: деньги уже вернули,
        // тариф уже выдали. Но молчать тоже нельзя.
        error_log('DARAVE admin_log: ' . $e->getMessage());
    }
}
