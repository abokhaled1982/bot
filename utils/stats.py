# utils/trader_stats.py

class TraderStats:
    def __init__(self):
        self.trades = 0
        self.wins = 0
        self.losses = 0

    def record_trade(self, outcome):
        self.trades += 1
        if outcome == "TP":
            self.wins += 1
        elif outcome == "SL":
            self.losses += 1

    def winrate(self):
        return (self.wins / self.trades * 100) if self.trades else 0
