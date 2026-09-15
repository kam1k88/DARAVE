<?php
declare(strict_types=1);

require_once __DIR__ . '/_db.php';

/**
 * ЮKassa. Только то, что нужно для продажи лицензии.
 *
 * Почему СБП, а не карта. На 3 900 ₽ комиссия по СБП — около 16 ₽
 * (от 0,4 %), по карте — около 137 ₽ (порядка 3,5 %). Разница на каждой
 * продаже больше сотни рублей, и при этом для покупателя СБП — это тот
 * же телефон и та же пара нажатий. Карту оставляем вторым способом, для
 * тех, кому так привычнее.
 *
 * Самозанятому онлайн-касса не нужна: чек формируется в «Мой налог».
 * Это НЕ автоматизировано здесь намеренно — выдача чека ваша
 * обязанность, и молча делать вид, что она закрыта кодом, нельзя.
 * Я не юрист и не бухгалтер: порядок выдачи чеков и лимиты уточните
 * у ЮKassa и в ФНС, они меняются.
 */

const YK_API = 'https://api.yookassa.ru/v3';

function yk_cfg(): array
{
    $c = cfg()['yookassa'] ?? null;
    if (!$c || empty($c['shop_id']) || empty($c['secret_key'])) {
        json_out(['error' => 'Приём оплаты не настроен'], 503);
    }
    return $c;
}

/**
 * Запрос к API. Idempotence-Key обязателен на создание платежа: при
 * обрыве связи повтор с тем же ключом вернёт ТОТ ЖЕ платёж, а не создаст
 * второй.
 */
function yk_request(string $method, string $path, ?array $body = null,
                    ?string $idem = null): array
{
    $c = yk_cfg();
    $ch = curl_init(YK_API . $path);
    $headers = ['Content-Type: application/json'];
    if ($idem !== null) {
        $headers[] = 'Idempotence-Key: ' . $idem;
    }
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_CUSTOMREQUEST  => $method,
        CURLOPT_USERPWD        => $c['shop_id'] . ':' . $c['secret_key'],
        CURLOPT_HTTPHEADER     => $headers,
        CURLOPT_TIMEOUT        => 20,
        CURLOPT_CONNECTTIMEOUT => 8,
    ]);
    if ($body !== null) {
        curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($body, JSON_UNESCAPED_UNICODE));
    }
    $raw  = curl_exec($ch);
    $code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $err  = curl_error($ch);
    curl_close($ch);

    if ($raw === false) {
        error_log('DARAVE yookassa curl: ' . $err);
        return ['_ok' => false, '_code' => 0, 'error' => 'Платёжный сервис недоступен'];
    }
    $data = json_decode((string)$raw, true);
    if (!is_array($data)) {
        error_log('DARAVE yookassa bad json: ' . substr((string)$raw, 0, 300));
        return ['_ok' => false, '_code' => $code, 'error' => 'Непонятный ответ платёжного сервиса'];
    }
    $data['_ok']   = $code >= 200 && $code < 300;
    $data['_code'] = $code;
    return $data;
}

/**
 * Платёж, каким его видит ЮKassa. Это единственный источник правды о
 * том, оплачено ли.
 *
 * Уведомление на webhook приходит обычным POST'ом без подписи, то есть
 * прислать его может кто угодно, набрав ваш адрес. Поэтому уведомление
 * мы используем ТОЛЬКО как повод сходить в API и спросить самим.
 * Проверка по списку IP работала бы тоже, но список меняется, и
 * протухший список — это тихо потерянные оплаты.
 */
function yk_payment(string $payment_id): array
{
    return yk_request('GET', '/payments/' . rawurlencode($payment_id));
}

/** Тарифы. Одно место, чтобы цена на сайте и в платеже не разъехались. */
function tiers(): array
{
    return [
        'personal' => ['title' => 'Личная',     'amount' => '3900.00'],
        'workshop' => ['title' => 'Мастерская', 'amount' => '12000.00'],
    ];
}
