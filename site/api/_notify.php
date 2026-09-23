<?php
declare(strict_types=1);

require_once __DIR__ . '/_db.php';

/**
 * Уведомления владельцу магазина в Telegram.
 *
 * Зачем не почта. Письмо от darave.ru на gmail — это SPF, DKIM и
 * репутация IP общего хостинга; первое же уведомление об оплате уходит
 * в спам, и узнаёшь об этом через неделю. У Telegram нет спам-папки:
 * сообщение либо доставлено, либо в логе стоит причина отказа.
 *
 * Правила, которые здесь важнее красоты:
 *
 * 1. Уведомление НИКОГДА не ломает операцию. Деньги уже приняты или уже
 *    возвращены; исключение из-за недоступного Telegram превратило бы
 *    удачную операцию в ошибку 500. Поэтому все броски глушатся, а
 *    неудача пишется в лог вместе с текстом — так сообщение не теряется
 *    совсем, его можно прочитать в логе сайта.
 *
 * 2. Никакого parse_mode. Разметка требует экранирования, а в тексте
 *    бывают и подчёркивания в адресах, и звёздочки. Один неэкранированный
 *    знак — и Telegram отвечает 400, то есть уведомление пропадает
 *    ровно тогда, когда оно нужно. Ссылки Telegram подсвечивает сам.
 *
 * 3. Настройки нет — молчим без ошибок. Сайт должен работать до того,
 *    как бот заведён, иначе первая же оплата упадёт на пустом токене.
 *
 * Как получить bot_token и chat_id — в PAYMENTS.md, раздел
 * «Уведомления».
 */

const NOTIFY_API = 'https://api.telegram.org';

function notify_cfg(): array
{
    $c = cfg()['telegram'] ?? [];
    return [
        'token' => trim((string)($c['bot_token'] ?? '')),
        'chat'  => trim((string)($c['chat_id']   ?? '')),
    ];
}

function notify_configured(): bool
{
    $c = notify_cfg();
    return $c['token'] !== '' && $c['chat'] !== '';
}

/**
 * Отдать ответ браузеру и только потом идти в Telegram.
 *
 * ЮKassa ждёт от webhook ответа 200 и считает задержку сбоем. Сходить в
 * Telegram — это до нескольких секунд чужой сети, и держать на ней
 * платёжный сервис незачем. Под php-fpm fastcgi_finish_request()
 * закрывает соединение, и остальное выполняется уже без слушателя.
 *
 * Функции может не быть (mod_php, CLI) — тогда просто ничего не
 * происходит, и уведомление уходит до закрытия соединения. Это медленнее,
 * но не сломано.
 */
function notify_detach(): void
{
    if (function_exists('fastcgi_finish_request')) {
        @fastcgi_finish_request();
    }
}

/**
 * Собрать и отправить сообщение.
 *
 * $title — первая строка, по ней уведомление узнаётся в списке чатов.
 * $lines — пары «подпись => значение»; пустые значения выбрасываются,
 * чтобы не слать «Причина: —».
 *
 * Возвращает true, если Telegram подтвердил доставку.
 */
function notify(string $title, array $lines = []): bool
{
    $text = $title;
    foreach ($lines as $k => $v) {
        $v = is_bool($v) ? ($v ? 'да' : 'нет') : trim((string)$v);
        if ($v === '') {
            continue;
        }
        $text .= "\n" . (is_string($k) && $k !== '' ? $k . ': ' : '') . $v;
    }
    return notify_raw($text);
}

function notify_raw(string $text): bool
{
    $c = notify_cfg();
    if ($c['token'] === '' || $c['chat'] === '') {
        // Не ошибка: бот просто не заведён. Но событие всё равно должно
        // остаться в логе, иначе оплата пройдёт совсем незаметно.
        error_log('DARAVE notify (бот не настроен): ' . str_replace("\n", ' | ', $text));
        return false;
    }

    // Telegram режет сообщение на 4096 знаках. Обрезаем сами, чтобы
    // получить укороченное уведомление вместо ошибки 400.
    if (mb_strlen($text) > 3900) {
        $text = mb_substr($text, 0, 3900) . "\n…";
    }

    $url = NOTIFY_API . '/bot' . $c['token'] . '/sendMessage';
    $payload = json_encode([
        'chat_id' => $c['chat'],
        'text'    => $text,
        'disable_web_page_preview' => true,
    ], JSON_UNESCAPED_UNICODE);

    // Две попытки. Одна сетевая осечка не должна стоить уведомления об
    // оплате, а три попытки по 8 секунд — это уже минута тишины.
    $lastErr = '';
    for ($attempt = 1; $attempt <= 2; $attempt++) {
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_POST           => true,
            CURLOPT_POSTFIELDS     => $payload,
            CURLOPT_HTTPHEADER     => ['Content-Type: application/json'],
            CURLOPT_TIMEOUT        => 8,
            CURLOPT_CONNECTTIMEOUT => 4,
        ]);
        $raw  = curl_exec($ch);
        $code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $err  = curl_error($ch);
        curl_close($ch);

        if ($raw !== false && $code >= 200 && $code < 300) {
            return true;
        }
        // 400 и 403 — это не «связь подвела», а «чат не тот» или «бота
        // заблокировали». Повтор ничего не изменит, только задержит.
        $lastErr = $raw === false ? $err : ('HTTP ' . $code . ' ' . substr((string)$raw, 0, 200));
        if ($code === 400 || $code === 401 || $code === 403 || $code === 404) {
            break;
        }
        if ($attempt === 1) {
            usleep(700000);
        }
    }

    error_log('DARAVE notify не доставлено (' . $lastErr . '): '
            . str_replace("\n", ' | ', $text));
    return false;
}

/**
 * Ссылка на админку. Без якоря на конкретную карточку: якорей админка не
 * понимает, а ссылка, которая открывает не то, что обещает, хуже её
 * отсутствия. Кого искать — написано в самом уведомлении.
 */
function notify_admin_link(): string
{
    return base_url() . '/admin/';
}
