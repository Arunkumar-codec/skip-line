-- ══════════════════════════════════════════════════════════════════════════════
-- database/init.sql
-- Runs once when the MySQL Docker container is first initialised.
-- Creates the application database.
--
-- SECURITY (WP1 fix): the application user is NO LONGER created here.
-- Previously this file hardcoded a weak placeholder password
-- ('skipline_pass') in a CREATE USER/GRANT block. That block has been
-- removed: user provisioning is environment-driven via the official MySQL
-- image entrypoint, which creates the user named by MYSQL_USER with the
-- MYSQL_PASSWORD value (reachable only through the environment; the Compose
-- template guards both MYSQL_ROOT_PASSWORD and MYSQL_PASSWORD with
-- ${VAR:?required} so initialization fails fast when configuration is
-- missing instead of silently creating weak credentials).
--
-- Note: MySQL entrypoint grants MYSQL_USER all privileges on MYSQL_DATABASE;
-- least-privilege split (runtime vs migrate vs backup identities) is defined
-- in docs/PRODUCTION_READINESS_REPORT.md §account-architecture (WP1).
-- ══════════════════════════════════════════════════════════════════════════════

CREATE DATABASE IF NOT EXISTS skipline
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;
