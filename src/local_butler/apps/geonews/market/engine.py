"""Logarithmic Market Scoring Rule (LMSR) engine.

LMSR gives us an automated market maker with always-available liquidity.
For a binary market with outcomes YES and NO:

    cost(q_yes, q_no) = b * ln(e^(q_yes/b) + e^(q_no/b))
    price_yes = e^(q_yes/b) / (e^(q_yes/b) + e^(q_no/b))

Where b is the liquidity parameter. Higher b = thicker market, smaller price moves.

All monetary values are stored as integer number of credits but computed as floats
internally. The smallest unit is one credit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PoolState:
    """Current LMSR state for one market pool."""

    q_yes: float
    q_no: float
    b: float

    @property
    def yes_price(self) -> float:
        exp_yes = math.exp(self.q_yes / self.b)
        exp_no = math.exp(self.q_no / self.b)
        return exp_yes / (exp_yes + exp_no)

    @property
    def no_price(self) -> float:
        return 1.0 - self.yes_price


class MarketEngine:
    """LMSR market maker for binary prediction markets."""

    BASE_LIQUIDITY = 100.0
    EPSILON = 1e-9

    def __init__(self, pool_state: PoolState | None = None):
        if pool_state is None:
            pool_state = PoolState(q_yes=0.0, q_no=0.0, b=self.BASE_LIQUIDITY)
        self._state = pool_state

    @classmethod
    def from_market(cls, market_model) -> "MarketEngine":
        """Build engine from a persisted Market row.

        The market keeps only the liquidity parameter B; outstanding quantities
        are derived from all open Bet rows.
        """
        q_yes = sum(
            b.shares for b in market_model.bets if b.side == "yes"
        )
        q_no = sum(
            b.shares for b in market_model.bets if b.side == "no"
        )
        return cls(PoolState(q_yes=float(q_yes), q_no=float(q_no), b=float(market_model.liquidity)))

    def state(self) -> PoolState:
        return self._state

    def price(self, side: Literal["yes", "no"]) -> float:
        if side == "yes":
            return self._state.yes_price
        return self._state.no_price

    def cost(self) -> float:
        """Total cost of current outstanding shares."""
        return self._state.b * math.log(
            math.exp(self._state.q_yes / self._state.b)
            + math.exp(self._state.q_no / self._state.b)
        )

    def cost_after_buy(self, side: Literal["yes", "no"], shares: float) -> float:
        """Cost after buying `shares` of `side`."""
        if side == "yes":
            next_state = PoolState(
                q_yes=self._state.q_yes + shares,
                q_no=self._state.q_no,
                b=self._state.b,
            )
        else:
            next_state = PoolState(
                q_yes=self._state.q_yes,
                q_no=self._state.q_no + shares,
                b=self._state.b,
            )
        return next_state.b * math.log(
            math.exp(next_state.q_yes / next_state.b)
            + math.exp(next_state.q_no / next_state.b)
        )

    def buy(self, side: Literal["yes", "no"], shares: float) -> dict:
        """Compute the cost to buy `shares` of `side` and return new state.

        Returns dict: {cost, avg_price, new_q_yes, new_q_no}.
        """
        if shares <= 0:
            raise ValueError("shares must be positive")

        cost_before = self.cost()
        new_cost = self.cost_after_buy(side, shares)
        cost = new_cost - cost_before
        avg_price = cost / shares if shares > self.EPSILON else 0.0

        new_state = self._state
        if side == "yes":
            new_state = PoolState(
                q_yes=new_state.q_yes + shares,
                q_no=new_state.q_no,
                b=new_state.b,
            )
        else:
            new_state = PoolState(
                q_yes=new_state.q_yes,
                q_no=new_state.q_no + shares,
                b=new_state.b,
            )

        return {
            "cost": cost,
            "avg_price": avg_price,
            "state": new_state,
        }

    def sell(self, side: Literal["yes", "no"], shares: float) -> dict:
        """Compute proceeds from selling `shares` of `side`.

        Treats it as buying negative shares. Returns dict: {proceeds, avg_price, state}.
        """
        if shares <= 0:
            raise ValueError("shares must be positive")

        cost_before = self.cost()
        if side == "yes":
            next_state = PoolState(
                q_yes=max(0.0, self._state.q_yes - shares),
                q_no=self._state.q_no,
                b=self._state.b,
            )
        else:
            next_state = PoolState(
                q_yes=self._state.q_yes,
                q_no=max(0.0, self._state.q_no - shares),
                b=self._state.b,
            )

        new_cost = next_state.b * math.log(
            math.exp(next_state.q_yes / next_state.b)
            + math.exp(next_state.q_no / next_state.b)
        )
        proceeds = cost_before - new_cost
        avg_price = proceeds / shares if shares > self.EPSILON else 0.0

        return {
            "proceeds": proceeds,
            "avg_price": avg_price,
            "state": next_state,
        }

    def payout(self, side: Literal["yes", "no"], shares: float, outcome: Literal["yes", "no"]) -> float:
        """Payout per share when market settles."""
        return shares if side == outcome else 0.0
