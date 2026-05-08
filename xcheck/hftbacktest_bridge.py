"""hftbacktest binding bridge (Phase 9, v1.28.36).

Drives a synthetic depth feed through the REAL hftbacktest 2.3.0 Python
binding (installed in this environment) and records book-top trace in
the framework's TraceRecord schema.

This unblocks the V1 cross-validation contract:
  framework.golden.priority_array_k_packed.PriorityArrayKPacked
                            ==
  hftbacktest.ROIVectorMarketDepth

per CLAUDE.md Rule 6 (SOTA Algorithm Adoption).

The framework math layer is documented as a bit-exact mirror of
hftbacktest's ROIVectorMarketDepth. This module proves that claim by
running both and diffing.

Tested with hftbacktest 2.3.0 (manylinux wheel from PyPI). The wheel
ships pre-built; no Rust toolchain required for the binding alone.

Notes on hftbacktest semantics learned during integration:

* `bt.current_timestamp` starts at `LLONG_MAX` (sentinel "uninitialized").
  Naive `delta = ev.ts - bt.current_timestamp` produces a huge negative
  number, so any `elapse(delta)` call is skipped. Use `wait_next_feed`
  instead: it advances current_timestamp to the next event's local_ts.
* `correct_event_order` from `hftbacktest.data.validation` MUST be
  applied to set EXCH_EVENT|LOCAL_EVENT bits. Pre-setting them on raw
  rows leaves the engine confused and the book stays empty.
* When `exch_ts == local_ts` (our case - synthetic tapes), the validator
  emits ONE row per input (no duplication), so feed length matches tape
  length 1:1.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

import numpy as np

# hftbacktest 2.3.0
import hftbacktest as hbt
from hftbacktest.data.validation import correct_event_order  # numba-JIT'd

TOOLS = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOOLS))

from xcheck.lobster_tape_loader import (
    TapeEvent,
)
from xcheck.hftbacktest_xcheck import (
    TraceRecord,
)


# ---------------------------------------------------------------------------
# Feed format conversion: framework TapeEvent -> hftbacktest event_dtype array
# ---------------------------------------------------------------------------


def tape_to_hbt_feed(
    tape: Iterable[TapeEvent],
    tick_size: float = 0.01,
    lot_size: float = 1.0,
) -> np.ndarray:
    """Translate framework TapeEvents to hftbacktest's structured numpy feed.

    For our synthetic single-asset tapes, exch_ts == local_ts on every
    row. In that case `correct_event_order` (numba-JIT'd) emits exactly
    one row per input with EXCH_EVENT|LOCAL_EVENT bits set. We replicate
    that behavior in pure Python because numba's nopython JIT fails to
    compile `correct_event_order` when pytest's assertion-rewriting hook
    is active (cocotb's runtime pulls pytest in, and `assert ... == ...`
    inside the JIT body references `_pytest.assertion.rewrite._call_reprcompare`
    which numba can't resolve). The bypass is exact for the equal-
    timestamp case (verified against `correct_event_order` directly:
    same bit pattern, same ordering).

    Each TapeEvent maps to ONE depth-update row in the validated form:
      ev = DEPTH_EVENT | (BUY_EVENT or SELL_EVENT) | EXCH_EVENT | LOCAL_EVENT
      exch_ts = local_ts = TapeEvent.timestamp_ns
      px = TapeEvent.price_ticks * tick_size
      qty = TapeEvent.new_abs_qty * lot_size  (zero qty = remove)

    Tape rows MUST be in non-decreasing timestamp order; this function
    does NOT sort.

    Returns a structured numpy array with hftbacktest.event_dtype, ready
    to feed `BacktestAsset.data(...)`.
    """
    rows = list(tape)
    arr = np.zeros(len(rows), dtype=hbt.event_dtype)
    validated_bits = (
        hbt.DEPTH_EVENT | hbt.EXCH_EVENT | hbt.LOCAL_EVENT
    )
    for i, ev in enumerate(rows):
        side_flag = hbt.BUY_EVENT if ev.side == 0 else hbt.SELL_EVENT
        arr[i]["ev"]       = validated_bits | side_flag
        arr[i]["exch_ts"]  = ev.timestamp_ns
        arr[i]["local_ts"] = ev.timestamp_ns
        arr[i]["px"]       = ev.price_ticks * tick_size
        arr[i]["qty"]      = ev.new_abs_qty * lot_size
        arr[i]["order_id"] = 0
    return arr


# ---------------------------------------------------------------------------
# Build a one-asset hftbacktest backtest with ROIVectorMarketDepth
# ---------------------------------------------------------------------------


def build_roivec_backtest_from_feed(
    feed: np.ndarray,
    *,
    tick_size: float = 0.01,
    lot_size: float = 1.0,
    roi_lb: float = 0.0,
    roi_ub: float = 1_000_000.0,
):
    """Construct an hftbacktest BacktestAsset and ROI-vector backtest runner.

    Uses zero-latency models and risk-adverse queue model (the simplest
    config; we're testing book-state correctness, not order matching).

    Returns a ROIVectorMarketDepthBacktest_TypeHint that can be used
    from njit'd code OR (less efficiently) directly from Python."""
    asset = hbt.BacktestAsset()
    asset.data(feed)
    asset.tick_size(tick_size)
    asset.lot_size(lot_size)
    asset.constant_latency(0, 0)  # zero-latency for book-state matching
    asset.risk_adverse_queue_model()
    asset.roi_lb(roi_lb)
    asset.roi_ub(roi_ub)
    asset.no_partial_fill_exchange()
    bt = hbt.ROIVectorMarketDepthBacktest([asset])
    return bt


# ---------------------------------------------------------------------------
# Replay a tape through real hftbacktest, record TraceRecords
# ---------------------------------------------------------------------------


def real_hftbacktest_replay_book_multi(
    tape: Iterable[TapeEvent],
    *,
    n_symbols: int,
    tick_size_per_sym: list[float] | None = None,
    lot_size_per_sym: list[float] | None = None,
    roi_lb_ticks_per_sym: list[int] | None = None,
    roi_ub_ticks_per_sym: list[int] | None = None,
    tick_size: float = 0.01,
    lot_size: float = 1.0,
) -> list[TraceRecord]:
    """Multi-asset version: drive ONE shared tape through N hftbacktest
    BacktestAssets (one per symbol), recording per-symbol book-top per event.

    The single tape is partitioned by `sym_id` into N feeds (one per
    asset). hftbacktest's MultiAssetBacktest steps the merged event
    stream by global timestamp; on each `wait_next_feed`, the asset
    whose next event has the lowest timestamp advances. We then capture
    book-top for the active asset and emit (bid, ask) trace rows.

    Returns one (bid, ask) TraceRecord pair per tape event, with
    `sym_id` set to the event's asset.

    Per-symbol params (tick_size, lot_size, roi_lb_ticks, roi_ub_ticks)
    can be specified individually via the `*_per_sym` lists; if None,
    the scalar defaults apply to every symbol.
    """
    rows = list(tape)
    if not rows:
        return []

    if tick_size_per_sym is None:
        tick_size_per_sym = [tick_size] * n_symbols
    if lot_size_per_sym is None:
        lot_size_per_sym = [lot_size] * n_symbols
    if roi_lb_ticks_per_sym is None:
        roi_lb_ticks_per_sym = [0] * n_symbols
    if roi_ub_ticks_per_sym is None:
        roi_ub_ticks_per_sym = [1_000_000] * n_symbols
    assert len(tick_size_per_sym) == n_symbols
    assert len(lot_size_per_sym) == n_symbols

    # Partition rows by sym_id into per-symbol feeds (preserving order).
    rows_by_sym: list[list[TapeEvent]] = [[] for _ in range(n_symbols)]
    for ev in rows:
        if 0 <= ev.sym_id < n_symbols:
            rows_by_sym[ev.sym_id].append(ev)

    feeds: list[np.ndarray] = []
    for sid in range(n_symbols):
        feeds.append(tape_to_hbt_feed(
            rows_by_sym[sid],
            tick_size=tick_size_per_sym[sid],
            lot_size=lot_size_per_sym[sid],
        ))

    # Build N BacktestAssets, one per symbol.
    assets = []
    for sid in range(n_symbols):
        asset = hbt.BacktestAsset()
        asset.data(feeds[sid])
        asset.tick_size(tick_size_per_sym[sid])
        asset.lot_size(lot_size_per_sym[sid])
        asset.constant_latency(0, 0)
        asset.risk_adverse_queue_model()
        asset.roi_lb(roi_lb_ticks_per_sym[sid] * tick_size_per_sym[sid])
        asset.roi_ub(roi_ub_ticks_per_sym[sid] * tick_size_per_sym[sid])
        asset.no_partial_fill_exchange()
        assets.append(asset)
    bt = hbt.ROIVectorMarketDepthBacktest(assets)

    INVALID_TICK_HI = 1 << 62
    INVALID_TICK_LO = -(1 << 62)

    def _safe_qty(qty_raw: float, lot_size: float) -> int:
        import math
        if qty_raw is None or math.isnan(qty_raw) or math.isinf(qty_raw):
            return 0
        return int(qty_raw / lot_size)

    def _capture_pair(sid: int, ts: int) -> tuple[TraceRecord, TraceRecord]:
        depth = bt.depth(sid)
        bid_t = int(depth.best_bid_tick)
        ask_t = int(depth.best_ask_tick)
        bid_v = INVALID_TICK_LO < bid_t < INVALID_TICK_HI and bid_t > 0
        ask_v = INVALID_TICK_LO < ask_t < INVALID_TICK_HI and ask_t > 0
        bid_q = _safe_qty(depth.bid_qty_at_tick(bid_t), lot_size_per_sym[sid]) if bid_v else 0
        ask_q = _safe_qty(depth.ask_qty_at_tick(ask_t), lot_size_per_sym[sid]) if ask_v else 0
        return (
            TraceRecord(timestamp_ns=ts, action="book_top", side=0,
                        px_ticks=bid_t if (bid_v and bid_q > 0) else 0,
                        qty=bid_q, sym_id=sid),
            TraceRecord(timestamp_ns=ts, action="book_top", side=1,
                        px_ticks=ask_t if (ask_v and ask_q > 0) else 0,
                        qty=ask_q, sym_id=sid),
        )

    # Walk the original tape: each event corresponds to ONE event in the
    # asset's feed; advancing wait_next_feed processes the asset's next
    # event (which is what we sent). The capture is per-event, not
    # per-asset-snapshot. The per-symbol feeds preserve per-asset order,
    # and hftbacktest's multi-asset engine merges by timestamp - which
    # for our equal-timestamp single-event-per-step case is just the
    # original tape order.
    out: list[TraceRecord] = []
    timeout_ns = 60 * 1_000_000_000
    for ev in rows:
        ret = bt.wait_next_feed(False, timeout_ns)
        if ret not in (1, 2):
            raise RuntimeError(
                f"hftbacktest wait_next_feed returned {ret} on multi-tape "
                f"ts={ev.timestamp_ns} sym={ev.sym_id}"
            )
        bid_rec, ask_rec = _capture_pair(ev.sym_id, ev.timestamp_ns)
        out.append(bid_rec)
        out.append(ask_rec)
        if ret == 1:
            break
    bt.close()
    return out


def real_hftbacktest_replay_book(
    tape: Iterable[TapeEvent],
    *,
    tick_size: float = 0.01,
    lot_size: float = 1.0,
    roi_lb_ticks: int = 0,
    roi_ub_ticks: int = 1_000_000,
) -> list[TraceRecord]:
    """Drive a tape through hftbacktest 2.3.0 with ROIVectorMarketDepth.

    Returns one TraceRecord(action='book_top') per side per event.
    bid first, ask second (matches sota_mirror_replay_book).
    """
    rows = list(tape)
    if not rows:
        return []
    feed = tape_to_hbt_feed(rows, tick_size=tick_size, lot_size=lot_size)

    bt = build_roivec_backtest_from_feed(
        feed,
        tick_size=tick_size, lot_size=lot_size,
        roi_lb=roi_lb_ticks * tick_size,
        roi_ub=roi_ub_ticks * tick_size,
    )

    # When book is empty on a side, hftbacktest returns INVALID_MAX/MIN
    # ticks and NaN qty. Guard with sane bounds + NaN check.
    INVALID_TICK_HI = 1 << 62
    INVALID_TICK_LO = -(1 << 62)

    def _safe_qty(qty_raw: float) -> int:
        import math
        if qty_raw is None or math.isnan(qty_raw) or math.isinf(qty_raw):
            return 0
        return int(qty_raw / lot_size)

    # `wait_next_feed` is the right primitive: returns when the engine has
    # processed the next market-feed event. Returns 2 on event, 1 on EOF.
    # Since correct_event_order emits one row per input when local_ts ==
    # exch_ts, feed length == tape length, and we record one trace pair
    # per tape row.
    out: list[TraceRecord] = []
    timeout_ns = 60 * 1_000_000_000  # 60-sec sim-time cap per event
    for ev in rows:
        ret = bt.wait_next_feed(False, timeout_ns)
        if ret not in (1, 2):
            # 0 = timeout (shouldn't happen on synthetic tape); other = error
            raise RuntimeError(
                f"hftbacktest wait_next_feed returned {ret} on tape ev "
                f"ts={ev.timestamp_ns} sym={ev.sym_id}"
            )
        depth = bt.depth(0)
        bid_tick_raw = int(depth.best_bid_tick)
        ask_tick_raw = int(depth.best_ask_tick)
        bid_valid = INVALID_TICK_LO < bid_tick_raw < INVALID_TICK_HI and bid_tick_raw > 0
        ask_valid = INVALID_TICK_LO < ask_tick_raw < INVALID_TICK_HI and ask_tick_raw > 0
        bid_qty = _safe_qty(depth.bid_qty_at_tick(bid_tick_raw)) if bid_valid else 0
        ask_qty = _safe_qty(depth.ask_qty_at_tick(ask_tick_raw)) if ask_valid else 0
        out.append(TraceRecord(
            timestamp_ns=ev.timestamp_ns, action="book_top", side=0,
            px_ticks=bid_tick_raw if (bid_valid and bid_qty > 0) else 0,
            qty=bid_qty, sym_id=ev.sym_id,
        ))
        out.append(TraceRecord(
            timestamp_ns=ev.timestamp_ns, action="book_top", side=1,
            px_ticks=ask_tick_raw if (ask_valid and ask_qty > 0) else 0,
            qty=ask_qty, sym_id=ev.sym_id,
        ))
        if ret == 1:
            break
    bt.close()
    return out
