<?php
declare(strict_types=1);
require_once __DIR__ . '/_auth.php';
require_once __DIR__ . '/_yookassa.php';

/**
 * Что со статусом заказа. Нужен потому, что уведомление от ЮKassa может
 * прийти и через несколько секунд после возврата покупателя на сайт:
 * страница спрашивает здесь, пока не увидит «оплачено», вместо того
 * чтобы показать «что-то пошло не так» на ровном месте.
 */

cors();
$u  = require_user();
$id = (int)($_GET['id'] ?? 0);

$st = db()->prepare('SELECT id, tier, amount, status, payment_id, paid_at FROM orders WHERE id = ? AND user_id = ?');
$st->execute([$id, (int)$u['id']]);
$order = $st->fetch();
if (!$order) {
    json_out(['error' => 'Заказ не найден'], 404);
}

// Пока висит в pending — спросим у ЮKassa напрямую: уведомление могло
// потеряться, а человек стоит и смотрит в экран.
if ($order['status'] === 'pending' && $order['payment_id']) {
    $p = yk_payment((string)$order['payment_id']);
    if (($p['_ok'] ?? false)) {
        $order['remote_status'] = $p['status'] ?? null;
    }
}

$st = db()->prepare('SELECT tier, tier_until FROM users WHERE id = ?');
$st->execute([(int)$u['id']]);
json_out(['order' => $order, 'user' => $st->fetch()]);
