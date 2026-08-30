-- The API publishes residual_load as a derived series. Verified by hand that
-- it equals load - solar - wind_onshore - wind_offshore exactly (16411.5 vs
-- 16411.499999999996).
--
-- If that identity stops holding, the upstream definition changed — which is
-- the kind of silent semantic drift no schema test would catch.

{{ config(severity='warn') }}

with pivoted as (

    select
        ts_utc,
        max(if(series_id = 'load',          value, null)) as load_mw,
        max(if(series_id = 'residual_load', value, null)) as residual_mw,
        max(if(series_id = 'solar',         value, null)) as solar_mw,
        max(if(series_id = 'wind_onshore',  value, null)) as wind_on_mw,
        max(if(series_id = 'wind_offshore', value, null)) as wind_off_mw
    from {{ source('raw', 'observations') }}
    where source = 'energy_charts.public_power'
      and series_id in ('load','residual_load','solar','wind_onshore','wind_offshore')
    group by ts_utc

)

select
    ts_utc,
    residual_mw,
    load_mw - solar_mw - wind_on_mw - wind_off_mw as expected_residual_mw,
    residual_mw - (load_mw - solar_mw - wind_on_mw - wind_off_mw) as discrepancy_mw
from pivoted
where load_mw is not null
  and residual_mw is not null
  and abs(residual_mw - (load_mw - solar_mw - wind_on_mw - wind_off_mw)) > 1.0