<?php
declare(strict_types=1);
require_once __DIR__ . '/_auth.php';

/**
 * Одноразовый токен спаривания для вошедшего пользователя.
 *
 * Зачем он нужен. Страница лежит на darave.ru, а работу делает программа
 * на 127.0.0.1. Программа не видит куку сайта — куки не пересекают
 * origin, — и поверить браузеру на слово «я вошёл как Вася» не может:
 * это сказал бы кто угодно. Поэтому сайт выдаёт короткоживущую бумажку,
 * браузер передаёт её программе, а программа ИДЁТ НА САЙТ и спрашивает,
 * настоящая ли она и чья. Секрет при этом не лежит в JavaScript дольше
 * нескольких секунд и сгорает при первом предъявлении.
 */

cors();
require_post();

$u = require_user();

$minutes = (int)cfg()['pair_token_minutes'];
[$token, $token_hash] = new_token();
db()->prepare(
    'INSERT INTO pair_tokens (token_hash, user_id, expires_at)
     VALUES (?, ?, (NOW() + INTERVAL ? MINUTE))'
)->execute([$token_hash, (int)$u['id'], $minutes]);

json_out([
    'ok'           => true,
    'pair_token'   => $token,
    'expires_in'   => $minutes * 60,
    'verify_url'   => 'https://darave.ru/api/verify.php',
]);
