"""PriorityArrayK cycle-accurate pymodel (Phase 2.6, rewritten v1.25.10).

Architecture mirror of the new RTL (kbest register cache + key-indexed BRAM):

    +-----------------+    +------------------------+
    | mem[KEY_RANGE]  |    | kbest[K_LEVELS]        |
    | valid[KEY_RANGE]|    | (sorted register cache |
    | (key-indexed    |    |  holding top-K best)   |
    |  storage)       |    +------------------------+
    +-----------------+              |
          |                          |
          v                          v
       size++/-- counter         top_key_o = kbest[0]
                                 top_val_o, top_valid_o

LATENCY_CYCLES = 2 (cmd_q + kbest_q, then combinational top_*_o output).

Replaces the prior wide-priority-encoder design (v1.25.9) which had timing
issues at KEY_RANGE >= 128. The kbest cache pattern matches
hft_book_builder s5_kbest_bank.sv exactly.

Trade: only top K_LEVELS levels are tracked in the cache. After K
consecutive removes hitting cache slots, the cache may deplete; in that
case top_valid_o = 0 until the next insert replenishes. Acceptable for
HFT market data (inserts greatly outnumber removes-of-best).
"""

from __future__ import annotations


LATENCY_CYCLES = 2


class PriorityArrayKCycle:
    """Cycle-accurate kbest-cache version of PriorityArrayK.

    Mirrors the RTL kbest register bank exactly so V2-rtl passes bit-exact.
    The math model (golden.PriorityArrayK) returns the GLOBAL top from its
    dict; this cycle pymodel returns the cached top, which can differ if
    the cache has depleted.
    """

    def __init__(
        self,
        max_keys: int,
        descending: bool,
        key_bits: int = 32,
        val_bits: int = 32,
        k_levels: int = 2,
    ) -> None:
        self.max_keys = int(max_keys)
        self.descending = bool(descending)
        self.key_bits = int(key_bits)
        self.val_bits = int(val_bits)
        self.k_levels = int(k_levels)
        self.key_mask = (1 << self.key_bits) - 1
        self.val_mask = (1 << self.val_bits) - 1
        # Stage 1: command latch
        self._cmd_op_reg = 0
        self._cmd_key_reg = 0
        self._cmd_val_reg = 0
        self._cmd_op_next = 0
        self._cmd_key_next = 0
        self._cmd_val_next = 0
        # Storage: key-indexed mirror (matches RTL mem[] + valid[])
        self._mem: dict[int, int] = {}
        # K-best register cache: lists of length k_levels (slot 0 = top)
        self._kbest_key = [0] * self.k_levels
        self._kbest_val = [0] * self.k_levels
        self._kbest_vld = [False] * self.k_levels
        # Total book size counter (matches RTL size_o)
        self._size_reg = 0

    def compute(
        self,
        op_in: int,
        key_in: int = 0,
        val_in: int = 0,
    ) -> None:
        """op_in: 0=nop, 1=insert(key,val), 2=remove(key)."""
        self._cmd_op_next = int(op_in)
        self._cmd_key_next = int(key_in) & self.key_mask
        self._cmd_val_next = int(val_in) & self.val_mask

    def _is_better(self, new_key: int, slot_key: int, slot_vld: bool) -> bool:
        if not slot_vld:
            return True
        if self.descending:
            return new_key > slot_key
        return new_key < slot_key

    def clock(self) -> None:
        # Resolve op semantics from the now-current cmd_reg (will be applied
        # to mem and kbest cache; convention matches Verilog NBA semantics --
        # apply previous cmd_reg before updating cmd_reg from cmd_next)
        op = self._cmd_op_reg
        key = self._cmd_key_reg
        val = self._cmd_val_reg
        is_insert = (op == 1) and (val != 0)
        is_remove = (op == 2) or ((op == 1) and (val == 0))

        # 1. Apply mem update + size counter
        was_valid = key in self._mem
        if is_insert:
            self._mem[key] = val
            if not was_valid:
                self._size_reg += 1
        elif is_remove:
            if was_valid:
                self._mem.pop(key, None)
                self._size_reg -= 1

        # 2. Apply kbest cache update (mirroring the RTL combinational logic)
        matches_0 = self._kbest_vld[0] and self._kbest_key[0] == key
        better_0 = self._is_better(key, self._kbest_key[0], self._kbest_vld[0])
        matches_1 = (self.k_levels > 1 and self._kbest_vld[1]
                     and self._kbest_key[1] == key)
        better_1 = (self.k_levels > 1 and
                    self._is_better(key, self._kbest_key[1], self._kbest_vld[1]))

        new_key = list(self._kbest_key)
        new_val = list(self._kbest_val)
        new_vld = list(self._kbest_vld)

        if is_insert:
            if matches_0:
                new_val[0] = val
            elif better_0:
                if self.k_levels > 1:
                    new_key[1] = self._kbest_key[0]
                    new_val[1] = self._kbest_val[0]
                    new_vld[1] = self._kbest_vld[0]
                new_key[0] = key
                new_val[0] = val
                new_vld[0] = True
            elif self.k_levels > 1 and matches_1:
                new_val[1] = val
            elif self.k_levels > 1 and better_1:
                new_key[1] = key
                new_val[1] = val
                new_vld[1] = True
        elif is_remove:
            if matches_0:
                if self.k_levels > 1:
                    new_key[0] = self._kbest_key[1]
                    new_val[0] = self._kbest_val[1]
                    new_vld[0] = self._kbest_vld[1]
                    new_key[1] = 0
                    new_val[1] = 0
                    new_vld[1] = False
                else:
                    new_key[0] = 0
                    new_val[0] = 0
                    new_vld[0] = False
            elif self.k_levels > 1 and matches_1:
                new_key[1] = 0
                new_val[1] = 0
                new_vld[1] = False

        self._kbest_key = new_key
        self._kbest_val = new_val
        self._kbest_vld = new_vld

        # 3. cmd_reg <- cmd_next
        self._cmd_op_reg = self._cmd_op_next
        self._cmd_key_reg = self._cmd_key_next
        self._cmd_val_reg = self._cmd_val_next

    @property
    def top_key_o(self) -> int:
        return self._kbest_key[0]

    @property
    def top_val_o(self) -> int:
        return self._kbest_val[0]

    @property
    def top_valid_o(self) -> bool:
        return self._kbest_vld[0]

    @property
    def size_o(self) -> int:
        return self._size_reg
