import logging
from ingest.energy_charts import fetch_actuals, fetch_load_forecast, to_records

logging.basicConfig(level=logging.INFO)

DAY = "2026-08-25"

actuals = fetch_actuals("de", DAY, DAY)
rows_a = to_records(actuals, "de", "energy_charts.public_power")
print("actual rows:", len(rows_a))
print(rows_a[0])

forecast = fetch_load_forecast("de", DAY, DAY)
rows_f = to_records(forecast, "de", "energy_charts.public_power_forecast")
print("forecast rows:", len(rows_f))
print(rows_f[0])

load_rows = [r for r in rows_a if r["series_id"] == "load"]
print("load rows:", len(load_rows))
print("units seen:", {r["unit"] for r in rows_a})