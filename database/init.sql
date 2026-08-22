-- ══════════════════════════════════════════════════════════════════════════════
-- database/init.sql
-- Runs once when the MySQL Docker container is first initialised.
-- Creates the application database and user with minimal privileges.
-- ══════════════════════════════════════════════════════════════════════════════

-- Create database
CREATE DATABASE IF NOT EXISTS skipline
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

-- Create application user (password injected via env var in real deployments;
-- here we use a placeholder — docker-compose overrides it via MYSQL_USER/PASSWORD)
CREATE USER IF NOT EXISTS 'skipline_user'@'%' IDENTIFIED BY 'skipline_pass';

-- Grant only what the application needs
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER
    ON skipline.* TO 'skipline_user'@'%';

FLUSH PRIVILEGES;
