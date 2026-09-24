<?php
declare(strict_types=1);
require_once __DIR__ . '/_auth.php';
require_once __DIR__ . '/_yookassa.php';
require_once __DIR__ . '/_notify.php';

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
 *
 * Здесь же — единственное место, где о состоявшейся продаже узнаёшь ты.
 * До этой доработки оплата была видна только в логе сайта, то есть
 * практически не видна. Уведомление уходит ПОСЛЕ ответа ЮKassa
 * (notify_detach), чтобы чужая сеть не держала платёжный сервис.
 */

header('Content-Type: application/json; charset=utf-8');

/**
 * Ответить ЮKassa и, если есть о чём, уведомить себя.
 *
 * Именно в таком порядке: сначала ответ, потом Telegram. Иначе каждая
 * секунда задержки Telegram — это секунда, которую ЮKassa считает
 * нашим сбоем.
 */
function hook_done(int $code, string $title = '', array $lines = []): never
{
    http_response_code($code);
    echo $code === 200 ? '{"ok":true}' : '{"ok":false}';
    if ($title !== '') {
        notify_detach();
        notify($title, $lines);
    }
    exit;
}

/** Почта покупателя для уведомления. Нет — не беда, покажем id. */
function hook_who(int $user_id): string
{
    try {
        $st = db()->prepare('SELECT email FROM users WHERE id = ?');
        $st->execute([$user_id]);
        $email = (string)($st->fetchColumn() ?: '');
        return $email !== '' ? $email : ('id ' . $user_id);
    } catch (Throwable $e) {
        return 'id ' . $user_id;
    }
}

$raw = file_get_contents('php://input') ?: '';
$in  = json_decode($raw, true);
$pid = is_array($in) ? (string)($in['object']['id'] ?? '') : '';

if ($pid === '') {
    hook_done(200);
}

$p = yk_payment($pid);
if (!($p['_ok'] ?? false)) {
    // Не смогли проверить — отвечаем 500, чтобы ЮKassa повторила позже.
    // Это как раз тот случай, когда повтор нужен. Уведомление не шлём:
    // если API ЮKassa лежит, повторы идут пачками, и телефон будет
    // звенеть вместо того, чтобы сообщить что-то новое. Лог остаётся.
    error_log('DARAVE webhook: не смог получить платёж ' . $pid);
    hook_done(500);
}

$status   = (string)($p['status'] ?? '');
$order_id = (int)($p['metadata']['order_id'] ?? 0);
$tier     = (string)($p['metadata']['tier'] ?? '');

$st = db()->prepare('SELECT id, user_id, tier, status, amount FROM orders WHERE id = ? AND payment_id = ?');
$st->execute([$order_id, $pid]);
$order = $st->fetch();
if (!$order) {
    // Деньги у ЮKassa есть, а заказа под них нет. Молча терять такое
    // нельзя: человек заплатил и ничего не получил.
    error_log("DARAVE webhook: заказ {$order_id} не найден для платежа {$pid}");
    hook_done(200, $status === 'succeeded' ? 'Оплата без заказа — разобраться' : '', [
        'Платёж' => $pid,
        'Заказ в метаданных' => $order_id ?: 'не указан',
        'Сумма'  => (string)($p['amount']['value'] ?? '?') . ' ₽',
        'Что делать' => 'Найти платёж в кабинете ЮKassa и выдать тариф руками либо вернуть деньги',
        'Админка' => notify_admin_link(),
    ]);
}

// Уже обработан — выходим молча. Уведомления приходят по нескольку раз,
// это нормальная работа сервиса, а не ошибка. Заодно это защита от
// второго уведомления в Telegram по той же продаже.
if ($order['status'] === 'succeeded') {
    hook_done(200);
}

if ($status === 'succeeded') {
    // Сверяем сумму: метаданные мы задавали сами, но цена могла
    // измениться между созданием платежа и оплатой.
    $paid = (string)($p['amount']['value'] ?? '0');
    if ((float)$paid + 0.001 < (float)$order['amount']) {
        error_log("DARAVE webhook: недоплата по заказу {$order_id}: {$paid} из {$order['amount']}");
        hook_done(200, 'Недоплата — тариф не выдан', [
            'Кто'    => hook_who((int)$order['user_id']),
            'Заказ'  => $order_id,
            'Пришло' => $paid . ' ₽ из ' . $order['amount'] . ' ₽',
            'Платёж' => $pid,
            'Что делать' => 'Решить: доплата, возврат или выдать тариф руками',
            'Админка' => notify_admin_link(),
        ]);
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

        // income_amount — сколько ЮKassa реально зачислит после своей
        // комиссии. Поле есть не у всех магазинов, поэтому пишем только
        // когда оно пришло: пустое значение затёрло бы уже известное.
        // Нужно не для расчётов, а для сверки удержания с реальностью.
        $income = (string)($p['income_amount']['value'] ?? '');
        $pdo->prepare("UPDATE orders SET status='succeeded', paid_at=NOW(),
                              tier_before=?, tier_until_before=?,
                              income_amount=COALESCE(?, income_amount) WHERE id=?")
            ->execute([$was['tier'], $was['tier_until'],
                       $income !== '' ? $income : null, $order_id]);
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
        // Деньги приняты, тариф не выдан. Это худшее из состояний, и
        // узнать о нём из лога через неделю нельзя. ЮKassa повторит
        // уведомление, но если причина в базе, повтор не поможет.
        hook_done(500, 'Оплата пришла, база НЕ обновилась', [
            'Кто'    => hook_who((int)$order['user_id']),
            'Заказ'  => $order_id . ' (' . $order['amount'] . ' ₽)',
            'Платёж' => $pid,
            'Ошибка' => $e->getMessage(),
            'Что делать' => 'Выдать тариф руками в админке — ЮKassa будет повторять уведомление сутки',
            'Админка' => notify_admin_link(),
        ]);
    }
    error_log("DARAVE: оплачен заказ {$order_id}, пользователь {$order['user_id']}, тариф {$tier}");

    $lines = [
        'Кто'   => hook_who((int)$order['user_id']),
        'Тариф' => $final,
        'Сумма' => $paid . ' ₽',
        'Заказ' => (string)$order_id,
    ];
    if ($final !== $new) {
        $lines['Внимание'] = 'оплачен «' . $new . '», но у человека уже «' . $had . '» — не понижали';
    }
    $lines['Не забыть'] = 'чек в «Мой налог»';
    hook_done(200, 'Оплата', $lines);
} elseif ($status === 'canceled') {
    // Отмену не уведомляем: её причина почти всегда «закрыл страницу
    // оплаты», и на каждую такую присылать сообщение значит приучить
    // себя не читать уведомления.
    db()->prepare("UPDATE orders SET status='canceled' WHERE id=?")->execute([$order_id]);
}

hook_done(200);
