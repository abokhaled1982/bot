import random

class FakeMarket:
    def __init__(self):
        self.phase = "idle"
        self.counter = 0
        self.price = random.uniform(22000, 28000)
        self.target_buys = random.randint(3, 8)
        self.target_sells = random.randint(1, 3)

    def get_next_price_and_rsi(self):
        # Phasenlogik
        if self.phase == "idle":
            self.phase = "buying"
            self.counter = 0
            self.target_buys = random.randint(4, 10)
        elif self.phase == "buying" and self.counter >= self.target_buys:
            self.phase = "selling"
            self.counter = 0
            self.target_sells = random.randint(2, 4)
        elif self.phase == "selling" and self.counter >= self.target_sells:
            self.phase = "idle"
            self.counter = 0

        # Daten passend zur Phase generieren
        if self.phase == "buying":
            self.price = random.uniform(19500, 23500)
            # höhere Wahrscheinlichkeit für RSI < 10
            rsi = random.choices(
                population=[random.uniform(1, 5), random.uniform(5, 10), random.uniform(10, 15)],
                weights=[0.4, 0.4, 0.2]
            )[0]
        elif self.phase == "selling":
            self.price = random.uniform(26500, 30500)
            rsi = random.uniform(85, 95)
        else:  # idle
            self.price = random.uniform(24000, 26000)
            rsi = random.uniform(35, 60)

        self.counter += 1
        return self.price, rsi
