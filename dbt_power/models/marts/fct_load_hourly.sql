{{
    config(
        materialized='table',
        partition_by={'field': 'hour_utc', 'data_type': 'timestamp', 'granularity': 'day'},
        cluster_by=['country', 'series_type']
    )
}}

-- The modelling grain: one row per country, series type and hour.
--
-- METRIC DEFINITION — the decision that matters here:
--   load_mw is the MEAN of the sub-hourly readings in that hour, not the sum.
--   The source publishes instantaneous power in MW at 15-minute resolution, so
--   summing four readings would report four times the true load. The mean in MW
--   is numerically the energy in MWh over that hour, which is what a capacity
--   planner uses.
--
-- Hours built from fewer readings than the interval implies are KEPT and
-- flagged with is_complete_hour, not dropped. Dropping them would make a
-- publication outage look like a collapse in demand.

with observations as (

    select * from {{ ref('stg_load') }}

),

hourly as (

    select
        country,
        series_type,
        timestamp_trunc(ts_utc, hour)   as hour_utc,
        avg(load_mw)                    as load_mw,
        min(load_mw)                    as load_mw_min,
        max(load_mw)                    as load_mw_max,
        count(load_mw)                  as readings_present,
        max(interval_minutes)           as interval_minutes,
        max(ingested_at)                as ingested_at
    from observations
    group by country, series_type, hour_utc

)

select
    concat(
        country, '|', series_type, '|',
        format_timestamp('%Y%m%d%H', hour_utc, 'UTC')
    ) as load_hour_key,

    country,
    series_type,
    hour_utc,

    load_mw,
    load_mw_min,
    load_mw_max,

    readings_present,
    div(60, interval_minutes)                    as readings_expected,
    readings_present = div(60, interval_minutes) as is_complete_hour,

    -- Calendar features in LOCAL time. Consumption follows local clocks, so a
    -- UTC hour-of-day would smear the evening peak across two hours for half
    -- the year as DST shifts.
    extract(hour      from datetime(hour_utc, 'Europe/Berlin')) as hour_of_day_local,
    extract(dayofweek from datetime(hour_utc, 'Europe/Berlin')) as day_of_week_local,
    extract(month     from datetime(hour_utc, 'Europe/Berlin')) as month_local,
    date(hour_utc, 'Europe/Berlin')                             as date_local,
    extract(dayofweek from datetime(hour_utc, 'Europe/Berlin')) in (1, 7) as is_weekend,

    ingested_at

from hourly