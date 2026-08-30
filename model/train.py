"""Backtest a load forecasting model against the operator's published forecast.

    python -m model.train

Reads fct_load_hourly from BigQuery, builds calendar and lag features, and runs
a walk-forward backtest: for each month, train on everything strictly before it
and predict that month. The model never sees the period it predicts.

Two decisions worth reading before trusting the numbers:

  1. lag_1d (yesterday's actual load at the same hour) is EXCLUDED by default.
     A day-ahead forecast is published before the previous day's load has
     settled, so using it would give this model information the operator did
     not have. Including it improves MAE from 1,980 to 1,647 MW — roughly 80%
     of the apparent advantage over the operator. Run with --allow-leakage to
     reproduce that figure and see the size of the effect.

  2. The first WARMUP_MONTHS of the backtest are reported separately. Before
     the model has seen a full seasonal cycle its error is several times worse
     (7,150 MW in the first month), which drags the full-period average. The
     exclusion is stated rather than applied silently.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import holidays
import lightgbm as lgb
import numpy as np
import pandas as pd
from dateutil.easter import easter
from google.cloud import bigquery

log = logging.getLogger("train")

PROJECT = "power-platform-kk"
MARTS_DATASET = "power_dev_marts"

MIN_TRAIN_MONTHS = 12   # months of history before the first prediction
WARMUP_MONTHS = 2       # backtest months reported separately as cold-start

TARGET = "actual_mw"

BASE_FEATURES = [
    "hour_of_day_local", "day_of_week_local", "month_local",
    "is_weekend", "is_holiday", "is_bridge_day",
    "is_easter_period", "is_christmas_period", "is_special_day",
    "lag_2d", "lag_7d", "lag_14d", "lag_7d_mean",
]
LEAKY_FEATURE = "lag_1d"

MODEL_PARAMS = dict(
    n_estimators=400,
    learning_rate=0.05,
    num_leaves=31,
    random_state=42,
    verbose=-1,
)


def load_hourly(country: str = "de") -> pd.DataFrame:
    """Pull the hourly mart and pivot actuals and forecast into columns."""
    query = f"""
        select
            hour_utc, date_local, series_type, load_mw,
            hour_of_day_local, day_of_week_local, month_local, is_weekend
        from `{PROJECT}.{MARTS_DATASET}.fct_load_hourly`
        where country = '{country}'
        order by hour_utc
    """
    client = bigquery.Client(project=PROJECT)
    raw = client.query(query).to_dataframe()

    wide = raw.pivot_table(
        index=["hour_utc", "date_local", "hour_of_day_local",
               "day_of_week_local", "month_local", "is_weekend"],
        columns="series_type",
        values="load_mw",
    ).reset_index()

    wide.columns.name = None
    wide = wide.rename(columns={"actual": "actual_mw", "forecast": "tso_forecast_mw"})
    return wide.sort_values("hour_utc").reset_index(drop=True)


def add_calendar_features(df: pd.DataFrame) -> pd.DataFrame:
    """Flag days on which consumption departs from its usual pattern.

    Note this is deliberately broader than 'public holiday'. Easter Sunday is
    not a statutory holiday — Sunday is already a non-working day — but it was
    the second-worst day in the whole dataset for the operator's forecast.
    What matters here is unusual consumption, not employment law.
    """
    out = df.copy()
    dates = pd.to_datetime(out["date_local"])
    years = range(dates.dt.year.min(), dates.dt.year.max() + 1)

    de_holidays = holidays.Germany(years=years)
    out["is_holiday"] = out["date_local"].isin(de_holidays).astype(int)

    prev_day = (dates - pd.Timedelta(days=1)).dt.date
    next_day = (dates + pd.Timedelta(days=1)).dt.date
    out["is_bridge_day"] = (
        (out["is_holiday"] == 0)
        & (~out["is_weekend"])
        & (
            (prev_day.isin(de_holidays) & (dates.dt.dayofweek == 4))
            | (next_day.isin(de_holidays) & (dates.dt.dayofweek == 0))
        )
    ).astype(int)

    easter_dates: set = set()
    for y in years:
        e = easter(y)
        easter_dates.update({e, e - pd.Timedelta(days=2).to_pytimedelta(),
                             e + pd.Timedelta(days=1).to_pytimedelta()})
    out["is_easter_period"] = out["date_local"].isin(easter_dates).astype(int)

    out["is_christmas_period"] = (
        ((dates.dt.month == 12) & (dates.dt.day >= 24))
        | ((dates.dt.month == 1) & (dates.dt.day <= 1))
    ).astype(int)

    out["is_special_day"] = (
        out[["is_holiday", "is_bridge_day",
             "is_easter_period", "is_christmas_period"]].max(axis=1)
    )
    out["is_weekend"] = out["is_weekend"].astype(int)
    return out


def add_lag_features(df: pd.DataFrame) -> pd.DataFrame:
    """Load at the same hour on previous days. 24 rows back = 24 hours."""
    out = df.sort_values("hour_utc").reset_index(drop=True)
    for days in (1, 2, 7, 14):
        out[f"lag_{days}d"] = out["actual_mw"].shift(24 * days)
    out["lag_7d_mean"] = (
        out["actual_mw"].shift(24).rolling(24 * 7, min_periods=24 * 7).mean()
    )

    before = len(out)
    out = out.dropna(subset=[f"lag_{d}d" for d in (1, 2, 7, 14)] + ["lag_7d_mean"])
    log.info("dropped %s rows lacking lag history; %s remain", before - len(out), len(out))
    return out.reset_index(drop=True)


def backtest(df: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    """Walk-forward: train on everything before each month, predict that month."""
    df = df.copy()
    df["month_start"] = pd.to_datetime(df["date_local"]).values.astype("datetime64[M]")
    months = sorted(df["month_start"].unique())

    results = []
    for i, month in enumerate(months):
        if i < MIN_TRAIN_MONTHS:
            continue
        train = df[df["month_start"] < month]
        test = df[df["month_start"] == month]
        if test.empty:
            continue

        model = lgb.LGBMRegressor(**MODEL_PARAMS)
        model.fit(train[features], train[TARGET])

        out = test.copy()
        out["model_mw"] = model.predict(test[features])
        results.append(out)

    log.info("backtested %s months", len(results))
    return pd.concat(results).reset_index(drop=True)


def metrics(frame: pd.DataFrame, pred_col: str) -> pd.Series:
    err = frame[TARGET] - frame[pred_col]
    return pd.Series({
        "MAE_mw": err.abs().mean(),
        "MAPE_pct": 100 * (err.abs() / frame[TARGET]).mean(),
        "bias_mw": err.mean(),
    })


def report(bt: pd.DataFrame) -> None:
    def compare(frame: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({
            "TSO day-ahead": metrics(frame, "tso_forecast_mw"),
            "This model": metrics(frame, "model_mw"),
        }).T.round(3)

    months = sorted(bt["month_start"].unique())
    warm = bt[bt["month_start"] >= months[WARMUP_MONTHS]]

    print(f"\nFull backtest (n={len(bt)})")
    print(compare(bt))

    print(f"\nExcluding {WARMUP_MONTHS} cold-start months (n={len(warm)})")
    print(compare(warm))

    for label, subset in [
        ("ordinary days", warm[warm.is_special_day == 0]),
        ("special days", warm[warm.is_special_day == 1]),
    ]:
        print(f"\n{label} (n={len(subset)})")
        print(compare(subset))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", default="de")
    parser.add_argument("--allow-leakage", action="store_true",
                        help="Include lag_1d. Inflates the result; see module docstring.")
    parser.add_argument("--output", default="model/backtest_results.parquet")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    features = BASE_FEATURES + ([LEAKY_FEATURE] if args.allow_leakage else [])
    if args.allow_leakage:
        log.warning("lag_1d included — results are NOT comparable to a day-ahead forecast")

    df = add_lag_features(add_calendar_features(load_hourly(args.country)))
    bt = backtest(df, features)
    report(bt)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    bt.to_parquet(args.output, index=False)
    log.info("wrote %s", args.output)


if __name__ == "__main__":
    main()