{{ config(materialized='view') }}

-- Electricity load at source resolution, one row per country, timestamp and
-- series type, latest ingested version only.
--
-- The raw table is append-only: re-running a day appends a second copy rather
-- than overwriting. This model collapses that to the most recent version of
-- each observation. Superseded versions stay in raw and are surfaced by
-- fct_load_revisions.

with raw_observations as (

    select
        country,
        ts_utc,
        ts_local,
        value,
        unit,
        interval_minutes,
        available_until,
        ingested_at,

        -- Collapse the long source strings into something readable. Done here
        -- so that downstream models never handle the raw endpoint names.
        case source
            when 'energy_charts.public_power'          then 'actual'
            when 'energy_charts.public_power_forecast' then 'forecast'
            else source
        end as series_type

    from {{ source('raw', 'observations') }}
    where series_id = 'load'

),

latest_version as (

    select *
    from raw_observations
    qualify row_number() over (
        partition by country, ts_utc, series_type
        order by ingested_at desc
    ) = 1

)

select
    concat(
        country, '|',
        series_type, '|',
        format_timestamp('%Y%m%d%H%M', ts_utc, 'UTC')
    ) as observation_key,

    country,
    ts_utc,
    ts_local,
    series_type,
    value as load_mw,
    interval_minutes,
    available_until,
    ingested_at

from latest_version