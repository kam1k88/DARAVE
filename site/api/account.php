<?php
declare(strict_types=1);
require_once __DIR__ . '/_auth.php';

/**
 * Управление своей учётной записью: имя, пароль, заказы, удаление.
 *
 * Один файл с полем action, а не четыре эндпоинта: проверка входа тогда
 * написана ровно один раз, и её нельзя забыть в новом файле.
 *
 *   POST /api/account.php  {"action": "profile",  "nickname": "kamjke"}
 *   POST /api/account.php  {"action": "password", "old": "...", "new": "..."}
 *   POST /api/account.php  {"action": "orders"}
 *   POST /api/account.php  {"action": "delete",   "password": "..."}
 */

cors();
require_post();

$u  = require_user();
$in = body();
$action = (string)($in['action'] ?? '');

// ------------------------------------------------------------- имя
if ($action === 'profile') {
    $nick = trim((string)($in['nickname'] ?? ''));
    // Пустое имя — это «убрать имя», а не ошибка.
    if ($nick === '') {
        db()->prepare('UPDATE users SET nickname = NULL WHERE id = ?')->execute([$u['id']]);
        json_out(['ok' => true, 'nickname' => null]);
    }
    if (mb_strlen($nick) > 40) {
        json_out(['error' => 'Имя длиннее 40 символов'], 400);
    }
    // Управляющие символы в имени не нужны никому, кроме того, кто
    // собирается сломать ими чужую вёрстку или журнал.
    if (preg_match('/[\x00-\x1F\x7F]/u', $nick)) {
        json_out(['error' => 'В имени есть недопустимые символы'], 400);
    }
    db()->prepare('UPDATE users SET nickname = ? WHERE id = ?')->execute([$nick, $u['id']]);
    json_out(['ok' => true, 'nickname' => $nick]);
}

// ---------------------------------------------------------- пароль
if ($action === 'password') {
    $old = (string)($in['old'] ?? '');
    $new = (string)($in['new'] ?? '');

    if (mb_strlen($new) < 10) {
        json_out(['error' => 'Новый пароль короче 10 символов'], 400);
    }
    if (mb_strlen($new) > 200) {
        json_out(['error' => 'Новый пароль длиннее 200 символов'], 400);
    }

    $st = db()->prepare('SELECT pass_hash FROM users WHERE id = ?');
    $st->execute([$u['id']]);
    $row = $st->fetch();
    if (!$row || !password_verify($old, (string)$row['pass_hash'])) {
        json_out(['error' => 'Текущий пароль не подходит'], 403);
    }

    $hash = password_hash($new, PASSWORD_DEFAULT);
    $pdo = db();
    $pdo->beginTransaction();
    try {
        $pdo->prepare('UPDATE users SET pass_hash = ? WHERE id = ?')->execute([$hash, $u['id']]);

        // Все ОСТАЛЬНЫЕ сессии закрываем. Пароль меняют в том числе
        // потому, что им кто-то завладел; оставить чужой вход живым
        // значит не сделать ничего. Текущую оставляем, иначе человек
        // выкинет сам себя тем же нажатием.
        $cur = hash('sha256', (string)($_COOKIE[COOKIE_NAME] ?? ''));
        $pdo->prepare('DELETE FROM sessions WHERE user_id = ? AND token_hash <> ?')
            ->execute([$u['id'], $cur]);
        $pdo->commit();
    } catch (Throwable $e) {
        $pdo->rollBack();
        error_log('DARAVE password: ' . $e->getMessage());
        json_out(['error' => 'Не удалось сменить пароль'], 500);
    }

    json_out(['ok' => true, 'note' => 'Пароль изменён. Входы на других устройствах закрыты.']);
}

// ---------------------------------------------------------- заказы
if ($action === 'orders') {
    $st = db()->prepare(
        'SELECT id, tier, amount, status, paid_at, refunded_at, refund_amount, created_at
           FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 50'
    );
    $st->execute([$u['id']]);
    json_out(['ok' => true, 'orders' => $st->fetchAll()]);
}

// --------------------------------------------------------- удаление
if ($action === 'delete') {
    $pass = (string)($in['password'] ?? '');
    $st = db()->prepare('SELECT pass_hash FROM users WHERE id = ?');
    $st->execute([$u['id']]);
    $row = $st->fetch();
    if (!$row || !password_verify($pass, (string)$row['pass_hash'])) {
        json_out(['error' => 'Пароль не подходит'], 403);
    }

    // Обезличиваем, а НЕ удаляем строку.
    //
    // У orders стоит ON DELETE CASCADE: DELETE FROM users снёс бы заказы,
    // а это документы по НПД — суммы, номера платежей ЮKassa, возвраты.
    // Их нужно хранить независимо от того, ушёл человек или нет.
    //
    // Поэтому: почта затирается (освобождая адрес для новой регистрации),
    // пароль становится невалидным, сессии и устройства закрываются.
    // Войти в такую учётку нельзя — current_user() отсекает её по
    // deleted_at.
    $pdo = db();
    $pdo->beginTransaction();
    try {
        $stub = 'deleted+' . $u['id'] . '@darave.invalid';
        $pdo->prepare(
            "UPDATE users
                SET email = ?, nickname = NULL, deleted_at = NOW(),
                    pass_hash = ?, tier = 'free', tier_until = NULL
              WHERE id = ?"
        )->execute([$stub, password_hash(bin2hex(random_bytes(32)), PASSWORD_DEFAULT), $u['id']]);
        $pdo->prepare('DELETE FROM sessions WHERE user_id = ?')->execute([$u['id']]);
        $pdo->prepare('UPDATE devices SET revoked = 1 WHERE user_id = ?')->execute([$u['id']]);
        $pdo->commit();
    } catch (Throwable $e) {
        $pdo->rollBack();
        error_log('DARAVE account delete: ' . $e->getMessage());
        json_out(['error' => 'Не удалось удалить учётную запись'], 500);
    }

    clear_session_cookie();
    json_out(['ok' => true, 'note' => 'Учётная запись удалена. История платежей сохранена как документы по закону.']);
}

json_out(['error' => 'Неизвестное действие'], 400);
