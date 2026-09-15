<?php
declare(strict_types=1);
require_once __DIR__ . '/_auth.php';
require_once __DIR__ . '/_yookassa.php';

/**
 * Создать платёж и отдать ссылку, куда отправить покупателя.
 *
 * По умолчанию СБП: на 3 900 ₽ это ~16 ₽ комиссии против ~137 ₽ по
 * карте. Карта остаётся вторым способом — «bank_card».
 */

cors();
require_post();

$u  = require_user();
$in = body();
$tier = (string)($in['tier'] ?? 'personal');
$method = (string)($in['method'] ?? 'sbp');

$tiers = tiers();
if (!isset($tiers[$tier])) {
    json_out(['error' => 'Неизвестный тариф'], 400);
}
if (!in_array($method, ['sbp', 'bank_card'], true)) {
    json_out(['error' => 'Неизвестный способ оплаты'], 400);
}

// Незакрытый заказ того же тарифа переиспользуем вместо создания нового:
// человек мог закрыть вкладку и вернуться, и плодить ему платежи незачем.
$st = db()->prepare(
    "SELECT id, payment_id, idem_key FROM orders
      WHERE user_id = ? AND tier = ? AND status IN ('new','pending')
        AND created_at > (NOW() - INTERVAL 1 HOUR)
      ORDER BY id DESC LIMIT 1"
);
$st->execute([(int)$u['id'], $tier]);
$existing = $st->fetch();

if ($existing && $existing['payment_id']) {
    $p = yk_payment((string)$existing['payment_id']);
    if (($p['_ok'] ?? false) && ($p['status'] ?? '') === 'pending') {
        $url = $p['confirmation']['confirmation_url'] ?? null;
        if ($url) {
            json_out(['ok' => true, 'payment_id' => $p['id'], 'confirmation_url' => $url,
                      'reused' => true]);
        }
    }
}

$amount = $tiers[$tier]['amount'];
$idem   = $existing['idem_key'] ?? sprintf(
    '%s-%s', bin2hex(random_bytes(8)), bin2hex(random_bytes(8))
);
$idem = substr($idem, 0, 36);

if (!$existing) {
    db()->prepare('INSERT INTO orders (user_id, tier, amount, idem_key) VALUES (?, ?, ?, ?)')
        ->execute([(int)$u['id'], $tier, $amount, $idem]);
    $order_id = (int)db()->lastInsertId();
} else {
    $order_id = (int)$existing['id'];
}

$payload = [
    'amount' => ['value' => $amount, 'currency' => 'RUB'],
    'capture' => true,          // списываем сразу, без двухстадийности
    'confirmation' => [
        'type' => 'redirect',
        'return_url' => 'https://darave.ru/app/?paid=' . $order_id,
    ],
    'description' => 'DARAVE — лицензия «' . $tiers[$tier]['title'] . '»',
    // Метаданные возвращаются в уведомлении и в API: по ним мы поймём,
    // чей это платёж, не доверяя тому, что пришлёт браузер.
    'metadata' => ['order_id' => (string)$order_id, 'user_id' => (string)$u['id'], 'tier' => $tier],
    'payment_method_data' => ['type' => $method],
];

$p = yk_request('POST', '/payments', $payload, $idem);
if (!($p['_ok'] ?? false)) {
    error_log('DARAVE buy: ' . json_encode($p, JSON_UNESCAPED_UNICODE));
    json_out(['error' => (string)($p['error'] ?? 'Не смог создать платёж')], 502);
}

db()->prepare("UPDATE orders SET payment_id = ?, status = 'pending' WHERE id = ?")
    ->execute([(string)$p['id'], $order_id]);

json_out([
    'ok' => true,
    'payment_id' => $p['id'],
    'confirmation_url' => $p['confirmation']['confirmation_url'] ?? null,
]);
