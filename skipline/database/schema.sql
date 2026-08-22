-- ══════════════════════════════════════════════════════════════════════════════
-- Skip Line — Complete MySQL Schema
-- Version: 1.0.0
-- Engine: InnoDB  |  Charset: utf8mb4  |  Collation: utf8mb4_unicode_ci
--
-- Run order matters — tables with FK dependencies come after their parents.
-- To apply:  mysql -u root -p skipline < database/schema.sql
-- ══════════════════════════════════════════════════════════════════════════════

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;
SET SQL_MODE = 'STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION';

CREATE DATABASE IF NOT EXISTS skipline
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE skipline;

-- ── 1. admins ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS admins (
    id            INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    name          VARCHAR(120)    NOT NULL,
    email         VARCHAR(255)    NOT NULL,
    password_hash VARCHAR(255)    NOT NULL,
    is_active     TINYINT(1)      NOT NULL DEFAULT 1,
    created_at    DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at    DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                  ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_admins_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Platform administrators';

-- ── 2. organizations ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS organizations (
    id                    INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    name                  VARCHAR(200)    NOT NULL,
    registration_number   VARCHAR(100)    NOT NULL,
    org_type              ENUM('hospital','clinic','bank','government',
                               'temple','educational','service_center','other')
                                          NOT NULL DEFAULT 'other',
    address               TEXT            NOT NULL,
    city                  VARCHAR(100)    NOT NULL,
    state                 VARCHAR(100)    NOT NULL,
    email                 VARCHAR(255)    NOT NULL,
    mobile                VARCHAR(20)     NOT NULL,
    password_hash         VARCHAR(255)    NOT NULL,
    status                ENUM('pending','approved','rejected','suspended')
                                          NOT NULL DEFAULT 'pending',
    logo_path             VARCHAR(500)    NULL,
    description           TEXT            NULL,
    is_active             TINYINT(1)      NOT NULL DEFAULT 1,
    approved_by_admin_id  INT UNSIGNED    NULL,
    approved_at           DATETIME(6)     NULL,
    created_at            DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at            DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                          ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_orgs_email (email),
    UNIQUE KEY uq_orgs_reg_number (registration_number),
    INDEX idx_orgs_status (status),
    INDEX idx_orgs_city (city),
    INDEX idx_orgs_state (state),
    INDEX idx_orgs_name (name),
    CONSTRAINT fk_orgs_admin
        FOREIGN KEY (approved_by_admin_id)
        REFERENCES admins (id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Registered service organizations';

-- ── 3. organization_verifications ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS organization_verifications (
    id                    INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    organization_id       INT UNSIGNED    NOT NULL,
    document_type         VARCHAR(100)    NOT NULL,
    document_url          VARCHAR(500)    NOT NULL,
    verification_status   ENUM('pending','approved','rejected')
                                          NOT NULL DEFAULT 'pending',
    reviewer_notes        TEXT            NULL,
    reviewed_at           DATETIME(6)     NULL,
    reviewed_by           INT UNSIGNED    NULL,
    created_at            DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at            DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                          ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    INDEX idx_orgver_org (organization_id),
    CONSTRAINT fk_orgver_org
        FOREIGN KEY (organization_id)
        REFERENCES organizations (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_orgver_admin
        FOREIGN KEY (reviewed_by)
        REFERENCES admins (id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Document verification records for organization onboarding';

-- ── 4. users ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id            INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    name          VARCHAR(120)    NOT NULL,
    email         VARCHAR(255)    NOT NULL,
    mobile        VARCHAR(20)     NOT NULL,
    address       TEXT            NULL,
    password_hash VARCHAR(255)    NOT NULL,
    is_active     TINYINT(1)      NOT NULL DEFAULT 1,
    profile_photo VARCHAR(500)    NULL,
    created_at    DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at    DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                  ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_users_email (email),
    INDEX idx_users_mobile (mobile)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='End users who book tokens';

-- ── 5. services ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS services (
    id                  INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    organization_id     INT UNSIGNED    NOT NULL,
    name                VARCHAR(200)    NOT NULL,
    description         TEXT            NULL,
    price               DECIMAL(10,2)   NOT NULL DEFAULT 0.00,
    estimated_minutes   INT UNSIGNED    NOT NULL DEFAULT 10,
    is_active           TINYINT(1)      NOT NULL DEFAULT 1,
    created_at          DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at          DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    INDEX idx_services_org (organization_id),
    CONSTRAINT fk_services_org
        FOREIGN KEY (organization_id)
        REFERENCES organizations (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Services offered by an organization';

-- ── 6. counters ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS counters (
    id              INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    organization_id INT UNSIGNED    NOT NULL,
    service_id      INT UNSIGNED    NULL,
    counter_name    VARCHAR(100)    NOT NULL,
    counter_number  INT UNSIGNED    NOT NULL,
    token_prefix    VARCHAR(5)      NOT NULL DEFAULT 'A',
    daily_limit     INT UNSIGNED    NOT NULL DEFAULT 200,
    status          ENUM('active','inactive','break')
                                    NOT NULL DEFAULT 'active',
    created_at      DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at      DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                    ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    INDEX idx_counters_org (organization_id),
    CONSTRAINT fk_counters_org
        FOREIGN KEY (organization_id)
        REFERENCES organizations (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_counters_service
        FOREIGN KEY (service_id)
        REFERENCES services (id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Physical service counters within an organization';

-- ── 7. tokens ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tokens (
    id              INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    organization_id INT UNSIGNED    NOT NULL,
    user_id         INT UNSIGNED    NULL,
    counter_id      INT UNSIGNED    NOT NULL,
    service_id      INT UNSIGNED    NULL,
    token_number    VARCHAR(20)     NOT NULL,
    sequence        INT UNSIGNED    NOT NULL,
    status          ENUM('waiting','called','in_progress','completed',
                         'skipped','cancelled','on_hold')
                                    NOT NULL DEFAULT 'waiting',
    queue_position  INT UNSIGNED    NOT NULL DEFAULT 0,
    guest_name      VARCHAR(120)    NULL,
    guest_mobile    VARCHAR(20)     NULL,
    qr_code_path    VARCHAR(500)    NULL,
    receipt_path    VARCHAR(500)    NULL,
    amount_paid     DECIMAL(10,2)   NOT NULL DEFAULT 0.00,
    booked_at       DATETIME(6)     NULL,
    called_at       DATETIME(6)     NULL,
    completed_at    DATETIME(6)     NULL,
    notes           TEXT            NULL,
    created_at      DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at      DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                    ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    INDEX idx_tokens_org (organization_id),
    INDEX idx_tokens_user (user_id),
    INDEX idx_tokens_counter (counter_id),
    INDEX idx_tokens_status (status),
    INDEX idx_tokens_number (token_number),
    INDEX idx_tokens_booked (booked_at),
    CONSTRAINT fk_tokens_org
        FOREIGN KEY (organization_id)
        REFERENCES organizations (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_tokens_user
        FOREIGN KEY (user_id)
        REFERENCES users (id)
        ON DELETE SET NULL,
    CONSTRAINT fk_tokens_counter
        FOREIGN KEY (counter_id)
        REFERENCES counters (id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_tokens_service
        FOREIGN KEY (service_id)
        REFERENCES services (id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Virtual queue tokens — core entity of the system';

-- ── 8. token_history ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS token_history (
    id                INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    token_id          INT UNSIGNED    NOT NULL,
    old_status        VARCHAR(30)     NULL,
    new_status        VARCHAR(30)     NOT NULL,
    changed_by_role   VARCHAR(30)     NOT NULL,
    changed_by_id     INT UNSIGNED    NULL,
    changed_at        DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    INDEX idx_th_token (token_id),
    INDEX idx_th_changed (changed_at),
    CONSTRAINT fk_th_token
        FOREIGN KEY (token_id)
        REFERENCES tokens (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Immutable audit trail for token status transitions';

-- ── 9. payments ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS payments (
    id                    INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    token_id              INT UNSIGNED    NOT NULL,
    user_id               INT UNSIGNED    NULL,
    organization_id       INT UNSIGNED    NOT NULL,
    amount                DECIMAL(10,2)   NOT NULL,
    currency              VARCHAR(10)     NOT NULL DEFAULT 'INR',
    payment_method        ENUM('razorpay','upi','cash','waived')
                                          NOT NULL DEFAULT 'razorpay',
    status                ENUM('pending','paid','failed','refunded')
                                          NOT NULL DEFAULT 'pending',
    razorpay_order_id     VARCHAR(100)    NULL,
    razorpay_payment_id   VARCHAR(100)    NULL,
    razorpay_signature    VARCHAR(500)    NULL,
    receipt_path          VARCHAR(500)    NULL,
    paid_at               DATETIME(6)     NULL,
    created_at            DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at            DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                          ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_payments_rzp_payment_id (razorpay_payment_id),
    INDEX idx_payments_token (token_id),
    INDEX idx_payments_user (user_id),
    INDEX idx_payments_org (organization_id),
    INDEX idx_payments_status (status),
    INDEX idx_payments_rzp_order (razorpay_order_id),
    CONSTRAINT fk_payments_token
        FOREIGN KEY (token_id)
        REFERENCES tokens (id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_payments_user
        FOREIGN KEY (user_id)
        REFERENCES users (id)
        ON DELETE SET NULL,
    CONSTRAINT fk_payments_org
        FOREIGN KEY (organization_id)
        REFERENCES organizations (id)
        ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Payment records for token booking fees';

-- ── 10. notifications ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notifications (
    id        INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    user_id   INT UNSIGNED    NOT NULL,
    token_id  INT UNSIGNED    NULL,
    type      ENUM('token_created','queue_nearing','token_called',
                   'token_completed','token_cancelled','org_approved',
                   'org_rejected','payment_success')  NOT NULL,
    channel   ENUM('email','sms','in_app')            NOT NULL,
    subject   VARCHAR(255)    NULL,
    message   TEXT            NOT NULL,
    is_read   TINYINT(1)      NOT NULL DEFAULT 0,
    sent_at   DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    INDEX idx_notif_user (user_id),
    INDEX idx_notif_token (token_id),
    INDEX idx_notif_read (is_read),
    CONSTRAINT fk_notif_user
        FOREIGN KEY (user_id)
        REFERENCES users (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_notif_token
        FOREIGN KEY (token_id)
        REFERENCES tokens (id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Notifications sent to users for queue events';

-- ── 11. queue_logs ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS queue_logs (
    id                  INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    organization_id     INT UNSIGNED    NOT NULL,
    counter_id          INT UNSIGNED    NULL,
    token_id            INT UNSIGNED    NULL,
    action              VARCHAR(60)     NOT NULL,
    performed_by_role   VARCHAR(30)     NOT NULL,
    performed_by_id     INT UNSIGNED    NULL,
    meta                VARCHAR(1000)   NULL COMMENT 'JSON context',
    logged_at           DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    INDEX idx_ql_org (organization_id),
    INDEX idx_ql_token (token_id),
    INDEX idx_ql_action (action),
    INDEX idx_ql_logged (logged_at),
    CONSTRAINT fk_ql_org
        FOREIGN KEY (organization_id)
        REFERENCES organizations (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_ql_counter
        FOREIGN KEY (counter_id)
        REFERENCES counters (id)
        ON DELETE SET NULL,
    CONSTRAINT fk_ql_token
        FOREIGN KEY (token_id)
        REFERENCES tokens (id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Append-only audit log for all queue actions';

-- ── 12. settings ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS settings (
    id              INT UNSIGNED    NOT NULL AUTO_INCREMENT,
    organization_id INT UNSIGNED    NOT NULL,
    `key`           VARCHAR(100)    NOT NULL,
    value           TEXT            NULL,
    updated_at      DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                    ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_org_setting_key (organization_id, `key`),
    INDEX idx_settings_org (organization_id),
    CONSTRAINT fk_settings_org
        FOREIGN KEY (organization_id)
        REFERENCES organizations (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Per-organization key-value configuration store';

SET FOREIGN_KEY_CHECKS = 1;
