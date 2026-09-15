<?php
declare(strict_types=1);

/**
 * Подключение к базе и общие ответы API.
 *
 * PDO с исключениями и подготовленными запросами — не «правильнее», а
 * единственный вариант, при котором вставка кавычки в email не
 * превращается в SQL-инъекцию.
 */

function cfg(): array
{
    static $cfg = null;
    if ($cfg === null) {
        $path = __DIR__ . '/_config.php';
        if (!is_file($path)) {
            json_out(['error' => 'Сервер не настроен: нет _config.php'], 500);
        }
        $cfg = require $path;
    }
    return $cfg;
}

function db(): PDO
{
    static $pdo = null;
    if ($pdo === null) {
        $c = cfg()['db'];
        $dsn = sprintf('mysql:host=%s;port=%d;dbname=%s;charset=utf8mb4',
                       $c['host'], $c['port'], $c['name']);
        try {
            $pdo = new PDO($dsn, $c['user'], $c['pass'], [
                PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
                PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
                PDO::ATTR_EMULATE_PREPARES   => false,
            ]);
        } catch (PDOException $e) {
            // Текст исключения PDO содержит имя базы и пользователя —
            // наружу его отдавать незачем.
            error_log('DARAVE db: ' . $e->getMessage());
            json_out(['error' => 'База недоступна'], 500);
        }
    }
    return $pdo;
}

function json_out(array $data, int $code = 200): never
{
    http_response_code($code);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode($data, JSON_UNESCAPED_UNICODE);
    exit;
}

/** Тело запроса как массив. Принимаем JSON и обычную форму. */
function body(): array
{
    $raw = file_get_contents('php://input') ?: '';
    if ($raw !== '') {
        $j = json_decode($raw, true);
        if (is_array($j)) {
            return $j;
        }
    }
    return $_POST;
}

function client_ip(): ?string
{
    $ip = $_SERVER['REMOTE_ADDR'] ?? '';
    $packed = @inet_pton($ip);
    return $packed === false ? null : $packed;
}

/**
 * CORS. Отвечаем заголовками только знакомым origin'ам и обязательно с
 * Allow-Credentials: без него браузер не пошлёт куку сессии, и вход
 * будет «проходить», но каждый следующий запрос окажется гостевым.
 */
function cors(): void
{
    $origin = $_SERVER['HTTP_ORIGIN'] ?? '';
    if ($origin !== '' && in_array($origin, cfg()['origins'], true)) {
        header('Access-Control-Allow-Origin: ' . $origin);
        header('Access-Control-Allow-Credentials: true');
        header('Vary: Origin');
    }
    header('Access-Control-Allow-Headers: Content-Type');
    header('Access-Control-Allow-Methods: GET, POST, OPTIONS');
    if (($_SERVER['REQUEST_METHOD'] ?? '') === 'OPTIONS') {
        http_response_code(204);
        exit;
    }
}

function require_post(): void
{
    if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
        json_out(['error' => 'Только POST'], 405);
    }
}

/** Случайный токен и его хеш для хранения. */
function new_token(): array
{
    $token = bin2hex(random_bytes(32));
    return [$token, hash('sha256', $token)];
}
