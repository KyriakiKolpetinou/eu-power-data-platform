"""Streamlit view over the backtest results.

Reads model/backtest_results.parquet rather than querying BigQuery, so this
deploys without credentials. The trade-off is that the data is fixed at the
last training run; the pipeline behind it is in ingest/ and dbt_power/.
"""

import pandas as pd
import streamlit as st

st.set_page_config(page_title="German load forecast accuracy", layout="wide")


@st.cache_data
def load_results() -> pd.DataFrame:
    df = pd.read_parquet("model/backtest_results.parquet")
    df["hour_utc"] = pd.to_datetime(df["hour_utc"])
    df["tso_ae"] = (df.actual_mw - df.tso_forecast_mw).abs()
    df["model_ae"] = (df.actual_mw - df.model_mw).abs()
    return df


df = load_results()

st.title("German electricity load: forecast accuracy")
st.caption(
    "Walk-forward backtest against the grid operator's published day-ahead "
    "forecast. Excludes yesterday's actual load as a feature — a day-ahead "
    "forecast is published before it has settled."
)

# ── Headline metrics ────────────────────────────────────────────────────────
def metrics(frame, col):
    err = frame.actual_mw - frame[col]
    return err.abs().mean(), 100 * (err.abs() / frame.actual_mw).mean(), err.mean()

tso_mae, tso_mape, tso_bias = metrics(df, "tso_forecast_mw")
mdl_mae, mdl_mape, mdl_bias = metrics(df, "model_mw")

c1, c2, c3 = st.columns(3)
c1.metric("Model MAPE", f"{mdl_mape:.3f}%", f"{mdl_mape - tso_mape:+.3f} pp vs TSO",
          delta_color="inverse")
c2.metric("Model MAE", f"{mdl_mae:,.0f} MW", f"{mdl_mae - tso_mae:+,.0f} MW vs TSO",
          delta_color="inverse")
c3.metric("Model bias", f"{mdl_bias:+,.0f} MW", f"TSO: {tso_bias:+,.0f} MW")

st.divider()

# ── Date range explorer ─────────────────────────────────────────────────────
st.subheader("Compare forecasts over a date range")

lo, hi = df.hour_utc.min().date(), df.hour_utc.max().date()
start, end = st.select_slider(
    "Range",
    options=pd.date_range(lo, hi, freq="D").date,
    value=(pd.Timestamp("2025-04-15").date(), pd.Timestamp("2025-04-25").date()),
)

window = df[(df.hour_utc.dt.date >= start) & (df.hour_utc.dt.date <= end)]

st.line_chart(
    window.set_index("hour_utc")[["actual_mw", "tso_forecast_mw", "model_mw"]],
    height=380,
)

specials = sorted(window[window.is_special_day == 1].date_local.unique())
if specials:
    st.caption("Holidays or bridge days in range: " + ", ".join(str(d) for d in specials))

st.divider()

# ── Breakdowns ──────────────────────────────────────────────────────────────
left, right = st.columns(2)

with left:
    st.subheader("Error by hour of day")
    by_hour = (df.groupby("hour_of_day_local")[["tso_ae", "model_ae"]]
                 .mean()
                 .rename(columns={"tso_ae": "TSO", "model_ae": "Model"}))
    st.line_chart(by_hour, height=300)
    st.caption(
        "Both degrade sharply at midday — behind-the-meter solar makes load "
        "hardest to predict when neither forecast has weather data."
    )

with right:
    st.subheader("Ordinary days vs special days")
    rows = []
    for label, sub in [("Ordinary", df[df.is_special_day == 0]),
                       ("Special", df[df.is_special_day == 1])]:
        t_mae, t_mape, t_bias = metrics(sub, "tso_forecast_mw")
        m_mae, m_mape, m_bias = metrics(sub, "model_mw")
        rows.append({"Day type": label, "n hours": len(sub),
                     "TSO MAPE %": round(t_mape, 3), "Model MAPE %": round(m_mape, 3),
                     "TSO bias MW": round(t_bias), "Model bias MW": round(m_bias)})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.caption(
        "On special days both forecasts are biased by over a gigawatt, in "
        "opposite directions: the operator over-forecasts, this model "
        "under-forecasts."
    )