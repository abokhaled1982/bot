import streamlit as st
import json

def render():
    st.markdown('<div class="section-header">🔄 Manual Trading</div>', unsafe_allow_html=True)
    st.markdown("Use this to manually queue trades for the AlphaEngine.")
    
    ticker = st.text_input("Ticker Symbol (e.g. AAPL)")
    action = st.radio("Action", ["BUY", "SELL"])
    qty = st.number_input("Quantity", min_value=0.01, step=0.01)
    
    if st.button("Queue Trade", type="primary"):
        with open("MANUAL_TRADE", "w") as f:
            json.dump({"ticker": ticker, "action": action, "quantity": qty}, f)
        st.success(f"Trade queued: {action} {qty} {ticker}")
