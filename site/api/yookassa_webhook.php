<?php
declare(strict_types=1);
require_once __DIR__ . '/_auth.php';
require_once __DIR__ . '/_yookassa.php';

/**
 * Уведомление от ЮKassa.
 *
 * Тело уведомления НЕ ПОДПИСАНО, то есть прислать такой POST может кто
 * угодно, узнав адрес. Поэтому из него берётся ровно одно — номер
 * платежа, — а всё остальное спрашивается у API напрямую. Поверить телу
 * на слово значит раздавать лицензии всем, кто набрал этот URL.
 *
 * Отвечаем 200 даже на чужой мусор: ЮKassa считает не-200 сбоем и будет
 * повторять уведомление сутки.
 */

header('Content-Type: application/json; charset=utf-8');

$raw = file_get_contents('php://input') ?: '';
$in  = json_decode($raw, true);
$pid = is_array($in) ? (string)($in['object']['id'] ?? '') : '';

if ($pid === '') {
    http_response_code(200);
    echo '{"ok":true}';
    exit;
}

$p = yk_payment($pid);
if (!($p['_ok'] ?? false)) {
    // Не смогли проверить — отвечаем 500, чтобы ЮKassa повторила позже.
    // Это как раз тот случай, когда повтор нужен.
    error_log('DARAVE webhook: не смог получить платёж ' . $pid);
    http_response_code(500);
    echo '{"ok":false}';
    exit;
}

$status   = (string)($p['status'] ?? '');
$order_id = (int)($p['metadata']['order_id'] ?? 0);
$tier     = (string)($p['metadata']['tier'] ?? '');

$st = db()->prepare('SELECT id, user_id, tier, status, amount FROM orders WHERE id = ? AND payment_id = ?');
$st->execute([$order_id, $pid]);
$order = $st->fetch();
if (!$order) {
    error_log("DARAVE webhook: заказ {$order_id} не найден для платежа {$pid}");
    http_response_code(200);
    echo '{"ok":true}';
    exit;
}

// Уже обработан — выходим молча. Уведомления приходят по нескольку раз,
// это нормальная работа сервиса, а не ошибка.
if ($order['status'] === 'succeeded') {
    http_response_code(200);
    echo '{"ok":true}';
    exit;
}

if ($status === 'succeeded') {
    // Сверяем сумму: метаданные мы задавали сами, но цена могла
    // измениться между созданием платежа и оплатой.
    $paid = (string)($p['amount']['value'] ?? '0');
    if ((float)$paid + 0.001 < (float)$order['amount']) {
        error_log("DARAVE webhook: недоплата по заказу {$order_id}: {$paid} из {$order['amount']}");
        http_response_code(200);
        echo '{"ok":true}';
        exit;
    }
    $pdo = db();
    $pdo->beginTransaction();
    try {
        // Запоминаем тариф ДО повышения. При возврате человека надо
        // вернуть туда, где он был, а не просто в 'free': иначе тот, кто
        // купил «Полную», потом доплатил за «С разбором» и попросил
        // вернуть деньги за доплату, потерял бы заодно и первую покупку.
        $st = $pdo->prepare('SELECT tier, tier_until FROM users WHERE id = ? FOR UPDATE');
        $st->execute([(int)$order['user_id']]);
        $was = $st->fetch() ?: ['tier' => 'free', 'tier_until' => null];

        $pdo->prepare("UPDATE orders SET status='succeeded', paid_at=NOW(),
                              tier_before=?, tier_until_before=? WHERE id=?")
            ->execute([$was['tier'], $was['tier_until'], $order_id]);
        // Лицензия БЕССРОЧНАЯ: tier_until = NULL. Мы продаём разовую
        // покупку, а не подписку (почему — в PRICING.md). NULL здесь не
        // «неизвестно когда», а «срок не истекает»: так он и читается
        // в tier_active() и так подписан в db.sql.
        //
        // Оплата не должна ПОНИЖАТЬ тариф. Если человек уже купил
        // «С разбором», а потом зачем-то оплатил «Полную» — он остаётся
        // на «С разбором». Иначе покупка отнимала бы оплаченное, и
        // разбираться с этим пришлось бы письмами.
        $rank = ['free' => 0, 'personal' => 1, 'workshop' => 2];
        $new  = (string)($tier ?: $order['tier']);
        $had  = (string)($was['tier'] ?? 'free');
        $had_active = $had !== 'free'
            && ($was['tier_until'] === null
                || strtotime((string)$was['tier_until']) > time());
        $final = ($had_active && ($rank[$had] ?? 0) > ($rank[$new] ?? 0)) ? $had : $new;
        if ($final !== $new) {
            error_log("DARAVE webhook: заказ {$order_id} на «{$new}», "
                    . "но у пользователя уже «{$had}» — тариф не понижаем");
        }

        $pdo->prepare('UPDATE users SET tier = ?, tier_until = NULL WHERE id = ?')
            ->execute([$final, (int)$order['user_id']]);
        $pdo->commit();
    } catch (Throwable $e) {
        $pdo->rollBack();
        error_log('DARAVE webhook: ' . $e->getMessage());
        http_response_code(500);
        echo '{"ok":false}';
        exit;
    }
    error_log("DARAVE: оплачен заказ {$order_id}, пользователь {$order['user_id']}, тариф {$tier}");
} elseif ($status === 'canceled') {
    db()->prepare("UPDATE orders SET status='canceled' WHERE id=?")->execute([$order_id]);
}

http_response_code(200);
echo '{"ok":true}';
