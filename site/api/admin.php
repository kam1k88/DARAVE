<?php
declare(strict_types=1);
require_once __DIR__ . '/_auth.php';
require_once __DIR__ . '/_refund.php';

/**
 * Админка: одна точка входа, одна проверка прав.
 *
 * Все действия POST с полем action. Отдельными файлами делать не стал
 * намеренно: require_admin() тогда пришлось бы писать в каждом, а
 * забытая проверка в одном новом файле открывает всю базу.
 *
 * Посторонним отвечаем 404, а не 403 — незачем сообщать, что админка
 * вообще существует.
 */

cors();
require_post();

$me = require_admin();
$in = body();
$action = (string)($in['action'] ?? '');
$pdo = db();

/** Пользователь по id — без хеша пароля: он не нужен даже админу. */
function admin_find(PDO $pdo, int $id): ?array
{
    $st = $pdo->prepare(
        'SELECT id, email, nickname, tier, tier_until, blocked, is_admin,
                admin_note, created_at, last_login_at, deleted_at
           FROM users WHERE id = ?'
    );
    $st->execute([$id]);
    return $st->fetch() ?: null;
}

// ----------------------------------------------------------- сводка
if ($action === 'stats') {
    $q = fn(string $sql) => (int)$pdo->query($sql)->fetchColumn();
    json_out(['ok' => true, 'stats' => [
        'users'      => $q('SELECT COUNT(*) FROM users WHERE deleted_at IS NULL'),
        'paid'       => $q("SELECT COUNT(*) FROM users WHERE tier <> 'free' AND deleted_at IS NULL"),
        'blocked'    => $q('SELECT COUNT(*) FROM users WHERE blocked = 1'),
        'deleted'    => $q('SELECT COUNT(*) FROM users WHERE deleted_at IS NOT NULL'),
        'orders_ok'  => $q("SELECT COUNT(*) FROM orders WHERE status = 'succeeded'"),
        'refunded'   => $q("SELECT COUNT(*) FROM orders WHERE status = 'refunded'"),
        'revenue'    => (float)$pdo->query("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status = 'succeeded'")->fetchColumn(),
        'devices'    => $q('SELECT COUNT(*) FROM devices WHERE revoked = 0'),
    ]]);
}

// ------------------------------------------------------------ список
if ($action === 'users') {
    $q = trim((string)($in['q'] ?? ''));
    $limit = min(200, max(1, (int)($in['limit'] ?? 50)));

    if ($q !== '') {
        // LIKE с экранированием % и _ — иначе поиск по «a_b» находит всё
        // подряд, а по «100%» не находит ничего.
        $like = '%' . str_replace(['\\', '%', '_'], ['\\\\', '\\%', '\\_'], $q) . '%';
        $st = $pdo->prepare(
            "SELECT id, email, nickname, tier, tier_until, blocked, is_admin,
                    created_at, last_login_at, deleted_at
               FROM users
              WHERE email LIKE ? ESCAPE '\\\\' OR nickname LIKE ? ESCAPE '\\\\'
              ORDER BY id DESC LIMIT {$limit}"
        );
        $st->execute([$like, $like]);
    } else {
        $st = $pdo->query(
            "SELECT id, email, nickname, tier, tier_until, blocked, is_admin,
                    created_at, last_login_at, deleted_at
               FROM users ORDER BY id DESC LIMIT {$limit}"
        );
    }
    json_out(['ok' => true, 'users' => $st->fetchAll()]);
}

// ---------------------------------------------------------- карточка
if ($action === 'user') {
    $id = (int)($in['id'] ?? 0);
    $u = admin_find($pdo, $id);
    if (!$u) json_out(['error' => 'Пользователь не найден'], 404);

    $dev = $pdo->prepare('SELECT device_id, name, app_version, first_seen, last_seen, revoked
                            FROM devices WHERE user_id = ? ORDER BY last_seen DESC');
    $dev->execute([$id]);

    $ord = $pdo->prepare('SELECT id, tier, amount, status, payment_id, paid_at,
                                 refunded_at, refund_amount, created_at
                            FROM orders WHERE user_id = ? ORDER BY id DESC');
    $ord->execute([$id]);

    $log = $pdo->prepare('SELECT action, details, created_at FROM admin_log
                           WHERE target_id = ? ORDER BY id DESC LIMIT 30');
    $log->execute([$id]);

    json_out(['ok' => true, 'user' => $u, 'devices' => $dev->fetchAll(),
              'orders' => $ord->fetchAll(), 'log' => $log->fetchAll(),
              'max_devices' => cfg()['max_devices'][effective_tier($u)] ?? 1]);
}

// ------------------------------------------------------------ тариф
if ($action === 'set_tier') {
    $id   = (int)($in['id'] ?? 0);
    $tier = (string)($in['tier'] ?? '');
    $until = trim((string)($in['until'] ?? ''));   // пусто = бессрочно

    if (!in_array($tier, ['free', 'personal', 'workshop'], true)) {
        json_out(['error' => 'Неизвестный тариф'], 400);
    }
    $u = admin_find($pdo, $id);
    if (!$u) json_out(['error' => 'Пользователь не найден'], 404);

    $untilVal = null;
    if ($until !== '') {
        $ts = strtotime($until);
        if ($ts === false) json_out(['error' => 'Дату не разобрать. Формат: 2027-01-31'], 400);
        $untilVal = date('Y-m-d H:i:s', $ts);
    }

    $pdo->prepare('UPDATE users SET tier = ?, tier_until = ? WHERE id = ?')
        ->execute([$tier, $untilVal, $id]);

    admin_log((int)$me['id'], 'set_tier', $id, [
        'from' => $u['tier'], 'from_until' => $u['tier_until'],
        'to' => $tier, 'to_until' => $untilVal,
        'reason' => (string)($in['reason'] ?? ''),
    ]);
    json_out(['ok' => true, 'user' => admin_find($pdo, $id)]);
}

// --------------------------------------------------------- блокировка
if ($action === 'block') {
    $id = (int)($in['id'] ?? 0);
    $on = (bool)($in['blocked'] ?? false);

    // Себя заблокировать нельзя. Это не забота, а единственная защита:
    // разблокировать было бы уже нечем — вход закрыт для всех сессий.
    if ($id === (int)$me['id']) {
        json_out(['error' => 'Себя блокировать нельзя — потом не войти'], 400);
    }
    $u = admin_find($pdo, $id);
    if (!$u) json_out(['error' => 'Пользователь не найден'], 404);

    $pdo->prepare('UPDATE users SET blocked = ? WHERE id = ?')->execute([$on ? 1 : 0, $id]);
    if ($on) {
        // Блокировка без закрытия сессий — это не блокировка: человек
        // остаётся внутри до истечения куки.
        $pdo->prepare('DELETE FROM sessions WHERE user_id = ?')->execute([$id]);
    }
    admin_log((int)$me['id'], $on ? 'block' : 'unblock', $id,
              ['reason' => (string)($in['reason'] ?? '')]);
    json_out(['ok' => true, 'user' => admin_find($pdo, $id)]);
}

// ------------------------------------------------------- сброс пароля
if ($action === 'reset_password') {
    $id = (int)($in['id'] ?? 0);
    $u = admin_find($pdo, $id);
    if (!$u) json_out(['error' => 'Пользователь не найден'], 404);

    // Пароль ГЕНЕРИРУЕМ, а не принимаем из формы.
    //
    // Во-первых, придуманный на ходу пароль обычно слабый. Во-вторых,
    // присланный в запросе пароль оседает в журналах веб-сервера, а
    // сгенерированный здесь — только в ответе, один раз.
    $plain = bin2hex(random_bytes(9));           // 18 знаков
    $pdo->prepare('UPDATE users SET pass_hash = ? WHERE id = ?')
        ->execute([password_hash($plain, PASSWORD_DEFAULT), $id]);
    $pdo->prepare('DELETE FROM sessions WHERE user_id = ?')->execute([$id]);

    admin_log((int)$me['id'], 'reset_password', $id, ['reason' => (string)($in['reason'] ?? '')]);
    json_out(['ok' => true, 'password' => $plain,
              'note' => 'Покажется один раз. Передайте человеку и попросите сменить.']);
}

// -------------------------------------------------------- заметка
if ($action === 'note') {
    $id = (int)($in['id'] ?? 0);
    $note = trim((string)($in['note'] ?? ''));
    if (mb_strlen($note) > 500) json_out(['error' => 'Заметка длиннее 500 символов'], 400);
    if (!admin_find($pdo, $id)) json_out(['error' => 'Пользователь не найден'], 404);

    $pdo->prepare('UPDATE users SET admin_note = ? WHERE id = ?')
        ->execute([$note === '' ? null : $note, $id]);
    admin_log((int)$me['id'], 'note', $id, null);
    json_out(['ok' => true]);
}

// -------------------------------------------------------- промокоды
if ($action === 'promo_list') {
    $st = $pdo->query('SELECT * FROM promo_codes ORDER BY created_at DESC LIMIT 100');
    json_out(['ok' => true, 'codes' => $st->fetchAll()]);
}

if ($action === 'promo_add') {
    $code = strtoupper(trim((string)($in['code'] ?? '')));
    if (!preg_match('/^[A-Z0-9-]{3,32}$/', $code)) {
        json_out(['error' => 'Код: 3-32 знака, латиница, цифры, дефис'], 400);
    }
    $percent = $in['percent'] ?? null;
    $fixed   = $in['fixed_price'] ?? null;
    if (($percent === null || $percent === '') && ($fixed === null || $fixed === '')) {
        json_out(['error' => 'Нужен либо процент, либо фиксированная цена'], 400);
    }
    if ($percent !== null && $percent !== '' && ($percent < 1 || $percent > 100)) {
        json_out(['error' => 'Процент вне 1..100'], 400);
    }
    $expires = trim((string)($in['expires_at'] ?? ''));
    $expVal = null;
    if ($expires !== '') {
        // strtotime() при непонятной строке возвращает false, а (int)false
        // это 0 — то есть 1970 год. Код с такой датой мёртв с рождения, и
        // никто не поймёт почему.
        $ts = strtotime($expires);
        if ($ts === false) json_out(['error' => 'Дату не разобрать. Формат: 2027-01-31'], 400);
        $expVal = date('Y-m-d H:i:s', $ts);
    }

    try {
        $pdo->prepare('INSERT INTO promo_codes (code, percent, fixed_price, tier, max_uses, expires_at, created_by)
                       VALUES (?, ?, ?, ?, ?, ?, ?)')
            ->execute([
                $code,
                ($percent === null || $percent === '') ? null : (int)$percent,
                ($fixed === null || $fixed === '') ? null : number_format((float)$fixed, 2, '.', ''),
                in_array($in['tier'] ?? '', ['personal', 'workshop'], true) ? $in['tier'] : null,
                max(1, (int)($in['max_uses'] ?? 1)),
                $expVal,
                (int)$me['id'],
            ]);
    } catch (PDOException $e) {
        // 23000 — нарушение уникальности, то есть код занят. Всё
        // остальное (нет таблицы, нет прав) — другая беда, и врать про
        // занятый код значит отправить себя искать не там.
        if (($e->errorInfo[0] ?? '') === '23000') {
            json_out(['error' => 'Такой код уже есть'], 409);
        }
        error_log('DARAVE promo_add: ' . $e->getMessage());
        json_out(['error' => 'Не удалось сохранить код. Выполнена ли db-admin.sql?'], 500);
    }
    admin_log((int)$me['id'], 'promo_add', null, ['code' => $code]);

    // Честно: код создан, но buy.php его пока НЕ применяет. Скидки
    // требуют правки расчёта цены и проверки кода при оплате — это
    // отдельный шаг, и делать вид, что он сделан, нельзя.
    json_out(['ok' => true, 'code' => $code,
              'warning' => 'Код сохранён, но оплата его пока не применяет — нужна доработка buy.php.']);
}

// ---------------------------------------------------------- журнал
if ($action === 'log') {
    $st = $pdo->query(
        'SELECT l.id, l.action, l.target_id, l.details, l.ip, l.created_at,
                a.email AS admin_email, t.email AS target_email
           FROM admin_log l
           LEFT JOIN users a ON a.id = l.admin_id
           LEFT JOIN users t ON t.id = l.target_id
          ORDER BY l.id DESC LIMIT 100'
    );
    json_out(['ok' => true, 'log' => $st->fetchAll()]);
}

// ------------------------------------------------- заявки на возврат
//
// Здесь оказываются только те заявки, по которым решать надо руками:
// в пределах 14 дней возврат уходит сам из refund_request.php, и сюда
// такая заявка попадает уже со статусом approved — как след, а не как
// работа.
if ($action === 'refund_requests') {
    try {
        $st = $pdo->query(
            "SELECT r.id, r.order_id, r.user_id, r.reason, r.status, r.refund_id,
                    r.admin_note, r.created_at, r.decided_at,
                    u.email, o.tier, o.amount, o.paid_at, o.status AS order_status,
                    DATEDIFF(NOW(), o.paid_at) AS days_passed
               FROM refund_requests r
               LEFT JOIN users  u ON u.id = r.user_id
               LEFT JOIN orders o ON o.id = r.order_id
              ORDER BY (r.status = 'pending') DESC, r.id DESC LIMIT 200"
        );
    } catch (PDOException $e) {
        error_log('DARAVE refund_requests: ' . $e->getMessage());
        json_out(['error' => 'Таблицы заявок нет. Выполните db-refund-requests.sql.'], 500);
    }
    json_out(['ok' => true, 'requests' => $st->fetchAll(),
              'free_days' => REFUND_FREE_DAYS]);
}

if ($action === 'refund_decide') {
    $id   = (int)($in['id'] ?? 0);
    $what = (string)($in['decision'] ?? '');
    $note = trim((string)($in['note'] ?? ''));
    if (!in_array($what, ['approve', 'decline'], true)) {
        json_out(['error' => 'Решение: approve или decline'], 400);
    }
    if (mb_strlen($note) > 500) {
        json_out(['error' => 'Пояснение длиннее 500 символов'], 400);
    }

    $st = $pdo->prepare('SELECT id, user_id, order_id, status FROM refund_requests WHERE id = ?');
    $st->execute([$id]);
    $req = $st->fetch();
    if (!$req) {
        json_out(['error' => 'Заявка не найдена'], 404);
    }
    if ($req['status'] !== 'pending') {
        json_out(['error' => "По заявке уже есть решение: «{$req['status']}»"], 409);
    }

    if ($what === 'decline') {
        // Отказ без пояснения — это отказ, который потом невозможно
        // объяснить ни человеку, ни себе через месяц.
        if ($note === '') {
            json_out(['error' => 'При отказе нужно пояснение'], 400);
        }
        $pdo->prepare("UPDATE refund_requests
                          SET status = 'declined', decided_by = ?, decided_at = NOW(), admin_note = ?
                        WHERE id = ? AND status = 'pending'")
            ->execute([(int)$me['id'], $note, $id]);
        admin_log((int)$me['id'], 'refund_decline', (int)$req['user_id'],
                  ['request' => $id, 'order' => (int)$req['order_id'], 'note' => $note]);
        json_out(['ok' => true, 'status' => 'declined']);
    }

    $res = refund_order((int)$req['order_id'], null, 'админка, по заявке');
    if (!($res['ok'] ?? false)) {
        // Заявку оставляем pending: решение не состоялось, и помечать её
        // выполненной значит потерять обращение.
        $code = (int)($res['http'] ?? 500);
        unset($res['http']);
        json_out($res, $code);
    }
    // status и refund_id заявке уже проставил refund_order(): он это
    // делает при любом полном возврате, откуда бы тот ни пришёл.
    // Здесь дописываем только то, чего он знать не может.
    $pdo->prepare('UPDATE refund_requests SET decided_by = ?, admin_note = ? WHERE id = ?')
        ->execute([(int)$me['id'], $note === '' ? null : $note, $id]);
    admin_log((int)$me['id'], 'refund_approve', (int)$req['user_id'],
              ['request' => $id, 'order' => (int)$req['order_id'],
               'amount' => $res['amount'], 'refund_id' => $res['refund_id']]);

    unset($res['http']);
    $res['status_request'] = 'approved';
    json_out($res);
}

json_out(['error' => 'Неизвестное действие'], 400);
