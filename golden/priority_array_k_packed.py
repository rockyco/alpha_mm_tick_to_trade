"""PriorityArrayKPacked (Layer 3 universal primitive, multi-symbol variant).

Symbol-packed priority array for HFT scale: N_SYMBOLS independent
priority arrays sharing one BRAM tile address space, addressed by
{symbol_id, key}. Per-symbol state (kbest cache, size counter) held
in flop arrays. Same algorithm as PriorityArrayK; only the storage
layout changes.

Math reference: just N independent PriorityArrayK instances, indexed
by symbol_id. The packing optimization happens at the cycle/RTL layer.

Universal across:
  - HFT M0 multi-symbol public book (one packed instance per side,
    holds N stocks instead of N copies of M0 wrapper)
  - QoS scheduler with N flow priority queues
  - DNS resolver with N domain hot-caches
  - Multi-stream packet classifier

Resource gain (depends on per-symbol footprint):
  - Per-symbol payload << RAMB36 -> N-fold density improvement
  - Per-symbol payload >= RAMB36 -> no benefit; use unpacked instances
"""

from __future__ import annotations

from golden.priority_array_k import PriorityArrayK


class PriorityArrayKPacked:
    """N_SYMBOLS independent bounded keyed top-K stores, math-equivalent."""

    def __init__(
        self,
        n_symbols: int,
        max_keys: int,
        descending: bool,
        key_bits: int = 8,
        val_bits: int = 32,
    ) -> None:
        self.n_symbols = int(n_symbols)
        self.max_keys = int(max_keys)
        self.descending = bool(descending)
        self.key_bits = int(key_bits)
        self.val_bits = int(val_bits)
        self.symbols = [
            PriorityArrayK(max_keys, descending, key_bits, val_bits)
            for _ in range(self.n_symbols)
        ]

    def insert(self, sym_id: int, key: int, val: int) -> None:
        self.symbols[int(sym_id)].insert(key, val)

    def remove(self, sym_id: int, key: int) -> None:
        self.symbols[int(sym_id)].remove(key)

    def get(self, sym_id: int, key: int, default: int = 0) -> int:
        return self.symbols[int(sym_id)].get(key, default)

    def peek_top(self, sym_id: int) -> tuple[int, int]:
        return self.symbols[int(sym_id)].peek_top()

    def size(self, sym_id: int) -> int:
        return len(self.symbols[int(sym_id)])

    def clear(self) -> None:
        for s in self.symbols:
            s.clear()
