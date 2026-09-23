-- Заявки на возврат. Выполнить один раз в phpMyAdmin поверх существующей
-- базы, ПОСЛЕ db-refunds.sql.
--
-- Почему отдельная таблица, а не колонка в orders. Заявка и заказ — разные
-- вещи с разной судьбой: заказ оплачен один раз и больше не меняется, а у
-- заявки есть причина от человека, дата обращения, решение и кто его
-- принял. В колонках orders это выглядело бы как пять полей, которые у
-- 99 % заказов пустые.
--
-- UNIQUE по order_id — одна заявка на оплату. Без него двойное нажатие
-- кнопки в кабинете создаёт две заявки, а обработка второй попытается
-- вернуть уже возвращённые деньги.

CREATE TABLE IF NOT EXISTS refund_requests (
  id         INT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id    INT UNSIGNED NOT NULL,
  order_id   INT UNSIGNED NOT NULL,
  reason     VARCHAR(500) NULL,
  -- pending  — ждёт решения (срок безусловного возврата вышел)
  -- approved — деньги возвращены (автоматически или кнопкой в админке)
  -- declined — отказано, с пояснением в admin_note
  status     ENUM('pending','approved','declined') NOT NULL DEFAULT 'pending',
  refund_id  VARCHAR(64)  NULL,
  decided_by INT UNSIGNED NULL,
  decided_at DATETIME     NULL,
  admin_note VARCHAR(500) NULL,
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_order (order_id),
  KEY idx_status (status, created_at),
  KEY idx_user (user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Внешних ключей нет намеренно: orders удаляется каскадом вместе с
-- пользователем, а заявка — след обращения, и терять его вместе со
-- строкой заказа незачем. Ссылки проверяет код, а не движок.
