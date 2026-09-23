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
    // Заявка на возврат приезжает вместе с оплатой: в кабинете кнопка
    // «Вернуть деньги» должна либо быть, либо показывать, что заявка уже
    // принята. Отдельный запрос за этим означал бы, что кнопка секунду
    // выглядит доступной там, где она уже нажата.
    //
    // Запрос с join под try: таблицы заявок может ещё не быть
    // (db-refund-requests.sql выполняется руками). Список оплат из-за
    // этого пропадать не должен — это его основная работа.
    $sql = 'SELECT o.id, o.tier, o.amount, o.status, o.paid_at, o.refunded_at,
                   o.refund_amount, o.created_at, %s
              FROM orders o %s
             WHERE o.user_id = ? ORDER BY o.id DESC LIMIT 50';
    try {
        $st = db()->prepare(sprintf($sql,
            'r.status AS refund_request, r.created_at AS refund_asked_at',
            'LEFT JOIN refund_requests r ON r.order_id = o.id'));
        $st->execute([$u['id']]);
        $orders = $st->fetchAll();
    } catch (PDOException $e) {
        error_log('DARAVE orders: заявки недоступны (' . $e->getMessage() . ')');
        $st = db()->prepare(sprintf($sql, 'NULL AS refund_request, NULL AS refund_asked_at', ''));
        $st->execute([$u['id']]);
        $orders = $st->fetchAll();
    }

    // Возврат без вопросов обещан офертой на 14 дней. Кабинет должен
    // показывать не «можно ли вообще», а сколько дней осталось: иначе
    // человек узнаёт о сроке в момент отказа.
    require_once __DIR__ . '/_refund.php';
    foreach ($orders as &$o) {
        $left = refund_days_left($o);
        $o['refundable']      = $o['status'] === 'succeeded'
                             && $o['refund_request'] === null
                             && $o['refund_amount'] === null;
        $o['refund_days_left'] = $left;
        $o['refund_free']      = $left !== null && $left >= 0;
    }
    unset($o);

    json_out(['ok' => true, 'orders' => $orders, 'refund_free_days' => REFUND_FREE_DAYS]);
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
