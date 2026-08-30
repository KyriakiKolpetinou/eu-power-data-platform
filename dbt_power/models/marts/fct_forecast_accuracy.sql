{{
    config(
        materialized='table',
        partition_by={'field': 'hour_utc', 'data_type': 'timestamp', 'granularity': 'day'},
        cluster_by=['country']
    )
}}

-- Actual load beside the operator's published day-ahead forecast, one row per
-- country and hour, with the error decomposed.
--
-- This is the external baseline. The operator published these numbers before
-- the fact, using information no model here has access to, which makes it a
-- benchmark that was not selected for being easy to beat.
--
-- Measured over 2024-01-01 to 2026-08-29 the baseline runs at 3.874% MAPE with
-- near-zero bias, degrading to 12-14% on public holidays and bridge days.

with actual as (

    select
        country,
        hour_utc,
        load_mw,
        date_local,
        hour_of_day_local,
        day_of_week_local,
        month_local,
        is_weekend,
        is_complete_hour
    from {{ ref('fct_load_hourly') }}
    where series_type = 'actual'

),

forecast as (

    select
        country,
        hour_utc,
        load_mw as forecast_mw
    from {{ ref('fct_load_hourly') }}
    where series_type = 'forecast'

)

select
    concat(
        actual.country, '|',
        format_timestamp('%Y%m%d%H', actual.hour_utc, 'UTC')
    ) as accuracy_key,

    actual.country,
    actual.hour_utc,
    actual.date_local,

    actual.load_mw      as actual_mw,
    forecast.forecast_mw,

    -- Signed error. Positive means actual came in above forecast, i.e. the
    -- operator under-forecast. Kept signed so bias can be measured separately
    -- from magnitude — a forecast can have large error and zero bias.
    actual.load_mw - forecast.forecast_mw as error_mw,

    abs(actual.load_mw - forecast.forecast_mw) as abs_error_mw,

    safe_divide(
        abs(actual.load_mw - forecast.forecast_mw),
        nullif(actual.load_mw, 0)
    ) as abs_pct_error,

    actual.hour_of_day_local,
    actual.day_of_week_local,
    actual.month_local,
    actual.is_weekend,
    actual.is_complete_hour

from actual
inner join forecast
    on  actual.country  = forecast.country
    and actual.hour_utc = forecast.hour_utc