<?php
declare(strict_types=1);

/**
 * api/migrate.php — привести базу к нужному виду, не заходя в phpMyAdmin.
 *
 * Открывается так:
 *     https://darave.ru/api/migrate.php?secret=<selftest_secret из _config.php>
 *
 * И, чтобы выдать себе права админа, — ещё один параметр:
 *     https://darave.ru/api/migrate.php?secret=...&admin=почта@пример.ру
 *
 * Зачем отдельный файл. Схему меняют руками в панели хостинга, а панель
 * есть не всегда: пароля от phpMyAdmin может просто не оказаться под
 * рукой. Здесь те же ALTER, но выполняет их PHP, у которого доступ к
 * базе уже настроен в _config.php.
 *
 * Повторный запуск безопасен. Каждый шаг сначала смотрит в
 * information_schema и пропускает то, что уже есть, — поэтому файл не
 * ломает базу, если открыть адрес дважды, и честно пишет «уже было».
 *
 * Секрет обязателен, как в selftest.php: этот адрес меняет схему, и
 * отдавать его угадавшему нельзя. После того как всё применилось, файл
 * лучше удалить — он больше не нужен.
 */

require_once __DIR__ . '/_db.php';

header('Content-Type: text/plain; charset=utf-8');
header('Cache-Control: no-store');
header('X-Robots-Tag: noindex');

$want = (string)(cfg()['selftest_secret'] ?? '');
$got  = (string)($_GET['secret'] ?? '');
if ($want === '' || !hash_equals($want, $got)) {
    http_response_code(404);
    exit('Not found');
}

$pdo  = db();
$dbName = (string)(cfg()['db']['name'] ?? '');
$done = [];
$skip = [];
$fail = [];

/** Есть ли колонка в таблице. */
function has_column(PDO $pdo, string $db, string $table, string $col): bool
{
    $st = $pdo->prepare(
        'SELECT COUNT(*) FROM information_schema.COLUMNS
          WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND COLUMN_NAME = ?'
    );
    $st->execute([$db, $table, $col]);
    return (int)$st->fetchColumn() > 0;
}

/** Есть ли таблица. */
function has_table(PDO $pdo, string $db, string $table): bool
{
    $st = $pdo->prepare(
        'SELECT COUNT(*) FROM information_schema.TABLES
          WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?'
    );
    $st->execute([$db, $table]);
    return (int)$st->fetchColumn() > 0;
}

/** Выполнить шаг, если он ещё не выполнен. */
function step(string $title, bool $already, callable $run): void
{
    global $done, $skip, $fail;
    if ($already) { $skip[] = $title; return; }
    try {
        $run();
        $done[] = $title;
    } catch (Throwable $e) {
        $fail[] = $title . ' — ' . $e->getMessage();
    }
}

// ------------------------------------------------------------- колонки
// По одной, а не одним ALTER с четырьмя ADD COLUMN: если половина уже
// есть, общий ALTER падает целиком и не делает ничего.
foreach ([
    ['nickname',   "ALTER TABLE users ADD COLUMN nickname VARCHAR(40) NULL AFTER email"],
    ['is_admin',   "ALTER TABLE users ADD COLUMN is_admin TINYINT(1) NOT NULL DEFAULT 0 AFTER blocked"],
    ['admin_note', "ALTER TABLE users ADD COLUMN admin_note VARCHAR(500) NULL AFTER is_admin"],
    ['deleted_at', "ALTER TABLE users ADD COLUMN deleted_at DATETIME NULL AFTER created_at"],
] as [$col, $sql]) {
    step("users.$col", has_column($pdo, $dbName, 'users', $col),
         function () use ($pdo, $sql) { $pdo->exec($sql); });
}

// ------------------------------------------------------------- таблицы
step('таблица admin_log', has_table($pdo, $dbName, 'admin_log'), function () use ($pdo) {
    $pdo->exec(
        "CREATE TABLE admin_log (
           id         INT UNSIGNED NOT NULL AUTO_INCREMENT,
           admin_id   INT UNSIGNED NOT NULL,
           action     VARCHAR(40)  NOT NULL,
           target_id  INT UNSIGNED NULL,
           details    TEXT         NULL,
           ip         VARCHAR(45)  NULL,
           created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
           PRIMARY KEY (id),
           KEY idx_admin (admin_id, created_at),
           KEY idx_target (target_id, created_at)
         ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
    );
});

step('таблица promo_codes', has_table($pdo, $dbName, 'promo_codes'), function () use ($pdo) {
    $pdo->exec(
        "CREATE TABLE promo_codes (
           code        VARCHAR(32)  NOT NULL,
           percent     TINYINT UNSIGNED NULL,
           fixed_price DECIMAL(10,2)    NULL,
           tier        ENUM('personal','workshop') NULL,
           max_uses    INT UNSIGNED NOT NULL DEFAULT 1,
           used        INT UNSIGNED NOT NULL DEFAULT 0,
           expires_at  DATETIME     NULL,
           created_by  INT UNSIGNED NULL,
           created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
           PRIMARY KEY (code)
         ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
    );
});

// ------------------------------------------------------ возвраты (db-refunds)
// Если эту часть когда-то не выполнили, админка и refund.php будут
// спрашивать колонки, которых нет. Проверяем заодно.
foreach ([
    ['tier_before',       "ALTER TABLE orders ADD COLUMN tier_before ENUM('free','personal','workshop') NULL AFTER tier"],
    ['tier_until_before', "ALTER TABLE orders ADD COLUMN tier_until_before DATETIME NULL AFTER tier_before"],
    ['refund_id',         "ALTER TABLE orders ADD COLUMN refund_id VARCHAR(64) NULL AFTER payment_id"],
    ['refunded_at',       "ALTER TABLE orders ADD COLUMN refunded_at DATETIME NULL AFTER paid_at"],
    ['refund_amount',     "ALTER TABLE orders ADD COLUMN refund_amount DECIMAL(10,2) NULL AFTER refunded_at"],
] as [$col, $sql]) {
    step("orders.$col", has_column($pdo, $dbName, 'orders', $col),
         function () use ($pdo, $sql) { $pdo->exec($sql); });
}

// Статус 'refunded' в ENUM. MODIFY безопасно повторять, поэтому смотрим
// на текущее определение колонки, а не гадаем.
$st = $pdo->prepare(
    'SELECT COLUMN_TYPE FROM information_schema.COLUMNS
      WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND COLUMN_NAME = ?'
);
$st->execute([$dbName, 'orders', 'status']);
$type = (string)$st->fetchColumn();
step("orders.status + 'refunded'", str_contains($type, 'refunded'), function () use ($pdo) {
    $pdo->exec(
        "ALTER TABLE orders MODIFY status
           ENUM('new','pending','succeeded','canceled','refunded') NOT NULL DEFAULT 'new'"
    );
});

// --------------------------------------------------------------- админ
$adminEmail = trim((string)($_GET['admin'] ?? ''));
$adminNote  = '';
if ($adminEmail !== '') {
    if (!has_column($pdo, $dbName, 'users', 'is_admin')) {
        $adminNote = "Права не выдал: колонки is_admin нет (см. ошибки выше).";
    } else {
        $st = $pdo->prepare('UPDATE users SET is_admin = 1 WHERE email = ?');
        $st->execute([$adminEmail]);
        $adminNote = $st->rowCount() > 0
            ? "Права админа выданы: {$adminEmail}"
            : "Учётной записи {$adminEmail} в базе нет — права никому не выданы. "
            . "Зарегистрируйтесь на сайте этой почтой и откройте адрес ещё раз.";
    }
}

// ---------------------------------------------------------------- итог
echo "DARAVE — приведение базы к нужному виду\n";
echo "База: {$dbName}\n";
echo str_repeat('-', 56) . "\n\n";

echo $done ? "Применено:\n  " . implode("\n  ", $done) . "\n\n"
           : "Применять было нечего.\n\n";
if ($skip) echo "Уже было:\n  " . implode("\n  ", $skip) . "\n\n";
if ($fail) echo "НЕ ПОЛУЧИЛОСЬ:\n  " . implode("\n  ", $fail) . "\n\n";
if ($adminNote !== '') echo $adminNote . "\n\n";

if ($fail) {
    http_response_code(500);
    echo "Часть шагов не прошла. Ничего из удавшегося не откатывалось:\n"
       . "перечитайте ошибки, поправьте и откройте адрес ещё раз —\n"
       . "повтор безопасен, уже сделанное будет пропущено.\n";
} else {
    echo "Готово. Этот файл больше не нужен — удалите api/migrate.php,\n"
       . "чтобы адрес, меняющий схему, не оставался на сайте.\n";
}
