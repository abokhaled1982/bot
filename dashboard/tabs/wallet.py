import streamlit as st
from dashboard.db import get_wallet_sol_balance, get_wallet_tokens, get_sol_price_and_change
from dashboard.components import fmt_usd, fmt_pct
from dashboard.config import WALLET_ADDRESS


def render():
    st.markdown('<div class="section-header">Wallet</div>', unsafe_allow_html=True)

    @st.fragment(run_every="30s")
    def _render():
        sol_bal            = get_wallet_sol_balance(WALLET_ADDRESS)
        sol_price, sol_chg = get_sol_price_and_change()
        sol_usd            = sol_bal * sol_price
        tokens             = get_wallet_tokens(WALLET_ADDRESS)

        c1, c2, c3 = st.columns(3)
        c1.metric("SOL Balance", f"{sol_bal:.4f} SOL", fmt_usd(sol_usd))
        c2.metric("SOL Price",   fmt_usd(sol_price),   fmt_pct(sol_chg))
        c3.metric("SPL Tokens",  str(len(tokens)),      "with balance > 0")

        st.markdown(f"[Solscan Wallet](https://solscan.io/account/{WALLET_ADDRESS})")
        st.caption(f"`{WALLET_ADDRESS}`")

        st.divider()
        st.markdown("**Token Holdings**")
        if not tokens:
            st.info("No SPL tokens with balance > 0 in wallet.")
        else:
            for t in tokens:
                from dashboard.db import get_live_price
                cp      = get_live_price(t["mint"])
                val_usd = t["amount"] * cp if cp > 0 else 0
                st.markdown(
                    f'<div style="display:flex;justify-content:space-between;padding:8px 12px;'
                    f'background:#111319;border:1px solid #1e293b;border-radius:6px;margin-bottom:4px">'
                    f'<span style="font-weight:700;color:#fff">{t["symbol"]}</span>'
                    f'<span style="color:#5eead4;font-size:0.85rem">{t["amount"]:.4f} tokens</span>'
                    f'<span style="color:#e0a846;font-size:0.85rem">${cp:.8f}</span>'
                    f'<span style="color:#ffffff;font-size:0.85rem">{fmt_usd(val_usd)}</span>'
                    f'<a href="https://solscan.io/token/{t["mint"]}" target="_blank" '
                    f'style="color:#3b82f6;font-size:0.88rem">Solscan</a>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    _render()
