-- Low-stock alert. Days of stock left = stock / (units sold in the last 14 days / 14).
-- SKUs with no sales in the window have days_left NULL ("no recent sales") and sort last.

create or replace function p8_low_stock(p_now timestamptz default now())
returns table (sku text, name text, stock integer, reorder_point integer, units_14d integer,
               avg_daily numeric, days_left numeric)
language sql stable as $$
    with sales as (
        select oi.sku, sum(oi.quantity)::int as units
        from order_items oi
        join orders o on o.id = oi.order_id
        where o.cancelled_at is null
          and o.created_at >= p_now - interval '14 days'
          and o.created_at < p_now
        group by oi.sku
    )
    select p.sku, p.name, p.stock, p.reorder_point, coalesce(s.units, 0),
           round(coalesce(s.units, 0) / 14.0, 2),
           case when coalesce(s.units, 0) = 0 then null else round(p.stock / (s.units / 14.0), 1) end
    from products p
    left join sales s on s.sku = p.sku
    where p.stock < p.reorder_point
    order by 7 asc nulls last, 1
$$;

-- Claims today's alerts (one row per SKU per Africa/Cairo day) and returns the ones not sent yet:
-- new SKUs plus any claimed earlier today whose email failed. The workflow marks them sent after the email.
create or replace function p8_claim_low_stock_alerts(p_now timestamptz default now())
returns table (alert_id bigint, sku text, name text, stock integer, reorder_point integer, units_14d integer,
               avg_daily numeric, days_left numeric, alert_date date)
language plpgsql as $$
#variable_conflict use_column
declare
    d date := (p_now at time zone 'Africa/Cairo')::date;
begin
    insert into alert_log (sku, alert_date, stock, reorder_point, units_14d, days_left)
    select l.sku, d, l.stock, l.reorder_point, l.units_14d, l.days_left
    from p8_low_stock(p_now) l
    on conflict (sku, alert_date) do nothing;

    return query
    select a.id, a.sku, p.name, a.stock, a.reorder_point, a.units_14d, round(a.units_14d / 14.0, 2),
           a.days_left, a.alert_date
    from alert_log a
    join products p on p.sku = a.sku
    where a.alert_date = d and a.sent_at is null
    order by a.days_left asc nulls last, a.sku;
end
$$;

create or replace function p8_mark_alerts_sent(p_ids bigint[])
returns integer
language sql as $$
    with u as (update alert_log set sent_at = now() where id = any (p_ids) and sent_at is null returning 1)
    select count(*)::int from u
$$;
