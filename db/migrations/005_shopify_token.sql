-- Shopify Admin API token cache (STORE_SOURCE=shopify). Client-credentials tokens live 24 h (expires_in 86399);
-- the "P8 · Shopify token" sub-workflow reuses the cached one and fetches a new one when it is missing or has less
-- than an hour left. One row per shop. The client secret is never stored here, only in the n8n credential.

create table shopify_token (
    shop       text primary key,             -- e.g. p8-demo-store.myshopify.com
    token      text not null,
    expires_at timestamptz not null,
    fetched_at timestamptz not null default now()
);

-- The cached token if it is still valid for at least p_min_valid, else NULL (= fetch a new one).
create or replace function p8_shopify_token_get(p_shop text, p_min_valid interval default interval '1 hour',
                                                p_now timestamptz default now())
returns text
language sql stable as $$
    select token from shopify_token where shop = p_shop and expires_at - p_min_valid > p_now
$$;

-- Store a freshly fetched token. Returns only its expiry time, so the token does not travel further than needed.
create or replace function p8_shopify_token_put(p_shop text, p_token text, p_expires_in integer,
                                                p_now timestamptz default now())
returns timestamptz
language plpgsql as $$
declare
    v_expires timestamptz;
begin
    if p_token is null or p_token = '' then
        raise exception 'p8_shopify_token_put: empty token';
    end if;
    if p_expires_in is null or p_expires_in <= 0 then
        raise exception 'p8_shopify_token_put: expires_in must be positive, got %', p_expires_in;
    end if;
    v_expires := p_now + make_interval(secs => p_expires_in);
    insert into shopify_token as t (shop, token, expires_at, fetched_at)
    values (p_shop, p_token, v_expires, p_now)
    on conflict (shop) do update set token = excluded.token, expires_at = excluded.expires_at,
                                     fetched_at = excluded.fetched_at;
    return v_expires;
end
$$;
