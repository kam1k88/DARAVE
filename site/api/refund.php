<?php
declare(strict_types=1);
require_once __DIR__ . '/_auth.php';
require_once __DIR__ . '/_refund.php';

/**
 * Возврат денег по заказу — вход для тебя, не для покупателя.
 *
 * Зачем это кодом, а не руками в кабинете ЮKassa. В кабинете вы вернёте
 * деньги — и только. Тариф в вашей базе останется повышенным, человек
 * продолжит пользоваться оплаченным, а вы об этом даже не узнаете.
 * Здесь возврат и понижение тарифа — одна операция.
 *
 * Сама операция живёт в _refund.php: её же запускает заявка из личного
 * кабинета (refund_request.php). Две копии проверок на деньгах — это
 * две разные логики, и расходиться они начнут молча.
 *
 * Вызов (curl или из браузера через консоль):
 *
 *   POST /api/refund.php
 *   {"order_id": 3, "secret": "<selftest_secret>"}
 *
 * Необязательно: "amount": "50.00" — частичный возврат. Тариф при
 * частичном возврате НЕ понижается: человек что-то доплатил и что-то
 * вернул, решение тут за вами.
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

$amount = trim((string)($in['amount'] ?? ''));
$res = refund_order($order_id, $amount === '' ? null : $amount,
                    $admin !== null ? 'админка' : 'скрипт');

if (!($res['ok'] ?? false)) {
    $code = (int)($res['http'] ?? 500);
    unset($res['http']);
    json_out($res, $code);
}

if ($admin !== null) {
    admin_log((int)$admin['id'], 'refund', (int)($res['order']['user_id'] ?? 0),
              ['order' => $order_id, 'amount' => $res['amount'], 'refund_id' => $res['refund_id']]);
}

unset($res['http']);
json_out($res);
