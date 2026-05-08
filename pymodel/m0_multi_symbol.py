"""M0MultiSymbolCycle - cycle-accurate model.

Mirrors the planned RTL: ITCH event -> ROI offset adapter (S0, comb)
-> bid+ask priority_array_k_packed (S1 latch + S2 kbest update).

Pipeline matches priority_array_k_packed: LATENCY_CYCLES = 2 from event
arrival to top_*_o reflecting the post-update state for that symbol.
"""

from __future__ import annotations

from pymodel.priority_array_k_packed import PriorityArrayKPackedCycle


LATENCY_CYCLES = 2


class M0MultiSymbolCycle:
    def __init__(
        self,
        n_symbols: int,
        roi_lb: int = 0,
        roi_size: int = 256,
        val_bits: int = 32,
        k_levels: int = 2,
        roi_lb_per_sym: list[int] | None = None,
    ) -> None:
        """Per-symbol ROI: pass `roi_lb_per_sym=[...]` of length n_symbols.
        If None, the scalar `roi_lb` is shared across all symbols
        (legacy single-asset config). `roi_size` is always shared - same
        BRAM depth per symbol for predictable memory sizing."""
        self.n_symbols = int(n_symbols)
        self.roi_size = int(roi_size)
        self.val_bits = int(val_bits)
        self.key_bits = (roi_size - 1).bit_length()
        self.k_levels = int(k_levels)
        if roi_lb_per_sym is not None:
            assert len(roi_lb_per_sym) == n_symbols
            self.roi_lb_per_sym = [int(x) for x in roi_lb_per_sym]
            self.roi_lb = int(roi_lb)  # legacy scalar; unused when per-sym set
            self._use_per_sym = True
        else:
            self.roi_lb = int(roi_lb)
            self.roi_lb_per_sym = [int(roi_lb)] * self.n_symbols
            self._use_per_sym = False

        self.bid = PriorityArrayKPackedCycle(
            n_symbols, max_keys=roi_size, descending=True,
            key_bits=self.key_bits, val_bits=val_bits, k_levels=k_levels,
        )
        self.ask = PriorityArrayKPackedCycle(
            n_symbols, max_keys=roi_size, descending=False,
            key_bits=self.key_bits, val_bits=val_bits, k_levels=k_levels,
        )
        # v1.28.40: register sym_o once more so its latency matches the
        # book outputs (2 cycles). _sym_o_q_next holds the value to be
        # latched on the next clock(); _sym_o_q is the registered value.
        self._sym_o_q = 0
        self._sym_o_q_next = 0

    def _to_roi(self, sym_id: int, price_tick: int) -> tuple[int, bool]:
        anchor = self.roi_lb_per_sym[int(sym_id)]
        offset = int(price_tick) - anchor
        in_range = 0 <= offset < self.roi_size
        return offset & ((1 << self.key_bits) - 1), in_range

    def compute(
        self,
        sym_id: int = 0,
        op: int = 0,
        side: int = 0,
        price_tick: int = 0,
        qty: int = 0,
    ) -> None:
        offset, in_range = self._to_roi(sym_id, price_tick)
        # Op semantics: 1=ADD/UPDATE (also delete if qty==0), 2=DELETE.
        # priority_array_k_packed expects op_in: 0=nop, 1=insert, 2=remove.
        # ADD with qty=0 -> remove behavior already supported.
        eff_op = op if (in_range and op in (1, 2)) else 0

        if side == 1:  # ASK
            self.ask.compute(sym_id=sym_id, op_in=eff_op,
                              key_in=offset, val_in=qty)
            self.bid.compute(sym_id=sym_id, op_in=0)  # nop
        else:           # BID
            self.bid.compute(sym_id=sym_id, op_in=eff_op,
                              key_in=offset, val_in=qty)
            self.ask.compute(sym_id=sym_id, op_in=0)  # nop

    def clock(self) -> None:
        # Capture _next BEFORE advancing the inner instances, mirroring the
        # RTL `sym_o_q <= bid_top_sym_w` (current cycle's value).
        self._sym_o_q_next = self.bid.top_sym_o
        self.bid.clock()
        self.ask.clock()
        self._sym_o_q = self._sym_o_q_next

    @property
    def best_bid_tick_o(self) -> int:
        if not self.bid.top_valid_o:
            return 0
        return self.roi_lb_per_sym[self.bid.top_sym_o] + self.bid.top_key_o

    @property
    def best_bid_qty_o(self) -> int:
        return self.bid.top_val_o

    @property
    def best_bid_valid_o(self) -> bool:
        return self.bid.top_valid_o

    @property
    def best_ask_tick_o(self) -> int:
        if not self.ask.top_valid_o:
            return 0
        return self.roi_lb_per_sym[self.ask.top_sym_o] + self.ask.top_key_o

    @property
    def best_ask_qty_o(self) -> int:
        return self.ask.top_val_o

    @property
    def best_ask_valid_o(self) -> bool:
        return self.ask.top_valid_o

    @property
    def sym_o(self) -> int:
        return self._sym_o_q
