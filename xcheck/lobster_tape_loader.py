"""LOBSTER market-data tape loader (Phase 9 cross-validation, v1.28.35).

LOBSTER (Limit Order Book System - The Efficient Reconstructor) publishes
free historical NASDAQ TotalView-ITCH event data at https://lobsterdata.com/.
Their format is a CSV pair per ticker per day:

  <ticker>_<date>_<time1>_<time2>_message_<level>.csv  -- one event per line
  <ticker>_<date>_<time1>_<time2>_orderbook_<level>.csv -- L<level> snapshot per event

Message file columns (1-indexed):
  1. Time      (seconds since midnight, fractional)
  2. Type      (1=submission, 2=cancellation, 3=deletion, 4=execution-visible,
                5=execution-hidden, 6=cross-trade, 7=trading-halt)
  3. Order ID
  4. Size      (number of shares)
  5. Price     (in $0.0001 = 1/10000 of a dollar)
  6. Direction (1=buy, -1=sell)

The orderbook file has 4 columns per level: ask_px, ask_sz, bid_px, bid_sz.
We don't need the orderbook file for cross-validation — the message file is
the canonical event stream. The orderbook can be reconstructed from messages
(which IS what hftbacktest does and what our framework's M0 does).

This loader emits TapeEvent records compatible with the synthetic-tape
schema in synth_lob_tape.py. Cross-validation harnesses can then drive
the same events through the framework's math/pymodel/RTL layers AND
through hftbacktest's reference (when installed).

LOBSTER format reference: https://lobsterdata.com/info/DataStructure.php
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


# LOBSTER event type codes
LOBSTER_TYPE_SUBMIT      = 1  # New limit order submission
LOBSTER_TYPE_CANCEL      = 2  # Partial cancellation (qty reduced)
LOBSTER_TYPE_DELETE      = 3  # Total deletion (qty -> 0)
LOBSTER_TYPE_EXEC_VIS    = 4  # Execution of a visible order
LOBSTER_TYPE_EXEC_HID    = 5  # Execution of a hidden (iceberg) order
LOBSTER_TYPE_CROSS       = 6  # Cross trade (auction)
LOBSTER_TYPE_HALT        = 7  # Trading halt indicator

# LOBSTER direction codes
LOBSTER_DIR_BUY  =  1
LOBSTER_DIR_SELL = -1

# Map LOBSTER → framework op codes (matches priority_array_k_packed):
#   op_i = 0  NOP
#   op_i = 1  INSERT/UPDATE (size at price level set to new value)
#   op_i = 2  REMOVE (size at price level cleared)
LOBSTER_TO_FRAMEWORK_OP = {
    LOBSTER_TYPE_SUBMIT:   1,  # new limit order -> insert at price
    LOBSTER_TYPE_CANCEL:   1,  # cancel reduces size -> insert with new size
    LOBSTER_TYPE_DELETE:   2,  # full delete -> remove
    LOBSTER_TYPE_EXEC_VIS: 1,  # execution shrinks visible qty -> insert with new
    LOBSTER_TYPE_EXEC_HID: 1,  # hidden execution: book unchanged from M0 view
    LOBSTER_TYPE_CROSS:    0,  # cross-trade not in continuous book
    LOBSTER_TYPE_HALT:     0,  # halt -> no book change
}


@dataclass(frozen=True)
class LobsterEvent:
    """One row of a LOBSTER message file."""
    timestamp_ns: int      # nanoseconds since midnight (LOBSTER time × 1e9)
    type: int              # LOBSTER event type (1..7)
    order_id: int
    size: int              # shares (LOBSTER 'Size' column)
    price_ticks: int       # price in $0.0001 ticks (LOBSTER 'Price' column)
    side: int              # 0 = bid, 1 = ask  (mapped from LOBSTER direction)


@dataclass(frozen=True)
class TapeEvent:
    """Framework-canonical event format (matches synth_lob_tape.py schema).

    The cross-validation harness drives one of these per cycle (or per
    valid_i pulse) into the framework's math/pymodel/RTL layers.
    """
    timestamp_ns: int
    sym_id: int            # in single-symbol replay this is constant 0
    op: int                # 0=nop, 1=insert/update, 2=remove
    side: int              # 0=bid, 1=ask
    price_ticks: int       # raw integer price (no scaling)
    new_abs_qty: int       # absolute qty at this price level after the event


def parse_lobster_message_row(row: list[str]) -> LobsterEvent | None:
    """Parse one row of a LOBSTER message file. Returns None if malformed."""
    if len(row) < 6:
        return None
    try:
        time_s = float(row[0])
        ev_type = int(row[1])
        order_id = int(row[2])
        size = int(row[3])
        price_ticks = int(row[4])
        direction = int(row[5])
    except (ValueError, IndexError):
        return None
    side = 0 if direction == LOBSTER_DIR_BUY else 1
    return LobsterEvent(
        timestamp_ns=int(time_s * 1_000_000_000),
        type=ev_type,
        order_id=order_id,
        size=size,
        price_ticks=price_ticks,
        side=side,
    )


def load_lobster_messages(path: Path | str) -> Iterator[LobsterEvent]:
    """Yield LobsterEvent rows from a LOBSTER message CSV file."""
    p = Path(path)
    with p.open() as f:
        reader = csv.reader(f)
        for row in reader:
            ev = parse_lobster_message_row(row)
            if ev is not None:
                yield ev


def lobster_to_framework_events(
    lob_events: Iterator[LobsterEvent],
    sym_id: int = 0,
    price_anchor: int | None = None,
    track_qty: dict[tuple[int, int, int], int] | None = None,
    drop_unknown: bool = True,
) -> Iterator[TapeEvent]:
    """Translate a LOBSTER message stream into framework TapeEvents.

    LOBSTER reports per-order messages (submit / cancel / delete / execute);
    the framework's M0 expects per-price-level absolute qty updates. We
    aggregate by (sym, side, price_tick) -> running qty:

      submit (size=S):       qty[level] += S
      cancel (size=S):       qty[level] -= S (partial)
      delete:                qty[level] -= order's full size (full)
      exec_vis (size=S):     qty[level] -= S
      exec_hid:              no change (hidden, M0 doesn't see)

    The LOBSTER file doesn't repeat order sizes on cancel/delete/exec, so
    we maintain an order_id -> size map to know how much to subtract.

    `price_anchor` is the ROI base tick used for the framework's
    `roi_offset = price_tick - price_anchor`. If None, computed as the
    first event's price.

    Yields one TapeEvent per LOBSTER event that maps to a framework op
    (op != 0). When `drop_unknown=True`, halt/cross events are dropped
    silently; otherwise they yield op=0 nops.
    """
    if track_qty is None:
        track_qty = {}
    order_size: dict[int, int] = {}  # order_id -> current outstanding size

    for ev in lob_events:
        if price_anchor is None:
            price_anchor = ev.price_ticks  # latch first event as anchor
        op = LOBSTER_TO_FRAMEWORK_OP.get(ev.type, 0)
        if op == 0:
            if drop_unknown:
                continue
            yield TapeEvent(
                timestamp_ns=ev.timestamp_ns, sym_id=sym_id, op=0,
                side=ev.side, price_ticks=ev.price_ticks, new_abs_qty=0,
            )
            continue

        level_key = (sym_id, ev.side, ev.price_ticks)
        # Update level qty based on event type
        if ev.type == LOBSTER_TYPE_SUBMIT:
            track_qty[level_key] = track_qty.get(level_key, 0) + ev.size
            order_size[ev.order_id] = ev.size
        elif ev.type == LOBSTER_TYPE_CANCEL:
            track_qty[level_key] = max(0, track_qty.get(level_key, 0) - ev.size)
            order_size[ev.order_id] = max(0, order_size.get(ev.order_id, 0) - ev.size)
        elif ev.type == LOBSTER_TYPE_DELETE:
            outstanding = order_size.pop(ev.order_id, ev.size)
            track_qty[level_key] = max(0, track_qty.get(level_key, 0) - outstanding)
        elif ev.type == LOBSTER_TYPE_EXEC_VIS:
            track_qty[level_key] = max(0, track_qty.get(level_key, 0) - ev.size)
            order_size[ev.order_id] = max(0, order_size.get(ev.order_id, 0) - ev.size)
        elif ev.type == LOBSTER_TYPE_EXEC_HID:
            # Hidden execution -> M0 sees no change (still emit nop for trace alignment)
            yield TapeEvent(
                timestamp_ns=ev.timestamp_ns, sym_id=sym_id, op=0,
                side=ev.side, price_ticks=ev.price_ticks, new_abs_qty=0,
            )
            continue

        new_qty = track_qty[level_key]
        # Map level qty == 0 to op=2 (remove); else op=1 (insert/update)
        emit_op = 2 if new_qty == 0 else 1
        yield TapeEvent(
            timestamp_ns=ev.timestamp_ns,
            sym_id=sym_id,
            op=emit_op,
            side=ev.side,
            price_ticks=ev.price_ticks,
            new_abs_qty=new_qty,
        )


def lobster_path_for(
    ticker: str, date: str, time1: str = "34200000", time2: str = "57600000",
    level: int = 10, root: Path | str = ".",
) -> Path:
    """Construct the canonical LOBSTER file name for `ticker_date_..._message_LV.csv`.

    Default time1=34200000 (= 9:30am in milliseconds), time2=57600000 (= 4:00pm).
    These are LOBSTER's standard NYSE/NASDAQ trading-hours snapshot.
    """
    fname = f"{ticker}_{date}_{time1}_{time2}_message_{level}.csv"
    return Path(root) / fname
