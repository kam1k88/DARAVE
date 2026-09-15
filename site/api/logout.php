<?php
declare(strict_types=1);
require_once __DIR__ . '/_auth.php';

cors();
require_post();

$token = $_COOKIE[COOKIE_NAME] ?? '';
if ($token !== '' && ctype_xdigit($token)) {
    db()->prepare('DELETE FROM sessions WHERE token_hash = ?')
        ->execute([hash('sha256', $token)]);
}
clear_session_cookie();
json_out(['ok' => true]);
