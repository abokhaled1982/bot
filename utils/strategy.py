# utils/strategy.py

class TradeStrategy:
    def __init__(self, entry_rsi=10, tp_pct=0.01, sl_pct=-0.5):
        self.entry_rsi = entry_rsi
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct

    def should_enter(self, in_position, rsi, current_price, ema, eur_balance):        
        if in_position:
            return False, "Bereits in Position"
        if eur_balance <= 0:
            return False, "Kein EUR-Guthaben"
        if rsi >= self.entry_rsi:
            return False, f"RSI zu hoch ({rsi:.2f})"
        # if current_price <= ema:
        #     return False, f"Kurs unter EMA ({current_price:.2f} <= {ema:.2f})"
        return True, "✅ Einstiegskriterium erfüllt"

    def should_exit(self, in_position, current_price, entry_price):
        if not in_position :
            return False, None, "Nicht in Position"        
        change_pct = (current_price - entry_price) / entry_price
        if change_pct >= self.tp_pct:
            return True, "TP", f"[TP] Take Profit erreicht: {change_pct*100:.2f}%"
        elif change_pct <= self.sl_pct:
            return True, "SL", f"[SL] Stop Loss erreicht: {change_pct*100:.2f}%"
        return False, None, "Noch kein Exit-Kriterium erfüllt"
