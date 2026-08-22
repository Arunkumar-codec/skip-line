-- ══════════════════════════════════════════════════════════════════════════════
-- database/seed.sql
-- Development seed data — do NOT run in production.
-- Passwords are bcrypt hashes of "Password@123"
-- ══════════════════════════════════════════════════════════════════════════════

USE skipline;

SET FOREIGN_KEY_CHECKS = 0;
TRUNCATE TABLE queue_logs;
TRUNCATE TABLE notifications;
TRUNCATE TABLE token_history;
TRUNCATE TABLE payments;
TRUNCATE TABLE tokens;
TRUNCATE TABLE settings;
TRUNCATE TABLE counters;
TRUNCATE TABLE services;
TRUNCATE TABLE organization_verifications;
TRUNCATE TABLE organizations;
TRUNCATE TABLE users;
TRUNCATE TABLE admins;
SET FOREIGN_KEY_CHECKS = 1;

-- ── Admins ────────────────────────────────────────────────────────────────────
INSERT INTO admins (name, email, password_hash, is_active) VALUES
('Super Admin',  'admin@skipline.app',  '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMqJqhcanFueB0Iku4hfvzv1Cu', 1),
('Support Admin','support@skipline.app','$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMqJqhcanFueB0Iku4hfvzv1Cu', 1);

-- ── Users ─────────────────────────────────────────────────────────────────────
INSERT INTO users (name, email, mobile, address, password_hash, is_active) VALUES
('Arjun Sharma',  'arjun@example.com',  '9876543210', '12 MG Road, Bengaluru', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMqJqhcanFueB0Iku4hfvzv1Cu', 1),
('Priya Nair',    'priya@example.com',  '9876543211', '45 Park Street, Chennai','$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMqJqhcanFueB0Iku4hfvzv1Cu', 1),
('Ravi Kumar',    'ravi@example.com',   '9876543212', '8 Anna Nagar, Mumbai',   '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMqJqhcanFueB0Iku4hfvzv1Cu', 1);

-- ── Organizations ─────────────────────────────────────────────────────────────
INSERT INTO organizations
    (name, registration_number, org_type, address, city, state,
     email, mobile, password_hash, status, approved_by_admin_id, approved_at)
VALUES
('City General Hospital',  'REG-HOSP-001', 'hospital',    '1 Hospital Road',   'Bengaluru', 'Karnataka', 'hospital@example.com',  '8012345678', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMqJqhcanFueB0Iku4hfvzv1Cu', 'approved', 1, NOW()),
('State Bank Branch',      'REG-BANK-001', 'bank',        '22 Finance Street', 'Chennai',   'Tamil Nadu','bank@example.com',      '8012345679', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMqJqhcanFueB0Iku4hfvzv1Cu', 'approved', 1, NOW()),
('Regional Passport Office','REG-GOVT-001','government',  '5 Civil Lines',     'Mumbai',    'Maharashtra','passport@example.com', '8012345680', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMqJqhcanFueB0Iku4hfvzv1Cu', 'pending',  NULL, NULL);

-- ── Services ──────────────────────────────────────────────────────────────────
INSERT INTO services (organization_id, name, description, price, estimated_minutes, is_active) VALUES
(1, 'General Consultation', 'OPD general physician visit',      0.00,  10, 1),
(1, 'Specialist Consultation','Referred specialist appointment', 200.00,20, 1),
(1, 'Lab Reports',           'Blood test / radiology reports',  50.00,  5, 1),
(1, 'Billing',               'Payment and billing counter',      0.00,   5, 1),
(2, 'Account Opening',       'New bank account',                 0.00,  15, 1),
(2, 'Loan Enquiry',          'Home / personal loan queries',     0.00,  20, 1),
(2, 'Cash Deposit',          'Counter cash deposit',             0.00,   5, 1);

-- ── Counters ──────────────────────────────────────────────────────────────────
INSERT INTO counters (organization_id, service_id, counter_name, counter_number, token_prefix, daily_limit, status) VALUES
(1, 1, 'OPD Counter 1',          1, 'A', 200, 'active'),
(1, 2, 'Specialist Counter',     2, 'B', 100, 'active'),
(1, 4, 'Billing Counter',        3, 'C', 300, 'active'),
(2, 5, 'Account Services',       1, 'A', 150, 'active'),
(2, 7, 'Cash Counter',           2, 'B', 400, 'active');

-- ── Settings (per org defaults) ───────────────────────────────────────────────
INSERT INTO settings (organization_id, `key`, value) VALUES
(1, 'payment_enabled',   'true'),
(1, 'sms_enabled',       'false'),
(1, 'kiosk_enabled',     'true'),
(1, 'announcement_text', 'Welcome to City General Hospital'),
(2, 'payment_enabled',   'false'),
(2, 'kiosk_enabled',     'true'),
(2, 'announcement_text', 'State Bank — serving you better');
