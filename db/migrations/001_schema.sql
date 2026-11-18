-- P8 schema (synthetic data). Applied by `python -m p8 migrate`.

create table products (
    sku               text primary key,
    name              text not null,
    price             numeric(12, 2) not null check (price > 0),
    stock             integer not null default 0 check (stock >= 0),
    reorder_point     integer not null default 0 check (reorder_point >= 0),
    inventory_item_id bigint unique,
    stock_updated_at  timestamptz,           -- store-side updated_at of the last inventory level applied
    price_updated_at  timestamptz,
    created_at        timestamptz not null default now()
);

create table orders (
    id                 bigint primary key,   -- store order id
    name               text not null,        -- e.g. #1001
    created_at         timestamptz not null,
    updated_at         timestamptz not null, -- store-side updated_at
    cancelled_at       timestamptz,
    financial_status   text,
    fulfillment_status text,
    currency           text not null,
    total_price        numeric(12, 2) not null,
    synced_at          timestamptz not null default now()
);
create index orders_created_at_idx on orders (created_at);

create table order_items (
    id       bigint primary key,             -- store line item id
    order_id bigint not null references orders (id) on delete cascade,
    sku      text not null,                  -- no FK: the store may sell SKUs this table does not know yet
    title    text,
    quantity integer not null check (quantity > 0),
    price    numeric(12, 2) not null
);
create index order_items_order_id_idx on order_items (order_id);
create index order_items_sku_idx on order_items (sku);

-- One row per synced stream ('orders', 'inventory_levels'). The cursor is the (updated_at, id) of the newest row
-- committed; it only moves forward and only inside the same transaction as the rows it covers.
create table sync_state (
    stream            text primary key,
    cursor_updated_at timestamptz,
    cursor_id         bigint,
    last_run_at       timestamptz
);

-- Low-stock alerts: at most one per SKU per (Africa/Cairo) day. sent_at is set after the email went out, so a
-- failed send is retried by the next run the same day instead of being lost.
create table alert_log (
    id            bigserial primary key,
    sku           text not null references products (sku),
    alert_date    date not null,
    stock         integer not null,
    reorder_point integer not null,
    units_14d     integer not null,
    days_left     numeric(10, 1),
    created_at    timestamptz not null default now(),
    sent_at       timestamptz,
    unique (sku, alert_date)
);

-- Supplier price-list imports.
create table import_log (
    id              bigserial primary key,
    source_file     text not null,
    status          text not null check (status in ('validated', 'applied', 'rejected', 'failed')),
    file_error      text,                    -- whole-file problem (e.g. a missing required column)
    rows_total      integer not null default 0,
    rows_valid      integer not null default 0,
    rows_rejected   integer not null default 0,
    rows_review     integer not null default 0,
    rows_changed    integer not null default 0,
    rows_unchanged  integer not null default 0,
    ai_enabled      boolean not null default false,
    created_at      timestamptz not null default now(),
    applied_at      timestamptz
);

-- Every row of every imported file with its validation outcome (rejects file and review list come from here).
create table import_rows (
    import_id     bigint not null references import_log (id) on delete cascade,
    row_no        integer not null,          -- spreadsheet row number (header = row 1)
    raw           jsonb not null,            -- the row as uploaded
    sku_in        text,
    name_in       text,
    price_in      text,
    sku           text,                      -- resolved SKU
    price         numeric(12, 2),
    old_price     numeric(12, 2),
    match_method  text check (match_method in ('sku', 'name', 'ai')),
    status        text not null check (status in ('valid', 'rejected', 'review', 'pending_ai')),
    reason        text,
    ai_sku        text,
    ai_confidence numeric(4, 3),
    primary key (import_id, row_no)
);

create table price_changes (
    id         bigserial primary key,
    import_id  bigint not null references import_log (id),
    sku        text not null references products (sku),
    old_price  numeric(12, 2) not null,
    new_price  numeric(12, 2) not null,
    changed_at timestamptz not null default now()
);
create index price_changes_sku_idx on price_changes (sku);
