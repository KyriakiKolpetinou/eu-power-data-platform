-- Fails if any hour is absent between the first and last hour held for a
-- country and series type.
--
-- A not_null test cannot catch this. A missing hour is not a null row, it is
-- the ABSENCE of a row — and absences are what break time-series models: the
-- lag features quietly reference the wrong timestamp and nothing errors.

{{ config(severity='warn') }}

with bounds as (

    select
        country,
        series_type,
        min(hour_utc) as first_hour,
        max(hour_utc) as last_hour
    from {{ ref('fct_load_hourly') }}
    group by country, series_type

),

expected as (

    select
        bounds.country,
        bounds.series_type,
        expected_hour
    from bounds,
    unnest(generate_timestamp_array(
        bounds.first_hour, bounds.last_hour, interval 1 hour
    )) as expected_hour

)

select
    expected.country,
    expected.series_type,
    expected.expected_hour as missing_hour
from expected
left join {{ ref('fct_load_hourly') }} as actual
    on  actual.country     = expected.country
    and actual.series_type = expected.series_type
    and actual.hour_utc    = expected.expected_hour
where actual.hour_utc is null