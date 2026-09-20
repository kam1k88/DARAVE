<?php
declare(strict_types=1);
require_once __DIR__ . '/_auth.php';
require_once __DIR__ . '/_yookassa.php';

/**
 * Возврат денег по заказу.
 *
 * Зачем это кодом, а не руками в кабинете ЮKassa. В кабинете вы вернёте
 * деньги — и только. Тариф в вашей базе останется повышенным, человек
 * продолжит пользоваться оплаченным, а вы об этом даже не узнаете.
 * Здесь возврат и понижение тарифа — одна операция.
 *
 * Возврат в оферте обещан безусловный в течение 14 дней, так что нужен
 * он будет регулярно, а не только для проверки.
 *
 * Вызов (curl или из браузера через консоль):
 *
 *   POST /api/refund.php
 *   {"order_id": 3, "secret": "<selftest_secret>"}
 *
 * Необязательно: "amount": "50.00" — частичный возврат. Тариф при
 * частичном возврате НЕ понижается: человек что-то доплатил и что-то
 * вернул, решение тут за вами.
 *
 * Закрыт тем же секретом, что selftest.php. Отдельной админки у вас
 * пока нет, а делать возврат доступным по обычному входу нельзя: это
 * движение денег.
 */

cors();
require_post();

$in = body();

// Два входа, оба надёжные.
//
// Секрет — для скриптов и боевой проверки из PROBE.md, когда браузера и
// сессии нет вовсе. Админская сессия — для кнопки в админке: гонять
// человека за секретом в _config.php на сервере каждый раз значит
// сделать кнопку, которой не пользуются.
$secret = (string)(cfg()['selftest_secret'] ?? '');
$bySecret = $secret !== '' && hash_equals($secret, (string)($in['secret'] ?? ''));
$admin = null;
if (!$bySecret) {
    $u = current_user();
    if ($u !== null && (int)($u['is_admin'] ?? 0) === 1) {
        $admin = $u;
    }
}
if (!$bySecret && $admin === null) {
    // 404, а не 403: постороннему незачем знать, что такой адрес есть.
    json_out(['error' => 'Not found'], 404);
}

$order_id = (int)($in['order_id'] ?? 0);
if ($order_id <= 0) {
    json_out(['error' => 'Нужен order_id'], 400);
}

$pdo = db();
$st = $pdo->prepare(
    'SELECT id, user_id, tier, tier_before, tier_until_before, amount, status, payment_id
       FROM orders WHERE id = ?'
);
$st->execute([$order_id]);
$order = $st->fetch();

if (!$order)                          json_out(['error' => "Заказ {$order_id} не найден"], 404);
if ($order['status'] === 'refunded')  json_out(['error' => 'По этому заказу уже был возврат'], 409);
if ($order['status'] !== 'succeeded') json_out(['error' => "Заказ в состоянии «{$order['status']}» — возвращать нечего"], 409);
if (!$order['payment_id'])            json_out(['error' => 'У заказа нет номера платежа'], 409);

$full   = (string)$order['amount'];
$amount = trim((string)($in['amount'] ?? '')) ?: $full;
if (!preg_match('/^\d+(\.\d{1,2})?$/', $amount) || (float)$amount <= 0) {
    json_out(['error' => 'Сумма указана неверно'], 400);
}
if ((float)$amount > (float)$full + 0.001) {
    json_out(['error' => "Больше оплаченного не вернуть: заказ на {$full} ₽"], 400);
}
$is_full = abs((float)$amount - (float)$full) < 0.001;

// Ключ идемпотентности от номера заказа и суммы: повторный вызов с теми
// же данными вернёт ТОТ ЖЕ возврат, а не сделает второй. Для операции с
// деньгами это не роскошь — двойной клик по кнопке стоит денег.
$idem = substr(hash('sha256', "refund:{$order_id}:{$amount}"), 0, 36);

$r = yk_request('POST', '/refunds', [
    'amount'     => ['value' => $amount, 'currency' => 'RUB'],
    'payment_id' => (string)$order['payment_id'],
    'description' => "Возврат по заказу {$order_id}",
], $idem);

if (!($r['_ok'] ?? false)) {
    error_log('DARAVE refund: ' . json_encode($r, JSON_UNESCAPED_UNICODE));
    json_out([
        'error' => (string)($r['description'] ?? $r['error'] ?? 'ЮKassa отказала в возврате'),
        'code'  => $r['_code'] ?? 0,
    ], 502);
}

// ЮKassa отвечает succeeded сразу либо pending (редко, по некоторым
// способам оплаты). Записываем и то и другое: деньги уже в пути.
$status = (string)($r['status'] ?? '');

$pdo->beginTransaction();
try {
    $pdo->prepare(
        'UPDATE orders SET status = ?, refund_id = ?, refunded_at = NOW(), refund_amount = ?
          WHERE id = ?'
    )->execute([$is_full ? 'refunded' : 'succeeded', (string)$r['id'], $amount, $order_id]);

    if ($is_full) {
        // Возвращаем человека туда, где он был до оплаты. Если заказ
        // старый и tier_before не записан (оплачен до этой доработки) —
        // честно опускаем в free.
        $back_tier  = $order['tier_before'] ?? 'free';
        $back_until = $order['tier_until_before'];
        $pdo->prepare('UPDATE users SET tier = ?, tier_until = ? WHERE id = ?')
            ->execute([$back_tier, $back_until, (int)$order['user_id']]);
    }
    $pdo->commit();
} catch (Throwable $e) {
    $pdo->rollBack();
    // Деньги уже вернулись, а база не обновилась — это надо увидеть
    // в логе, иначе человек останется с тарифом, за который не платил.
    error_log("DARAVE refund: возврат {$r['id']} прошёл, база НЕ обновлена: " . $e->getMessage());
    json_out([
        'error' => 'Деньги возвращены, но база не обновилась. Смотрите лог сайта.',
        'refund_id' => $r['id'],
    ], 500);
}

if ($admin !== null) {
    admin_log((int)$admin['id'], 'refund', (int)$order['user_id'],
              ['order' => $order_id, 'amount' => $amount, 'refund_id' => $r['id'] ?? null]);
}

$st = $pdo->prepare('SELECT email, tier, tier_until FROM users WHERE id = ?');
$st->execute([(int)$order['user_id']]);

json_out([
    'ok'          => true,
    'refund_id'   => $r['id'],
    'status'      => $status,
    'amount'      => $amount,
    'full_refund' => $is_full,
    'order'       => ['id' => $order_id, 'was' => $full],
    'user'        => $st->fetch(),
    'note'        => $is_full
        ? 'Тариф возвращён к тому, что было до оплаты.'
        : 'Частичный возврат: тариф не менялся.',
]);
