{{
    config(
        materialized='table',
        cluster_by=['country']
    )
}}

-- Every case where the upstream source restated a value it had already
-- published.
--
-- This model is the reason the raw layer is append-only. Without it a revision
-- is invisible: the number changes and any model trained yesterday was trained
-- on figures that no longer exist anywhere. With it, you can answer "how much
-- does load data move after publication, and how long until it settles?" —
-- which determines how long the pipeline should wait before treating a day as
-- final.
--
-- Note this reads the SOURCE directly rather than stg_load, because staging
-- deliberately collapses to the latest version. The superseded versions are
-- exactly what this model needs.

with versions as (

    select
        country,
        ts_utc,
        value,
        ingested_at,
        case source
            when 'energy_charts.public_power'          then 'actual'
            when 'energy_charts.public_power_forecast' then 'forecast'
            else source
        end as series_type
    from {{ source('raw', 'observations') }}
    where series_id = 'load'

),

with_previous as (

    select
        country,
        ts_utc,
        series_type,
        value,
        ingested_at,
        lag(value)       over w as previous_value,
        lag(ingested_at) over w as previous_ingested_at
    from versions
    window w as (
        partition by country, ts_utc, series_type
        order by ingested_at
    )

)

select
    country,
    ts_utc,
    series_type,

    previous_value      as previous_mw,
    value               as revised_mw,
    value - previous_value as revision_mw,

    safe_divide(value - previous_value, nullif(previous_value, 0)) as revision_pct,

    previous_ingested_at,
    ingested_at         as revised_at,

    -- How long after the observed instant the correction arrived. The 95th
    -- percentile of this is the answer to "how long should the pipeline wait
    -- before treating a day as final?"
    timestamp_diff(ingested_at, ts_utc, hour) as hours_after_observation

from with_previous
where previous_ingested_at is not null
  and (
        (previous_value is null and value is not null)
        or abs(coalesce(value, 0) - coalesce(previous_value, 0)) > 0.001
      )