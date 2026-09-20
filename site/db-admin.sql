-- Профили, админка и журнал действий.
-- Выполнить в phpMyAdmin поверх существующей базы. Безопасно повторять:
-- все ALTER написаны так, чтобы ошибка «колонка уже есть» была видна и
-- ничего не ломала.

-- --------------------------------------------------------------- профиль
ALTER TABLE users
  ADD COLUMN nickname   VARCHAR(40)  NULL AFTER email,
  ADD COLUMN is_admin   TINYINT(1)   NOT NULL DEFAULT 0 AFTER blocked,
  ADD COLUMN admin_note VARCHAR(500) NULL AFTER is_admin,
  ADD COLUMN deleted_at DATETIME     NULL AFTER created_at;

-- deleted_at, а не DELETE FROM users.
--
-- У orders стоит ON DELETE CASCADE: удаление пользователя снесло бы его
-- заказы, а это документы по НПД — суммы, номера платежей, возвраты.
-- Поэтому «удалить учётную запись» обезличивает: почта затирается,
-- пароль становится невалидным, заказы остаются.

-- ------------------------------------------------------- журнал админа
-- Любое действие с чужим тарифом или деньгами должно оставлять след.
-- Без журнала на вопрос «почему у этого человека Полная» ответа нет.
CREATE TABLE IF NOT EXISTS admin_log (
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------ промокоды
-- Заготовка под скидки. Пока ни одним кодом никто не пользуется —
-- таблица есть, чтобы не менять схему, когда понадобится.
CREATE TABLE IF NOT EXISTS promo_codes (
  code        VARCHAR(32)  NOT NULL,
  percent     TINYINT UNSIGNED NULL,     -- либо процент,
  fixed_price DECIMAL(10,2)    NULL,     -- либо цена целиком
  tier        ENUM('personal','workshop') NULL,   -- NULL = на любой
  max_uses    INT UNSIGNED NOT NULL DEFAULT 1,
  used        INT UNSIGNED NOT NULL DEFAULT 0,
  expires_at  DATETIME     NULL,
  created_by  INT UNSIGNED NULL,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- --------------------------------------------------- сделать себя админом
-- Подставьте свою почту и выполните. Это единственный способ получить
-- права: через интерфейс их выдать нельзя, иначе первый же зашедший
-- сделал бы админом себя.
-- UPDATE users SET is_admin = 1 WHERE email = 'kam1k88@gmail.com';
