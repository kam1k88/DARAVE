<?php
declare(strict_types=1);
require_once __DIR__ . '/_auth.php';
require_once __DIR__ . '/_refund.php';

/**
 * Заявка на возврат из личного кабинета.
 *
 *   POST /api/refund_request.php
 *   {"order_id": 3, "reason": "не подошло"}
 *
 * Почему не просто письмо, как было в оферте. Письмо значит: человек
 * ищет адрес, пишет, называет заказ словами; я ищу заказ по почте в
 * админке, сверяю дату, нажимаю «Вернуть». Обе стороны ждут друг друга
 * сутки из-за операции, которая занимает секунду и решается по дате
 * оплаты.
 *
 * Оферта, пункт 7, обещает безусловный возврат в течение 14 дней. Раз
 * условий нет, то и решать нечего: в пределах срока деньги уходят
 * сразу, без моего участия. Дальше срока заявка ложится в список, и
 * решаю я — но человек уже ничего не ищет и никуда не пишет.
 *
 * Возврат делает refund_order() из _refund.php — тот же код, что у
 * кнопки в админке.
 */

cors();
require_post();

$u  = require_user();
$in = body();

$order_id = (int)($in['order_id'] ?? 0);
if ($order_id <= 0) {
    json_out(['error' => 'Не указан заказ'], 400);
}

$reason = trim((string)($in['reason'] ?? ''));
if (mb_strlen($reason) > 500) {
    json_out(['error' => 'Причина длиннее 500 символов'], 400);
}
// Управляющие символы в тексте, который потом уйдёт в уведомление и в
// админку, не нужны никому, кроме того, кто ломает ими чужую вёрстку.
$reason = preg_replace('/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/u', '', $reason) ?? '';

$pdo = db();

// Заказ спрашиваем ВМЕСТЕ с user_id. Не «нашли заказ, потом сверили
// хозяина»: любая проверка после выборки однажды окажется забытой, и
// чужой order_id вернёт чужие деньги.
$st = $pdo->prepare(
    'SELECT id, tier, amount, status, paid_at, refund_amount
       FROM orders WHERE id = ? AND user_id = ?'
);
$st->execute([$order_id, (int)$u['id']]);
$order = $st->fetch();

if (!$order) {
    json_out(['error' => 'Такой оплаты у вас нет'], 404);
}
if ($order['status'] === 'refunded') {
    json_out(['error' => 'Деньги по этой оплате уже возвращены'], 409);
}
if ($order['status'] !== 'succeeded') {
    json_out(['error' => 'Эта оплата не прошла — возвращать нечего'], 409);
}

// Заявку записываем ДО возврата. Если ЮKassa откажет или связь оборвётся,
// заявка всё равно останется в списке — то есть обращение не потеряется
// из-за чужой недоступности.
try {
    $pdo->prepare('INSERT INTO refund_requests (user_id, order_id, reason) VALUES (?, ?, ?)')
        ->execute([(int)$u['id'], $order_id, $reason === '' ? null : $reason]);
} catch (PDOException $e) {
    if (($e->errorInfo[0] ?? '') === '23000') {
        $st = $pdo->prepare('SELECT status FROM refund_requests WHERE order_id = ?');
        $st->execute([$order_id]);
        $was = (string)($st->fetchColumn() ?: 'pending');
        json_out(['error' => $was === 'declined'
            ? 'По этой оплате уже был отказ. Напишите на kam1k88@gmail.com.'
            : 'Заявка по этой оплате уже принята.'], 409);
    }
    error_log('DARAVE refund_request: ' . $e->getMessage());
    json_out(['error' => 'Не удалось записать заявку. Выполнена ли db-refund-requests.sql?'], 500);
}

$days = refund_days_left($order);
$auto = refund_is_automatic($order);

if ($auto) {
    $res = refund_order($order_id, null, 'заявка из кабинета');
    if ($res['ok'] ?? false) {
        json_out([
            'ok'     => true,
            'status' => 'refunded',
            'amount' => $res['amount'],
            'note'   => 'Возврат оформлен: ' . $res['amount'] . ' ₽ вернутся тем же способом, '
                      . 'которым вы платили, обычно в течение нескольких рабочих дней. '
                      . 'Оплаченные возможности уже отключены.',
        ]);
    }
    // Деньги не ушли, но заявка записана и уведомление про отказ ЮKassa
    // уже отправлено из refund_order(). Врать про «возврат оформлен»
    // нельзя, а пугать человека кодом ошибки ЮKassa незачем.
    json_out([
        'ok'     => true,
        'status' => 'pending',
        'note'   => 'Заявка принята, но автоматический возврат не прошёл — '
                  . 'оформлю вручную. Если за три рабочих дня ничего не произойдёт, '
                  . 'напишите на kam1k88@gmail.com.',
    ]);
}

// Срок вышел (или даты оплаты нет) — решение за человеком.
notify('Заявка на возврат', [
    'Кто'     => (string)$u['email'],
    'Заказ'   => $order_id . ' (' . $order['tier'] . ', ' . $order['amount'] . ' ₽)',
    'Оплачен' => (string)($order['paid_at'] ?? 'дата неизвестна'),
    'Срок'    => $days === null
        ? 'даты оплаты нет — решать вручную'
        : ($days < 0 ? 'безусловные ' . REFUND_FREE_DAYS . ' дней прошли ' . abs($days) . ' дн. назад' : 'в пределах срока'),
    'Причина' => $reason,
    'Решить'  => notify_admin_link(),
]);

json_out([
    'ok'     => true,
    'status' => 'pending',
    'note'   => 'Заявка принята. ' . REFUND_FREE_DAYS . ' дней с оплаты уже прошли, '
              . 'поэтому решение по такой заявке я принимаю сам — отвечу в течение трёх рабочих дней.',
]);
