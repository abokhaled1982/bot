# strategy/engine.py

class StrategyEngine:
    def __init__(self,  config):
        self.indicators = config.get("indicators")
        self.symbol = config.get("symbol")
        self.pct=config.get('pct', 1)
        self.config = config
        self.name = f"{self.symbol}@{self.indicators}"
        self.target_pct = self.pct / 100  # z.B. 0.005 für +0.5 %

    def should_enter(self, market_data):
       
        method = getattr(self, f"_check_{self.indicators}", None)
        if method and not method(market_data):
            return False
        return True

    def should_sell(self, market_data):       
        rsi_val = market_data.get("rsi")
        threshold = 70
        return rsi_val is not None and rsi_val > threshold

    def _check_rsi(self, market_data):
        rsi_val = market_data.get("rsi")
        threshold = self.config.get("rsi", 10)
        return rsi_val is not None and rsi_val < threshold

    def _check_ema(self, market_data):
        ema = market_data.get("ema")
        price = market_data.get("price")
        delta = self.config.get("ema_delta", 1.0)
        return ema is not None and ((price - ema) / ema * 100) <= delta
    
    def _check_bollinger(self, market_data):
        lower = market_data.get("bollinger_lower")
        price = market_data.get("close")
        margin_pct = self.config.get("bollinger_margin", 0.0)
        if lower is None or price is None:
            return False
        return price <= lower * (1 + margin_pct)

    def _check_rsi_bollinger(self, market_data):
        """Kombinierte Strategie: RSI unter Schwelle UND Preis unter Bollinger-Untergrenze."""
        rsi_val = market_data.get("rsi")
        threshold = self.config.get("rsi", 10)
        rsi_ok = rsi_val is not None and rsi_val < threshold

        lower = market_data.get("bollinger_lower")
        price = market_data.get("close")
        margin_pct = self.config.get("bollinger_margin", 0.0)
        boll_ok = lower is not None and price is not None and price <= lower * (1 + margin_pct)

        return rsi_ok and boll_ok
