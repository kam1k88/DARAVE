<?php
declare(strict_types=1);
require_once __DIR__ . '/_auth.php';

cors();
require_post();

$in    = body();
$email = strtolower(trim((string)($in['email'] ?? '')));
$pass  = (string)($in['password'] ?? '');

if (!valid_email($email)) {
    json_out(['error' => 'Проверьте адрес почты'], 400);
}
// Длина, а не «одна заглавная, одна цифра и звёздочка». Требования к
// составу символов гонят людей в «Password1!» — короткий и предсказуемый
// пароль; длина честно усложняет перебор.
if (mb_strlen($pass) < 10) {
    json_out(['error' => 'Пароль короче 10 символов. Лучше три случайных слова, чем «Qwerty1!»'], 400);
}
if (mb_strlen($pass) > 200) {
    json_out(['error' => 'Пароль длиннее 200 символов'], 400);
}

$hash = password_hash($pass, PASSWORD_DEFAULT);

try {
    db()->prepare('INSERT INTO users (email, pass_hash) VALUES (?, ?)')
        ->execute([$email, $hash]);
} catch (PDOException $e) {
    if ($e->errorInfo[1] === 1062) {   // дубль по uniq_email
        json_out(['error' => 'Такой адрес уже зарегистрирован'], 409);
    }
    error_log('DARAVE register: ' . $e->getMessage());
    json_out(['error' => 'Не смог создать учётную запись'], 500);
}

$uid = (int)db()->lastInsertId();
[$token, $token_hash] = new_token();
$days = (int)cfg()['session_days'];
db()->prepare(
    'INSERT INTO sessions (token_hash, user_id, expires_at, ip, user_agent)
     VALUES (?, ?, (NOW() + INTERVAL ? DAY), ?, ?)'
)->execute([$token_hash, $uid, $days, client_ip(),
            substr((string)($_SERVER['HTTP_USER_AGENT'] ?? ''), 0, 255)]);
set_session_cookie($token, $days);

json_out(['ok' => true, 'user' => [
    'email' => $email, 'tier' => 'free', 'tier_paid' => 'free',
    'tier_until' => null, 'expired' => false,
]]);
