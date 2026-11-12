# SEC-009 — Legal Review Handover (human approval required)

Status: **PENDING — requires approval by an authorized human reviewer.**
This document is the review handover only; no approval exists and none is
created by this file. Nothing here should be read as legal advice.

Source requirement (`docs/FEATURE_REQUIREMENTS_MATRIX.md`, row **SEC-009**,
MUST, Phases 1 & 8):

> "Run dependency, secret and vulnerability scanning before release; patch
> critical/high findings. Legal review: Identity documents, Aadhaar-related
> handling, payments, refunds, tax, data retention and organization
> licensing must be reviewed against applicable Indian law, gateway terms
> and organization-sector obligations before production."

Related requirement context (`docs/MASTER_REQUIREMENTS.txt` §17/open
decision, §25 item 17): "Data retention, privacy, and any applicable
legal/compliance requirements" are an explicitly open decision.

---

## 1. Implemented technical controls relevant to the review

| Control | Evidence location |
|---|---|
| Secret & dependency scanning tooling (all tracked files + `pip-audit` for CVEs) | `scripts/security_scan.py`; Makefile targets `security-scan` / `pip-audit` |
| Phase-8 scan outcome: 104 findings, all confined to example payloads in tests/docs; **zero findings in shipped source** | recorded in the requirements matrix (SEC-009 row) and Phase-8 evidence |
| Fresh full-tree secret classification scan (Nov 8): zero real credentials tracked; local `.env` git-ignored and never committed; CI uses synthetic values only | `docs/PRODUCTION_ACCEPTANCE_REPORT.md` §9 |
| Organization KYC / verification workflow with auditable decisions, suspension/reinstatement under dual approval | `app/models/organization_kyc.py`, `organization_verification.py`, enforcement service; Phase-9 audit |
| Payment integrity: server-side verification, webhook retry safety, idempotency keys, no client-claims-paid path | `app/services/payment_service.py`, `app/models/idempotency.py`; Phase-9 evidence + hosted suite |
| Data minimisation primitives: uniform 404s for cross-tenant reads, signed-purpose QR (scan/track/receipt), constant-time compares | `app/utils/signed_qr.py`; Phase-9 audit |
| Audit logging of verification/admin/enforcement actions | `app/models/audit_log.py` + service usage (Phase-9 evidence) |

## 2. Matters the reviewer must assess (legal/privacy surface in the code)

1. **Aadhaar / identity documents** — whether any KYC upload, storage,
   format, or deletion behaviour in the organization-verification flow
   complies with applicable Indian rules (e.g., DPDP Act 2023 / Aadhaar
   regulations) for collection, retention limits, and redaction.
2. **Payments & refunds** — Razorpay (test-mode locally) terms: customer
   consent UI, refund/cancellation authorisation thresholds, tax/invoice
   fields in receipts, reconciliation and disputes.
3. **Guest mobile numbers & kiosk walk-ins** — lawful basis for contact
   data use, retention windows, and whether SMS/email notifications need
   opt-in or DLT registration before launch.
4. **Data retention & deletion** — no product-wide retention schedule is
   implemented beyond audit trails; reviewer must set policy and the
   implementation plan (OPEN DECISIONS §25 item 17 remains open).
5. **Organization licensing** — verification evidence requirements per
   organization category (OPEN DECISIONS §25 item 3) affect whether the
   product may list an organization pre-approval.
6. **Healthcare-adjacent exposure** — if organization types include
   clinics/hospitals, sector-specific privacy obligations may apply (§25
   item 20 leaves scope open).

## 3. Unresolved legal questions (needs the reviewer's written answer)

1. Which identity-document data may be retained, for how long, and in what
   encrypted/redacted form?
2. Is an explicit consent record required for guest mobile collection at
   kiosk/AI channels, and where must it live?
3. Do refund/cash flows require customer-facing receipt statements
   (tax, complaint channel) to satisfy Indian consumer/tax rules?
4. Applicable retention/deletion policy for conversations with the AI
   assistant (prompt/content logs, `ai_conversations`).
5. Any legal gating on listing an organization while verification is
   pending (affecting public display rules).
6. Whether the deployment target (Indian jurisdiction assumed) changes the
   data-residency or localization requirements for the database/backup.

## 4. Evidence references for the reviewer

- `docs/FEATURE_REQUIREMENTS_MATRIX.md` — SEC-009 row (MUST; current status
  "tooling shipped; legal review remains an explicitly human,
  pre-production obligation").
- `docs/PHASE_9_FINAL_ACCEPTANCE_AUDIT.md` — security & privacy-relevant
  acceptance evidence.
- `docs/PRODUCTION_READINESS_REPORT.md` §9 (traceability) and §5 lead rows
  (Nov 6–8 session evidence).
- `docs/PRODUCTION_ACCEPTANCE_REPORT.md` §9 (full secret-scan
  classification; zero tracked credentials).
- `docs/OPEN_DECISIONS.md` — open decision §25 items 3 & 17.

## 5. Approval block (to be completed by the authorized human reviewer)

```
Requirements reviewed (SPEC §24.8/§24.9 + SEC-009 text):  [ ] yes [ ] no
Technical controls accepted:                              [ ] yes [ ] no
Legal/privacy questions answered in writing:              [ ] yes [ ] no
Conditions imposed (list): ________________________________
Approval:        APPROVED / APPROVED WITH CONDITIONS / REJECTED
Reviewer name:  ______________________  Role: ______________
Date:           ______________________
```

Until this block is signed, SEC-009 remains **NOT VERIFIED** and the overall
production-readiness verdict may not advance to A.
