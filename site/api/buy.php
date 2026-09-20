<?php
declare(strict_types=1);
require_once __DIR__ . '/_auth.php';
require_once __DIR__ . '/_yookassa.php';

/**
 * Создать платёж и отдать ссылку, куда отправить покупателя.
 *
 * По умолчанию СБП: на 690 ₽ это ~3 ₽ комиссии против ~24 ₽ по карте.
 * В рублях немного, но это 3 % выручки с каждой продажи. Карта
 * остаётся вторым способом — «bank_card».
 */

cors();
require_post();

$u  = require_user();
$in = body();
$tier = (string)($in['tier'] ?? 'personal');
$method = (string)($in['method'] ?? 'sbp');

$tiers = tiers();

// Скрытый тариф для боевой проверки: 100 ₽ вместо 690. Нужен потому,
// что СБП в тестовом магазине ЮKassa не работает вовсе, и убедиться, что
// приём и возврат денег действительно работают, можно только на живых
// деньгах. Сто рублей для этого достаточно.
//
// Закрыт тем же секретом, что и selftest: иначе любой, кто прочитает
// исходник страницы, купит «Полную» за сто рублей.
if ($tier === 'test') {
    $secret = (string)(cfg()['selftest_secret'] ?? '');
    if ($secret === '' || !hash_equals($secret, (string)($in['secret'] ?? ''))) {
        json_out(['error' => 'Неизвестный тариф'], 400);
    }
    $tiers['test'] = ['title' => 'Проверка приёма оплаты', 'amount' => '100.00'];
}

if (!isset($tiers[$tier])) {
    json_out(['error' => 'Неизвестный тариф'], 400);
}
if (!in_array($method, ['sbp', 'bank_card'], true)) {
    json_out(['error' => 'Неизвестный способ оплаты'], 400);
}

// СБП в тестовом магазине ЮKassa недоступен. Молча подменяем на карту —
// иначе тестовый прогон падает с невнятным отказом, и непонятно, дело
// в ключах или в коде.
$test = (bool)(cfg()['yookassa']['test_mode'] ?? false);
if ($test && $method === 'sbp') {
    $method = 'bank_card';
}

// Незакрытый заказ того же тарифа переиспользуем вместо создания нового:
// человек мог закрыть вкладку и вернуться, и плодить ему платежи незачем.
$st = db()->prepare(
    "SELECT id, payment_id, idem_key FROM orders
      WHERE user_id = ? AND tier = ? AND status IN ('new','pending')
        AND created_at > (NOW() - INTERVAL 1 HOUR)
      ORDER BY id DESC LIMIT 1"
);
$st->execute([(int)$u['id'], $tier === 'test' ? 'personal' : $tier]);
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

// В таблице orders колонка tier — это ENUM('personal','workshop'), и
// 'test' туда не ляжет. Пишем personal: проверочный платёж и правда
// открывает «Полную», просто за сто рублей.
$db_tier = $tier === 'test' ? 'personal' : $tier;

if (!$existing) {
    db()->prepare('INSERT INTO orders (user_id, tier, amount, idem_key) VALUES (?, ?, ?, ?)')
        ->execute([(int)$u['id'], $db_tier, $amount, $idem]);
    $order_id = (int)db()->lastInsertId();
} else {
    $order_id = (int)$existing['id'];
}

$payload = [
    'amount' => ['value' => $amount, 'currency' => 'RUB'],
    'capture' => true,          // списываем сразу, без двухстадийности
    'confirmation' => [
        'type' => 'redirect',
        // Возвращаем на ТОТ ЖЕ хост, с которого человек пришёл.
        // Жёстко зашитый darave.ru ломал оплату у всех, кто открыл сайт
        // как www.darave.ru: куку сессии браузер привязывает к имени
        // хоста, и после возврата с ЮKassa человек оказывался гостем —
        // «Нужно войти» сразу после того, как заплатил.
        'return_url' => base_url() . '/app/?paid=' . $order_id,
    ],
    'description' => 'DARAVE — лицензия «' . $tiers[$tier]['title'] . '»',
    // Метаданные возвращаются в уведомлении и в API: по ним мы поймём,
    // чей это платёж, не доверяя тому, что пришлёт браузер.
    'metadata' => ['order_id' => (string)$order_id, 'user_id' => (string)$u['id'],
                   'tier' => $db_tier, 'probe' => $tier === 'test' ? '1' : '0'],
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
    'test_mode' => $test,
    'method' => $method,
]);
