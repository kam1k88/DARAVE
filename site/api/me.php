<?php
declare(strict_types=1);
require_once __DIR__ . '/_auth.php';

cors();
$u = current_user();
if ($u === null) {
    json_out(['authenticated' => false]);
}
$st = db()->prepare(
    'SELECT device_id, name, app_version, last_seen FROM devices
      WHERE user_id = ? AND revoked = 0 ORDER BY last_seen DESC'
);
$st->execute([$u['id']]);
json_out([
    'authenticated' => true,
    'user'          => user_public($u),
    'devices'       => $st->fetchAll(),
    'max_devices'   => cfg()['max_devices'][effective_tier($u)] ?? 1,
]);
