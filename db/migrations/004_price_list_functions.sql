-- Supplier price list: validate -> (optional AI name matching -> review list) -> apply in one transaction.
-- Rules match by SKU, then by product name (exact after normalising, then same words in any order).
-- AI suggestions are NEVER applied: they go to the review list with their confidence.

-- Header aliases -> canonical column (sku | name | price). Unknown headers are ignored.
create or replace function p8_norm_key(k text)
returns text
language sql immutable as $$
    select case trim(regexp_replace(lower(k), '[^a-z0-9]+', ' ', 'g'))
        when 'sku' then 'sku' when 'item code' then 'sku' when 'product code' then 'sku' when 'code' then 'sku'
        when 'name' then 'name' when 'product' then 'name' when 'product name' then 'name'
        when 'item' then 'name' when 'item name' then 'name' when 'description' then 'name'
        when 'price' then 'price' when 'unit price' then 'price' when 'new price' then 'price'
        when 'price egp' then 'price' when 'unit price egp' then 'price'
    end
$$;

create or replace function p8_get(r jsonb, canon text)
returns text
language sql immutable as $$
    select nullif(trim(r ->> k), '') from jsonb_object_keys(r) k where p8_norm_key(k) = canon limit 1
$$;

create or replace function p8_norm_name(t text)
returns text
language sql immutable as $$
    select trim(regexp_replace(lower(coalesce(t, '')), '[^a-z0-9]+', ' ', 'g'))
$$;

create or replace function p8_name_tokens(t text)
returns text
language sql immutable as $$
    select string_agg(w, ' ' order by w) from regexp_split_to_table(p8_norm_name(t), ' ') w where w <> ''
$$;

-- '1,250.50', 'EGP 99', ' 12.5 ' -> numeric; anything else -> NULL. Comma decimals ('12,5') are not numbers.
create or replace function p8_parse_price(t text)
returns numeric
language sql immutable as $$
    with c as (select regexp_replace(upper(coalesce(t, '')), '(EGP|LE|E£|\s)', '', 'g') as v)
    select case
        when v ~ '^-?\d+(\.\d+)?$' then v::numeric
        when v ~ '^-?\d{1,3}(,\d{3})+(\.\d+)?$' then replace(v, ',', '')::numeric
    end
    from c
$$;

create or replace function p8_import_recount(p_import bigint)
returns void
language sql as $$
    update import_log l set
        rows_total    = c.total,
        rows_valid    = c.valid,
        rows_rejected = c.rejected,
        rows_review   = c.review,
        rows_changed  = case when l.status = 'applied' then l.rows_changed else c.changed end,
        rows_unchanged = case when l.status = 'applied' then l.rows_unchanged else c.valid - c.changed end
    from (
        select count(*)::int as total,
               count(*) filter (where status = 'valid')::int as valid,
               count(*) filter (where status = 'rejected')::int as rejected,
               count(*) filter (where status = 'review')::int as review,
               count(*) filter (where status = 'valid' and price is distinct from old_price)::int as changed
        from import_rows where import_id = p_import
    ) c
    where l.id = p_import
$$;

-- Validates an uploaded file (rows = JSON objects keyed by the file's headers). Writes import_log + import_rows.
-- Returns the report; rows the rules could not match are 'pending_ai' when AI matching is on.
create or replace function p8_validate_price_list(p_file text, p_rows jsonb, p_ai_enabled boolean default false)
returns jsonb
language plpgsql as $$
declare
    v_import  bigint;
    v_keys    text[];
    v_error   text;
begin
    if p_rows is null or jsonb_typeof(p_rows) <> 'array' or jsonb_array_length(p_rows) = 0 then
        v_error := 'the file has no data rows';
    else
        select coalesce(array_agg(distinct p8_norm_key(k)) filter (where p8_norm_key(k) is not null), '{}')
        into v_keys
        from jsonb_array_elements(p_rows) r, jsonb_object_keys(r) k;
        if not 'price' = any (v_keys) then
            v_error := 'missing required column: price';
        elsif not ('sku' = any (v_keys) or 'name' = any (v_keys)) then
            v_error := 'missing required column: sku or name (at least one is needed)';
        end if;
    end if;

    insert into import_log (source_file, status, file_error, ai_enabled)
    values (coalesce(p_file, 'upload'), case when v_error is null then 'validated' else 'rejected' end, v_error,
            p_ai_enabled)
    returning id into v_import;

    if v_error is not null then
        return p8_price_list_report(v_import);
    end if;

    insert into import_rows (import_id, row_no, raw, sku_in, name_in, price_in, status)
    select v_import, (t.ord + 1)::int, t.r, upper(p8_get(t.r, 'sku')), p8_get(t.r, 'name'), p8_get(t.r, 'price'),
           'valid'
    from jsonb_array_elements(p_rows) with ordinality as t(r, ord);

    -- price
    update import_rows set status = 'rejected', reason = 'price is missing'
    where import_id = v_import and price_in is null;
    update import_rows set status = 'rejected', reason = format('price is not a number (%s)', price_in)
    where import_id = v_import and price_in is not null and p8_parse_price(price_in) is null;
    update import_rows set status = 'rejected', reason = format('price must be greater than 0 (%s)', price_in)
    where import_id = v_import and p8_parse_price(price_in) <= 0;
    update import_rows set price = round(p8_parse_price(price_in), 2)
    where import_id = v_import and status = 'valid';

    -- SKU, by rule
    update import_rows r set sku = r.sku_in, match_method = 'sku'
    from products p
    where r.import_id = v_import and r.sku_in = p.sku;

    update import_rows r
    set status = 'rejected', reason = concat_ws('; ', r.reason, format('unknown SKU %s', r.sku_in))
    where r.import_id = v_import and r.sku_in is not null and r.sku is null;

    update import_rows r
    set status = 'rejected', reason = concat_ws('; ', r.reason, 'SKU and name are both empty')
    where r.import_id = v_import and r.sku_in is null and r.name_in is null;

    -- name rules: exact normalised name, then the same words in any order; must point to exactly one product
    update import_rows r set sku = m.sku, match_method = 'name'
    from (
        select r2.row_no, min(p.sku) as sku
        from import_rows r2
        join products p on p8_norm_name(p.name) = p8_norm_name(r2.name_in)
        where r2.import_id = v_import and r2.sku_in is null and r2.name_in is not null
        group by r2.row_no
        having count(*) = 1
    ) m
    where r.import_id = v_import and r.row_no = m.row_no;

    update import_rows r set sku = m.sku, match_method = 'name'
    from (
        select r2.row_no, min(p.sku) as sku
        from import_rows r2
        join products p on p8_name_tokens(p.name) = p8_name_tokens(r2.name_in)
        where r2.import_id = v_import and r2.sku_in is null and r2.name_in is not null and r2.sku is null
        group by r2.row_no
        having count(*) = 1
    ) m
    where r.import_id = v_import and r.row_no = m.row_no;

    update import_rows r
    set status = case when p_ai_enabled and r.status = 'valid' then 'pending_ai' else 'rejected' end,
        reason = case when p_ai_enabled and r.status = 'valid' then null
                      else concat_ws('; ', r.reason, 'no SKU and the name matches no product') end
    where r.import_id = v_import and r.sku_in is null and r.name_in is not null and r.sku is null;

    -- duplicate SKUs in the file: every copy is rejected (we cannot know which price is right)
    update import_rows r
    set status = 'rejected',
        reason = concat_ws('; ', r.reason, format('duplicate SKU %s in the file (rows %s)', r.sku, d.rows_list))
    from (
        select sku, string_agg(row_no::text, ', ' order by row_no) as rows_list
        from import_rows where import_id = v_import and sku is not null
        group by sku having count(*) > 1
    ) d
    where r.import_id = v_import and r.sku = d.sku;

    -- diff against current prices
    update import_rows r set old_price = p.price
    from products p
    where r.import_id = v_import and r.sku = p.sku;

    perform p8_import_recount(v_import);
    return p8_price_list_report(v_import);
end
$$;

-- p_matches = [{"row_no": 7, "sku": "SKU-1003", "confidence": 0.93}, ...] for the 'pending_ai' rows.
-- At/above the threshold -> 'review' (suggestion only, never applied). Below, unknown SKU or missing -> rejected.
create or replace function p8_record_ai_matches(p_import bigint, p_matches jsonb, p_threshold numeric,
                                                p_error text default null)
returns jsonb
language plpgsql as $$
begin
    with m as (
        select distinct on (x.row_no) x.row_no, upper(x.sku) as sku, x.confidence
        from jsonb_to_recordset(coalesce(p_matches, '[]'::jsonb)) as x(row_no int, sku text, confidence numeric)
        order by x.row_no, x.confidence desc nulls last
    )
    update import_rows r
    set ai_sku = case when p.sku is not null then m.sku end,
        ai_confidence = case when p.sku is not null then least(greatest(m.confidence, 0), 1) end,
        status = case when p.sku is not null and m.confidence >= p_threshold then 'review' else 'rejected' end,
        match_method = case when p.sku is not null and m.confidence >= p_threshold then 'ai' end,
        reason = case
            when p.sku is not null and m.confidence >= p_threshold
                then format('AI suggestion %s (confidence %s), needs review: not applied', m.sku,
                            round(m.confidence, 2))
            when p.sku is not null
                then format('no SKU and the name matches no product (AI best guess %s at %s, below %s)', m.sku,
                            round(m.confidence, 2), p_threshold)
            when p_error is not null
                then format('no SKU and the name matches no product (AI matching failed: %s)', left(p_error, 200))
            else 'no SKU and the name matches no product (AI found no match)'
        end
    from import_rows r0
    left join m on m.row_no = r0.row_no
    left join products p on p.sku = m.sku
    where r0.import_id = p_import and r0.status = 'pending_ai'
      and r.import_id = r0.import_id and r.row_no = r0.row_no;

    perform p8_import_recount(p_import);
    return p8_price_list_report(p_import);
end
$$;

-- Applies every valid row of a validated import in ONE transaction (this function call) and logs each change.
-- Idempotent: an import that is not 'validated' any more is returned unchanged.
create or replace function p8_apply_price_list(p_import bigint)
returns jsonb
language plpgsql as $$
declare
    v_status  text;
    v_changed integer;
    v_valid   integer;
begin
    select status into v_status from import_log where id = p_import for update;
    if v_status is null then
        raise exception 'price import % not found', p_import;
    end if;
    if v_status <> 'validated' then
        return p8_price_list_report(p_import);
    end if;
    if exists (select 1 from import_rows where import_id = p_import and status = 'pending_ai') then
        raise exception 'price import % still has rows waiting for AI matching', p_import;
    end if;

    perform 1 from products p join import_rows r on r.sku = p.sku
    where r.import_id = p_import and r.status = 'valid'
    for update of p;

    insert into price_changes (import_id, sku, old_price, new_price)
    select p_import, p.sku, p.price, r.price
    from products p join import_rows r on r.sku = p.sku
    where r.import_id = p_import and r.status = 'valid' and p.price <> r.price;
    get diagnostics v_changed = row_count;

    update products p set price = r.price, price_updated_at = now()
    from import_rows r
    where r.import_id = p_import and r.status = 'valid' and r.sku = p.sku and p.price <> r.price;

    select count(*) into v_valid from import_rows where import_id = p_import and status = 'valid';
    update import_log
    set status = case when v_valid > 0 then 'applied' else 'rejected' end,
        rows_changed = v_changed, rows_unchanged = v_valid - v_changed, applied_at = now()
    where id = p_import;

    perform p8_import_recount(p_import);
    return p8_price_list_report(p_import);
end
$$;

create or replace function p8_price_list_report(p_import bigint)
returns jsonb
language sql stable as $$
    select jsonb_build_object(
        'import_id', l.id,
        'source_file', l.source_file,
        'status', l.status,
        'file_error', l.file_error,
        'ai_enabled', l.ai_enabled,
        'rows_total', l.rows_total, 'rows_valid', l.rows_valid, 'rows_rejected', l.rows_rejected,
        'rows_review', l.rows_review, 'rows_changed', l.rows_changed, 'rows_unchanged', l.rows_unchanged,
        'rows_pending_ai', (select count(*) from import_rows where import_id = l.id and status = 'pending_ai'),
        'changes', coalesce((
            select jsonb_agg(jsonb_build_object(
                       'row_no', r.row_no, 'sku', r.sku, 'name', p.name, 'old_price', r.old_price,
                       'new_price', r.price,
                       'pct', round(100 * (r.price - r.old_price) / nullif(r.old_price, 0), 1),
                       'match_method', r.match_method)
                   order by abs(r.price - r.old_price) / nullif(r.old_price, 0) desc nulls last, r.sku)
            from import_rows r join products p on p.sku = r.sku
            where r.import_id = l.id and r.status = 'valid' and r.price is distinct from r.old_price), '[]'),
        'rejects', coalesce((
            select jsonb_agg(jsonb_build_object(
                       'row_no', r.row_no, 'sku', r.sku_in, 'name', r.name_in, 'price', r.price_in,
                       'reason', r.reason) order by r.row_no)
            from import_rows r where r.import_id = l.id and r.status = 'rejected'), '[]'),
        'review', coalesce((
            select jsonb_agg(jsonb_build_object(
                       'row_no', r.row_no, 'name', r.name_in, 'price', r.price_in, 'suggested_sku', r.ai_sku,
                       'suggested_name', p.name, 'confidence', r.ai_confidence, 'current_price', p.price)
                   order by r.row_no)
            from import_rows r left join products p on p.sku = r.ai_sku
            where r.import_id = l.id and r.status = 'review'), '[]'),
        'pending_ai', coalesce((
            select jsonb_agg(jsonb_build_object('row_no', r.row_no, 'name', r.name_in, 'price', r.price_in)
                   order by r.row_no)
            from import_rows r where r.import_id = l.id and r.status = 'pending_ai'), '[]')
    )
    from import_log l where l.id = p_import
$$;
