create table if not exists orders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  order_id text not null,
  order_id_norm text not null,
  order_date timestamptz,
  customer_email text,
  currency text not null,
  gross_amount numeric(12,2) not null,
  discount numeric(12,2) not null default 0,
  net_amount numeric(12,2) not null,
  status text not null,
  upload_batch_id uuid not null,
  created_at timestamptz not null default now()
);
create index if not exists idx_orders_user_norm on orders (user_id, order_id_norm);

create table if not exists payments (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  transaction_ref text not null,
  processed_at timestamptz,
  order_reference text not null,
  order_reference_norm text not null,
  currency text not null,
  amount numeric(12,2) not null,
  fee numeric(12,2) not null default 0,
  net_settled numeric(12,2),
  type text not null,
  status text not null,
  upload_batch_id uuid not null,
  created_at timestamptz not null default now()
);
create index if not exists idx_payments_user_norm on payments (user_id, order_reference_norm);

create table if not exists reconciliation_runs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  created_at timestamptz not null default now(),
  orders_count int not null,
  payments_count int not null,
  total_reconciled_value numeric(14,2) not null,
  total_disputed_value numeric(14,2) not null,
  money_at_risk numeric(14,2) not null,
  status text not null default 'complete'
);

create table if not exists discrepancies (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references reconciliation_runs(id) on delete cascade,
  user_id uuid not null,
  type text not null,
  order_id text,
  payment_ref text,
  order_amount numeric(12,2),
  payment_amount numeric(12,2),
  currency_order text,
  currency_payment text,
  difference numeric(12,2),
  detail jsonb,
  explanation jsonb,
  explained_at timestamptz
);
create index if not exists idx_discrepancies_run on discrepancies (run_id);
create index if not exists idx_discrepancies_user_type on discrepancies (user_id, type);
