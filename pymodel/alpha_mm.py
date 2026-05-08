"""Alpha-driven MM cycle-accurate pymodel (v1.28.48).

Mirrors the planned RTL: 2-stage pipeline.
  S0: parallel alpha components + mid + skew
  S1: reservation + adaptive spread + quotes

LATENCY_CYCLES = 2 from valid_i to valid_o.

Mechanical 1:1 translation from golden/alpha_mm.py AlphaMM, with
explicit per-cycle register state.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from golden.alpha_mm import AlphaMMParams


LATENCY_CYCLES = 2


@dataclass
class _S0Reg:
    valid: bool = False
    mid: int = 0
    alpha: int = 0
    inv_skew: int = 0
    abs_alpha: int = 0


@dataclass
class _S1Reg:
    valid: bool = False
    bid_px: int = 0
    bid_qty: int = 0
    ask_px: int = 0
    ask_qty: int = 0


class AlphaMMCycle:
    """Cycle-accurate model for alpha_mm RTL pipeline."""

    def __init__(
        self,
        params: AlphaMMParams,
        px_bits: int = 16,
        qty_bits: int = 16,
        pos_bits: int = 32,
    ) -> None:
        self.params = params
        self.px_bits = int(px_bits)
        self.qty_bits = int(qty_bits)
        self.pos_bits = int(pos_bits)
        self._s0 = _S0Reg()
        self._s1 = _S1Reg()
        self._s0_next = _S0Reg()
        self._s1_next = _S1Reg()

    def compute(
        self,
        valid_i: bool = False,
        bid_px: int = 0,
        bid_qty: int = 0,
        ask_px: int = 0,
        ask_qty: int = 0,
        position: int = 0,
        ofi_signal: int = 0,
    ) -> None:
        """Combinational compute: read self._s* state, set self._s*_next."""
        p = self.params

        # ===== S0 update from inputs =====
        mid = (bid_px + ask_px) >> 1
        imb_diff = bid_qty - ask_qty
        imb_alpha = imb_diff >> p.alpha_imb_shift
        flow_alpha = ofi_signal >> p.alpha_ofi_shift
        alpha = imb_alpha + flow_alpha
        inv_skew = position >> p.gamma_shift
        abs_alpha = abs(alpha)

        self._s0_next = _S0Reg(
            valid=bool(valid_i),
            mid=int(mid), alpha=int(alpha),
            inv_skew=int(inv_skew), abs_alpha=int(abs_alpha),
        )

        # ===== S1 update from S0 (current registered state) =====
        if self._s0.valid:
            reservation = self._s0.mid + self._s0.alpha - self._s0.inv_skew
            if self._s0.abs_alpha > p.confident_threshold:
                half_spread = max(1, p.half_spread - 1)
            else:
                spread_bump = self._s0.abs_alpha >> p.spread_alpha_shift
                half_spread = p.half_spread + spread_bump
            bid_q = reservation - half_spread
            ask_q = reservation + half_spread
            self._s1_next = _S1Reg(
                valid=True,
                bid_px=int(bid_q), bid_qty=p.default_qty,
                ask_px=int(ask_q), ask_qty=p.default_qty,
            )
        else:
            self._s1_next = _S1Reg(valid=False)

    def clock(self) -> None:
        self._s0 = self._s0_next
        self._s1 = self._s1_next

    @property
    def valid_o(self) -> bool:
        return bool(self._s1.valid)

    @property
    def bid_price_o(self) -> int:
        return self._s1.bid_px

    @property
    def bid_qty_o(self) -> int:
        return self._s1.bid_qty

    @property
    def ask_price_o(self) -> int:
        return self._s1.ask_px

    @property
    def ask_qty_o(self) -> int:
        return self._s1.ask_qty
