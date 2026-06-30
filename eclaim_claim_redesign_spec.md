# e-Claim — Corporate Expense Claim Redesign (design study)

Status: **proposal / for review**
Author: design study, 2026-06-27
Scope: turn the single-receipt carbon-capture `claim` into a full corporate
expense-claim system for a listed company (~3,000 staff), while keeping the
carbon ledger (tCO₂e) computed quietly underneath.

---

## 1. Why

The current `claim` row is a **carbon-capture record**: one claim = one receipt,
and the columns are emissions-first (`scope`, `factor_key`, `tco2e`, `basis`,
`data_quality`). The review screen looks "too simple" because the model only has
~8 business fields to show. To capture *most* of a staff claim (not just fuel) we
must extend the model first; the richer UI then follows from the richer data.

### Decisions (settled with stakeholder)

| Decision | Choice |
|---|---|
| Claim grain | Multi-line claim, under an **optional Event** (trip/training) holding purpose, pax, dates & budget |
| Approval | **Single approver, one action**, with line-level approve / query / reject (partial approval) |
| Settlement | Approve → **report or ERP/accounting integration**; ERP does the payout (no in-house payroll engine) |
| Primary purpose | **Reimbursement-first**; carbon (tCO₂e) computed underneath, non-blocking |

---

## 2. The three-tier model

```
EVENT (optional)   title · purpose · pax · dates · location · cost centre · project · BUDGET
  └ CLAIM          one employee's submission, tied to an event (or standalone)
      └ LINE ITEM  one receipt: vendor · date · amount · GST · category · payment method · carbon
```

An Event is **optional**: a solo taxi claim is just a 1-line claim. Anything with
a trip / training / multiple bills gets the Event wrapper — that is where
**budget** and **related-bill consolidation** live, and it aggregates across many
claims and many people.

---

## 3. Field study — what a claim system needs

Grouped by tier. **Bold = mandatory**, *(carbon)* = computed underneath, not keyed.

### 3.1 Event (optional grouping)
- **Title** (e.g. "A City — Sales Training")
- **Purpose / description**
- Event type — training / travel / client meeting / conference / team / project
- **Attendee count** (e.g. 20 pax) · optional attendee list
- **Start date**, **end date**
- Location / city / country
- **Department / cost centre**
- Project / job code
- **Budget amount** + currency
- Organiser (owning employee)
- Rollup status: total spent · committed · remaining

### 3.2 Claim (per-employee submission)
- **Claim number** (auto)
- **Claimant**: name, employee ref, department, cost centre, grade/band *(band drives per-diem caps)*
- Linked Event (optional)
- **Title / purpose** (if standalone)
- **Submission date**, period/month
- **Claim currency**
- **Status**: draft → submitted → in_review → (approved | partially_approved | sent_back | rejected) → exported → paid
- Approver, approval date, approver note
- Totals: **claimed**, approved, reimbursable, non-reimbursable (corp-card)

### 3.3 Line item (per receipt / expense) — the rich record
- **Category / expense type** — airfare, hotel, meals, taxi/Grab, mileage, parking, toll, fuel, entertainment, telco, office, per-diem, …
- **Expense date**
- **Vendor / merchant**
- **Business reason / description** (line note)
- Document / invoice no
- **Gross amount**
- **Tax**: GST/SST amount, tax code, tax-inclusive flag → net amount
- Foreign currency: original amount, **FX rate**, base-currency amount
- Quantity / unit (nights, km, pax)
- **Payment method**: out-of-pocket / corporate card / company-paid → drives *reimbursable* flag
- **Receipt attachment** + OCR confidence *(upload flow already good)*
- Cost-centre / project override (if line posts elsewhere)
- Attendees (entertainment — who was hosted; tax deductibility)
- Mileage block: from, to, km, rate/km
- Per-diem block: days, daily rate
- Policy result: within-limit / over-limit / receipt-required
- GL account / export code
- *(carbon)* scope, factor_key, factor_version, basis, tco2e, data_quality
- **Line status**: approved / queried / rejected (+ **reason** when not approved)

### 3.4 Policy & control (listed-co / SOX-style)
- Per-category spend cap (e.g. meals RM50/day)
- Receipt-required threshold
- Approver authority limit (already on `app_user.authority_limit`)
- Duplicate detection: same vendor + amount + date
- Late-submission flag: days since expense date
- **Split-claim detection**: related claims same event/purpose/date → consolidated total

### 3.5 Settlement / export
- GL account + cost centre + tax code for posting
- Payee ref (employee ID for payroll) or bank details
- Export batch id, date, format (CSV / ERP API)
- Payment status: exported → paid → reconciled; payment date, ref

---

## 4. Data-model changes

Today's `claim` row ≈ tomorrow's **`claim_line`**. We split header from line and
add the Event.

### New: `event`
`id, firm_id, client_id, title, purpose, event_type, attendee_count,
start_date, end_date, location, department, cost_centre, project_code,
budget_amount, budget_currency, organiser_user_id, status, created_at`

### Reworked: `claim` (now a header)
Keep: `id, firm_id, client_id, created_by_user_id, submitted_by_claimant_id,
approved_by_user_id, source_channel, claimant_ref, status, received_at,
created_at, updated_at`.
Add: `event_id (nullable FK), title, purpose, claim_currency, period,
total_claimed, total_approved, total_reimbursable, approver_note`.
New statuses: add `partially_approved`, `exported`, `paid` to the status CHECK.

### New: `claim_line` (the per-receipt record — most of today's `claim`)
Moves here from `claim`: `vendor, doc_no, doc_date, currency, total_amount,
expense_type, quantity, unit, ocr_confidence, image_path, image_sha256,
scope, factor_key, factor_version, basis, tco2e, data_quality, category_id`.
Add: `claim_id FK, line_no, business_reason, tax_amount, tax_code, tax_inclusive,
net_amount, fx_rate, base_amount, payment_method, reimbursable, gl_code,
cost_centre_override, attendees (jsonb), mileage (jsonb), per_diem (jsonb),
policy_result, line_status, line_reason`.

### Migration path (`0008_*`)
1. create `event`, `claim_line`.
2. backfill: one `claim_line` per existing `claim` (copy the OCR + carbon cols),
   `line_no = 1`, `payment_method = 'out_of_pocket'`, `reimbursable = true`.
3. add header columns to `claim`; set `total_* ` from the single line.
4. leave old columns on `claim` for one release (read from line), then drop in a
   later migration. Carbon release/export reads from `claim_line` after cutover.

> `emission_entry` (the carbon ledger) now projects from `claim_line`, not
> `claim` — one entry per carbon-relevant line. SoD CHECK (`approved_by ≠
> created_by`) stays on the claim header.

---

## 5. UI redesign — the richer review/detail screen

Today: image on the left, ~8 flat read-only fields on the right. New layout keeps
the receipt viewer (you like the upload/preview), and replaces the flat fields
with an **event context panel + budget bar + line-item table + related-claims
strip + partial-approval actions.**

```
┌─ Review · Claim #2381 ───────────────────────────────────  [submitted] ──┐
│ ◀ Inbox                                              3 more to verify  ▶  │
├──────────────────────────────────────────────────────────────────────────┤
│  EVENT  "A City — Sales Training"     👥 20 pax · 12–14 Mar · KL          │
│  Purpose: Regional sales enablement   Cost centre: SALES-02 · Proj: T-1187│
│  Budget ███████████░░░░  RM 7,200 / 10,000   ·  Remaining RM 2,800        │
│                                                                            │
│  ⚠ 3rd claim for this event. Combined spend RM 6,050 → RM 7,200.          │
│     Related: #2381 (Aisha) · #2390 (Ben) · #2455 (late hotel)            │
├───────────────┬────────────────────────────────────────────────────────┤
│  RECEIPT      │  LINES                                  claimed RM 4,200 │
│  ┌─────────┐  │  ┌───────────────────────────────────────────────────┐  │
│  │ [image] │  │  │ # Category   Vendor    Date    Amt   Pay    Stat  │  │
│  │ for the │  │  │ 1 Venue/F&B  Hilton   12 Mar 3,000  OOP   ✅      │  │
│  │ selected│  │  │ 2 Trainer    Acme     12 Mar 1,200  Card  ✅      │  │
│  │ line    │  │  │ 3 Grab x3    Grab     13 Mar    60   OOP   ❌ no rcpt│  │
│  │ zoom ↻ ⤓│  │  └───────────────────────────────────────────────────┘  │
│  └─────────┘  │  ▼ Line 1 detail                                        │
│  sha256 9f3c… │     GST RM 170 (incl) · net RM 2,830 · GL 6200          │
│               │     Reason: venue hire + lunch, 20 pax                  │
│               │     🌿 Scope 3 · 0.42 tCO₂e  (computed)                  │
│               │                                                          │
│               │  Approved RM 4,140 · Rejected RM 60 · Reimbursable 2,940│
│               │  [ Reject all ] [ Send back ]      [ ✓ Approve claim ]  │
└───────────────┴────────────────────────────────────────────────────────┘
```

Key UI behaviours:
- **Event panel first** so the approver gets context (what, why, who, budget)
  before any receipt — fast decision.
- **Budget bar** turns red if approving would exceed the event budget.
- **Related-claims alert** appears when ≥2 claims share an event (or fuzzy match:
  same purpose + overlapping dates + claimant) — catches late bills *and* split
  claims, with the **combined figure added together**.
- **Line table** with per-line status chips; click a line → loads its receipt in
  the left viewer + expands its detail (GST, GL, reason, attendees, carbon chip).
- **Partial approval**: one **Approve claim** action; lines individually flipped
  to query/reject carry a mandatory reason. Reimbursable total = Σ approved
  out-of-pocket lines. Corp-card lines reconcile only (no payout).
- On approve → claim becomes `approved`/`partially_approved`, then **Export**
  (CSV or ERP API) carries GL code, cost centre, tax code, payee ref.

---

## 6. Phasing

1. **Model split** — `event`, `claim_line`, header columns, migration `0008` +
   backfill. Carbon release reads from `claim_line`.
2. **Capture** — keep the loved upload flow; add line grouping into one claim +
   the header fields (purpose, cost centre, payment method per line).
3. **Review UI** — the screen in §5 (event panel, budget bar, line table,
   related-claims strip, partial approval).
4. **Events & budget** — event create/link, budget tracking, related-claim
   detection + split-claim alert.
5. **Settlement** — export report + ERP/accounting integration; payment status
   write-back.

Carbon stays automatic throughout: each carbon-relevant line still resolves a
factor and posts tCO₂e to the shared ledger on release — the user just sees a
quiet green chip, not a carbon form.
```
