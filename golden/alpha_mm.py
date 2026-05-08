"""Alpha-driven MM strategy (math layer, v1.28.48).

A low-latency market-making strategy that replaces Stoikov MM's
microprice (which requires 12-cycle seq_div_lutmult reciprocal) with
SINGLE-CYCLE alpha arithmetic. All operations are shift+add+compare;
no division.

Alpha sources:
  1. TOB imbalance:  (bid_qty - ask_qty) >> ALPHA_IMB_SHIFT
  2. OFI signal:     order-flow imbalance from OFI primitive
  3. Inventory:      position >> GAMMA_SHIFT

Reservation = mid + alpha - inventory_skew
  - alpha leans INTO the predicted price move
  - inventory_skew leans AWAY from current position (risk management)

Adaptive spread:
  - HALF_SPREAD + (|alpha| >> SPREAD_SHIFT)
  - tighten when |alpha| small (high confidence), widen when uncertain.
  - This prevents adverse selection during high-alpha periods.

Quote ladder: simple 2-level (bid + ask) at +/- half_spread from
reservation; can be extended to N-level via LadderGenerator if needed.

This strategy is "based on alpha" in the sense that it uses MULTIPLE
predictive signals (imbalance + OFI), each weighted, to compute a
direction-leaning reservation price. It is NOT a microprice
approximation — it's a multi-signal alpha aggregator.

Reference reading: Cartea/Jaimungal/Penalva "Algorithmic and
High-Frequency Trading" (2015), chapter 9. The common form is:
  alpha_t = w_1 * imb_t + w_2 * ofi_t + w_3 * trend_t
  reservation = mid + alpha_t - gamma * sigma^2 * (T-t) * position
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AlphaMMParams:
    """Configuration for alpha-driven MM."""
    half_spread: int = 2
    default_qty: int = 10
    alpha_imb_shift: int = 6        # bid_qty-ask_qty >> 6 -> ticks
    alpha_ofi_shift: int = 4        # ofi_signal >> 4 -> ticks
    gamma_shift: int = 7            # position >> 7 (= /128)
    spread_alpha_shift: int = 4     # |alpha| >> 4 -> spread bump
    confident_threshold: int = 4    # |alpha| > 4 -> tight spread


@dataclass
class AlphaMMQuote:
    """One bid + ask quote pair."""
    bid_px: int
    bid_qty: int
    ask_px: int
    ask_qty: int


class AlphaMM:
    """Single-cycle alpha-driven MM strategy (math layer).

    Stateless: each compute() call is a pure function of inputs.
    """

    def __init__(self, params: AlphaMMParams) -> None:
        self.params = params

    def compute(
        self,
        bid_px: int,
        bid_qty: int,
        ask_px: int,
        ask_qty: int,
        position: int = 0,
        ofi_signal: int = 0,
    ) -> tuple[int, int, AlphaMMQuote]:
        """Compute (alpha, reservation, quote) given book + position + OFI.

        All ops are shift / add / compare - NO multiplication or
        division. Mirrors the single-cycle RTL data path.
        """
        p = self.params
        # Stage 0: parallel arithmetic (1 cycle in RTL)
        mid       = (bid_px + ask_px) >> 1
        imb_diff  = bid_qty - ask_qty                # signed
        imb_alpha = self._signed_shr(imb_diff, p.alpha_imb_shift)
        flow_alpha = self._signed_shr(ofi_signal, p.alpha_ofi_shift)
        alpha     = imb_alpha + flow_alpha
        inv_skew  = self._signed_shr(position, p.gamma_shift)

        # Stage 1: combine + adaptive spread (1 cycle in RTL)
        reservation = mid + alpha - inv_skew
        abs_alpha   = abs(alpha)
        # Adaptive: |alpha| > threshold -> tighten (confident); else widen
        spread_bump = self._signed_shr(abs_alpha, p.spread_alpha_shift)
        if abs_alpha > p.confident_threshold:
            half_spread = max(1, p.half_spread - 1)  # confident: tighter
        else:
            half_spread = p.half_spread + spread_bump  # uncertain: wider

        bid_quote_px = reservation - half_spread
        ask_quote_px = reservation + half_spread
        quote = AlphaMMQuote(
            bid_px=bid_quote_px,
            bid_qty=p.default_qty,
            ask_px=ask_quote_px,
            ask_qty=p.default_qty,
        )
        return alpha, reservation, quote

    @staticmethod
    def _signed_shr(x: int, n: int) -> int:
        """Arithmetic right shift, matches Verilog signed `>>>`."""
        if n <= 0:
            return x
        # Python `>>` already rounds toward -inf, matching arithmetic shift
        return x >> n


def alpha_mm(
    bid_px: int, bid_qty: int, ask_px: int, ask_qty: int,
    position: int = 0, ofi_signal: int = 0,
    params: AlphaMMParams | None = None,
) -> tuple[int, int, AlphaMMQuote]:
    """Functional interface: returns (alpha, reservation, quote)."""
    p = params or AlphaMMParams()
    return AlphaMM(p).compute(
        bid_px, bid_qty, ask_px, ask_qty, position, ofi_signal,
    )
