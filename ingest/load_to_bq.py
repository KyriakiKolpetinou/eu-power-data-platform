"""Land energy-charts observations in BigQuery.

    python -m ingest.load_to_bq --start 2026-08-25 --end 2026-08-25 --dry-run
    python -m ingest.load_to_bq --start 2026-08-25 --end 2026-08-25

The raw table is APPEND-ONLY. Re-running the same day writes a second copy of
those rows with a later ingested_at. That is deliberate:

  - Load figures settle after publication (we watched the last hour of
    2026-08-25 fill in over five days). Append-only means the correction is
    visible rather than silently overwriting the earlier value.
  - Reruns after a failure are safe; there is no partial-delete window.
  - De-duplication to "latest version wins" happens once, in dbt staging,
    where it is visible and testable.

The cost is duplicate storage, which at this volume is megabytes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from google.cloud import bigquery

from ingest.energy_charts import fetch_actuals, fetch_load_forecast, to_records

log = logging.getLogger("load_to_bq")

PROJECT = os.environ.get("GCP_PROJECT", "power-platform-kk")
DATASET = "power_raw"
TABLE = "observations"
LOCATION = "EU"

SCHEMA = [
    bigquery.SchemaField("country", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("bidding_zone", "STRING"),
    bigquery.SchemaField("ts_utc", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("ts_local", "STRING"),
    bigquery.SchemaField("series_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("series_name", "STRING"),
    bigquery.SchemaField("value", "FLOAT64"),
    bigquery.SchemaField("unit", "STRING"),
    bigquery.SchemaField("interval_minutes", "INT64"),
    bigquery.SchemaField("source", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("source_url", "STRING"),
    bigquery.SchemaField("schema_version", "STRING"),
    bigquery.SchemaField("available_until", "STRING"),
    bigquery.SchemaField("generated_at", "STRING"),
    bigquery.SchemaField("payload_sha256", "STRING"),
    bigquery.SchemaField("ingested_at", "TIMESTAMP", mode="REQUIRED"),
]


def ensure_table(client: bigquery.Client) -> None:
    table = bigquery.Table(f"{PROJECT}.{DATASET}.{TABLE}", schema=SCHEMA)
    # Partition on UTC date. Note this splits a local day across two partitions
    # (local midnight is 22:00 or 23:00 UTC depending on DST) — a deliberate
    # trade: UTC is the stable join key, and date_local is derived in dbt.
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field="ts_utc"
    )
    table.clustering_fields = ["country", "series_id", "source"]
    client.create_table(table, exists_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", default="de")
    parser.add_argument("--start", required=True, help="ISO date")
    parser.add_argument("--end", required=True, help="ISO date, INCLUSIVE")
    parser.add_argument("--dry-run", action="store_true",
                        help="Write data/sample.json instead. Touches no cloud resources.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    rows: list[dict] = []

    actuals = fetch_actuals(args.country, args.start, args.end)
    rows += to_records(actuals, args.country, "energy_charts.public_power")

    forecast = fetch_load_forecast(args.country, args.start, args.end)
    rows += to_records(forecast, args.country, "energy_charts.public_power_forecast")

    log.info("prepared %s rows", len(rows))

    if not rows:
        log.error("no rows returned — refusing to continue")
        return 1

    if args.dry_run:
        os.makedirs("data", exist_ok=True)
        with open("data/sample.json", "w") as fh:
            json.dump(rows[:200], fh, indent=2)
        log.info("wrote data/sample.json (first 200 rows). Go read it.")
        return 0

    client = bigquery.Client(project=PROJECT, location=LOCATION)
    ensure_table(client)

    job = client.load_table_from_json(
        rows,
        f"{PROJECT}.{DATASET}.{TABLE}",
        job_config=bigquery.LoadJobConfig(
            schema=SCHEMA,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        ),
    )
    job.result()
    log.info("appended %s rows to %s.%s.%s", len(rows), PROJECT, DATASET, TABLE)
    return 0


if __name__ == "__main__":
    sys.exit(main())