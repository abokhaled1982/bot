"""
tests/test_binance_leaderboard.py — Tests fuer den Binance-Leaderboard-Trader-Finder

Deckt die eigentliche Business-Logik ab ("Trader finden, die innerhalb eines
Tages mit gutem Profit handeln, und alles andere rausfiltern"):
  - merge_rank_rows:    rohe Leaderboard-Zeilen -> TraderMetrics je Trader
  - evaluate_candidate: harte Gates (Tages-ROI/PnL, Follower, Sharing, Konsistenz)
  - quality_score:      Ranking-Zahl fuer die Sortierung
  - find_intraday_traders: End-to-End mit gemocktem HTTP-Layer

Netzwerkzugriffe werden ausschliesslich ueber `fetch_leaderboard_rank`
gemockt (monkeypatch) - es findet kein echter Request auf binance.com statt.
"""
from __future__ import annotations

import time

import pytest

from src.adapters import binance_leaderboard as bl


def _row(uid: str, value: float, *, followers: int = 100,
         shared: bool = True, nick: str = "trader", age_sec: float = 60.0) -> dict:
    return {
        "encryptedUid": uid,
        "nickName": nick,
        "value": str(value),
        "followerCount": followers,
        "positionShared": shared,
        "updateTime": (time.time() - age_sec) * 1000.0,
    }


# ── merge_rank_rows ───────────────────────────────────────────────────────

def test_merge_rank_rows_combines_all_windows():
    rank_data = {
        ("day", "ROI"): [_row("uidA", 12.5)],
        ("day", "PNL"): [_row("uidA", 300.0)],
        ("week", "ROI"): [_row("uidA", 20.0)],
        ("week", "PNL"): [_row("uidA", 900.0)],
        ("month", "ROI"): [_row("uidA", 40.0)],
        ("month", "PNL"): [_row("uidA", 2000.0)],
    }
    metrics = bl.merge_rank_rows(rank_data)
    assert set(metrics) == {"uidA"}
    m = metrics["uidA"]
    assert m.day_roi == 12.5
    assert m.day_pnl == 300.0
    assert m.week_roi == 20.0
    assert m.week_pnl == 900.0
    assert m.month_roi == 40.0
    assert m.month_pnl == 2000.0
    assert m.follower_count == 100
    assert m.position_shared is True


def test_merge_rank_rows_missing_window_stays_none():
    rank_data = {
        ("day", "ROI"): [_row("uidA", 12.5)],
        ("day", "PNL"): [_row("uidA", 300.0)],
        ("week", "ROI"): [],
        ("week", "PNL"): [],
        ("month", "ROI"): [],
        ("month", "PNL"): [],
    }
    metrics = bl.merge_rank_rows(rank_data)
    m = metrics["uidA"]
    assert m.day_roi == 12.5
    assert m.week_roi is None
    assert m.month_roi is None


def test_merge_rank_rows_ignores_rows_without_uid():
    rank_data = {("day", "ROI"): [{"value": "5.0"}]}
    metrics = bl.merge_rank_rows(rank_data)
    assert metrics == {}


def test_merge_rank_rows_follower_count_takes_max_seen():
    rank_data = {
        ("day", "ROI"): [_row("uidA", 10.0, followers=50)],
        ("day", "PNL"): [_row("uidA", 100.0, followers=200)],
    }
    metrics = bl.merge_rank_rows(rank_data)
    assert metrics["uidA"].follower_count == 200


# ── evaluate_candidate ────────────────────────────────────────────────────

def _good_metrics(**overrides) -> bl.TraderMetrics:
    base = dict(
        day_roi=10.0, day_pnl=500.0, week_roi=15.0, week_pnl=1000.0,
        month_roi=30.0, month_pnl=3000.0, follower_count=100,
        position_shared=True, last_update_age=60.0,
    )
    base.update(overrides)
    return bl.TraderMetrics(**base)


def test_evaluate_candidate_accepts_good_intraday_trader():
    verified, reasons = bl.evaluate_candidate(_good_metrics())
    assert verified is True
    assert reasons == []


def test_evaluate_candidate_rejects_low_day_roi():
    metrics = _good_metrics(day_roi=0.5)
    verified, reasons = bl.evaluate_candidate(metrics, min_day_roi_pct=3.0)
    assert verified is False
    assert any("Tages-ROI" in r for r in reasons)


def test_evaluate_candidate_rejects_low_day_pnl():
    metrics = _good_metrics(day_pnl=1.0)
    verified, reasons = bl.evaluate_candidate(metrics, min_day_pnl_usd=50.0)
    assert verified is False
    assert any("Tages-PnL" in r for r in reasons)


def test_evaluate_candidate_rejects_unshared_positions():
    metrics = _good_metrics(position_shared=False)
    verified, reasons = bl.evaluate_candidate(metrics, require_position_shared=True)
    assert verified is False
    assert any("nicht oeffentlich geteilt" in r for r in reasons)


def test_evaluate_candidate_allows_unshared_when_not_required():
    metrics = _good_metrics(position_shared=False)
    verified, reasons = bl.evaluate_candidate(metrics, require_position_shared=False)
    assert verified is True
    assert reasons == []


def test_evaluate_candidate_rejects_negative_week_roi():
    metrics = _good_metrics(week_roi=-5.0)
    verified, reasons = bl.evaluate_candidate(metrics, require_positive_week=True)
    assert verified is False
    assert any("7T-ROI negativ" in r for r in reasons)


def test_evaluate_candidate_ignores_missing_week_data():
    metrics = _good_metrics(week_roi=None)
    verified, reasons = bl.evaluate_candidate(metrics, require_positive_week=True)
    assert verified is True
    assert reasons == []


def test_evaluate_candidate_rejects_inactive_trader():
    metrics = _good_metrics(last_update_age=48 * 3600.0)
    verified, reasons = bl.evaluate_candidate(metrics, max_last_update_age_sec=24 * 3600.0)
    assert verified is False
    assert any("inaktiv" in r for r in reasons)


def test_evaluate_candidate_rejects_too_few_followers():
    metrics = _good_metrics(follower_count=2)
    verified, reasons = bl.evaluate_candidate(metrics, min_followers=10)
    assert verified is False
    assert any("Follower" in r for r in reasons)


# ── quality_score ─────────────────────────────────────────────────────────

def test_quality_score_higher_for_better_day_performance():
    weak = bl.quality_score(_good_metrics(day_roi=1.0, day_pnl=10.0))
    strong = bl.quality_score(_good_metrics(day_roi=15.0, day_pnl=1500.0))
    assert strong > weak


def test_quality_score_rewards_consistency_across_windows():
    consistent = bl.quality_score(_good_metrics(week_roi=10.0, month_roi=10.0))
    inconsistent = bl.quality_score(_good_metrics(week_roi=-10.0, month_roi=-10.0))
    assert consistent > inconsistent


def test_quality_score_is_bounded_and_deterministic():
    metrics = _good_metrics(day_roi=1000.0, day_pnl=1_000_000.0, follower_count=10**9)
    score = bl.quality_score(metrics)
    assert 0.0 <= score <= 100.0
    assert score == bl.quality_score(metrics)


# ── find_intraday_traders (End-to-End mit gemocktem HTTP) ────────────────

def test_find_intraday_traders_filters_and_sorts(monkeypatch):
    def fake_rank(period="day", statistics_type="ROI", trade_type="PERPETUAL", limit=50):
        table = {
            ("day", "ROI"): [
                _row("good", 12.0, nick="Good Trader"),
                _row("bad_day_roi", 0.5, nick="Bad ROI"),
                _row("great", 25.0, nick="Great Trader"),
            ],
            ("day", "PNL"): [
                _row("good", 400.0), _row("bad_day_roi", 5.0), _row("great", 900.0),
            ],
            ("week", "ROI"): [_row("good", 18.0), _row("great", 30.0)],
            ("week", "PNL"): [_row("good", 800.0), _row("great", 1500.0)],
            ("month", "ROI"): [_row("good", 35.0), _row("great", 60.0)],
            ("month", "PNL"): [_row("good", 2200.0), _row("great", 4000.0)],
        }
        return table.get((period, statistics_type), [])

    monkeypatch.setattr(bl, "fetch_leaderboard_rank", fake_rank)

    results = bl.find_intraday_traders(min_day_roi_pct=3.0, min_day_pnl_usd=50.0)

    uids = [c.uid for c in results]
    assert "bad_day_roi" not in uids  # zu niedriger Tages-ROI, rausgefiltert
    assert uids == ["great", "good"]  # nach Quality-Score sortiert
    assert all(c.verified for c in results)


def test_find_intraday_traders_respects_limit(monkeypatch):
    def fake_rank(period="day", statistics_type="ROI", trade_type="PERPETUAL", limit=50):
        if (period, statistics_type) == ("day", "ROI"):
            return [_row(f"uid{i}", 10.0 + i) for i in range(5)]
        if (period, statistics_type) == ("day", "PNL"):
            return [_row(f"uid{i}", 100.0 + i) for i in range(5)]
        return [_row(f"uid{i}", 10.0 + i) for i in range(5)]

    monkeypatch.setattr(bl, "fetch_leaderboard_rank", fake_rank)

    results = bl.find_intraday_traders(limit=2, min_day_roi_pct=0.0, min_day_pnl_usd=0.0)
    assert len(results) == 2


def test_find_intraday_traders_empty_when_leaderboard_unreachable(monkeypatch):
    monkeypatch.setattr(bl, "fetch_leaderboard_rank", lambda **kwargs: [])
    assert bl.find_intraday_traders() == []


# ── Positions-Parsing (fuer die Copy-Signal-Pipeline) ─────────────────────

def test_parse_position_row_long():
    row = {
        "symbol": "BTCUSDT", "amount": "0.5", "positionSide": "LONG",
        "entryPrice": "50000", "leverage": "10", "roe": "0.05",
    }
    parsed = bl.BinanceLeaderboardTrader._parse_position_row(row)
    assert parsed["coin"] == "BTC"
    assert parsed["symbol"] == "BTCUSDT"
    assert parsed["size"] == 0.5
    assert parsed["leverage"] == 10.0
    assert parsed["pnl_pct"] == pytest.approx(5.0)


def test_parse_position_row_short_flips_sign():
    row = {"symbol": "ETHUSDT", "amount": "1.0", "positionSide": "SHORT", "entryPrice": "3000"}
    parsed = bl.BinanceLeaderboardTrader._parse_position_row(row)
    assert parsed["size"] == -1.0


def test_parse_position_row_maps_micro_price_futures_symbol():
    row = {"symbol": "1000PEPEUSDT", "amount": "1000", "entryPrice": "0.001"}
    parsed = bl.BinanceLeaderboardTrader._parse_position_row(row)
    assert parsed["symbol"] == "PEPEUSDT"
    assert parsed["coin"] == "PEPE"


def test_parse_position_row_missing_symbol_returns_none():
    assert bl.BinanceLeaderboardTrader._parse_position_row({}) is None


def test_futures_symbol_to_spot_symbol_identity_for_regular_pairs():
    assert bl.futures_symbol_to_spot_symbol("btcusdt") == "BTCUSDT"


def test_binance_leaderboard_url_contains_uid():
    url = bl.binance_leaderboard_url("abc123")
    assert "abc123" in url
    assert url.startswith("https://www.binance.com/")
