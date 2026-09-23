<?php
declare(strict_types=1);

require_once __DIR__ . '/_db.php';
require_once __DIR__ . '/_yookassa.php';
require_once __DIR__ . '/_notify.php';

/**
 * Возврат денег — одна реализация на всех.
 *
 * Раньше это лежало прямо в refund.php. Как только возврат научился
 * запускаться сам по заявке из личного кабинета, стало два входа в одну
 * операцию с деньгами. Две копии проверок расходятся: в одной забудут
 * про частичный возврат, в другой про понижение тарифа, и разница
 * обнаружится на живых деньгах. Поэтому здесь ровно одна функция,
 * а refund.php и refund_request.php — только входы в неё.
 *
 * Срок безусловного возврата обещан в оферте, пункт 7.
 */

const REFUND_FREE_DAYS = 14;

/** Заказ со всем, что нужно для возврата. */
function refund_order_row(PDO $pdo, int $order_id): ?array
{
    $st = $pdo->prepare(
        'SELECT id, user_id, tier, tier_before, tier_until_before, amount, status,
                payment_id, paid_at, refund_amount
           FROM orders WHERE id = ?'
    );
    $st->execute([$order_id]);
    return $st->fetch() ?: null;
}

/**
 * Сколько дней осталось от безусловного срока. null — дату оплаты не
 * знаем, значит решать человеку, а не сроку.
 */
function refund_days_left(array $order): ?int
{
    if (empty($order['paid_at'])) {
        return null;
    }
    $ts = strtotime((string)$order['paid_at']);
    if ($ts === false) {
        return null;
    }
    $passed = (int)floor((time() - $ts) / 86400);
    return REFUND_FREE_DAYS - $passed;
}

/**
 * Возвращать ли без вопросов.
 *
 * Только полный возврат нетронутого заказа в пределах срока. Если по
 * заказу уже был частичный возврат, автоматика молчит: там кто-то что-то
 * решал руками, и дорешивать за него нельзя.
 */
function refund_is_automatic(array $order): bool
{
    if ((string)$order['status'] !== 'succeeded') return false;
    if ($order['refund_amount'] !== null)         return false;
    $left = refund_days_left($order);
    return $left !== null && $left >= 0;
}

/**
 * Собственно возврат.
 *
 * $amount — сумма или null для «всё, что ещё не возвращено».
 * $source — откуда пришли, попадает в уведомление: «админка», «заявка»,
 *           «скрипт». Нужно, чтобы по уведомлению было видно, ты это
 *           сделал или оно само.
 *
 * Возвращает массив с ok=true либо ok=false, error и http.
 * json_out() здесь НЕ вызывается: решение, что отдать наружу, за
 * вызывающим — у заявки и у админки ответы разные.
 */
function refund_order(int $order_id, ?string $amount = null, string $source = 'скрипт'): array
{
    $pdo = db();
    $order = refund_order_row($pdo, $order_id);

    if (!$order)                          return ['ok' => false, 'http' => 404, 'error' => "Заказ {$order_id} не найден"];
    if ($order['status'] === 'refunded')  return ['ok' => false, 'http' => 409, 'error' => 'По этому заказу уже был возврат'];
    if ($order['status'] !== 'succeeded') return ['ok' => false, 'http' => 409, 'error' => "Заказ в состоянии «{$order['status']}» — возвращать нечего"];
    if (!$order['payment_id'])            return ['ok' => false, 'http' => 409, 'error' => 'У заказа нет номера платежа'];

    // Сколько ещё можно вернуть. refund_amount — это УЖЕ возвращённое
    // всего, а не последний возврат: иначе два частичных возврата по
    // 400 ₽ с заказа на 690 ₽ выглядели бы законными, и отказала бы
    // только ЮKassa — то есть ошибку мы бы увидели в чужом ответе.
    $total   = (float)$order['amount'];
    $already = (float)($order['refund_amount'] ?? 0);
    $left    = $total - $already;
    if ($left <= 0.001) {
        return ['ok' => false, 'http' => 409, 'error' => 'По этому заказу уже возвращена вся сумма'];
    }

    $amount = trim((string)($amount ?? '')) ?: number_format($left, 2, '.', '');
    if (!preg_match('/^\d+(\.\d{1,2})?$/', $amount) || (float)$amount <= 0) {
        return ['ok' => false, 'http' => 400, 'error' => 'Сумма указана неверно'];
    }
    if ((float)$amount > $left + 0.001) {
        return ['ok' => false, 'http' => 400,
                'error' => $already > 0
                    ? sprintf('Осталось вернуть %s ₽ из %s ₽', number_format($left, 2, '.', ''), $order['amount'])
                    : "Больше оплаченного не вернуть: заказ на {$order['amount']} ₽"];
    }
    $is_full = abs($already + (float)$amount - $total) < 0.001;

    // Ключ идемпотентности от заказа, суммы и уже возвращённого:
    // повторный вызов с теми же данными вернёт ТОТ ЖЕ возврат, а не
    // сделает второй. Для операции с деньгами это не роскошь — двойной
    // клик по кнопке стоит денег.
    $idem = substr(hash('sha256', "refund:{$order_id}:{$amount}:{$already}"), 0, 36);

    $r = yk_request('POST', '/refunds', [
        'amount'      => ['value' => $amount, 'currency' => 'RUB'],
        'payment_id'  => (string)$order['payment_id'],
        'description' => "Возврат по заказу {$order_id}",
    ], $idem);

    if (!($r['_ok'] ?? false)) {
        error_log('DARAVE refund: ' . json_encode($r, JSON_UNESCAPED_UNICODE));
        notify('Возврат НЕ прошёл', [
            'Заказ'   => $order_id,
            'Сумма'   => $amount . ' ₽',
            'Откуда'  => $source,
            'Причина' => (string)($r['description'] ?? $r['error'] ?? 'ЮKassa отказала'),
            'Что делать' => 'Открыть кабинет ЮKassa и вернуть руками, либо повторить в админке',
        ]);
        return ['ok' => false, 'http' => 502,
                'error' => (string)($r['description'] ?? $r['error'] ?? 'ЮKassa отказала в возврате'),
                'code'  => $r['_code'] ?? 0];
    }

    // ЮKassa отвечает succeeded сразу либо pending (редко, по некоторым
    // способам оплаты). Записываем и то и другое: деньги уже в пути.
    $status = (string)($r['status'] ?? '');

    $pdo->beginTransaction();
    try {
        $pdo->prepare(
            'UPDATE orders SET status = ?, refund_id = ?, refunded_at = NOW(), refund_amount = ?
              WHERE id = ?'
        )->execute([
            $is_full ? 'refunded' : 'succeeded',
            (string)$r['id'],
            number_format($already + (float)$amount, 2, '.', ''),
            $order_id,
        ]);

        if ($is_full) {
            // Возвращаем человека туда, где он был до оплаты. Если заказ
            // старый и tier_before не записан (оплачен до этой доработки) —
            // честно опускаем в free.
            $pdo->prepare('UPDATE users SET tier = ?, tier_until = ? WHERE id = ?')
                ->execute([$order['tier_before'] ?? 'free', $order['tier_until_before'], (int)$order['user_id']]);
        }
        $pdo->commit();
    } catch (Throwable $e) {
        $pdo->rollBack();
        // Деньги уже вернулись, а база не обновилась — это надо увидеть
        // немедленно, иначе человек останется с тарифом, за который не
        // платил, и узнать об этом будет негде.
        error_log("DARAVE refund: возврат {$r['id']} прошёл, база НЕ обновлена: " . $e->getMessage());
        notify('ВНИМАНИЕ: деньги вернулись, база не обновилась', [
            'Заказ'      => $order_id,
            'Возврат'    => (string)$r['id'],
            'Сумма'      => $amount . ' ₽',
            'Что делать' => 'Поправить заказ руками: status, refund_id, refund_amount и тариф пользователя '
                          . (int)$order['user_id'],
            'Админка'    => notify_admin_link(),
        ]);
        return ['ok' => false, 'http' => 500,
                'error' => 'Деньги возвращены, но база не обновилась. Смотрите лог сайта.',
                'refund_id' => $r['id']];
    }

    // Если возврат шёл по заявке из личного кабинета — закрываем заявку.
    // Отдельно от транзакции и под try: таблицы заявок может ещё не быть
    // (db-refund-requests.sql не выполнена), а деньги уже вернулись —
    // ронять это нельзя.
    if ($is_full) {
        try {
            $pdo->prepare("UPDATE refund_requests
                              SET status = 'approved', refund_id = ?, decided_at = NOW()
                            WHERE order_id = ? AND status = 'pending'")
                ->execute([(string)$r['id'], $order_id]);
        } catch (Throwable $e) {
            error_log('DARAVE refund: заявку отметить не удалось: ' . $e->getMessage());
        }
    }

    $st = $pdo->prepare('SELECT email, tier, tier_until FROM users WHERE id = ?');
    $st->execute([(int)$order['user_id']]);
    $user = $st->fetch() ?: [];

    notify($is_full ? 'Возврат выполнен' : 'Частичный возврат выполнен', [
        'Кто'     => (string)($user['email'] ?? ('id ' . (int)$order['user_id'])),
        'Заказ'   => $order_id . ' (' . $order['tier'] . ', ' . $order['amount'] . ' ₽)',
        'Вернули' => $amount . ' ₽' . ($already > 0 ? sprintf(' (всего %s ₽)', number_format($already + (float)$amount, 2, '.', '')) : ''),
        'Тариф'   => $is_full
            ? 'возвращён к «' . ($order['tier_before'] ?? 'free') . '»'
            : 'не менялся (частичный возврат)',
        'Откуда'  => $source,
        'Не забыть' => 'возвратный чек в «Мой налог»',
    ]);

    return [
        'ok'          => true,
        'refund_id'   => $r['id'],
        'status'      => $status,
        'amount'      => $amount,
        'full_refund' => $is_full,
        'order'       => ['id' => $order_id, 'was' => (string)$order['amount'], 'user_id' => (int)$order['user_id']],
        'user'        => $user,
        'note'        => $is_full
            ? 'Тариф возвращён к тому, что было до оплаты.'
            : 'Частичный возврат: тариф не менялся.',
    ];
}
