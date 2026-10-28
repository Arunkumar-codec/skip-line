# SkipLine: Feature Requirements Traceability Matrix

> **Generated Date:** October 12, 2026
> **Source Specification:** [docs/quirments.docx](file:///c:/Arunakumara/skip-line-FINAL/skip-line/docs/quirments.docx)
> **Total Requirements:** 193 across 35 categories

## Matrix Summary

| Status | Count | Percentage | Description |
|---|:---:|:---:|---|
| **Implemented** | 8 | 4.1% | Verified in current code and passing tests |
| **Incomplete** | 58 | 30.1% | Partially implemented; lacks edge cases, sub-entities, or security controls |
| **Broken** | 2 | 1.0% | Implemented in code but disconnected, failing, or introducing security risks |
| **Missing** | 125 | 64.8% | Required by SRS but not yet present in codebase |
| **Total** | **193** | **100.0%** | Full functional and non-functional requirements scope |

> **Reconciliation log (October 28, 2026):** Rows in categories ARC, USR, SVC, FORM and CTR were re-verified against the actual code and the passing test suite (119/119 green, uncommitted Phase 2 work) and updated with file-path evidence. Other categories retain their October 12 audit values and are **not** yet reconciled — the summary counts above are therefore stale until a full re-audit is performed.

---

## 1. Release Success Criteria (SUC)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **SUC-001** | MUST | Phase 0 / Gate | All visible production navigation items open functional, permission-correct features backed by real data. | 🟡 **Incomplete** | **Code:** app/<br>**Tests:** tests/ | System baseline functional but gates pending full phase completion |
| **SUC-002** | MUST | Phase 0 / Gate | Concurrent booking tests prove that daily and slot capacity cannot be exceeded. | 🟡 **Incomplete** | **Code:** app/<br>**Tests:** tests/ | System baseline functional but gates pending full phase completion |
| **SUC-003** | MUST | Phase 0 / Gate | All money movements reconcile from payment/refund/adjustment ledger entries to organization balances and payouts. | 🟡 **Incomplete** | **Code:** app/<br>**Tests:** tests/ | System baseline functional but gates pending full phase completion |
| **SUC-004** | MUST | Phase 0 / Gate | Tenant-isolation tests prove that an employee or organization cannot access another tenant by changing IDs or URLs. | 🟡 **Incomplete** | **Code:** app/<br>**Tests:** tests/ | System baseline functional but gates pending full phase completion |
| **SUC-005** | MUST | Phase 0 / Gate | Public display and QR APIs expose no disallowed personal, identity, form or payment fields. | 🟡 **Incomplete** | **Code:** app/<br>**Tests:** tests/ | System baseline functional but gates pending full phase completion |
| **SUC-006** | MUST | Phase 0 / Gate | Critical workflows pass automated and end-to-end tests for every role. | 🟡 **Incomplete** | **Code:** app/<br>**Tests:** tests/ | System baseline functional but gates pending full phase completion |


## 2. Architecture Refactoring & Baseline Strategy (ARC)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **ARC-001** | MUST | Phase 0 & 1 | Do not replace working functionality merely to change style. Add migrations and refactor behind tests. | 🟡 **Incomplete** | **Code:** app/<br>**Tests:** None | Refactoring roadmap defined; pending incremental execution |
| **ARC-002** | MUST | Phase 0 & 1 | Replace single counter-service links with a many-to-many counter_services structure while preserving existing assignments. | 🟢 **Implemented** | **Code:** models/counter_service.py, app/services/catalogue_service.py (assign_services_to_counter, get_eligible_counters_for_service), migrations/versions/0004_catalogue_multiservice_forms_identity.py (backfill of existing counters.service_id)<br>**Tests:** tests/api/test_catalogue_multiservice.py::test_multi_service_counter_routing | Legacy counters.service_id retained as primary-service fallback (catalogue_service.py:250-258); backfill SQL not exercised by test suite (tests use create_all on SQLite) |
| **ARC-003** | MUST | Phase 0 & 1 | Replace overloaded organization accounts with explicit organization-head and employee authorization boundaries. | 🟡 **Incomplete** | **Code:** app/<br>**Tests:** None | Refactoring roadmap defined; pending incremental execution |
| **ARC-004** | MUST | Phase 0 & 1 | Evolve payment records into an immutable ledger model supporting items, tax, discounts, refunds, cash reconciliation and payouts. | 🟡 **Incomplete** | **Code:** app/<br>**Tests:** None | Refactoring roadmap defined; pending incremental execution |
| **ARC-005** | MUST | Phase 0 & 1 | Create one support-case domain for user-to-organization, user-to-admin, organization-to-admin, disputes and feedback reports. | 🟡 **Incomplete** | **Code:** app/<br>**Tests:** None | Refactoring roadmap defined; pending incremental execution |
| **ARC-006** | MUST | Phase 0 & 1 | Keep old routes temporarily compatible or provide redirects/deprecation handling during migration. | 🟡 **Incomplete** | **Code:** app/<br>**Tests:** None | Refactoring roadmap defined; pending incremental execution |


## 3. Roles, Tenancy and Access Control (RBAC) (AUTHZ)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **AUTHZ-001** | MUST | Phase 1 | Every tenant-owned record MUST include organization_id directly or through a non-ambiguous parent relationship. | 🟡 **Incomplete** | **Code:** models/token.py, models/counter.py<br>**Tests:** tests/unit/test_token_service.py | organization_id present on most models, but branches missing |
| **AUTHZ-002** | MUST | Phase 1 | Backend dependencies MUST derive organization, role and permissions from the authenticated principal; client-supplied tenant IDs are never trusted. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Granular RBAC, employee principal, and permission checks absent |
| **AUTHZ-003** | MUST | Phase 1 | Object-level authorization MUST verify ownership for every read, update, download, scan, message and action. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Granular RBAC, employee principal, and permission checks absent |
| **AUTHZ-004** | MUST | Phase 1 | Organization-head interfaces MUST not be accessible to normal employees. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Granular RBAC, employee principal, and permission checks absent |
| **AUTHZ-005** | MUST | Phase 1 | Employees MUST use separate credentials and a separate employee panel; shared employee accounts are prohibited. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Granular RBAC, employee principal, and permission checks absent |
| **AUTHZ-006** | MUST | Phase 1 | Public display credentials MUST carry only public_queue:read for assigned branches/counters/services. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Granular RBAC, employee principal, and permission checks absent |
| **AUTHZ-007** | MUST | Phase 1 | Sensitive admin duties MUST be separated into verification, support, finance, fraud and senior approval permissions. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Granular RBAC, employee principal, and permission checks absent |
| **AUTHZ-008** | MUST | Phase 1 | High-impact enforcement, payout, refund and bank-change actions MUST require reason, audit and configurable second approval. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Granular RBAC, employee principal, and permission checks absent |


## 4. Authentication, Sessions and Password Recovery (AUTH)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **AUTH-001** | MUST | Phase 1 | Provide separate /user/login, /employee/login, /org/login and /admin/login interfaces with correct redirects. | 🟢 **Implemented** | **Code:** app/routers/auth.py<br>**Tests:** tests/api/test_auth.py (Failing host header) | Basic login endpoints exist for admin/org/user |
| **AUTH-002** | MUST | Phase 1 | Hash passwords using Argon2id or bcrypt; never store, log, email or display plain passwords. | 🟡 **Incomplete** | **Code:** app/dependencies.py<br>**Tests:** None | Session exists but no multi-device tracking or employee session |
| **AUTH-003** | MUST | Phase 1 | Use Secure, HttpOnly, SameSite cookies in production; implement CSRF protection for cookie-authenticated mutations. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Password recovery / reset flow not implemented |
| **AUTH-004** | MUST | Phase 1 | Implement session expiry, refresh rotation or secure server sessions, logout, logout-all-devices and session revocation. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Password recovery / reset flow not implemented |
| **AUTH-005** | MUST | Phase 1 | Rate-limit login and recovery; add progressive delay/lock and CAPTCHA after repeated failures. | 🟢 **Implemented** | **Code:** app/routers/views_admin.py, views_user.py<br>**Tests:** None | Change password page implemented |
| **AUTH-006** | MUST | Phase 1 | Require MFA for super admins and support MFA for organization heads and privileged finance users. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | MFA not implemented |
| **AUTH-007** | MUST | Phase 1 | Record login success/failure, device/session summary, IP where lawful, password change, recovery and revocation without logging secrets. | 🟡 **Incomplete** | **Code:** models/user.py (profile_photo)<br>**Tests:** None | Profile photo column exists, upload flow missing |


## 5. User Profile Management (PROF)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **PROF-001** | MUST | Phase 1 | Users, employees, organization heads and admins can upload, preview, crop, replace and remove a profile picture; organization logos remain separate. | 🟡 **Incomplete** | **Code:** models/user.py, templates/user/profile.html<br>**Tests:** tests/api/test_auth.py | Basic user fields exist; government ID & verification absent |
| **PROF-002** | MUST | Phase 1 | Accept validated JPEG, PNG and WebP up to 5 MB, minimum 200x200; generate normalized thumbnails and randomized filenames. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Identity submission and verification missing |
| **PROF-003** | MUST | Phase 1 | Verify content signatures, strip metadata, scan uploads where available and prohibit executable/polyglot files. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Identity submission and verification missing |
| **PROF-004** | MUST | Phase 1 | Default to initials-based avatars. Anonymous feedback and public displays MUST not reveal profile pictures. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Identity submission and verification missing |


## 6. User Identity & Document Verification (USR)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **USR-001** | MUST | Phase 2 | Account creation should require minimum contact credentials; profile completion may follow, with clear completion status. | 🟢 **Implemented** | **Code:** app/services/user_service.py (register_user: name, email, mobile, password), app/routers/auth.py<br>**Tests:** tests/api/test_auth.py::test_user_register | Profile-completion status tracking not separately surfaced |
| **USR-002** | MUST | Phase 2 | ID proof is requested only when a selected organization/service has a justified verification requirement. | 🟢 **Implemented** | **Code:** models/service.py (requires_identity_verification), app/services/token_service.py (book_token gating), app/services/identity_service.py (check_user_identity_verified)<br>**Tests:** tests/api/test_identity_verification.py::test_booking_gated_by_identity_verification | Identities may also be submitted proactively (no booking); acceptable per requirement wording |
| **USR-003** | MUST | Phase 2 | Supported identity records include configurable type, encrypted number, name on ID, private document, status, reviewer, reason and timestamps. | 🟡 **Incomplete** | **Code:** models/user_identity.py (id_type, encrypted_id_number, document_path, status, rejection_reason, reviewed_by_id, reviewed_at, expires_at), app/services/identity_service.py (Fernet AES encryption)<br>**Tests:** tests/api/test_identity_verification.py (encryption, review, isolation) | "Name on ID" field not captured; ID type is a free string, not a platform-controlled catalogue |
| **USR-004** | MUST | Phase 2 | Statuses are NOT_SUBMITTED, PENDING, VERIFIED, REJECTED and EXPIRED; a user cannot self-verify. | 🟡 **Incomplete** | **Code:** models/user_identity.py (pending/approved/rejected + expires_at), app/services/identity_service.py (check_user_identity_verified enforces approved + non-expired), app/routers/org.py (review endpoints require identity:review permission)<br>**Tests:** tests/api/test_identity_verification.py::test_identity_review_desk_approval_and_rejection | NOT_SUBMITTED is implicit (absence of record); EXPIRED handled via expires_at query rather than a stored status; self-verify is prevented by permission-gated review endpoints |
| **USR-005** | MUST | Phase 2 | Mask stored identifiers in normal views and record every authorized access to full values/documents. | 🟡 **Incomplete** | **Code:** app/services/identity_service.py (mask_id_number), app/schemas/identity.py (UserIdentityRead exposes masked_id_number only)<br>**Tests:** tests/api/test_identity_verification.py::test_identity_submission_encryption_and_masking, test_secure_document_access_and_tenant_isolation | Masking verified; **access-to-full-value/document audit logging NOT implemented** (download endpoints app/routers/user.py:229, app/routers/org.py:888 return files without recording an audit event) |
| **USR-006** | MUST | Phase 2 | Validate date, mobile, PIN, file type/size and ID format; detect duplicates for manual review. | 🟡 **Incomplete** | **Code:** app/services/identity_service.py (save_identity_document: extension allow-list .pdf/.jpg/.jpeg/.png/.webp)<br>**Tests:** tests/api/test_identity_verification.py::test_identity_document_upload_unguessable_uuid | File size limit, ID-number format validation, date/PIN validation, and duplicate-submission detection not implemented |
| **USR-007** | MUST | Phase 2 | Never collect Aadhaar OTP, biometrics, card credentials, account passwords or unrelated sensitive data. | 🟢 **Implemented** | **Code:** app/services/form_service.py (PROHIBITED_FIELD_PATTERNS enforced on field key/label/placeholder/help_text), identity capture limited to type/number/document (app/services/identity_service.py)<br>**Tests:** tests/api/test_forms.py::test_form_builder_rejects_prohibited_fields (8 cases) | No automated check prevents an org from asking for OTP/biometric *data inside free-text answers*; only field definitions are screened |


## 7. Organization Discovery & Search Engine (SRCH)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **SRCH-001** | MUST | Phase 3 | Search organization name, service/sub-service, category, city, area, PIN code, landmark and branch code. | 🟡 **Incomplete** | **Code:** app/routers/user.py, user_service.py<br>**Tests:** tests/api/test_public.py | Basic name/type/city search exists; radius/branch search missing |
| **SRCH-002** | MUST | Phase 3 | Provide filters for category, location, service, open-now, token availability, online booking, free/paid, verification requirement and verified organizations. | 🟡 **Incomplete** | **Code:** app/routers/user.py, user_service.py<br>**Tests:** tests/api/test_public.py | Basic name/type/city search exists; radius/branch search missing |
| **SRCH-003** | MUST | Phase 3 | Support relevance, nearest, shortest-wait, popularity, recent and A-Z sorting. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Distance, calendar, and slot availability filters missing |
| **SRCH-004** | MUST | Phase 3 | Use backend query/pagination, database indexes, 300-500 ms debounce, minimum query length and no per-keystroke full scans. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Distance, calendar, and slot availability filters missing |
| **SRCH-005** | MUST | Phase 3 | Return total count, page metadata, applied filters and only result-card fields. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Distance, calendar, and slot availability filters missing |
| **SRCH-006** | MUST | Phase 3 | Results show name/logo, verified state, address/distance, open state, services, queue estimate, next availability, rating and booking action. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Distance, calendar, and slot availability filters missing |
| **SRCH-007** | MUST | Phase 3 | Preserve filters on back-navigation; provide recent/popular results, loading skeletons, clear filters and honest empty/error states. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Distance, calendar, and slot availability filters missing |
| **SRCH-008** | SHOULD | Phase 3 | Add typo tolerance only after indexed exact/partial search is correct; cache safe category/city/popular metadata. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Distance, calendar, and slot availability filters missing |


## 8. Service Catalogue & Sub-services (SVC)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **SVC-001** | MUST | Phase 2 | A counter can provide multiple services and a service can be assigned to multiple counters. | 🟢 **Implemented** | **Code:** models/counter_service.py, app/services/catalogue_service.py (assign_services_to_counter, get_counter_services), templates/org/counters.html (multi-service selector)<br>**Tests:** tests/api/test_catalogue_multiservice.py::test_multi_service_counter_routing | — |
| **SVC-002** | MUST | Phase 2 | Services support parent_service_id for sub-services, code, description, active state, price, tax, fees, duration, capacity, schedule and verification requirement. | 🟡 **Incomplete** | **Code:** models/sub_service.py (child table with code, description, additional_price, additional_minutes, is_active, display_order), models/service.py (service_code, price, estimated_minutes, requires_identity_verification), models/custom_form.py (form linkage)<br>**Tests:** tests/api/test_catalogue_multiservice.py (sub-services CRUD, code uniqueness, rollup) | Hierarchy implemented as child `sub_services` table instead of `parent_service_id`; tax, fees, capacity, and schedule fields not present (schedule is Phase 3) |
| **SVC-003** | MUST | Phase 2 | Organization configures whether one token may contain one service or multiple services; selected items are stored in token_services. | 🟡 **Incomplete** | **Code:** models/token_service_item.py (table `token_services`), app/services/token_service.py (book_token records item breakdown incl. sub-services), app/routers/user.py (TokenBookRequest.sub_service_ids)<br>**Tests:** tests/api/test_catalogue_multiservice.py (TokenServiceItem breakdown persistence) | Organization-level one-vs-multi-service token configuration toggle not implemented; token currently records one primary service + sub-service items |
| **SVC-004** | MUST | Phase 2 | For multi-service tokens, backend recalculates total duration, capacity consumption and price from active server-side records. | 🟡 **Incomplete** | **Code:** app/services/catalogue_service.py (calculate_service_rollup), app/services/token_service.py (server-side price/duration rollup from active sub-services, token_service.py:183-230)<br>**Tests:** tests/api/test_catalogue_multiservice.py::test_service_pricing_and_duration_rollup | Price and duration rollup verified; capacity consumption recalculation not implemented (only per-counter daily limit) |


## 9. Dynamic Custom Form Builder (FORM)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **FORM-001** | MUST | Phase 2 | Organization heads can build forms at organization, counter, service or sub-service level. | 🟡 **Incomplete** | **Code:** models/custom_form.py (organization_id + service_id), app/services/form_service.py (create_form, list_forms_by_org), templates/org/forms.html<br>**Tests:** tests/api/test_forms.py (CRUD, tenant isolation) | Forms bind at organization/service level only; counter-level and sub-service-level scoping not implemented |
| **FORM-002** | MUST | Phase 2 | Field types include short/long text, number, date/time, select, radio, checkbox, yes/no, phone, email, address, file and consent. | 🟡 **Incomplete** | **Code:** models/custom_form.py (field_type free String(50)), app/services/form_service.py (options, conditional evaluation)<br>**Tests:** tests/api/test_forms.py (select/option handling) | field_type is an unconstrained string; no server-side enumeration of the required type set; file/consent types not specially handled |
| **FORM-003** | MUST | Phase 2 | Fields support label, help, placeholder, required, order, min/max, allowed values, validation, conditional visibility and sensitivity classification. | 🟡 **Incomplete** | **Code:** models/custom_form.py (label, help_text, placeholder, is_required, order_index, validation_regex), models/custom_form.py (FormFieldCondition show/hide/require), app/services/form_service.py (validate_form_submission)<br>**Tests:** tests/api/test_forms.py (regex validation, conditions, immutable snapshots) | min/max numeric bounds, typed allowed values, and sensitivity classification not implemented |
| **FORM-004** | MUST | Phase 2 | Forms support DRAFT, PUBLISHED and RETIRED versions, preview, duplicate, reorder and publish validation. | 🟡 **Incomplete** | **Code:** app/services/form_service.py (version increment on schema change, retire_form, duplicate_form, is_published flag)<br>**Tests:** tests/api/test_forms.py (lifecycle publish/retire/duplicate, versioning) | Draft/published/retired modelled as two booleans + integer version rather than explicit states; no preview endpoint, no dedicated reorder operation, no publish-time required-field validation |
| **FORM-005** | MUST | Phase 2 | Every booking stores a form-version snapshot plus question/answer snapshots so later edits do not change historical submissions. | 🟢 **Implemented** | **Code:** models/custom_form.py (TokenFormSnapshot.schema_snapshot JSON + TokenFormAnswer), app/services/form_service.py (create_token_form_snapshot), app/services/token_service.py (snapshot on booking)<br>**Tests:** tests/api/test_forms.py::test_form_validation_and_immutable_snapshots_on_booking | — |
| **FORM-006** | MUST | Phase 2 | When services share an identical approved field, show it once and bind the answer to every required service. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Shared-field deduplication across multi-service tokens not implemented |
| **FORM-007** | MUST | Phase 2 | Reject organization attempts to request passwords, OTPs, PINs, CVV, banking credentials or prohibited information. | 🟢 **Implemented** | **Code:** app/services/form_service.py (validate_prohibited_terms on create/update/duplicate paths)<br>**Tests:** tests/api/test_forms.py::test_form_builder_rejects_prohibited_fields (password, pin, otp, cvv, card, netbanking variants → 422) | — |
| **FORM-008** | MUST | Phase 2 | Counter employees see only fields required to deliver assigned services; sensitive downloads/views are audited. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | No employee-scoped field filtering; no audit logging of sensitive form/document views (overlaps USR-005 gap) |


## 10. Counter Channels & Multi-service Capabilities (CTR)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **CTR-001** | MUST | Phase 2 | All channels use the same service catalogue, custom form versions, schedule, capacity rules, queue engine, payment ledger and token state machine. | 🟡 **Incomplete** | **Code:** app/services/catalogue_service.py (get_eligible_counters_for_service), app/services/token_service.py (shared book_token with auto-routing used by online/kiosk/employee paths), app/services/form_service.py (active published form per service)<br>**Tests:** tests/api/test_catalogue_multiservice.py::test_multi_service_counter_routing | Catalogue, forms, queue engine and token state machine are now shared across channels; schedule and capacity rules are Phase 3, payment ledger is Phase 5 |
| **CTR-002** | MUST | Phase 2 | Every token records immutable booking_source, organization, branch, counter/queue, selected items, form snapshot, actor/device and price snapshot. | 🟡 **Incomplete** | **Code:** models/token.py (amount_paid price snapshot, guest fields), models/token_service_item.py (selected items), models/custom_form.py (TokenFormSnapshot via token_services cascade)<br>**Tests:** tests/api/test_forms.py (snapshot immutability), test_catalogue_multiservice.py (item breakdown) | Immutable `booking_source` column, actor/device attribution, and branch reference on the token are not recorded |
| **CTR-003** | MUST | Phase 2 | Capacity can be allocated by source and unused capacity transferred only through configured rules. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Source-based capacity allocation is out of current scope until Phase 3 slot/capacity engine |


## 12. Self-Service Kiosk Workflow & Privacy (KSK)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **KSK-001** | MUST | Phase 4 | A registered kiosk is assigned to one organization/branch and approved counters/services; it has a revocable device credential. | 🟢 **Implemented** | **Code:** app/routers/public.py, templates/public/kiosk.html<br>**Tests:** tests/api/test_public.py | Kiosk booking flow exists |
| **KSK-002** | MUST | Phase 4 | Collect guest name and mobile at minimum when needed; OTP may retrieve permitted returning-user data after consent. | 🟡 **Incomplete** | **Code:** templates/public/kiosk.html<br>**Tests:** None | Kiosk exists but auto-clear privacy timer missing |
| **KSK-003** | MUST | Phase 4 | Use touch-friendly steps, large controls, progress, language selection, on-screen keyboard, accessibility and final review. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Kiosk device credentials, rate limiting, and cash routing missing |
| **KSK-004** | MUST | Phase 4 | Automatically clear temporary data and session after completion/cancellation/inactivity; prevent access to previous visitor data. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Kiosk device credentials, rate limiting, and cash routing missing |
| **KSK-005** | MUST | Phase 4 | Support print, SMS, email or phone-scannable QR delivery; printing failure must not duplicate the token. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Kiosk device credentials, rate limiting, and cash routing missing |
| **KSK-006** | MUST | Phase 4 | Kiosk reports device ID, online/offline state and last activity; organization head may revoke it. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Kiosk device credentials, rate limiting, and cash routing missing |


## 13. Employee-Assisted Physical Booking (PHY)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **PHY-001** | MUST | Phase 4 | Employee searches an existing verified mobile or creates a tenant-safe guest record, selects services, completes forms and issues a token. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Employee-assisted counter booking missing |
| **PHY-002** | MUST | Phase 4 | There is no untracked 'skip payment'. Options are CASH_COLLECTED, CASH_PENDING, PAY_AT_COUNTER, FREE, WAIVED or ONLINE. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Employee-assisted counter booking missing |
| **PHY-003** | MUST | Phase 4 | A waiver requires reason and, above configured policy, supervisor approval and supporting evidence. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Employee-assisted counter booking missing |
| **PHY-004** | MUST | Phase 4 | Cash collected is revenue immediately but remains UNRECONCILED until shift close. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Employee-assisted counter booking missing |


## 14. Employee Accounts & Authorization Boundaries (EMP)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **EMP-001** | MUST | Phase 1 | Organization head creates employee credentials, role, branch, counter/service assignments, shift and granular permissions. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Employee model, routes, and panel absent |
| **EMP-002** | MUST | Phase 1 | Employee logs directly into /employee/login and never receives organization-head navigation or tokens. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Employee model, routes, and panel absent |
| **EMP-003** | MUST | Phase 1 | Temporary passwords must be changed at first login; activation/deactivation and assignment changes are audited. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Employee model, routes, and panel absent |


## 15. Counter Shift State Machine & Daily Approvals (SHIFT)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **SHIFT-001** | MUST | Phase 4 | Before accepting tokens/payment, employee submits counter, shift, services, device readiness, opening cash and remarks. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Daily counter shift opening/closing state machine missing |
| **SHIFT-002** | MUST | Phase 4 | Organization head or explicitly delegated manager approves/rejects with reason, approved period, services and payment permissions. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Daily counter shift opening/closing state machine missing |
| **SHIFT-003** | MUST | Phase 4 | An unapproved employee cannot issue tokens, collect cash or change queue state even through direct API requests. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Daily counter shift opening/closing state machine missing |
| **SHIFT-004** | MUST | Phase 4 | Logout does not silently close a live counter; counter must pause, hand over or complete formal closing. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Daily counter shift opening/closing state machine missing |
| **SHIFT-005** | MUST | Phase 4 | Closing report includes token counts, payments, refunds, waivers, expected/actual cash, difference and remarks. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Daily counter shift opening/closing state machine missing |
| **SHIFT-006** | MUST | Phase 4 | Head may approve, request correction, record shortage/excess or escalate; reconciled entries cannot be edited, only adjusted. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Daily counter shift opening/closing state machine missing |


## 16. Scheduling, Availability & Atomic Capacity (SCH)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **SCH-001** | MUST | Phase 3 | Organization defines recurring weekly hours, breaks and closed days and publishes at least one month of user-visible availability. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Weekly schedules, exceptions, time slots, and capacity missing |
| **SCH-002** | MUST | Phase 3 | Schedules exist at organization, branch, counter and service levels; the most specific published override wins. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Weekly schedules, exceptions, time slots, and capacity missing |
| **SCH-003** | MUST | Phase 3 | Date exceptions support holiday, emergency closure, maintenance, special hours, reduced capacity and service/counter closure with user-facing reason. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Weekly schedules, exceptions, time slots, and capacity missing |
| **SCH-004** | MUST | Phase 3 | Capacity supports daily, slot, counter, service, source, user and priority reserve limits. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Weekly schedules, exceptions, time slots, and capacity missing |
| **SCH-005** | MUST | Phase 3 | Support day-queue or configurable 15/30/60/custom-minute slots, advance window, same-day cutoff, cancel/reschedule cutoff and future-booking limit. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Weekly schedules, exceptions, time slots, and capacity missing |
| **SCH-006** | MUST | Phase 3 | Use atomic reservations/locking to prevent overbooking; payment reservations expire and release capacity. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Weekly schedules, exceptions, time slots, and capacity missing |
| **SCH-007** | MUST | Phase 3 | Schedule edits show affected confirmed tokens and require a controlled honor/reschedule/cancel/refund workflow plus notifications. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Weekly schedules, exceptions, time slots, and capacity missing |
| **SCH-008** | MUST | Phase 3 | User calendars show AVAILABLE, LIMITED, FULL, CLOSED, HOLIDAY, NOT_OPEN and SERVICE_UNAVAILABLE with working hours and requirements. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Weekly schedules, exceptions, time slots, and capacity missing |
| **SCH-009** | MUST | Phase 3 | Store UTC timestamps and an IANA timezone per organization/branch; render using that timezone. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Weekly schedules, exceptions, time slots, and capacity missing |


## 17. Queue Engine, Transitions & Real-Time Sync (QUE)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **QUE-001** | MUST | Phase 6 | Generate human-readable token codes unique per organization/branch/queue/date while keeping stable internal IDs private. | 🟡 **Incomplete** | **Code:** app/services/token_service.py<br>**Tests:** tests/unit/test_token_service.py | Token state machine implemented; atomic capacity locking missing |
| **QUE-002** | MUST | Phase 6 | Every transition records old/new status, actor type/ID, reason, device, timestamp and metadata in token history. | 🟡 **Incomplete** | **Code:** app/websocket/manager.py<br>**Tests:** None | In-memory manager without Redis pub/sub |
| **QUE-003** | MUST | Phase 6 | Queue order and estimated wait consider source allocation, priority, service duration, multiple service items, active counters and delays. | 🟡 **Incomplete** | **Code:** app/services/token_service.py<br>**Tests:** tests/unit/test_token_service.py | Token state machine implemented; atomic capacity locking missing |
| **QUE-004** | MUST | Phase 6 | Manual priority, skip, transfer, cancellation and override require permission and reason. | 🟡 **Incomplete** | **Code:** app/services/token_service.py<br>**Tests:** tests/unit/test_token_service.py | Token state machine implemented; atomic capacity locking missing |
| **QUE-005** | MUST | Phase 6 | WebSocket events derive from committed database changes and update user, employee, organization and public display views consistently. | 🔴 **Broken** | **Code:** app/websocket/broadcaster.py<br>**Tests:** None | broadcast_queue_update is never called by token_service |
| **QUE-006** | MUST | Phase 6 | Reconnect clients using a last-event/version check and refresh authoritative state; never rely only on transient socket messages. | 🟡 **Incomplete** | **Code:** app/websocket/manager.py<br>**Tests:** None | In-memory manager without Redis pub/sub |
| **QUE-007** | MUST | Phase 6 | Use idempotency keys for booking and other retry-prone create operations. | 🟡 **Incomplete** | **Code:** app/websocket/manager.py<br>**Tests:** None | In-memory manager without Redis pub/sub |


## 18. Payment Gateway Integration & Webhooks (PAY)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **PAY-001** | MUST | Phase 5 | Integrate an official hosted gateway flow (current configuration supports Razorpay) using test mode before live credentials. | 🟡 **Incomplete** | **Code:** app/services/payment_service.py<br>**Tests:** tests/unit/test_token_service.py | Razorpay order/verify implemented; webhook handling missing |
| **PAY-002** | MUST | Phase 5 | Never store card number, CVV, UPI PIN, banking password or OTP. Secrets remain in environment/secret storage. | 🟡 **Incomplete** | **Code:** app/services/payment_service.py<br>**Tests:** tests/unit/test_token_service.py | Razorpay order/verify implemented; webhook handling missing |
| **PAY-003** | MUST | Phase 5 | Verify checkout signatures and signed webhooks server-side; webhook handling is idempotent and replay-safe. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Webhook idempotency, partial payment, and refunds missing |
| **PAY-004** | MUST | Phase 5 | Payment states include INITIATED, PENDING, SUCCESSFUL, FAILED, CANCELLED, UNDER_REVIEW, DISPUTED, PARTIALLY_REFUNDED and REFUNDED. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Webhook idempotency, partial payment, and refunds missing |
| **PAY-005** | MUST | Phase 5 | A successful token is confirmed only once; delayed webhook and client verification cannot create duplicate tokens or revenue. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Webhook idempotency, partial payment, and refunds missing |


## 19. Immutable Financial Ledger & Snapshots (LED)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **LED-001** | MUST | Phase 5 | Record charges, taxes, platform fees, discounts, refunds, cash, adjustments, chargebacks, settlements and payouts as append-only ledger entries. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Immutable double-entry ledger entries missing |
| **LED-002** | MUST | Phase 5 | Every entry links to organization, token, payment, service items, actor and source transaction where applicable. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Immutable double-entry ledger entries missing |
| **LED-003** | MUST | Phase 5 | Do not edit completed financial history; post reversal/adjustment entries with reason and approval. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Immutable double-entry ledger entries missing |
| **LED-004** | MUST | Phase 5 | Dashboard totals must be derived from ledger entries and reconcile to gateway and cash reports. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Immutable double-entry ledger entries missing |


## 20. Cash Collection & Shift Reconciliation (CASH)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **CASH-001** | MUST | Phase 5 | Cash records capture collecting employee, shift, counter, token, amount, time and reconciliation state. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Cash collection and shift reconciliation missing |
| **CASH-002** | MUST | Phase 5 | Shift close reports MATCHED, SHORTAGE, EXCESS or PENDING_VERIFICATION and requires supervisor/head review. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Cash collection and shift reconciliation missing |
| **CASH-003** | MUST | Phase 5 | Report gross revenue, refunds, discounts, tax, platform fees, net organization revenue, pending/unreconciled cash and payouts by date/service/counter/source. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Cash collection and shift reconciliation missing |


## 21. Admin Financial Controls & Settlements (FIN)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **FIN-001** | MUST | Phase 5 | Authorized finance admins can review, hold/release payout, approve configured refunds, restrict payment methods and pause paid bookings with reason/audit. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Admin payouts, dispute holds, and financial settlement missing |
| **FIN-002** | MUST | Phase 5 | Bank-account changes require strong verification, owner notification, temporary hold and review. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Admin payouts, dispute holds, and financial settlement missing |
| **FIN-003** | MUST | Phase 5 | Risk rules flag unusual amount/volume, duplicate references, refund spikes, after-hours activity, cash mismatch, waivers, price changes and payment-token mismatch. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Admin payouts, dispute holds, and financial settlement missing |
| **FIN-004** | MUST | Phase 5 | Risk flags trigger proportional review; they do not automatically prove fraud or permanently punish an account. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Admin payouts, dispute holds, and financial settlement missing |


## 22. QR Codes, PDF Receipts & Scanning (QR)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **QR-001** | MUST | Phase 6 | Every confirmed token can display/download/print a PDF slip containing organization, branch, token, service, date/slot, queue, payment state, QR and instructions. | 🟡 **Incomplete** | **Code:** app/utils/pdf_generator.py<br>**Tests:** tests/unit/test_pdf_generator.py | PDF receipt generated; missing branch/slot metadata |
| **QR-002** | MUST | Phase 6 | QR contains only a random or signed opaque reference; no name, mobile, email, ID, form answer or payment credential. | 🔴 **Broken** | **Code:** app/utils/qr_generator.py<br>**Tests:** None | Plaintext URL with sequential ID; lacks cryptographic signature |
| **QR-003** | MUST | Phase 6 | Authorized employee/head scanning validates signature/reference, token state, expiry, organization, branch and service before revealing permitted details. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Authorized scanner validation and scan audit logging missing |
| **QR-004** | MUST | Phase 6 | Scan results include VALID, ALREADY_CHECKED_IN, COMPLETED, CANCELLED, EXPIRED, WRONG_BRANCH, WRONG_SERVICE and TAMPERED. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Authorized scanner validation and scan audit logging missing |
| **QR-005** | MUST | Phase 6 | Record issue/expiry, revocation, scan count, first/last scan, device, employee and result. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Authorized scanner validation and scan audit logging missing |
| **QR-006** | MUST | Phase 6 | A scan performs no payment or completion unless a separately authorized explicit action follows. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Authorized scanner validation and scan audit logging missing |
| **QR-007** | MUST | Phase 6 | Printing failure allows safe reprint and alternative SMS/QR delivery without creating another token. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Authorized scanner validation and scan audit logging missing |


## 23. Verified Token Feedback & Ratings (FDB)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **FDB-001** | MUST | Phase 7 | Only the user/guest linked to an eligible completed token may submit one active verified feedback record. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |
| **FDB-002** | MUST | Phase 7 | Collect overall, waiting, staff, service and configurable category ratings, completion result, text, optional attachment, anonymity and follow-up request. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |
| **FDB-003** | MUST | Phase 7 | Public reviews show approved content, verified-token badge, service and month/year; never expose token, contact, ID, exact visit or private details. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |
| **FDB-004** | MUST | Phase 7 | Organization can respond and report, but cannot edit rating/comment or delete negative feedback. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |
| **FDB-005** | MUST | Phase 7 | Moderation states: PENDING, PUBLISHED, HIDDEN, REPORTED and REMOVED with reason and audit history. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |


## 24. Unified Support Cases & Tickets (SUP)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **SUP-001** | MUST | Phase 7 | Use one support-case domain for USER_TO_ORG, USER_TO_ADMIN, ORG_TO_ADMIN, ORG_REPORT_USER, PAYMENT_DISPUTE, FEEDBACK_REPORT and SECURITY_REPORT. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |
| **SUP-002** | MUST | Phase 7 | Common categories include booking, payment/refund, cancellation, QR, queue, closure, service, employee behaviour, identity, login, privacy/security and technical issues. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |
| **SUP-003** | MUST | Phase 7 | When started from a token/payment/feedback, bind known records automatically instead of asking the reporter to retype them. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |
| **SUP-004** | MUST | Phase 7 | Statuses: SUBMITTED, ACKNOWLEDGED, ASSIGNED, IN_REVIEW, WAITING_FOR_PARTY, SOLUTION_PROPOSED, RESOLVED, CLOSED, REOPENED, ESCALATED, REJECTED and DUPLICATE. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |
| **SUP-005** | MUST | Phase 7 | Provide secure threaded messages, attachments, read state, status timeline, assignments, public-party messages and separate admin-internal notes. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |
| **SUP-006** | MUST | Phase 7 | Support user-admin, org-admin, user-org with admin monitoring and controlled three-party conversations without exposing personal contact by default. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |
| **SUP-007** | MUST | Phase 7 | A customer can accept resolution, reject it or request more help; sending a reply alone does not equal successful resolution. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |


## 25. Suggested Solutions & Knowledge Base (KB)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **KB-001** | MUST | Phase 7 | Organizations/admins maintain categorized FAQ, troubleshooting, refund and service response templates. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |
| **KB-002** | MUST | Phase 7 | Agents may select and edit a suggested solution; sensitive/financial responses require human review before sending. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |


## 26. Case Data Binding & Access Controls (CASE)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **CASE-001** | MUST | Phase 7 | Cases reference stable IDs for user, organization, branch, employee, token, counter, service, payment, feedback, parent case and assigned admin. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |
| **CASE-002** | MUST | Phase 7 | Backend validates that linked token/payment/feedback belongs to the reporting participant and organization. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |
| **CASE-003** | MUST | Phase 7 | Suspension or soft deletion must not break historical cases; retain references and audit-safe snapshots. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |
| **CASE-004** | MUST | Phase 7 | Case attachments are private, authorized and access-logged; notification previews omit sensitive content. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Feedback and unified support case domain missing |


## 27. Organization Verification & Onboarding (ORGKYC)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **ORGKYC-001** | MUST | Phase 7 | Statuses: DRAFT, CONTACT_VERIFIED, SUBMITTED, UNDER_REVIEW, MORE_INFO_REQUIRED, APPROVED, REJECTED, RESTRICTED, SUSPENDED, BLOCKED, EXPIRED and REVERIFICATION_REQUIRED. | 🟡 **Incomplete** | **Code:** models/organization_verification.py, app/routers/admin.py<br>**Tests:** tests/api/test_organizations.py | Basic document record; multi-stage KYC workflow missing |
| **ORGKYC-002** | MUST | Phase 7 | Collect legal/public name, type, registration, registered/operating address, contacts, representative, category, services and expected transaction profile. | 🟡 **Incomplete** | **Code:** models/organization_verification.py, app/routers/admin.py<br>**Tests:** tests/api/test_organizations.py | Basic document record; multi-stage KYC workflow missing |
| **ORGKYC-003** | MUST | Phase 7 | Document requirements are configurable by organization category and may include registration, PAN, GST, licence, address, bank proof and authorization. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Tiered verification, reverification, and appeals missing |
| **ORGKYC-004** | MUST | Phase 7 | Verify authorized representative identity/contact and authorization; branch verification is separate where required. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Tiered verification, reverification, and appeals missing |
| **ORGKYC-005** | MUST | Phase 7 | Unverified organizations may configure drafts but cannot publicly accept tokens or payments. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Tiered verification, reverification, and appeals missing |
| **ORGKYC-006** | MUST | Phase 7 | Review interface compares fields/documents, detects missing/expired/duplicate indicators, shows history and supports approve, more-info, reject, restrict and escalate. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Tiered verification, reverification, and appeals missing |
| **ORGKYC-007** | MUST | Phase 7 | Private storage, encryption/masking, malware scan, access logs, retention and least-privilege review are mandatory. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Tiered verification, reverification, and appeals missing |


## 28. Monitoring, Risk Rules & Enforcement (ENF)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **ENF-001** | MUST | Phase 7 | Monitor document expiry, ownership/bank changes, new branches, service changes, complaints, refunds, cancellations, cash mismatch and suspicious employees. | 🟡 **Incomplete** | **Code:** app/routers/admin.py<br>**Tests:** tests/api/test_organizations.py | Admin can approve/suspend org; fraud rules and audit absent |
| **ENF-002** | MUST | Phase 7 | Actions progress from warning to correction deadline, scoped restriction, temporary suspension and permanent block based on evidence and policy. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Risk flags, automated anomaly detection, and appeals missing |
| **ENF-003** | MUST | Phase 7 | Before suspension/block, show active/future tokens, pending payments/refunds, support cases and cash; provide user notification and refund/honor handling. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Risk flags, automated anomaly detection, and appeals missing |
| **ENF-004** | MUST | Phase 7 | Organizations can view reasons, correct data, replace documents, appeal and track reverification. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Risk flags, automated anomaly detection, and appeals missing |
| **ENF-005** | MUST | Phase 7 | High-impact action records evidence, decision maker, second approval where required and appeal outcome. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Risk flags, automated anomaly detection, and appeals missing |


## 29. Public Live Queue Display Screens (DSP)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **DSP-001** | MUST | Phase 6 | A public display is a registered read-only device with no forms, buttons, settings, navigation, organization cookies or management privileges. | 🟡 **Incomplete** | **Code:** templates/public/display.html, app/routers/public.py<br>**Tests:** tests/api/test_public.py | Display screen exists; multi-counter layout missing |
| **DSP-002** | MUST | Phase 6 | It displays organization/branch, date/time, now serving, counter, recent/upcoming tokens, delays, closures and approved announcements. | 🟡 **Incomplete** | **Code:** templates/public/display.html, app/routers/public.py<br>**Tests:** tests/api/test_public.py | Display screen exists; multi-counter layout missing |
| **DSP-003** | MUST | Phase 6 | Support branch-wide, selected counter/service and waiting-area display modes with real-time reconnect and authoritative refresh. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Audio chime, responsive split, and offline display mode missing |
| **DSP-004** | MUST | Phase 6 | Visitor-facing page cannot exit into organization features; management occurs only from authenticated employee/head interfaces. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Audio chime, responsive split, and offline display mode missing |
| **DSP-005** | MUST | Phase 6 | Public API returns a purpose-built allowlisted DTO; never send a full token/user object and hide fields with CSS. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Audio chime, responsive split, and offline display mode missing |
| **DSP-006** | MUST | Phase 6 | Organization head can customize logo, theme, font, language, counter/service subset, row counts, announcements, voice and safe approved fields. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Audio chime, responsive split, and offline display mode missing |
| **DSP-007** | MUST | Phase 6 | Provide MAXIMUM_PRIVACY, STANDARD, ASSISTED_IDENTIFICATION and CUSTOM_SAFE modes; platform privacy rules override organization choices. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Audio chime, responsive split, and offline display mode missing |
| **DSP-008** | MUST | Phase 6 | Custom-form answers default private. Only non-personal operational labels may be approved; passwords, identity, health and payment data remain blocked. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Audio chime, responsive split, and offline display mode missing |


## 30. Display Customization & Remote Device Management (DSPM)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **DSPM-001** | MUST | Phase 6 | Head and authorized employee panel can view status, start, pause, refresh, reset, switch services/counters, restart session and deactivate within permissions. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Remote display lock, device activation token, and commands missing |
| **DSPM-002** | MUST | Phase 6 | Head alone by default can change branch/theme, revoke credential or remove device; employee scope is explicitly delegated. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Remote display lock, device activation token, and commands missing |
| **DSPM-003** | MUST | Phase 6 | Reset must not delete queue/tokens; distinguish refresh, clear transient state, restart session, default configuration and deactivate. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Remote display lock, device activation token, and commands missing |
| **DSPM-004** | MUST | Phase 6 | Browser-only shutdown ends/deactivates the display session. Physical OS power-off is exposed only when a separately approved local device agent supports it. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Remote display lock, device activation token, and commands missing |
| **DSPM-005** | MUST | Phase 6 | Audit command requester, previous/new configuration, time, result and device response. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Remote display lock, device activation token, and commands missing |


## 31. Database Blueprint & Data Integrity Constraints (DB)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **DB-001** | MUST | Phase 0 & 1 | Unique gateway payment IDs, webhook event IDs, QR references and idempotency keys prevent duplicates. | 🟡 **Incomplete** | **Code:** models/payment.py<br>**Tests:** tests/unit/test_token_service.py | Unique payment constraint exists; idempotency keys missing |
| **DB-002** | MUST | Phase 0 & 1 | Money uses DECIMAL with defined currency precision; never binary floating point. | 🟢 **Implemented** | **Code:** models/service.py, models/payment.py<br>**Tests:** tests/unit/test_token_service.py | DECIMAL(10,2) used for money fields |
| **DB-003** | MUST | Phase 0 & 1 | Store price, service, form and schedule snapshots necessary to interpret historical tokens. | 🟡 **Incomplete** | **Code:** models/<br>**Tests:** None | Snapshotting and cascade delete protections partially defined |
| **DB-004** | MUST | Phase 0 & 1 | Use created_at, updated_at and appropriate status history; financial/audit records are not hard-deleted. | 🟢 **Implemented** | **Code:** models/mixins.py<br>**Tests:** tests/unit/test_token_service.py | created_at and updated_at mixins present |
| **DB-005** | MUST | Phase 0 & 1 | Index tenant/status/date search paths and organization name/type/city/PIN/service search fields. | 🟢 **Implemented** | **Code:** database/schema.sql<br>**Tests:** tests/unit/test_token_service.py | Indexes on status, org_id, user_id defined |
| **DB-006** | MUST | Phase 0 & 1 | Define cascade rules deliberately; do not cascade-delete payments, tokens, cases, verification or audit evidence. | 🟡 **Incomplete** | **Code:** models/<br>**Tests:** None | Snapshotting and cascade delete protections partially defined |


## 32. API Standards, Serialization & Data Flow (API)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **API-001** | MUST | Phase 0 & 1 | Use versioned JSON APIs for interactive features and consistent success, validation, authorization, conflict and error schemas. | 🟢 **Implemented** | **Code:** app/schemas/common.py<br>**Tests:** tests/unit/test_token_service.py | APIResponse and PaginatedResponse defined |
| **API-002** | MUST | Phase 0 & 1 | Validate all input with Pydantic schemas; trim/normalize contact data and reject unknown protected fields. | 🟢 **Implemented** | **Code:** app/schemas/common.py<br>**Tests:** tests/unit/test_token_service.py | APIResponse and PaginatedResponse defined |
| **API-003** | MUST | Phase 0 & 1 | Use pagination, filter allowlists, deterministic sorting and maximum page sizes. | 🟡 **Incomplete** | **Code:** app/routers/<br>**Tests:** None | Error handling standardized; idempotency headers missing |
| **API-004** | MUST | Phase 0 & 1 | Return public reference codes instead of predictable database IDs when exposure is unnecessary. | 🟡 **Incomplete** | **Code:** app/routers/<br>**Tests:** None | Error handling standardized; idempotency headers missing |
| **API-005** | MUST | Phase 0 & 1 | Use 409 for state/capacity conflicts, 422 for validation, 401/403 correctly, and never leak stack traces/secrets in production. | 🟡 **Incomplete** | **Code:** app/routers/<br>**Tests:** None | Error handling standardized; idempotency headers missing |


## 11. Online Booking Flow (FLOW)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **FLOW-001** | MUST | Phase 4 | Use an outbox/retry mechanism for notifications and WebSocket events so external failure does not roll back committed business records or lose events. | 🟡 **Incomplete** | **Code:** app/routers/user.py, token_service.py<br>**Tests:** tests/unit/test_token_service.py | Basic online token booking exists; slot reservation missing |


## 33. Multi-channel Notifications & Outbox (NTF)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **NTF-001** | MUST | Phase 8 | Support in-app first, with configurable email/SMS channels for verification, booking, queue call, schedule change, payment/refund, support and security events. | 🟡 **Incomplete** | **Code:** app/services/notification_service.py<br>**Tests:** tests/unit/test_notification_service.py | Email and in-app templates exist; SMS integration incomplete |
| **NTF-002** | MUST | Phase 8 | Store recipient, type, template/version, related entity, channel, delivery state, attempts and timestamps. | 🟡 **Incomplete** | **Code:** app/services/notification_service.py<br>**Tests:** tests/unit/test_notification_service.py | Email and in-app templates exist; SMS integration incomplete |
| **NTF-003** | MUST | Phase 8 | Avoid sensitive personal/complaint/payment details in lock-screen, email subject or SMS preview. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Notification outbox and SMS webhook delivery missing |
| **NTF-004** | MUST | Phase 8 | Users manage optional notifications; essential security, payment and service-disruption notices cannot be disabled where required. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Notification outbox and SMS webhook delivery missing |
| **NTF-005** | MUST | Phase 8 | Queue-call notifications are deduplicated and rate-limited; failed delivery does not change token state. | ⚪ **Missing** | **Code:** None<br>**Tests:** None | Notification outbox and SMS webhook delivery missing |


## 34. Security, Privacy & Compliance Engineering (SEC)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **SEC-001** | MUST | Phase 1 & 8 | Use least privilege, deny-by-default authorization, tenant filters and object-level checks. | 🟡 **Incomplete** | **Code:** app/dependencies.py<br>**Tests:** None | Tenant checks exist on some routes, but not unified or comprehensive |
| **SEC-002** | MUST | Phase 1 & 8 | Encrypt transport with HTTPS; encrypt high-risk identifiers/documents at rest and manage keys outside source control. | 🟡 **Incomplete** | **Code:** app/main.py<br>**Tests:** tests/unit/test_security.py | Security headers, audit trails, and data masking partially configured |
| **SEC-003** | MUST | Phase 1 & 8 | Validate uploads by content, size and policy; use private object storage/signed short-lived access and malware scanning. | 🟡 **Incomplete** | **Code:** app/main.py<br>**Tests:** tests/unit/test_security.py | Security headers, audit trails, and data masking partially configured |
| **SEC-004** | MUST | Phase 1 & 8 | Implement output escaping, CSP, secure headers, SQLAlchemy parameterization, CSRF and rate limiting. | 🟡 **Incomplete** | **Code:** app/main.py<br>**Tests:** tests/unit/test_security.py | Security headers, audit trails, and data masking partially configured |
| **SEC-005** | MUST | Phase 1 & 8 | Do not place secrets or real passwords in ZIP, Git, logs or frontend code. Keep .env ignored and provide .env.example placeholders. | 🟡 **Incomplete** | **Code:** app/main.py<br>**Tests:** tests/unit/test_security.py | Security headers, audit trails, and data masking partially configured |
| **SEC-006** | MUST | Phase 1 & 8 | Log sensitive reads/actions without logging secrets or full sensitive values; protect audit access and retention. | 🟡 **Incomplete** | **Code:** app/main.py<br>**Tests:** tests/unit/test_security.py | Security headers, audit trails, and data masking partially configured |
| **SEC-007** | MUST | Phase 1 & 8 | Define consent, purpose, retention and deletion/anonymization policies for identity, form, support and analytics data. | 🟡 **Incomplete** | **Code:** app/main.py<br>**Tests:** tests/unit/test_security.py | Security headers, audit trails, and data masking partially configured |
| **SEC-008** | MUST | Phase 1 & 8 | Public display, notification and export paths use data minimization and field allowlists. | 🟡 **Incomplete** | **Code:** app/main.py<br>**Tests:** tests/unit/test_security.py | Security headers, audit trails, and data masking partially configured |
| **SEC-009** | MUST | Phase 1 & 8 | Run dependency, secret and vulnerability scanning before release; patch critical/high findings. Legal review: Identity documents, Aadhaar-related handling, payments, refunds, tax, data retention and organization licensing must be reviewed against applicable Indian law, gateway terms and organization-sector obligations before production. | 🟡 **Incomplete** | **Code:** app/main.py<br>**Tests:** tests/unit/test_security.py | Security headers, audit trails, and data masking partially configured |


## 35. No-Dummy Interface Quality Standard (UIQ)

| ID | Level | Target Phase | Requirement Summary | Status | Existing Code & Tests | Gap / Action |
|---|:---:|:---:|---|:---:|---|---|
| **UIQ-001** | MUST | Phase 0 & 8 | A visible navigation item MUST open a real permission-correct page containing usable features. | 🟡 **Incomplete** | **Code:** templates/<br>**Tests:** None | Some dashboards lack honest no-data states; several settings buttons lack backend endpoints |
| **UIQ-002** | MUST | Phase 0 & 8 | A visible button MUST call a real backend operation, show loading, prevent duplicate action, report success/error and refresh state. | 🟡 **Incomplete** | **Code:** templates/<br>**Tests:** None | Some dashboards lack honest no-data states; several settings buttons lack backend endpoints |
| **UIQ-003** | MUST | Phase 0 & 8 | Dashboards MUST use real database aggregates; when empty, show an honest no-data state rather than invented numbers. | 🟡 **Incomplete** | **Code:** templates/<br>**Tests:** None | Some dashboards lack honest no-data states; several settings buttons lack backend endpoints |
| **UIQ-004** | MUST | Phase 0 & 8 | Incomplete features remain hidden behind controlled feature flags; do not ship empty settings or placeholder routes. | 🟡 **Incomplete** | **Code:** templates/<br>**Tests:** None | Some dashboards lack honest no-data states; several settings buttons lack backend endpoints |
| **UIQ-005** | MUST | Phase 0 & 8 | Every form includes labels, help where needed, client convenience validation, authoritative server validation and preserved safe input after errors. | 🟡 **Incomplete** | **Code:** templates/<br>**Tests:** None | Some dashboards lack honest no-data states; several settings buttons lack backend endpoints |

