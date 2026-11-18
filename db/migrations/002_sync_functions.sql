-- Orders + inventory sync. Each page is applied by one function call = one transaction: the rows and the cursor
-- that covers them commit together, so a failed page leaves the cursor where it was.

-- Where the next run should start: the cursor minus an overlap window (store-side clocks and same-second updates).
-- NULL means "full sync".
create or replace function p8_sync_since(p_stream text, p_overlap interval default interval '5 minutes')
returns timestamptz
language sql stable as $$
    select cursor_updated_at - p_overlap from sync_state where stream = p_stream
$$;

create or replace function p8_advance_cursor(p_stream text, p_ts timestamptz, p_id bigint)
returns void
language sql as $$
    insert into sync_state as s (stream, cursor_updated_at, cursor_id, last_run_at)
    values (p_stream, p_ts, p_id, now())
    on conflict (stream) do update set
        cursor_updated_at = case when s.cursor_updated_at is null
                                   or (excluded.cursor_updated_at, excluded.cursor_id) > (s.cursor_updated_at, s.cursor_id)
                                 then excluded.cursor_updated_at else s.cursor_updated_at end,
        cursor_id         = case when s.cursor_updated_at is null
                                   or (excluded.cursor_updated_at, excluded.cursor_id) > (s.cursor_updated_at, s.cursor_id)
                                 then excluded.cursor_id else s.cursor_id end,
        last_run_at = now()  -- an empty page (p_ts NULL) only records the run time
$$;

-- p = {"orders": [<store order JSON>, ...]}
create or replace function p8_sync_orders_page(p jsonb)
returns jsonb
language plpgsql as $$
declare
    v_orders   jsonb := coalesce(p -> 'orders', '[]'::jsonb);
    v_changed  bigint[];
    v_items    integer;
    v_ts       timestamptz;
    v_id       bigint;
begin
    with src as (
        select * from jsonb_to_recordset(v_orders) as x(
            id bigint, name text, created_at timestamptz, updated_at timestamptz, cancelled_at timestamptz,
            financial_status text, fulfillment_status text, currency text, total_price numeric)
    ), up as (
        insert into orders as o (id, name, created_at, updated_at, cancelled_at, financial_status,
                                 fulfillment_status, currency, total_price)
        select id, name, created_at, updated_at, cancelled_at, financial_status, fulfillment_status, currency,
               total_price
        from src
        on conflict (id) do update set
            name = excluded.name, created_at = excluded.created_at, updated_at = excluded.updated_at,
            cancelled_at = excluded.cancelled_at, financial_status = excluded.financial_status,
            fulfillment_status = excluded.fulfillment_status, currency = excluded.currency,
            total_price = excluded.total_price, synced_at = now()
        -- never go back to an older version; skip identical re-deliveries (overlap window, re-runs)
        where excluded.updated_at >= o.updated_at
          and (o.name, o.created_at, o.updated_at, o.cancelled_at, o.financial_status, o.fulfillment_status,
               o.currency, o.total_price)
              is distinct from
              (excluded.name, excluded.created_at, excluded.updated_at, excluded.cancelled_at,
               excluded.financial_status, excluded.fulfillment_status, excluded.currency, excluded.total_price)
        returning o.id
    )
    select coalesce(array_agg(id), '{}') into v_changed from up;

    -- line items of changed orders: drop removed lines, upsert the rest
    delete from order_items oi
    where oi.order_id = any (v_changed)
      and not exists (
          select 1 from jsonb_array_elements(v_orders) o, jsonb_array_elements(o -> 'line_items') li
          where (li ->> 'id')::bigint = oi.id);

    insert into order_items as t (id, order_id, sku, title, quantity, price)
    select (li ->> 'id')::bigint, (o ->> 'id')::bigint, li ->> 'sku', li ->> 'title',
           (li ->> 'quantity')::int, (li ->> 'price')::numeric
    from jsonb_array_elements(v_orders) o, jsonb_array_elements(o -> 'line_items') li
    where (o ->> 'id')::bigint = any (v_changed)
    on conflict (id) do update set
        order_id = excluded.order_id, sku = excluded.sku, title = excluded.title,
        quantity = excluded.quantity, price = excluded.price;
    get diagnostics v_items = row_count;

    select (x ->> 'updated_at')::timestamptz, (x ->> 'id')::bigint into v_ts, v_id
    from jsonb_array_elements(v_orders) x
    order by 1 desc, 2 desc
    limit 1;
    perform p8_advance_cursor('orders', v_ts, v_id);

    return jsonb_build_object(
        'stream', 'orders',
        'received', jsonb_array_length(v_orders),
        'changed', cardinality(v_changed),
        'items_written', v_items,
        'cursor', (select to_jsonb(s) - 'stream' from sync_state s where stream = 'orders'));
end
$$;

-- p = {"inventory_levels": [{"inventory_item_id", "sku", "available", "updated_at"}, ...]}
create or replace function p8_sync_inventory_page(p jsonb)
returns jsonb
language plpgsql as $$
declare
    v_levels  jsonb := coalesce(p -> 'inventory_levels', '[]'::jsonb);
    v_changed integer;
    v_unknown integer;
    v_ts      timestamptz;
    v_id      bigint;
begin
    with src as (
        select * from jsonb_to_recordset(v_levels) as x(
            inventory_item_id bigint, sku text, available integer, updated_at timestamptz)
    ), up as (
        update products pr
        set stock = greatest(0, s.available), stock_updated_at = s.updated_at
        from src s
        where pr.inventory_item_id = s.inventory_item_id
          and (pr.stock_updated_at is null or s.updated_at >= pr.stock_updated_at)
          and (pr.stock, pr.stock_updated_at) is distinct from (greatest(0, s.available), s.updated_at)
        returning pr.sku
    )
    select count(*) into v_changed from up;

    select count(*) into v_unknown
    from jsonb_array_elements(v_levels) x
    where not exists (select 1 from products pr where pr.inventory_item_id = (x ->> 'inventory_item_id')::bigint);

    select (x ->> 'updated_at')::timestamptz, (x ->> 'inventory_item_id')::bigint into v_ts, v_id
    from jsonb_array_elements(v_levels) x
    order by 1 desc, 2 desc
    limit 1;
    perform p8_advance_cursor('inventory_levels', v_ts, v_id);

    return jsonb_build_object(
        'stream', 'inventory_levels',
        'received', jsonb_array_length(v_levels),
        'changed', v_changed,
        'unknown_items', v_unknown,
        'cursor', (select to_jsonb(s) - 'stream' from sync_state s where stream = 'inventory_levels'));
end
$$;

-- Rows for the daily Excel export: every order line created on p_day (Africa/Cairo). Times are Cairo local time as
-- text and amounts are float8 so the spreadsheet shows them as written (node-postgres turns numeric into strings).
create or replace function p8_orders_export(p_day date)
returns table (order_name text, order_id text, created_at_cairo text, financial_status text,
               fulfillment_status text, cancelled boolean, sku text, title text, quantity integer,
               unit_price float8, line_total float8, currency text)
language sql stable as $$
    select o.name, o.id::text, to_char(o.created_at at time zone 'Africa/Cairo', 'YYYY-MM-DD HH24:MI:SS'),
           o.financial_status, o.fulfillment_status, o.cancelled_at is not null, oi.sku, oi.title, oi.quantity,
           oi.price::float8, (oi.quantity * oi.price)::float8, o.currency
    from orders o join order_items oi on oi.order_id = o.id
    where (o.created_at at time zone 'Africa/Cairo')::date = p_day
    order by o.created_at, o.id, oi.id
$$;
