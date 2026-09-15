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
        'SELECT u.id, u.email, u.tier, u.tier_until, u.blocked
           FROM sessions s JOIN users u ON u.id = s.user_id
          WHERE s.token_hash = ? AND s.expires_at > NOW()'
    );
    $st->execute([$hash]);
    $row = $st->fetch();
    if (!$row || (int)$row['blocked'] === 1) {
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
        'tier'       => effective_tier($u),
        'tier_paid'  => $u['tier'],
        'tier_until' => $u['tier_until'],
        'expired'    => !tier_active($u),
    ];
}
