-- Миграция для добавления поля username в таблицу audit_log
-- Выполните этот скрипт для существующих баз данных auth.db

-- Добавляем столбец username, если он ещё не существует
ALTER TABLE audit_log ADD COLUMN username TEXT DEFAULT '';

-- Создаём индекс для ускорения поиска по username
CREATE INDEX IF NOT EXISTS idx_audit_username ON audit_log(username);
