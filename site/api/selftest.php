<?php
declare(strict_types=1);

/**
 * api/selftest.php — «почему не работает» одной страницей.
 *
 * Открывается так:
 *     https://darave.ru/api/selftest.php?secret=<selftest_secret из _config.php>
 *
 * Зачем. Между «залил файлы» и «платёж прошёл» стоит десяток условий:
 * версия PHP, расширения, доступ к базе, наличие таблиц, правильные
 * ключи ЮKassa, доступный снаружи адрес уведомлений. Когда что-то из
 * этого не так, наружу это выглядит одинаково — «не работает», — и
 * выяснять приходится наугад. Здесь всё проверяется по очереди и
 * называется своим именем.
 *
 * Секрет обязателен: страница показывает версию PHP, имя базы и
 * состояние ключей. Это не тайна первой величины, но и не то, что стоит
 * отдавать любому, кто угадал адрес.
 */

require_once __DIR__ . '/_db.php';

header('Content-Type: text/html; charset=utf-8');
header('Cache-Control: no-store');
header('X-Robots-Tag: noindex');

$want = (string)(cfg()['selftest_secret'] ?? '');
$got  = (string)($_GET['secret'] ?? '');
if ($want === '' || !hash_equals($want, $got)) {
    http_response_code(404);
    exit('Not found');
}

/** @var array<int,array{0:string,1:string,2:string}> [уровень, что, подробности] */
$rows = [];

function ok(string $what, string $note = ''): void   { global $rows; $rows[] = ['ok', $what, $note]; }
function warn(string $what, string $note = ''): void { global $rows; $rows[] = ['warn', $what, $note]; }
function bad(string $what, string $note = ''): void  { global $rows; $rows[] = ['bad', $what, $note]; }

// ------------------------------------------------------------ окружение
$php = PHP_VERSION;
if (version_compare($php, '8.1', '>=')) {
    ok("PHP $php");
} else {
    // json_out() объявлен с типом возврата never — он появился в 8.1.
    bad("PHP $php", 'нужен 8.1 или новее: код использует тип never');
}

foreach (['pdo_mysql' => 'работа с базой',
          'curl'      => 'запросы к ЮKassa',
          'mbstring'  => 'длина пароля в символах, а не байтах',
          'openssl'   => 'https и случайные токены'] as $ext => $why) {
    extension_loaded($ext) ? ok("расширение $ext", $why)
                           : bad("расширение $ext не установлено", $why);
}

$https = (($_SERVER['HTTPS'] ?? '') === 'on')
      || (($_SERVER['HTTP_X_FORWARDED_PROTO'] ?? '') === 'https');
$https ? ok('страница открыта по https')
       : bad('страница открыта по http',
             'кука сессии ставится с флагом Secure и по http не сохранится — вход не заработает');

// Куку браузер привязывает к ИМЕНИ хоста. Если сайт отвечает и на
// darave.ru, и на www.darave.ru, человек, вошедший на одном, окажется
// гостем на другом — и это выглядит как «вход не запоминается».
$host = (string)($_SERVER['HTTP_HOST'] ?? '');
if (str_starts_with($host, 'www.')) {
    warn("сайт открыт как $host",
         'выберите ОДНО написание (с www или без) и уводите на него редиректом: '
         . 'кука сессии с darave.ru не отправится на www.darave.ru и наоборот');
} else {
    ok("хост $host");
}

// ----------------------------------------------------------------- база
try {
    $pdo = db();
    $ver = (string)$pdo->query('SELECT VERSION()')->fetchColumn();
    ok('база отвечает', "MySQL $ver, " . (string)(cfg()['db']['name'] ?? ''));

    $need = ['users', 'sessions', 'pair_tokens', 'devices', 'login_attempts', 'orders'];
    $have = $pdo->query('SHOW TABLES')->fetchAll(PDO::FETCH_COLUMN);
    $miss = array_values(array_diff($need, $have));
    if ($miss) {
        bad('не хватает таблиц: ' . implode(', ', $miss), 'выполните db.sql в phpMyAdmin');
    } else {
        $n = (int)$pdo->query('SELECT COUNT(*) FROM users')->fetchColumn();
        $o = (int)$pdo->query('SELECT COUNT(*) FROM orders')->fetchColumn();
        ok('все 6 таблиц на месте', "пользователей: $n, заказов: $o");
    }

    // Таблицы из ручных миграций. Их отсутствие не ломает вход и оплату,
    // но ломает то, что на них опирается, — и молча: кнопка возврата в
    // кабинете просто не появится, и понять почему будет негде.
    foreach (['admin_log' => 'db-admin.sql', 'refund_requests' => 'db-refund-requests.sql'] as $t => $file) {
        if (in_array($t, $have, true)) {
            ok("таблица $t на месте");
        } else {
            warn("нет таблицы $t", "выполните $file — без неё "
                . ($t === 'refund_requests'
                    ? 'заявки на возврат из личного кабинета не работают'
                    : 'админка не ведёт журнал'));
        }
    }

    // Колонки возвратов. db-refunds.sql выполняется руками, и без неё
    // возврат падает уже ПОСЛЕ того, как ЮKassa отдала деньги.
    $cols = $pdo->query('SHOW COLUMNS FROM orders')->fetchAll(PDO::FETCH_COLUMN);
    $needCols = array_values(array_diff(
        ['tier_before', 'tier_until_before', 'refund_id', 'refunded_at', 'refund_amount'], $cols));
    if ($needCols) {
        bad('в orders нет колонок для возврата: ' . implode(', ', $needCols),
            'выполните db-refunds.sql — иначе деньги вернутся, а база об этом не узнает');
    } else {
        ok('orders умеет возвраты');
    }
    if (!in_array('income_amount', $cols, true)) {
        warn('нет колонки orders.income_amount',
             'выполните db-income-amount.sql — без неё не с чем сверять удержание, '
             . 'и смена тарифа ЮKassa пройдёт незамеченной');
    }
} catch (Throwable $e) {
    bad('база недоступна', $e->getMessage());
}

// -------------------------------------------------------------- ЮKassa
$yk = cfg()['yookassa'] ?? [];
$test = (bool)($yk['test_mode'] ?? false);

if (empty($yk['shop_id']) || empty($yk['secret_key'])) {
    warn('ключи ЮKassa не заданы', 'оплата выключена, остальное работает');
} else {
    require_once __DIR__ . '/_yookassa.php';
    // Лёгкий запрос к списку платежей: проверяет ключ, ничего не создаёт
    // и денег не трогает.
    $r = yk_request('GET', '/payments?limit=1');
    if ($r['_ok'] ?? false) {
        ok($test ? 'ключи ТЕСТОВОГО магазина приняты' : 'боевые ключи приняты',
           'shop_id ' . substr((string)$yk['shop_id'], 0, 4) . '…');
        if ($test) {
            warn('включён тестовый режим',
                 'деньги не списываются; СБП в тестовом магазине не работает, '
                 . 'оплата принудительно идёт картой. Перед запуском поставьте test_mode => false '
                 . 'и подставьте боевые ключи');
        }
    } elseif (($r['_code'] ?? 0) === 401) {
        bad('ЮKassa не приняла ключи (401)',
            'перепутаны shop_id и секретный ключ, либо тестовые ключи стоят с test_mode => false');
    } else {
        bad('ЮKassa ответила ошибкой ' . (string)($r['_code'] ?? '?'),
            (string)($r['description'] ?? $r['error'] ?? ''));
    }
}

// ------------------------------------------------------- адрес уведомлений
$base = ($https ? 'https://' : 'http://') . $host;
$hook = $base . '/api/yookassa_webhook.php';
$ch = curl_init($hook);
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true, CURLOPT_POST => true,
    CURLOPT_POSTFIELDS => '{}', CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
    CURLOPT_TIMEOUT => 10,
]);
$body = curl_exec($ch);
$code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);

if ($code === 200) {
    ok('адрес уведомлений отвечает', $hook);
} elseif ($code === 0) {
    warn('не смог проверить адрес уведомлений',
         'хостинг не пускает сам к себе — проверьте вручную, ЮKassa достучится извне');
} else {
    bad("адрес уведомлений вернул $code", $hook . ' — ЮKassa считает не-200 сбоем и будет слать повторы сутки');
}

// ----------------------------------------------- удержание при отказе
//
// Цифра в коде и цифра в пункте 7 оферты обязаны совпадать: удержание,
// которого нет в публичном документе, обосновать нечем. Сверить их
// может только человек, поэтому просто показываем ставку рядом.
$feePct = cfg()['refund']['fee_percent'] ?? null;
if ($feePct === null) {
    warn('ставка удержания не задана', 'берётся значение по умолчанию 3,5 % — '
        . 'пропишите refund.fee_percent в _config.php явно');
} elseif ((float)$feePct <= 0) {
    warn('удержание выключено (' . $feePct . ' %)',
         'при отказе вернётся вся сумма, а комиссию ЮKassa вы заплатите из своего кармана');
} else {
    ok('удержание при отказе: ' . $feePct . ' %',
       'та же цифра должна стоять в пункте 7 оферты — /offer.html#refund');
}

// ------------------------------------------------------------- Telegram
//
// Проверяем getMe, а не отправку: сообщение «проверка» в чат при каждом
// открытии страницы — это способ приучить себя не читать уведомления.
// getMe отвечает на те же вопросы: жив ли токен, тот ли бот.
$tg = cfg()['telegram'] ?? [];
$tgToken = trim((string)($tg['bot_token'] ?? ''));
$tgChat  = trim((string)($tg['chat_id'] ?? ''));
if ($tgToken === '' || $tgChat === '') {
    warn('уведомления в Telegram не настроены',
         'оплаты и заявки на возврат будут видны только в логе сайта — '
         . 'заполните telegram.bot_token и telegram.chat_id в _config.php (как — в PAYMENTS.md)');
} else {
    $ch = curl_init('https://api.telegram.org/bot' . $tgToken . '/getMe');
    curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 8]);
    $raw = curl_exec($ch);
    $code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $err = curl_error($ch);
    curl_close($ch);
    $me = is_string($raw) ? json_decode($raw, true) : null;
    if ($code === 200 && is_array($me) && ($me['ok'] ?? false)) {
        ok('Telegram: бот @' . (string)($me['result']['username'] ?? '?') . ' на связи',
           'чат ' . $tgChat . ($tgChat[0] === '-' ? ' (группа)' : ''));
    } elseif ($code === 401) {
        bad('Telegram не принял токен (401)', 'токен отозван или скопирован с обрезанным началом');
    } elseif ($code === 0) {
        warn('не смог проверить Telegram', $err !== '' ? $err : 'хостинг не пускает наружу');
    } else {
        bad('Telegram ответил ошибкой ' . $code, substr((string)$raw, 0, 200));
    }
}

// ------------------------------------------------------- страницы для ЮKassa
foreach (['/' => 'витрина с ценами', '/offer.html' => 'оферта',
          '/requisites.html' => 'реквизиты с ИНН', '/app/' => 'вход'] as $path => $what) {
    $ch = curl_init($base . $path);
    curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_NOBODY => true,
                            CURLOPT_TIMEOUT => 10, CURLOPT_FOLLOWLOCATION => true]);
    curl_exec($ch);
    $c = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    if ($c === 200)      ok("$what — $path");
    elseif ($c === 0)    warn("$what — не смог проверить", $path);
    else                 bad("$what вернула $c", $path . ' — ЮKassa проверяет эти страницы вручную');
}

// ----------------------------------------------------------------- вывод
$bad  = count(array_filter($rows, fn($r) => $r[0] === 'bad'));
$warn = count(array_filter($rows, fn($r) => $r[0] === 'warn'));
$mark = ['ok' => '✓', 'warn' => '!', 'bad' => '✕'];
$col  = ['ok' => '#2fbf9b', 'warn' => '#f0a83c', 'bad' => '#f75700'];
$e    = fn($s) => htmlspecialchars((string)$s, ENT_QUOTES, 'UTF-8');
?><!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DARAVE — проверка</title>
<style>
  body { background:#001b24; color:#e6f0f3; margin:0; padding:28px 18px;
         font:15px/1.6 ui-sans-serif,system-ui,"Segoe UI",sans-serif; }
  .wrap { max-width:780px; margin:0 auto; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sum { color:#7fa0ab; font-size:14px; margin-bottom:22px; }
  .r { display:flex; gap:12px; padding:11px 0; border-bottom:1px solid #10505f; }
  .m { font-weight:700; width:16px; flex:none; }
  .w { font-size:14.5px; }
  .n { color:#7fa0ab; font-size:13px; margin-top:2px; }
  code { font-family:ui-monospace,Menlo,monospace; font-size:12.5px; }
</style></head><body><div class="wrap">
<h1>DARAVE — проверка установки</h1>
<div class="sum">
<?php if ($bad): ?>
  Не в порядке: <b style="color:<?= $col['bad'] ?>"><?= $bad ?></b><?php
  if ($warn) echo ', предупреждений: ' . $warn; ?>. Правьте сверху вниз.
<?php elseif ($warn): ?>
  Ошибок нет, предупреждений: <b style="color:<?= $col['warn'] ?>"><?= $warn ?></b>.
<?php else: ?>
  Всё сходится. Можно принимать платежи.
<?php endif; ?>
</div>
<?php foreach ($rows as [$lvl, $what, $note]): ?>
  <div class="r">
    <span class="m" style="color:<?= $col[$lvl] ?>"><?= $mark[$lvl] ?></span>
    <span><span class="w"><?= $e($what) ?></span>
    <?php if ($note): ?><div class="n"><?= $e($note) ?></div><?php endif; ?></span>
  </div>
<?php endforeach; ?>
<p class="n" style="margin-top:24px">
  Эта страница закрыта секретом из <code>_config.php</code>. Перед запуском
  можно удалить её совсем — на работу сайта она не влияет.
</p>
</div></body></html>
