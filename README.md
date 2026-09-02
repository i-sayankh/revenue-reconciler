# Revenue Reconciler

A full-stack tool for reconciling an e-commerce store's order exports against
its payment processor's transaction exports. It ingests two CSVs (orders,
payments), runs a deterministic rules engine to classify every order into a
fixed discrepancy taxonomy, persists the results per authenticated user, and
presents them on a dashboard with LLM-generated plain-language explanations
for each discrepancy.

Nothing about *which bucket a discrepancy falls into* is ever decided by the
LLM — that is a pure, offline, unit-tested function of the two CSVs. The LLM
only narrates a classification the engine already made.

## Architecture

- **Frontend**: Next.js (App Router) + TypeScript, styled with Tailwind and
  shadcn/ui components (charts via Recharts). Deployed target is Vercel
  (not yet deployed as of this commit — see "Deployment" below).
- **Backend**: FastAPI (Python), talking to Postgres directly via `asyncpg`.
  There is no ORM and no migration framework — `backend/schema.sql` is a
  single idempotent script (every statement is `create ... if not exists`)
  applied once to set up the four tables. Deployed target is Render.
- **Database**: Supabase Postgres. The app only uses Supabase for Postgres
  hosting and for auth — there's no dependency on Supabase's client-side
  data APIs; all reads/writes go through the FastAPI backend.
- **Auth**: Supabase email/password. The frontend signs in via
  `@supabase/supabase-js`, holds the session, and attaches the access token
  as `Authorization: Bearer <token>` on every backend call
  (`frontend/src/lib/api.ts`). The backend verifies that JWT on every
  protected route (`backend/app/auth.py`): by default it fetches the
  project's JWKS and verifies an RS256/ES256 signature; a legacy mode
  verifies HS256 against a shared `SUPABASE_JWT_SECRET` instead, for
  Supabase projects still on that older signing scheme. Either way, the
  verified `sub` claim becomes the `user_id` every query is scoped by —
  there is no cross-user data access at any layer.
- **LLM**: Groq, called only from the backend (`backend/app/llm/groq_client.py`).
  The API key never reaches the frontend. See "LLM approach" below.

### Directory structure

```
backend/
  app/
    auth.py          # JWT verification (JWKS + legacy HS256 modes)
    config.py         # Settings (env vars)
    db.py              # asyncpg connection/pool lifecycle
    errors.py          # exception handlers -> consistent JSON error shape
    models.py           # plain dataclasses for the 4 tables (no ORM)
    engine/
      reconcile.py       # the deterministic reconciliation engine
    ingest/
      parsing.py          # CSV parsing/normalization (pure functions)
      loader.py            # per-row validation + DB inserts for uploads
    reconcile/
      service.py            # DB <-> engine glue, run/discrepancy persistence
    llm/
      groq_client.py         # discrepancy explanations via Groq
    routers/
      whoami.py               # GET /api/whoami
      ingest.py                # POST /api/ingest/orders, /api/ingest/payments
      reconcile.py              # POST /api/reconcile/run, GET runs/latest,
                                 # GET /api/discrepancies, POST .../explain
  scripts/
    init_db.py           # one-shot: apply schema.sql to DATABASE_URL
  schema.sql              # orders, payments, reconciliation_runs, discrepancies
  tests/                  # pytest suite (engine, parsing, ingest, auth, ...)
  requirements.txt
  .env.example
frontend/
  src/
    app/
      page.tsx            # landing page
      login/, signup/     # auth pages
      upload/              # CSV upload flow
      dashboard/            # stat cards, chart, discrepancy table, explanations
    components/
      dashboard/             # stat-cards, discrepancy-chart, discrepancy-table,
                              # explanation-panel, type-badge
      ui/                     # shadcn/ui primitives
      auth-guard.tsx          # redirects unauthenticated users
    hooks/use-session.ts      # Supabase session hook
    lib/
      api.ts                   # authenticated fetch wrapper for the backend
      supabase.ts               # Supabase client
      discrepancy-types.ts       # shared discrepancy-type constants/labels
  .env.example
sample-data/
  orders.csv, payments.csv   # sample data for manual/local testing (git-ignored)
```

## Local setup

### Prerequisites

- Node 20+
- Python 3.11+
- A Supabase project (for Postgres + auth)
- A Groq API key

### Backend

```bash
cd backend
python -m venv venv
# Windows: venv\Scripts\activate    macOS/Linux: source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in DATABASE_URL, SUPABASE_URL, GROQ_API_KEY (and SUPABASE_JWT_SECRET
# only if your Supabase project is on the legacy shared-secret signing mode)

python scripts/init_db.py       # applies schema.sql once; safe to re-run
uvicorn app.main:app --reload   # http://localhost:8000
```

`DATABASE_URL` caveat: Supabase's direct connection string
(`db.<project-ref>.supabase.co:5432`) can be IPv6-only and unreachable on
some networks. If `init_db.py` or the app can't connect, use the
pooled/session connection string from the Supabase dashboard's
**Settings → Database** page instead — it works over IPv4.

### Frontend

```bash
cd frontend
cp .env.example .env.local
# fill in NEXT_PUBLIC_SUPABASE_URL, NEXT_PUBLIC_SUPABASE_ANON_KEY
# (NEXT_PUBLIC_API_URL already defaults to http://localhost:8000)

npm install
npm run dev   # http://localhost:3000
```

### Running tests

With the backend virtualenv from above active:

```bash
cd backend
pytest
```

### Deployment

Deployment has not happened yet for this project. The intended targets are
Vercel for the frontend and Render for the backend; when deployed, the
backend's `ALLOWED_ORIGINS` env var gets the deployed frontend origin added
(comma-separated) and the frontend's `NEXT_PUBLIC_API_URL` points at the
deployed backend — no code changes required for either.

## Reconciliation logic

### Matching

Orders and payments are matched on
`order.order_id_norm == payment.order_reference_norm`. Both sides are
normalized with `strip().upper()` at parse time so that case- and
whitespace-mangled references (` fix-1012 ` vs `FIX-1012`) still match.

### Tolerance

Amount comparison uses a fixed **$0.02 absolute** tolerance — not a
percentage. It compares the order's `net_amount` (never `gross_amount`)
against the settled payment's `amount` (never `net_settled`).
`net_settled = amount − fee`, and the processor's fee is a cost of doing
business, not a discrepancy between what the store expected and what the
processor recorded — so fees are simply not part of the comparison.

### Taxonomy and evaluation order

A single order can plausibly satisfy more than one condition at once (a
double-charged order can also look like an "amount mismatch" against one
of the two charges), so the engine checks conditions in a fixed priority
order and returns on the first match:

1. **MISSING_PAYMENT** — the order is `completed` and no charge-type payment
   matched at all.
2. **DUPLICATE_CHARGE** — more than one *settled* charge matched, with no
   settled refund offsetting them. Checked early: if an order was
   double-billed, that's the more important, more actionable fact than
   whatever a currency/amount check against just one of the two charges
   would report.
3. **STATUS_CONTRADICTION** — the order's own status flatly disagrees with
   the charge/refund trail (see below). Checked before currency/amount/
   settlement checks, because those checks assume the order's status is a
   trustworthy frame to interpret the payment against.
4. **UNSETTLED_PAYMENT** — the order is `completed`, but its matched charge
   never actually settled (`failed`/`pending`). There's no settled money to
   compare amounts or currency against yet.
5. **CURRENCY_MISMATCH** — the order and its settled charge are in different
   currencies. No FX conversion is in scope, so amounts are never compared
   across currencies.
6. **AMOUNT_MISMATCH** — same currency, but the settled charge amount and
   the order's net amount differ by more than $0.02.
7. **RECONCILED** — everything else: matched (or deliberately not
   contradicted), same currency where applicable, within tolerance,
   settled, status agrees.

**ORPHAN_PAYMENT** sits outside this per-order chain — it's evaluated from
the payment side: any settled charge whose normalized order reference
matches no order at all.

`STATUS_CONTRADICTION` has two internal flavors that matter a lot
financially even though both report the same top-level type:

- **cancelled-but-charged**: the order says `cancelled`, but a settled
  charge was never offset by a settled refund. The money is still out the
  door — real money at risk until it's refunded.
- **completed-but-refunded**: the order still says `completed`, but the
  charge/refund trail shows the money already went back. No money is
  currently at risk — the books already reflect the refund; the only
  problem is that nobody updated the order's status. It's a data-hygiene
  issue, not a financial one.

`RECONCILED` also has two internal flavors:

- **verified**: an actual settled charge was found, matched within
  tolerance, same currency, agreeing status — a genuine, checked
  reconciliation.
- **no_charge_activity**: the fallback case — no settled charge exists to
  compare against at all (e.g. an order still `pending` with nothing
  settled matched yet). There's no contradicting evidence, so it correctly
  reports `RECONCILED`, but nothing was actually verified, so this flavor
  is deliberately excluded from `total_reconciled_value` — otherwise
  unverified, still-pending orders would silently inflate the headline
  "money successfully reconciled" number. It is also excluded from the
  `by_type` chart's RECONCILED value (though not its count) for the same
  reason, so the stat card and the chart never disagree on the same run.

### Money at risk

`money_at_risk` is a sum of three components, deliberately narrower than
the "total disputed value" the dashboard also reports (which is every
non-RECONCILED order's value plus every orphan payment's amount — a wider
"needs a human to look at this" net):

1. The full `order.net_amount` for every **MISSING_PAYMENT** and
   **UNSETTLED_PAYMENT** — money the store is owed and hasn't (verifiably)
   received.
2. The **signed** net over/undercharge for every **AMOUNT_MISMATCH**
   (`payment.amount − order.net_amount`) — an overcharge adds to risk
   (money that may need to be refunded), an undercharge subtracts from it
   (money the store is still short).
3. The full order value for the **cancelled-but-charged** flavor of
   **STATUS_CONTRADICTION** only — the order was cancelled but the charge
   was never refunded, so that money is sitting with the store despite the
   order no longer being valid.

The **completed-but-refunded** flavor of `STATUS_CONTRADICTION`
deliberately contributes **$0** to money at risk. This is a headline number
someone will ask about, so to be explicit: the refund already happened and
the books already reflect it. Counting it here would double-count money
that isn't actually exposed — the real problem in that case is a stale
order status, not outstanding money, and it's surfaced separately (via
`total_disputed_value` and the discrepancy list) as a data-hygiene item
rather than folded into the risk figure.

## What was found in the sample source data

Running the engine against this project's own sample orders/payments
(185 orders, 187 payments after parsing) surfaced essentially every category
the taxonomy was built for, not just textbook cases:

- **One exact duplicate order row.** A single order appeared twice in the
  export, byte-for-byte identical. The parser dedupes exact-duplicate rows
  before anything else runs (`app.ingest.parsing._dedup_exact_rows`), and
  the engine dedupes again defensively on normalized order id
  (`_dedup_orders`) in case two rows for the same order ever differ in
  content — so a re-export or a copy/paste error in the source system never
  gets double-counted as two orders.
- **Orders with no payment on file at all** — a `completed` order the
  processor has no record of. These are `MISSING_PAYMENT`: money the store
  believes it should have collected but has no evidence it did.
- **Payments referencing an order id that doesn't exist** — a processor-side
  transaction whose order reference matches nothing in the orders export.
  These surface as `ORPHAN_PAYMENT`, evaluated separately from the per-order
  taxonomy since there's no order to attach the discrepancy to.
- **Orders that were double-charged** — two separate settled charges against
  the same order with no offsetting refund. This is the classic "billed the
  customer twice" failure mode, and it's checked early in the priority chain
  precisely because it's the most urgent thing to flag.
- **Case/whitespace-mangled reference values** — a processor export with an
  order reference like `" fix-1012 "` against an orders export with
  `FIX-1012`. Normalizing both sides with `strip().upper()` before matching
  means formatting noise between two systems never masquerades as a missing
  or orphaned record.
- **Currency mismatches** between an order and its settled payment. These
  are never silently converted — there's no FX logic in this engine at all —
  they're flagged as their own category (`CURRENCY_MISMATCH`) so a human
  decides what actually happened.
- **Amount mismatches within the same currency**, at two very different
  scales: a couple of differences of a cent or two (inside the $0.02
  tolerance, so correctly *not* flagged) sitting right next to several real,
  material mismatches — most obviously discount handling: an order recorded
  a discount that the settled charge apparently didn't honor, producing a
  clean, explainable overcharge.
- **Orders marked `completed` whose only payment attempt is `failed` or
  `pending`.** The order status says the sale went through; the processor
  says otherwise. These become `UNSETTLED_PAYMENT` and count as money at
  risk exactly like a missing payment does — there is no settled money to
  point to.
- **Direct status contradictions** — a `cancelled` order with a settled
  charge that was never refunded (money still out the door), and a
  `completed` order whose charge was, in fact, refunded (money already
  back, but the status was never updated). These are the two
  `STATUS_CONTRADICTION` flavors described above, and the sample data
  contained both — which is exactly why the distinction between
  "still-at-risk" and "already-resolved-but-mislabeled" needed to exist as
  a first-class concept rather than being lumped into one bucket.
- **Null `customer_email` and null `processed_at`.** Both are tolerated, not
  treated as validation failures — a missing email doesn't stop an order
  from being reconciled, and a missing processed timestamp doesn't stop a
  payment from being matched. Only fields the engine actually needs to
  compare (amounts, currency, status, the reference itself) are required.
- **Two different date formats between the two systems**: orders use
  `YYYY-MM-DD HH:MM:SS`, payments use `DD/MM/YYYY HH:MM`. Each side is
  parsed with its own format string; nothing about the reconciliation logic
  itself depends on dates lining up, but a naive shared parser would have
  silently misread one side's dates as the wrong day/month.
- **`net_settled = amount − fee` holds at a roughly consistent fee rate**
  across the settled charges — which is exactly why `net_settled` is not
  used for reconciliation at all. The fee is the processor's cut, a real
  and expected cost, not evidence of a discrepancy between what the store
  charged and what came back. Reconciling against the gross charge
  `amount` (not `net_settled`) is what keeps a normal processing fee from
  ever showing up as a false "amount mismatch."

On this sample dataset the run produced a reconciled value of roughly
**$40,203.28** (169 verified-reconciled orders), a total disputed value of
roughly **$2,374.37**, and a money-at-risk figure of roughly **$1,010.85** —
with the gap between the disputed total and the risk figure itself telling
part of the story: several thousand dollars' worth of orders need a human's
attention (currency mismatches, already-resolved status contradictions,
orphan payments), but only a fraction of that is money actually still
exposed today. That gap is the whole reason `money_at_risk` and
`total_disputed_value` are reported as two separate numbers instead of one.

Taken together, the implication for the business is less "the numbers don't
add up" and more "a handful of specific, recurring failure modes account for
nearly all of the discrepancies": duplicate/failed charge attempts that
were never cleaned up, order statuses that don't get updated after a
refund, and a small number of orders where the discount applied at
checkout and the amount actually charged disagree. None of these require
guesswork to fix — each one is a specific order id and a specific,
named reason.

## LLM approach

Once the engine has classified a discrepancy, the backend can optionally
ask Groq to explain it in plain language for a store operator
(`POST /api/discrepancies/{id}/explain`). The explanation is generated once
and cached on the row (`discrepancies.explanation`) — a repeat view of the
same discrepancy never re-spends a Groq call.

- **Model**: `openai/gpt-oss-120b` on Groq (`backend/app/llm/groq_client.py`),
  chosen as a fast, low-cost model well suited to structured JSON output.
- **Prompting**: a fixed system message establishes the role ("You explain
  discrepancies that a deterministic system already found... you never
  decide whether records match"). The user message carries the
  discrepancy's already-fixed fields — type, order id/amount/currency,
  payment reference/amount/currency, the computed difference, and the
  engine's own one-line classification reason as extra grounding context —
  and asks for **strict JSON only**, matching a fixed schema: `summary`,
  `likely_cause`, `recommended_action`, `confidence`.
- **Temperature 0.2, and why**: this is a factual, explanatory task, not a
  creative one. A low temperature favors consistent, grounded explanations
  of the same discrepancy across repeat calls, rather than varied or
  speculative phrasing.
- **Malformed-response handling**: the raw response is parsed and validated
  against a Pydantic schema (`Explanation`). On failure — invalid JSON, a
  schema mismatch, or any call-time failure such as a network or API error
  — the request is retried exactly once with an added "return ONLY valid
  JSON, no prose, no markdown fences" instruction. If that also fails, a
  fixed fallback object is returned (`FALLBACK_EXPLANATION`) instead of
  raising, so a flaky LLM response never turns into a 500 for the route —
  the frontend renders the fallback like any other explanation.
- **The "never decides a match" boundary**: the LLM only narrates a
  classification the deterministic engine already produced. It receives the
  discrepancy's type and fields as fixed input and is explicitly told not
  to re-decide or second-guess them; nothing about which taxonomy bucket a
  discrepancy lands in, or whether it's a discrepancy at all, ever depends
  on an LLM call. The engine (`backend/app/engine/reconcile.py`) is
  enforced to be free of any LLM/HTTP/DB import so this boundary can't
  silently erode.

## What I'd improve with more time

- **Real browser/end-to-end test coverage.** The frontend was verified
  through scripted API-level reproduction of the user flows and a
  controller-run Playwright smoke pass late in the build, but that pass
  isn't wired into CI — there's no automated regression coverage for the
  actual rendered UI (upload flow, dashboard, filters) today, only for the
  backend it talks to.
- **`DUPLICATE_CHARGE` doesn't account for partial refunds.** The check is
  "more than one settled charge, and *no* settled refund at all"
  (`len(settled_charges) > 1 and not settled_refunds`). An order with two
  settled charges and one *partial* refund against only one of them would
  currently fall through to a different (and less accurate) classification
  instead of still being flagged as a duplicate charge with a partial
  offset — the engine only distinguishes "any refund exists" from "no
  refund exists," not how much of the duplicate charge that refund
  actually covers.
- **`STATUS_CONTRADICTION` only models two status values.** It checks
  `cancelled`-with-a-live-charge and `completed`-with-a-refund; any other
  status value the source system might use (e.g. `refunded` as an order's
  own status, not just an inferred state) isn't checked against the
  charge/refund trail at all and falls through to the ordinary
  amount/currency/settlement checks instead. In practice this can be the
  right behavior (an order already correctly marked `refunded` doesn't need
  a contradiction flag), but it means any *other* unexpected status value
  in future source data would silently bypass contradiction detection
  rather than being explicitly handled or rejected.
- **No FX conversion.** `CURRENCY_MISMATCH` is intentionally a dead end
  today — the engine flags the mismatch but makes no attempt to convert and
  compare amounts across currencies, so those orders always need a human
  decision.
- **Pagination/filtering on `/api/discrepancies` covers the common cases
  well (type, text search, amount range) but not everything a larger
  dataset might need** — e.g. filtering by date range, or sorting by
  anything other than the fixed `order_amount desc` order.

## A note on tooling

AI-assisted coding tools were used during the development of this project,
alongside the usual manual engineering process of design, implementation,
testing, and review.
