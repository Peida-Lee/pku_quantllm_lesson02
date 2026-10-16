"""Causal execution proxies derived ONLY from the provided daily matrices.

No external security master is read. Historical ST status and official price
limits remain unknown. A price pattern is a risk proxy, never a true ST label.
"""
import numpy as np
import pandas as pd


def board_labels(symbols):
    result = []
    for value in symbols:
        symbol = str(value)
        if symbol.endswith(".SH") and symbol.startswith(("688", "689")):
            result.append("star")
        elif symbol.endswith(".SZ") and symbol.startswith("30"):
            result.append("growth")
        elif (symbol.endswith(".SH") and symbol.startswith("6")) or (symbol.endswith(".SZ") and symbol.startswith("00")):
            result.append("main")
        else:
            result.append("unknown")
    return np.asarray(result)


def round_price_half_up(values):
    """Positive A-share 0.01 price ticks; avoid NumPy's bankers rounding."""
    values = np.asarray(values, dtype=float)
    return np.floor(values * 100. + .5 + 1e-9) / 100.


def universe_mask(symbols, universe):
    boards = board_labels(symbols)
    if universe == "main_a":
        return boards == "main"
    if universe == "growth_a":
        return np.isin(boards, ["growth", "star"])
    if universe == "all_a":
        return boards != "unknown"
    raise ValueError("Unknown universe")


def corporate_action_mask(market):
    cache = getattr(market, "_execution_metadata_cache", None)
    if cache is None:
        cache = market._execution_metadata_cache = {}
    if "corporate_action_mask" in cache:
        return cache["corporate_action_mask"]
    factors = market.raw["cumadj"]
    changed = np.zeros(factors.shape, dtype=bool)
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = factors[1:] / factors[:-1]
    changed[1:] = np.isfinite(ratio) & (factors[1:] > 0) & (factors[:-1] > 0) & (np.abs(ratio - 1.) > 1e-8)
    cache["corporate_action_mask"] = changed
    return changed


def price_risk_proxy(market, window=20):
    """Past-window main-board approximately-5% closing-lock pattern.

    The current signal-date bar is available after its close. No following
    date is used. This heuristic has both false positives and false negatives;
    growth-board ST status cannot be inferred from its usual 20% price limit.
    """
    if not 1 <= int(window) <= 120:
        raise ValueError("Risk proxy window must be from 1 to 120")
    cache = getattr(market, "_execution_metadata_cache", None)
    if cache is None:
        cache = market._execution_metadata_cache = {}
    key = ("price_risk_proxy", int(window))
    if key in cache:
        return cache[key]
    main = np.flatnonzero(board_labels(market.symbols) == "main")
    risk = np.zeros(market.close.shape, dtype=bool)
    if len(main):
        raw = market.raw
        close = raw["close"][:, main]
        high = raw["high"][:, main]
        low = raw["low"][:, main]
        prior = np.vstack([np.full((1, len(main)), np.nan), close[:-1]])
        upper = round_price_half_up(prior * 1.05)
        lower = round_price_half_up(prior * .95)
        upper_lock = (np.abs(close - upper) < .0049) & (np.abs(high - upper) < .0049)
        lower_lock = (np.abs(close - lower) < .0049) & (np.abs(low - lower) < .0049)
        # A zero-price-change tiny tick cannot establish a 5% risk pattern.
        with np.errstate(invalid="ignore", divide="ignore"):
            move = close / prior - 1.
        valid = np.isfinite(prior) & (prior > 0) & (raw["amt"][:, main] > 0) & (np.abs(move) >= .04)
        event = valid & (upper_lock | lower_lock) & ~corporate_action_mask(market)[:, main]
        recent = pd.DataFrame(event.astype(np.uint8)).rolling(int(window), min_periods=1).max().to_numpy() > 0
        risk[:, main] = recent
    cache[key] = risk
    return risk


def theoretical_limits(market, index, boards=None):
    """Board-based theoretical limits, NOT official exchange limit fields.

    Actual IPO/resumption/special sessions and ST regimes are unavailable.
    Corporate-action days must be rejected independently by the executor.
    """
    prior = market.raw["close"][index - 1]
    boards = board_labels(market.symbols) if boards is None else boards
    rate = np.where(boards == "main", .10, np.where(np.isin(boards, ["growth", "star"]), .20, np.nan))
    if market.dates[index] < pd.Timestamp("2020-08-24"):
        rate = np.where(boards == "growth", .10, rate)
    return round_price_half_up(prior * (1 + rate)), round_price_half_up(prior * (1 - rate))


def metadata_summary(market, cfg, indices=None):
    rows = np.arange(len(market.dates)) if indices is None else np.asarray(indices)
    observed = np.isfinite(market.raw["close"][rows])
    proxy = price_risk_proxy(market, cfg.risk_proxy_window)[rows] if cfg.st_policy == "price_proxy" else np.zeros(observed.shape, dtype=bool)
    boards = board_labels(market.symbols)
    return {"source": "provided zip daily matrices only", "historical_st_known_observations": 0,
            "historical_st_unknown_observations": int(observed.sum()),
            "historical_st_known_symbols": 0, "historical_st_status": "unavailable",
            "official_price_limits": "unavailable; only board-based theoretical prices",
            "st_policy": cfg.st_policy, "proxy_excluded_observations": int((observed & proxy).sum()),
            "proxy_excluded_symbols": int((observed & proxy).any(axis=0).sum()),
            "proxy_cannot_establish_non_st": True,
            "board_counts": {name: int(np.count_nonzero(boards == name)) for name in ["main", "growth", "star", "unknown"]},
            "universe": cfg.universe,
            "corporate_actions": "synthetic total-return adjusted units; action-day orders rejected; not a physical settlement ledger"}
