-- Возвраты. Выполнить один раз в phpMyAdmin поверх существующей базы.
--
-- Зачем колонки tier_before/tier_until_before: при возврате надо вернуть
-- человека ТУДА, ГДЕ ОН БЫЛ, а не просто в 'free'. Иначе диджей, который
-- продлил «Личную» на второй год и попросил вернуть деньги за продление,
-- потеряет и первый год тоже.

ALTER TABLE orders
  MODIFY status ENUM('new','pending','succeeded','canceled','refunded')
    NOT NULL DEFAULT 'new',
  ADD COLUMN tier_before ENUM('free','personal','workshop') NULL AFTER tier,
  ADD COLUMN tier_until_before DATETIME NULL AFTER tier_before,
  ADD COLUMN refund_id VARCHAR(64) NULL AFTER payment_id,
  ADD COLUMN refunded_at DATETIME NULL AFTER paid_at,
  ADD COLUMN refund_amount DECIMAL(10,2) NULL AFTER refunded_at;
