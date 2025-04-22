# strategy/engine.py

class StrategyEngine:
    def __init__(self, indicators, config):
        
        self.indicators = indicators
        self.config = config
        self.name = "+".join(indicators) + f"@{config.get('pct', 1)}%"
        self.target_pct = config.get("pct", 1) / 100  # → z. B. 0.005 für +0.5 %

    def should_enter(self, market_data):
        for indicator in self.indicators:
            method = getattr(self, f"_check_{indicator}", None)
            if method and not method(market_data):
                return False
        return True

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

        # Optional: Schwelle in Prozent unterhalb der unteren Bandgrenze
        margin_pct = self.config.get("bollinger_margin", 0.0)
        if lower is None or price is None:
            return False
        return price <= lower * (1 + margin_pct)
