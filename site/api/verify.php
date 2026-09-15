<?php
declare(strict_types=1);
require_once __DIR__ . '/_auth.php';

/**
 * Сюда стучится ЛОКАЛЬНАЯ программа, а не браузер.
 *
 * Принимает токен спаривания и отпечаток машины, отвечает, кто это и что
 * ему разрешено. Токен сгорает здесь же: повторно предъявленный —
 * недействителен, поэтому перехваченный и уже использованный ничего не
 * даёт.
 *
 * CORS тут намеренно НЕ вызывается: запрос идёт не из браузера, а из
 * Python, и заголовки origin ему не нужны. Заодно это значит, что чужая
 * страница не сможет дёрнуть этот эндпоинт из браузера жертвы.
 */

require_post();
header('Content-Type: application/json; charset=utf-8');

$in     = body();
$token  = (string)($in['pair_token'] ?? '');
$device = (string)($in['device_id'] ?? '');
$name   = trim((string)($in['device_name'] ?? ''));
$ver    = trim((string)($in['app_version'] ?? ''));

if ($token === '' || !ctype_xdigit($token) || strlen($device) < 16) {
    json_out(['ok' => false, 'error' => 'Неполный запрос'], 400);
}
$device = substr(hash('sha256', $device), 0, 64);

$pdo = db();
$pdo->beginTransaction();
try {
    // SELECT ... FOR UPDATE, потому что «проверить и погасить» должно быть
    // одной операцией: иначе два одновременных запроса с одним токеном
    // оба увидят его непогашенным.
    $st = $pdo->prepare(
        'SELECT user_id FROM pair_tokens
          WHERE token_hash = ? AND used_at IS NULL AND expires_at > NOW()
          FOR UPDATE'
    );
    $st->execute([hash('sha256', $token)]);
    $row = $st->fetch();
    if (!$row) {
        $pdo->rollBack();
        json_out(['ok' => false, 'error' => 'Код спаривания недействителен или уже использован'], 401);
    }
    $uid = (int)$row['user_id'];
    $pdo->prepare('UPDATE pair_tokens SET used_at = NOW() WHERE token_hash = ?')
        ->execute([hash('sha256', $token)]);
    $pdo->commit();
} catch (Throwable $e) {
    $pdo->rollBack();
    error_log('DARAVE verify: ' . $e->getMessage());
    json_out(['ok' => false, 'error' => 'Внутренняя ошибка'], 500);
}

$st = $pdo->prepare('SELECT id, email, tier, tier_until, blocked FROM users WHERE id = ?');
$st->execute([$uid]);
$u = $st->fetch();
if (!$u || (int)$u['blocked'] === 1) {
    json_out(['ok' => false, 'error' => 'Учётная запись недоступна'], 403);
}

$tier = effective_tier($u);
$limit = (int)(cfg()['max_devices'][$tier] ?? 1);

// Считаем машины ДО добавления этой: если она уже известна, лимит не
// при чём — человек просто вернулся за свой же компьютер.
$st = $pdo->prepare('SELECT COUNT(*) c FROM devices WHERE user_id = ? AND revoked = 0 AND device_id <> ?');
$st->execute([$uid, $device]);
$others = (int)$st->fetch()['c'];

$st = $pdo->prepare('SELECT id FROM devices WHERE user_id = ? AND device_id = ? AND revoked = 0');
$st->execute([$uid, $device]);
$known = (bool)$st->fetch();

if (!$known && $others >= $limit) {
    json_out([
        'ok' => false,
        'error' => "На тарифе «{$tier}» можно работать с {$limit} " .
                   ($limit === 1 ? 'компьютера' : 'компьютеров') .
                   '. Отвяжите лишний в настройках на darave.ru — или откройте доступ шире.',
        'reason' => 'device_limit',
    ], 403);
}

$pdo->prepare(
    'INSERT INTO devices (user_id, device_id, name, app_version)
     VALUES (?, ?, ?, ?)
     ON DUPLICATE KEY UPDATE last_seen = NOW(), name = VALUES(name),
                             app_version = VALUES(app_version), revoked = 0'
)->execute([$uid, $device, substr($name, 0, 190) ?: null, substr($ver, 0, 40) ?: null]);

json_out([
    'ok'      => true,
    'user'    => ['email' => $u['email'], 'tier' => $tier, 'expired' => !tier_active($u)],
    // Сколько программа может работать, не переспрашивая. Не «навсегда»,
    // но и не «каждый запуск»: у диджея сеть пропадает в самый
    // неподходящий момент, и программа, требующая интернет на старте,
    // подводит его ровно на выступлении.
    'valid_days' => 30,
]);
