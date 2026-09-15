-- DARAVE — схема учётных записей. Выполнить один раз в phpMyAdmin
-- (база u3647882_darave.bd).
--
-- Здесь нет ни одного поля с музыкой или путями к ней: библиотека и всё,
-- что о ней известно, остаётся на машине диджея. Сервер знает только
-- «кто это» и «можно ли ему сегодня работать».

CREATE TABLE IF NOT EXISTS users (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
  email         VARCHAR(190) NOT NULL,
  pass_hash     VARCHAR(255) NOT NULL,   -- password_hash(), не наш самодельный
  tier          ENUM('free','personal','workshop') NOT NULL DEFAULT 'free',
  tier_until    DATETIME NULL,           -- NULL = бессрочно
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_login_at DATETIME NULL,
  blocked       TINYINT(1) NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Сессии браузера. Храним ХЕШ токена, а не сам токен: если базу однажды
-- сольют, украденными окажутся бесполезные строки, а не живые входы.
CREATE TABLE IF NOT EXISTS sessions (
  token_hash  CHAR(64) NOT NULL,          -- sha256 от значения в куке
  user_id     INT UNSIGNED NOT NULL,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at  DATETIME NOT NULL,
  ip          VARBINARY(16) NULL,
  user_agent  VARCHAR(255) NULL,
  PRIMARY KEY (token_hash),
  KEY idx_user (user_id),
  KEY idx_expires (expires_at),
  CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Спаривание браузера с локальным клиентом. Токен живёт минуты и
-- сгорает при первом использовании: он только для того, чтобы клиент
-- понял, от чьего имени с ним говорят.
CREATE TABLE IF NOT EXISTS pair_tokens (
  token_hash  CHAR(64) NOT NULL,
  user_id     INT UNSIGNED NOT NULL,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at  DATETIME NOT NULL,
  used_at     DATETIME NULL,
  PRIMARY KEY (token_hash),
  KEY idx_expires (expires_at),
  CONSTRAINT fk_pair_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Устройства, на которых человек работал. Нужны для двух вещей: показать
-- ему список («это мой ноутбук, а это что?») и ограничить число машин на
-- тариф, не привязывая намертво к железу.
CREATE TABLE IF NOT EXISTS devices (
  id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id     INT UNSIGNED NOT NULL,
  device_id   CHAR(64) NOT NULL,          -- отпечаток, который шлёт клиент
  name        VARCHAR(190) NULL,          -- имя компьютера, для списка
  app_version VARCHAR(40) NULL,
  first_seen  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  revoked     TINYINT(1) NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_user_device (user_id, device_id),
  CONSTRAINT fk_devices_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Неудачные входы — чтобы перебор пароля упирался в задержку.
-- Отдельная таблица, а не счётчик в users: перебирают обычно чужие
-- адреса, и заводить строку в users на каждый выдуманный email нельзя.
CREATE TABLE IF NOT EXISTS login_attempts (
  id         INT UNSIGNED NOT NULL AUTO_INCREMENT,
  email      VARCHAR(190) NOT NULL,
  ip         VARBINARY(16) NULL,
  at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_email_at (email, at),
  KEY idx_ip_at (ip, at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------- оплата
--
-- Заказы. Хранится НОМЕР платежа в ЮKassa и наш статус — никаких данных
-- карты, счёта или телефона: они живут у платёжного провайдера, и это не
-- экономия места, а отсутствие обязанностей, которые появляются вместе с
-- такими данными.
CREATE TABLE IF NOT EXISTS orders (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id       INT UNSIGNED NOT NULL,
  tier          ENUM('personal','workshop') NOT NULL,
  amount        DECIMAL(10,2) NOT NULL,
  currency      CHAR(3) NOT NULL DEFAULT 'RUB',
  payment_id    VARCHAR(64) NULL,          -- id платежа в ЮKassa
  status        ENUM('new','pending','succeeded','canceled') NOT NULL DEFAULT 'new',
  -- Ключ идемпотентности. Без него двойной клик по «Оплатить» создаёт
  -- ДВА платежа, и человек платит дважды.
  idem_key      CHAR(36) NOT NULL,
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  paid_at       DATETIME NULL,
  receipt_sent  TINYINT(1) NOT NULL DEFAULT 0,   -- чек в «Мой налог» выдан
  PRIMARY KEY (id),
  UNIQUE KEY uniq_idem (idem_key),
  KEY idx_payment (payment_id),
  KEY idx_user (user_id),
  CONSTRAINT fk_orders_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
