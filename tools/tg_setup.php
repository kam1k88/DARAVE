<?php
/**
 * Вписать telegram.bot_token и telegram.chat_id в site/api/_config.php.
 *
 * Лежит в tools/, а НЕ в site/: всё, что внутри site/, уезжает на сервер
 * и доступно по адресу. Скрипт, который правит конфиг, по адресу
 * доступен быть не должен ни при каких обстоятельствах.
 *
 * Запускается на хостинге по SSH:
 *
 *   TG_TOKEN='1234:AA...' TG_CHAT='123456789' php83 tg_setup.php ~/…/api/_config.php
 *
 * Токен берём из окружения, а не из аргументов: аргументы видны и в
 * истории команд, и в списке процессов, а токен — это пароль от бота.
 *
 * Файл переписывается только после того, как новый вариант прошёл
 * php -l и прочитался как массив с нужным токеном. Прежний остаётся
 * рядом с суффиксом .bak.
 */

$path = $argv[1] ?? '';
$tok  = (string)getenv('TG_TOKEN');
$chat = (string)getenv('TG_CHAT');
if ($path === '' || $tok === '' || $chat === '') {
    fwrite(STDERR, "нужны путь к _config.php и переменные TG_TOKEN, TG_CHAT\n");
    exit(1);
}
if (!is_file($path)) {
    fwrite(STDERR, "нет файла $path\n");
    exit(1);
}

$s = file_get_contents($path);
$q = fn(string $v) => "'" . str_replace(['\\', "'"], ['\\\\', "\\'"], $v) . "'";

if (preg_match("/'telegram'\s*=>\s*\[.*?\],\n/s", $s)) {
    // Блок уже есть — заменяем его целиком, а не правим значения внутри.
    //
    // Через preg_replace с подставляемым значением это ломалось: в строке
    // замены обратный слэш и $ имеют своё значение, и токен, который ими
    // оканчивается, разваливал файл. Колбэк возвращает строку как есть.
    $s = preg_replace_callback(
        "/'telegram'\s*=>\s*\[.*?\],\n/s",
        fn() => "'telegram' => [\n        'bot_token' => " . $q($tok)
              . ",\n        'chat_id'   => " . $q($chat) . ",\n    ],\n",
        $s, 1);
    $what = 'обновил';
} else {
    $pos = strrpos($s, '];');
    if ($pos === false) {
        fwrite(STDERR, "не нашёл конец массива в $path\n");
        exit(1);
    }
    $block = "\n    // Уведомления в Telegram: оплаты, возвраты, заявки на возврат.\n"
           . "    'telegram' => [\n"
           . "        'bot_token' => " . $q($tok) . ",\n"
           . "        'chat_id'   => " . $q($chat) . ",\n"
           . "    ],\n";
    $s = substr($s, 0, $pos) . $block . substr($s, $pos);
    $what = 'вписал';
}

// Проверяем ДО записи: синтаксис и то, что массив читается и содержит
// именно то, что мы клали. Конфиг с опечаткой кладёт весь сайт.
$tmp = $path . '.new';
file_put_contents($tmp, $s);

// Голого «php» в PATH на shared-хостинге может не быть (там php83, php84
// и так далее). PHP_BINARY — интерпретатор, которым запущен этот скрипт,
// то есть заведомо существующий.
exec(escapeshellarg(PHP_BINARY) . ' -l ' . escapeshellarg($tmp) . ' 2>&1', $out, $rc);

// include только после удачного php -l: файл с синтаксической ошибкой
// валит процесс намертво, такое не перехватывается.
$cfg = $rc === 0 ? @include $tmp : null;
if ($rc !== 0 || !is_array($cfg) || ($cfg['telegram']['bot_token'] ?? '') !== $tok) {
    @unlink($tmp);
    fwrite(STDERR, "не стал трогать файл: " . implode(' ', $out) . "\n");
    exit(1);
}

copy($path, $path . '.bak');
rename($tmp, $path);
echo "$what telegram в $path (прежний рядом, с .bak)\n";
