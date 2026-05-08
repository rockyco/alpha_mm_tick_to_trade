"""PriorityArrayK (Layer 3 universal primitive).

Bounded keyed top-K priority store. Maps to a sorted register-bank +
comparator-tree in RTL emission. Universal across:
- HFT M0 best-K bid/ask price levels (K=1..16)
- HFT M1 top-K outstanding orders by price-distance from mid
- HFT M3 strategy ladder generator (post K orders at +/-k*tick)
- Cache eviction LRU/LFU top-K eviction candidates
- Packet QoS scheduler top-K priority queues
- Scheduler runqueue top-K highest-priority tasks
- DNS top-K cached entries by hit count

The math layer uses a dict for O(1) update; sort is lazy on top_n().
The cycle/RTL layer keeps the array sorted at all times via single-cycle
comparator-shift on insert (see kbest_bank lessons in v1.20.x for FPGA
implementation).

Promoted from projects/hft_book_builder/golden/primitives/sorted_array.py
(Phase 1.1 of HFT_FULL_STACK_ROADMAP, v1.22.1). Class renamed from
`PriorityArray` to `PriorityArrayK` per plan; `max_keys` kept as parameter
name (semantically clear).

LATENCY_CYCLES = 2 in cycle layer (1 for compare-tree + 1 for shift commit).
"""

from __future__ import annotations

from typing import Iterator


class PriorityArrayK:
    """Canonical full-ROI keyed priority store, mirroring hftbacktest's
    `ROIVectorMarketDepth` (Rule 6 SOTA reference).

    Per CLAUDE.md Rule 6.5 (Canonical Math Reference, ABSOLUTE), this is
    the math-layer truth: a Python dict that accepts every distinct key
    in `[0, key_mask]` simultaneously. There is NO K-bound, NO cache, NO
    eviction at this layer.

    Hardware layers (cycle pymodel + RTL) MAY add caches and scan FSMs
    as latency optimizations -- the legacy `priority_array_k.sv` does
    exactly this with a K=2 register cache + group-bitmap-accelerated
    scan that walks `mem_q` from the cached top to find the new top
    when removes deplete the cache. But every hardware optimization
    MUST converge to this canonical state at steady state.

    Parameters
    ----------
    max_keys : int
        INFORMATIONAL only (kept for API back-compat). Per Rule 6.5 the
        math layer has no K-bound; max_keys does not raise. The natural
        upper bound is `2 ** key_bits` (the keyspace size).
    descending : bool
        True for descending-priority (largest key on top); False for ascending.
        For LOB this maps to bid (descending=True) vs ask (descending=False).
    key_bits : int
        Bit-width of the key; inputs masked on write.
    val_bits : int
        Bit-width of the value; inputs masked on write.
    """

    def __init__(
        self,
        max_keys: int,
        descending: bool,
        key_bits: int = 32,
        val_bits: int = 32,
    ) -> None:
        self.max_keys = int(max_keys)
        self.descending = bool(descending)
        self.key_mask = (1 << int(key_bits)) - 1
        self.val_mask = (1 << int(val_bits)) - 1
        self._store: dict[int, int] = {}

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: int) -> bool:
        return (int(key) & self.key_mask) in self._store

    def insert(self, key: int, val: int) -> None:
        """Set the value for a key. Creates if absent, replaces if present.

        Removes the entry if val == 0 (zero-qty-ADD-filter discipline,
        v1.20.17). Per Rule 6.5: NO bounded-K, NO eviction, NO overflow at
        the math layer. The structure is the canonical full ROI vector
        mirroring nkaz001/hftbacktest's ROIVectorMarketDepth: every distinct
        key in [0, key_mask] can be present simultaneously. Hardware
        layers (cycle pymodel + RTL) MAY add caches / scan FSMs as latency
        optimizations but MUST converge to this state at steady state.
        """
        k = int(key) & self.key_mask
        v = int(val) & self.val_mask
        if v == 0:
            self._store.pop(k, None)
            return
        self._store[k] = v

    def remove(self, key: int) -> None:
        """Drop a key. No-op if absent."""
        self._store.pop(int(key) & self.key_mask, None)

    def get(self, key: int, default: int = 0) -> int:
        """Return the value for a key, or default if absent."""
        return self._store.get(int(key) & self.key_mask, default)

    def peek_top(self) -> tuple[int, int]:
        """Return (key, val) of the top entry, or (0, 0) if empty."""
        if not self._store:
            return (0, 0)
        top_key = max(self._store) if self.descending else min(self._store)
        return (top_key, self._store[top_key])

    def top_n(self, n: int) -> list[tuple[int, int]]:
        """Return top-n entries sorted by priority. Pads with (0, 0) to length n."""
        items = sorted(
            self._store.items(),
            key=lambda kv: kv[0],
            reverse=self.descending,
        )[:n]
        while len(items) < n:
            items.append((0, 0))
        return items

    def items(self) -> Iterator[tuple[int, int]]:
        """Iterate raw (key, val) pairs in storage order. Mostly for testing."""
        return iter(self._store.items())

    def clear(self) -> None:
        self._store.clear()


# Backward-compat alias for legacy hft_book_builder imports
PriorityArray = PriorityArrayK
