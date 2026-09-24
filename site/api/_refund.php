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

/**
 * Комиссия платёжной системы при отказе от лицензии.
 *
 * ЮKassa берёт комиссию с каждого успешно принятого платежа и при
 * возврате её НЕ возвращает — это их прямой ответ, не догадка. Значит
 * возврат полной суммы означает, что за каждый отказ платит магазин, а
 * цикл «оплатил — сразу вернул» не стоит тому, кто его крутит, ничего.
 *
 * Поэтому при отказе удерживается фактически понесённый расход — та
 * самая невозвратная комиссия. Основание — статья 32 ЗоЗПП: отказаться
 * можно в любое время, но за вычетом фактически понесённых исполнителем
 * расходов. Формулировка в оферте, пункт 7; цифра здесь и цифра там
 * должны совпадать, иначе удержание нечем объяснить.
 *
 * Ставка лежит в _config.php, а не зашита в коде: тариф меняется
 * письмом от ЮKassa, а не выкладкой новой версии сайта.
 */
function refund_fee_percent(): float
{
    $p = (float)(cfg()['refund']['fee_percent'] ?? 3.5);
    // Отрицательная ставка вернула бы БОЛЬШЕ оплаченного, ставка выше
    // 100 — отрицательную выплату. И то и другое — опечатка в конфиге,
    // которая не должна доходить до денег.
    return max(0.0, min(100.0, $p));
}

function money(float $v): string
{
    return number_format($v, 2, '.', '');
}

/** Сколько удержим по этому заказу, в рублях. */
function refund_fee_for(array $order): string
{
    return money((float)$order['amount'] * refund_fee_percent() / 100);
}

/**
 * Сколько человек получит обратно при отказе от лицензии: всё, что ещё
 * не возвращено, минус удержание. Ниже нуля не опускаем — это не долг
 * покупателя, а просто «возвращать уже нечего».
 */
function refund_payout_for(array $order): string
{
    $left = (float)$order['amount'] - (float)($order['refund_amount'] ?? 0);
    return money(max(0.0, $left - (float)refund_fee_for($order)));
}

/**
 * Что ЮKassa удержала на самом деле, если она это сообщила.
 *
 * Нужна не для расчёта, а для сверки: удерживаем мы ровно столько,
 * сколько написано в оферте, а вот РАСХОЖДЕНИЕ между обещанной ставкой и
 * настоящей надо заметить сразу, а не через квартал по выписке.
 */
function refund_fee_reported(array $order): ?float
{
    $income = (float)($order['income_amount'] ?? 0);
    if ($income <= 0) {
        return null;
    }
    $fee = (float)$order['amount'] - $income;
    return $fee >= 0 ? $fee : null;
}

/** Заказ со всем, что нужно для возврата. */
function refund_order_row(PDO $pdo, int $order_id): ?array
{
    $st = $pdo->prepare(
        'SELECT id, user_id, tier, tier_before, tier_until_before, amount, status,
                payment_id, paid_at, refund_amount, income_amount
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

    // Два разных действия, и их нельзя мерить одной меркой.
    //
    // ОТКАЗ ОТ ЛИЦЕНЗИИ ($amount не задан) — это про договор, а не про
    // сумму: лицензия прекращается, тариф падает к прежнему, а денег
    // возвращается столько, сколько осталось минус удержание. Считать
    // такой возврат «неполным» только потому, что ушло не всё до копейки,
    // значит оставить человека с оплаченным доступом после отказа.
    //
    // ЧАСТИЧНЫЙ ВОЗВРАТ (сумма задана руками) — наоборот, только про
    // деньги: удержание не применяется, потому что сумму назвал человек,
    // и тариф не трогается.
    $terminate = ($amount === null || trim((string)$amount) === '');
    $fee       = $terminate ? (float)refund_fee_for($order) : 0.0;

    if ($terminate) {
        $amount = refund_payout_for($order);
    } else {
        $amount = trim((string)$amount);
        if (!preg_match('/^\d+(\.\d{1,2})?$/', $amount) || (float)$amount <= 0) {
            return ['ok' => false, 'http' => 400, 'error' => 'Сумма указана неверно'];
        }
        if ((float)$amount > $left + 0.001) {
            return ['ok' => false, 'http' => 400,
                    'error' => $already > 0
                        ? sprintf('Осталось вернуть %s ₽ из %s ₽', money($left), $order['amount'])
                        : "Больше оплаченного не вернуть: заказ на {$order['amount']} ₽"];
        }
    }
    $is_full = $terminate;

    // Возвращать нечего, но отказ всё равно должен состояться: лицензия
    // прекращается, деньги просто не идут. Так бывает, когда почти всё
    // уже вернули руками и остаток съеден удержанием.
    if ((float)$amount <= 0.001) {
        if (!$terminate) {
            return ['ok' => false, 'http' => 400, 'error' => 'Сумма указана неверно'];
        }
        return refund_finish($pdo, $order, null, '0.00', $already, true, $fee, $source,
                             'Возвращать нечего: остаток меньше удержания. Лицензия прекращена.');
    }

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
    return refund_finish($pdo, $order, $r, $amount, $already, $is_full, $fee, $source, null);
}

/**
 * Записать состоявшийся отказ и рассказать о нём.
 *
 * Отдельной функцией, потому что сюда приходят два пути: обычный, с
 * возвратом денег, и вырожденный, когда возвращать уже нечего, а
 * лицензию прекратить надо. Писать это дважды значит однажды забыть в
 * одной из копий понижение тарифа.
 *
 * $r === null — денег не переводили.
 */
function refund_finish(PDO $pdo, array $order, ?array $r, string $amount, float $already,
                       bool $is_full, float $fee, string $source, ?string $note): array
{
    $order_id   = (int)$order['id'];
    $refund_id  = $r === null ? null : (string)$r['id'];
    $status     = $r === null ? 'none' : (string)($r['status'] ?? '');
    $total_back = money($already + (float)$amount);

    $pdo->beginTransaction();
    try {
        $pdo->prepare(
            'UPDATE orders SET status = ?, refund_id = COALESCE(?, refund_id),
                    refunded_at = NOW(), refund_amount = ?
              WHERE id = ?'
        )->execute([$is_full ? 'refunded' : 'succeeded', $refund_id, $total_back, $order_id]);

        if ($is_full) {
            // Возвращаем человека туда, где он был до оплаты. Если заказ
            // старый и tier_before не записан (оплачен до этой доработки) —
            // честно опускаем в free.
            //
            // Это происходит при ЛЮБОМ отказе от лицензии, даже когда
            // денег ушло меньше уплаченного: отказ — про договор, а не
            // про сумму.
            $pdo->prepare('UPDATE users SET tier = ?, tier_until = ? WHERE id = ?')
                ->execute([$order['tier_before'] ?? 'free', $order['tier_until_before'], (int)$order['user_id']]);
        }
        $pdo->commit();
    } catch (Throwable $e) {
        $pdo->rollBack();
        // Деньги уже вернулись, а база не обновилась — это надо увидеть
        // немедленно, иначе человек останется с тарифом, за который не
        // платил, и узнать об этом будет негде.
        error_log("DARAVE refund: возврат {$refund_id} прошёл, база НЕ обновлена: " . $e->getMessage());
        notify('ВНИМАНИЕ: деньги вернулись, база не обновилась', [
            'Заказ'      => $order_id,
            'Возврат'    => (string)$refund_id,
            'Сумма'      => $amount . ' ₽',
            'Что делать' => 'Поправить заказ руками: status, refund_id, refund_amount и тариф пользователя '
                          . (int)$order['user_id'],
            'Админка'    => notify_admin_link(),
        ]);
        return ['ok' => false, 'http' => 500,
                'error' => 'Деньги возвращены, но база не обновилась. Смотрите лог сайта.',
                'refund_id' => $refund_id];
    }

    // Если отказ шёл по заявке из личного кабинета — закрываем заявку.
    // Отдельно от транзакции и под try: таблицы заявок может ещё не быть
    // (db-refund-requests.sql не выполнена), а деньги уже вернулись —
    // ронять это нельзя.
    if ($is_full) {
        try {
            $pdo->prepare("UPDATE refund_requests
                              SET status = 'approved', refund_id = ?, decided_at = NOW()
                            WHERE order_id = ? AND status = 'pending'")
                ->execute([$refund_id, $order_id]);
        } catch (Throwable $e) {
            error_log('DARAVE refund: заявку отметить не удалось: ' . $e->getMessage());
        }
    }

    $st = $pdo->prepare('SELECT email, tier, tier_until FROM users WHERE id = ?');
    $st->execute([(int)$order['user_id']]);
    $user = $st->fetch() ?: [];

    $lines = [
        'Кто'     => (string)($user['email'] ?? ('id ' . (int)$order['user_id'])),
        'Заказ'   => $order_id . ' (' . $order['tier'] . ', ' . $order['amount'] . ' ₽)',
        'Вернули' => $amount . ' ₽' . ($already > 0 ? sprintf(' (всего %s ₽)', $total_back) : ''),
    ];
    if ($fee > 0) {
        $lines['Удержали'] = money($fee) . ' ₽ — невозвратная комиссия ('
                           . rtrim(rtrim(number_format(refund_fee_percent(), 2, '.', ''), '0'), '.') . '%)';
    }
    $lines['Тариф'] = $is_full
        ? 'прекращён, вернулся к «' . ($order['tier_before'] ?? 'free') . '»'
        : 'не менялся (частичный возврат)';
    $lines['Откуда'] = $source;

    // Сверка обещанного с настоящим. Удерживаем мы ровно столько,
    // сколько написано в оферте, но если ЮKassa взяла другое, об этом
    // надо узнать сегодня, а не через квартал по выписке: расходится —
    // значит либо тариф сменили, либо в оферте стоит не та цифра.
    $real = refund_fee_reported($order);
    if ($real !== null && abs($real - $fee) > 0.51 && $fee > 0) {
        $lines['Внимание'] = 'ЮKassa удержала ' . money($real) . ' ₽, а мы удержали '
                           . money($fee) . ' ₽ — сверьте ставку в _config.php и в оферте';
    }
    $lines['Не забыть'] = 'возвратный чек в «Мой налог»';

    notify($is_full ? 'Отказ от лицензии, деньги возвращены' : 'Частичный возврат выполнен', $lines);

    return [
        'ok'          => true,
        'refund_id'   => $refund_id,
        'status'      => $status,
        'amount'      => $amount,
        'fee'         => money($fee),
        'full_refund' => $is_full,
        'order'       => ['id' => $order_id, 'was' => (string)$order['amount'], 'user_id' => (int)$order['user_id']],
        'user'        => $user,
        'note'        => $note ?? ($is_full
            ? ($fee > 0
                ? 'Лицензия прекращена. Удержано ' . money($fee) . ' ₽ — невозвратная комиссия платёжной системы.'
                : 'Лицензия прекращена, тариф возвращён к тому, что было до оплаты.')
            : 'Частичный возврат: тариф не менялся.'),
    ];
}
