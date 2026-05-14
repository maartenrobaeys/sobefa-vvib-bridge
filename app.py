import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
from io import BytesIO
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime

st.set_page_config(
    page_title="SOBEFA VV → IB Bridge",
    page_icon="📈",
    layout="wide"
)

# =====================================================
# HEADER
# =====================================================

st.title("📈 SOBEFA — VV → IB Bridge V2")
st.markdown("### VectorVest Intelligence → Interactive Brokers Execution Layer")

st.info(
    "Upload je VectorVest exports → automatische position sizing → TP/SL berekening → delayed live tracking → IB CSV export"
)

# =====================================================
# SIDEBAR SETTINGS
# =====================================================

st.sidebar.header("⚙ Portfolio Settings")

portfolio_capital = st.sidebar.number_input(
    "Beschikbaar kapitaal ($)",
    min_value=1000,
    value=10000,
    step=1000
)

max_positions = st.sidebar.slider(
    "Max open posities",
    min_value=1,
    max_value=20,
    value=10
)

profit_target_pct = st.sidebar.number_input(
    "Take Profit %",
    value=6.0,
    step=0.1
)

stop_loss_pct = st.sidebar.number_input(
    "Stop Loss %",
    value=1.9,
    step=0.1
)

ib_fee_pct = st.sidebar.number_input(
    "IB fees schatting %",
    value=0.05,
    step=0.01
)

position_size_usd = portfolio_capital / max_positions

st.sidebar.metric(
    "Positiegrootte per trade",
    f"${position_size_usd:,.2f}"
)

# =====================================================
# VECTORVEST TRADE RECONSTRUCTION ENGINE
# =====================================================

import re


def parse_trade_history(df_raw):
    parsed_rows = []

    # mogelijke VV trade history formaten
    possible_symbol_cols = [
        "Symbol",
        "symbol"
    ]

    possible_open_cols = [
        "Open",
        "Open Date",
        "OpenDate"
    ]

    possible_close_cols = [
        "Close",
        "Close Date",
        "CloseDate"
    ]

    possible_price_cols = [
        "Initial Price",
        "InitialPrice",
        "Price"
    ]

    possible_share_cols = [
        "Shares",
        "Quantity"
    ]

    symbol_col = next((c for c in possible_symbol_cols if c in df_raw.columns), None)
    open_col = next((c for c in possible_open_cols if c in df_raw.columns), None)
    close_col = next((c for c in possible_close_cols if c in df_raw.columns), None)
    price_col = next((c for c in possible_price_cols if c in df_raw.columns), None)
    share_col = next((c for c in possible_share_cols if c in df_raw.columns), None)

    if symbol_col is None:
        st.error("Geen Symbol kolom gevonden in trade history.")
        st.stop()

    # reconstrueer actieve trades
    for _, row in df_raw.iterrows():

        symbol = row.get(symbol_col)

        close_value = ""

        if close_col:
            close_value = str(row.get(close_col, "")).strip()

        # enkel OPEN trades behouden
        is_open_trade = (
            close_value == "" or
            close_value.lower() == "nan" or
            close_value.lower() == "none"
        )

        if is_open_trade:

            parsed_rows.append({
                "Symbol": symbol,
                "VV_BuyPrice": row.get(price_col, np.nan),
                "Shares": row.get(share_col, np.nan),
                "OpenDate": row.get(open_col, "")
            })

    parsed_df = pd.DataFrame(parsed_rows)

    # fallback wanneer VV alles als gesloten markeert
    # → laatste 10 trades gebruiken
    if parsed_df.empty:

        st.warning(
            "Geen open trades gedetecteerd → fallback naar laatste actieve VV trades."
        )

        fallback_df = df_raw.tail(10).copy()

        parsed_df = pd.DataFrame({
            "Symbol": fallback_df[symbol_col],
            "VV_BuyPrice": fallback_df.get(price_col, np.nan),
            "Shares": fallback_df.get(share_col, np.nan),
            "OpenDate": fallback_df.get(open_col, "")
        })

    parsed_df = parsed_df.drop_duplicates(subset=["Symbol"])

    return parsed_df

# =====================================================
# FILE UPLOADS
# =====================================================

st.header("📂 Upload VectorVest Files")

col1, col2 = st.columns(2)

with col1:
    holdings_file = st.file_uploader(
        "Upload Holdings Export (.csv)",
        type=["csv"]
    )

with col2:
    trade_log_file = st.file_uploader(
        "Upload Trade History (.csv)",
        type=["csv"]
    )

# =====================================================
# SAMPLE DATA FALLBACK
# =====================================================

def create_sample_data():
    return pd.DataFrame({
        "Symbol": ["AAPL", "MSFT", "NVDA", "AMZN"],
        "VV_BuyPrice": [180.25, 425.10, 950.50, 182.90],
        "Shares": [10, 5, 2, 8],
        "Stop": [176.82, 417.02, 932.44, 179.42],
        "REC": ["BUY", "BUY", "BUY", "BUY"]
    })

df = None

# trade history wordt nu PRIMARY SOURCE OF TRUTH
if trade_log_file:

    try:
        raw_trade_df = pd.read_csv(trade_log_file)

        st.success(
            f"Trade history geladen: {len(raw_trade_df)} rows"
        )

        df = parse_trade_history(raw_trade_df)

    except Exception as e:

        st.error(f"Kon trade history niet verwerken: {e}")
        st.stop()

# holdings export enkel als fallback
elif holdings_file:

    try:
        df = pd.read_csv(holdings_file)

    except:
        st.error("Kon holdings file niet lezen.")
        st.stop()

else:

    st.warning(
        "Geen bestanden geüpload → sample data geladen"
    )

    df = create_sample_data()

# =====================================================
# COLUMN STANDARDIZATION
# =====================================================

column_mapping = {
    "Ticker": "Symbol",
    "ticker": "Symbol",
    "symbol": "Symbol",
    "Price": "VV_BuyPrice",
    "price": "VV_BuyPrice",
    "BuyPrice": "VV_BuyPrice",
    "Shares": "Shares",
    "Quantity": "Shares",
    "Qty": "Shares",
    "Stop": "Stop",
    "REC": "REC"
}

for old_col, new_col in column_mapping.items():
    if old_col in df.columns:
        df.rename(columns={old_col: new_col}, inplace=True)

required_columns = ["Symbol"]

for col in required_columns:
    if col not in df.columns:
        st.error(f"Ontbrekende kolom: {col}")
        st.stop()

# =====================================================
# FETCH DELAYED MARKET DATA
# =====================================================

@st.cache_data(ttl=900)
def fetch_prices(tickers):
    prices = {}

    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="1d")

            if not hist.empty:
                prices[ticker] = round(hist["Close"].iloc[-1], 2)
            else:
                prices[ticker] = np.nan

        except:
            prices[ticker] = np.nan

    return prices

st.header("📡 Delayed Market Data")

with st.spinner("Koersen ophalen..."):
    tickers = df["Symbol"].tolist()
    market_prices = fetch_prices(tickers)

# =====================================================
# AUTO-FIX MISSING COLUMNS
# =====================================================

if "VV_BuyPrice" not in df.columns:
    st.warning(
        "Geen VV_BuyPrice gevonden in CSV → huidige marktprijs wordt tijdelijk gebruikt als referentieprijs."
    )
    df["VV_BuyPrice"] = np.nan

if "Shares" not in df.columns:
    df["Shares"] = 0

if "Stop" not in df.columns:
    df["Stop"] = np.nan

# =====================================================
# BUILD CORE TRACKER
# =====================================================

tracker_df = pd.DataFrame()

tracker_df["Ticker"] = df["Symbol"]
tracker_df["VV Buy Price"] = df.get("VV_BuyPrice", np.nan)
tracker_df["Current Price"] = tracker_df["Ticker"].map(market_prices)

# fallback wanneer VV buyprice ontbreekt
tracker_df["VV Buy Price"] = tracker_df["VV Buy Price"].fillna(
    tracker_df["Current Price"]
)

tracker_df["IB Entry Price"] = tracker_df["Current Price"]

tracker_df["Position Size $"] = position_size_usd

tracker_df["Shares To Buy"] = (
    tracker_df["Position Size $"] /
    tracker_df["Current Price"]
).round(0)

tracker_df["TP Price"] = (
    tracker_df["IB Entry Price"] *
    (1 + ((profit_target_pct + ib_fee_pct) / 100))
).round(2)

tracker_df["SL Price"] = (
    tracker_df["IB Entry Price"] *
    (1 - ((stop_loss_pct + ib_fee_pct) / 100))
).round(2)

tracker_df["VV vs IB %"] = (
    (
        tracker_df["IB Entry Price"] -
        tracker_df["VV Buy Price"]
    ) /
    tracker_df["VV Buy Price"]
) * 100

tracker_df["VV vs IB %"] = tracker_df["VV vs IB %"].round(2)

# =====================================================
# RISK ENGINE
# =====================================================

conditions = []

for value in tracker_df["VV vs IB %"]:
    if pd.isna(value):
        conditions.append("UNKNOWN")
    elif abs(value) < 0.5:
        conditions.append("OK")
    elif abs(value) < 1.9:
        conditions.append("CAUTION")
    else:
        conditions.append("SKIP")

tracker_df["Execution Status"] = conditions

# =====================================================
# DISPLAY TRACKER
# =====================================================

st.header("📊 Live Portfolio Tracker")

st.dataframe(
    tracker_df,
    use_container_width=True,
    height=500
)

# =====================================================
# EXECUTION SUMMARY
# =====================================================

st.header("🧠 Execution Intelligence")

ok_trades = len(tracker_df[tracker_df["Execution Status"] == "OK"])
caution_trades = len(tracker_df[tracker_df["Execution Status"] == "CAUTION"])
skip_trades = len(tracker_df[tracker_df["Execution Status"] == "SKIP"])

col1, col2, col3 = st.columns(3)

col1.metric("✅ OK Trades", ok_trades)
col2.metric("⚠ CAUTION", caution_trades)
col3.metric("❌ SKIP", skip_trades)

# =====================================================
# VISUAL TP/SL GRAPH
# =====================================================

st.header("🎯 TP / SL Visualisation")

selected_ticker = st.selectbox(
    "Selecteer ticker",
    tracker_df["Ticker"]
)

selected_row = tracker_df[
    tracker_df["Ticker"] == selected_ticker
].iloc[0]

if pd.isna(selected_row["Current Price"]):
    st.error(
        "Geen prijsdata beschikbaar voor deze ticker. Controleer ticker symbool of Yahoo Finance availability."
    )
    st.stop()

fig = go.Figure()

fig.add_trace(go.Indicator(
    mode="gauge+number",
    value=float(selected_row["Current Price"]),
    title={"text": selected_ticker},
    gauge={
        "axis": {
            "range": [
                float(selected_row["SL Price"] * 0.95),
                float(selected_row["TP Price"] * 1.05)
            ]
        },
        "steps": [
            {
                "range": [
                    float(selected_row["SL Price"]),
                    float(selected_row["Current Price"])
                ],
                "color": "red"
            },
            {
                "range": [
                    float(selected_row["Current Price"]),
                    float(selected_row["TP Price"])
                ],
                "color": "green"
            }
        ],
        "threshold": {
            "line": {"color": "blue", "width": 4},
            "value": float(selected_row["VV Buy Price"])
        }
    }
))

st.plotly_chart(fig, use_container_width=True)

# =====================================================
# IB CSV EXPORT
# =====================================================

st.header("📤 Interactive Brokers Export")

ib_orders = pd.DataFrame()

ib_orders["Action"] = "BUY"
ib_orders["Quantity"] = tracker_df["Shares To Buy"]
ib_orders["Symbol"] = tracker_df["Ticker"]
ib_orders["SecType"] = "STK"
ib_orders["Exchange"] = "SMART"
ib_orders["Currency"] = "USD"
ib_orders["TIF"] = "DAY"
ib_orders["OrderType"] = "MKT"
ib_orders["LimitPrice"] = ""
ib_orders["StopPrice"] = ""

stop_orders = pd.DataFrame()

stop_orders["Action"] = "SELL"
stop_orders["Quantity"] = tracker_df["Shares To Buy"]
stop_orders["Symbol"] = tracker_df["Ticker"]
stop_orders["SecType"] = "STK"
stop_orders["Exchange"] = "SMART"
stop_orders["Currency"] = "USD"
stop_orders["TIF"] = "GTC"
stop_orders["OrderType"] = "STP"
stop_orders["LimitPrice"] = ""
stop_orders["StopPrice"] = tracker_df["SL Price"]

col1, col2 = st.columns(2)

with col1:
    st.subheader("BUY Orders")
    st.dataframe(ib_orders, use_container_width=True)

with col2:
    st.subheader("STOP Orders")
    st.dataframe(stop_orders, use_container_width=True)

# =====================================================
# DOWNLOAD BUTTONS
# =====================================================


def dataframe_to_csv_download(df_input):
    return df_input.to_csv(index=False).encode("utf-8")

buy_csv = dataframe_to_csv_download(ib_orders)
stop_csv = dataframe_to_csv_download(stop_orders)
tracker_csv = dataframe_to_csv_download(tracker_df)

col1, col2, col3 = st.columns(3)

with col1:
    st.download_button(
        label="⬇ Download BUY CSV",
        data=buy_csv,
        file_name="ib_buy_orders.csv",
        mime="text/csv"
    )

with col2:
    st.download_button(
        label="⬇ Download STOP CSV",
        data=stop_csv,
        file_name="ib_stop_orders.csv",
        mime="text/csv"
    )

with col3:
    st.download_button(
        label="⬇ Download Tracker CSV",
        data=tracker_csv,
        file_name="vv_ib_tracker.csv",
        mime="text/csv"
    )

# =====================================================
# EQUITY SIMULATION
# =====================================================

st.header("📈 Compound Growth Simulation")

years = st.slider(
    "Aantal jaren",
    min_value=1,
    max_value=30,
    value=10
)

cagr = st.slider(
    "Verwachte CAGR %",
    min_value=1,
    max_value=50,
    value=11
)

capital_curve = []
capital = portfolio_capital

for year in range(years + 1):
    capital_curve.append({
        "Year": year,
        "Capital": capital
    })

    capital *= (1 + (cagr / 100))

capital_df = pd.DataFrame(capital_curve)

fig2 = px.line(
    capital_df,
    x="Year",
    y="Capital",
    title="Compound Growth Projection"
)

st.plotly_chart(fig2, use_container_width=True)

# =====================================================
# HOW TO USE
# =====================================================

st.header("🛠 Workflow")

st.markdown(
    """
### Stap 1
Exporteer ENKEL tradehistory.csv uit VectorVest.

Holdings exports zijn niet langer vereist.

### Stap 2
Upload tradehistory.csv.

De app reconstrueert automatisch:
- open trades
- VV buy prices
- shares
- huidige holdings

### Stap 3
De app:
- haalt delayed market data op
- berekent position sizing
- berekent TP/SL
- vergelijkt VV vs IB prijzen
- toont risk state

### Stap 4
Download BUY + STOP CSV.

### Stap 5
Importeer in Interactive Brokers:
File → Import → Basket Orders.

### Stap 6
Review orders → send.
"""
)

# =====================================================
# FOOTER
# =====================================================

st.markdown("---")

st.success(
    "SOBEFA VV → IB Bridge V2 klaar voor MVP deployment."
)

st.caption(
    "Delayed pricing via Yahoo Finance | Built for educational/research purposes"
)
