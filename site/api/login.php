<?php
declare(strict_types=1);
require_once __DIR__ . '/_auth.php';

cors();
require_post();

$in    = body();
$email = strtolower(trim((string)($in['email'] ?? '')));
$pass  = (string)($in['password'] ?? '');
$ip    = client_ip();

if ($email === '' || $pass === '') {
    json_out(['error' => 'Введите почту и пароль'], 400);
}
if (too_many_attempts($email, $ip)) {
    json_out(['error' => 'Слишком много попыток. Подождите 15 минут.'], 429);
}

$st = db()->prepare('SELECT id, email, pass_hash, tier, tier_until, blocked FROM users WHERE email = ?');
$st->execute([$email]);
$u = $st->fetch();

// Один и тот же ответ на «нет такого адреса» и «неверный пароль»:
// иначе форма входа превращается в способ узнать, кто у вас
// зарегистрирован. И password_verify выполняем даже при отсутствии
// пользователя — по времени ответа тоже можно отличить.
$dummy = '$2y$12$usesomesillystringfoeasdfghjklqwertyuiopzxcvbnmasdfghj';
$ok = $u ? password_verify($pass, $u['pass_hash']) : password_verify($pass, $dummy);

if (!$u || !$ok || (int)$u['blocked'] === 1) {
    note_attempt($email, $ip);
    json_out(['error' => 'Неверная почта или пароль'], 401);
}

// Пароль правильный, но хеш старого формата — пересчитываем молча.
if (password_needs_rehash($u['pass_hash'], PASSWORD_DEFAULT)) {
    db()->prepare('UPDATE users SET pass_hash = ? WHERE id = ?')
        ->execute([password_hash($pass, PASSWORD_DEFAULT), $u['id']]);
}

[$token, $token_hash] = new_token();
$days = (int)cfg()['session_days'];
db()->prepare(
    'INSERT INTO sessions (token_hash, user_id, expires_at, ip, user_agent)
     VALUES (?, ?, (NOW() + INTERVAL ? DAY), ?, ?)'
)->execute([$token_hash, (int)$u['id'], $days, $ip,
            substr((string)($_SERVER['HTTP_USER_AGENT'] ?? ''), 0, 255)]);
db()->prepare('UPDATE users SET last_login_at = NOW() WHERE id = ?')->execute([$u['id']]);
set_session_cookie($token, $days);

// Чистим протухшее — раз в несколько входов, чтобы не делать этого на каждом.
if (random_int(1, 20) === 1) {
    db()->exec('DELETE FROM sessions WHERE expires_at < NOW()');
    db()->exec('DELETE FROM pair_tokens WHERE expires_at < NOW()');
}

json_out(['ok' => true, 'user' => user_public($u)]);
