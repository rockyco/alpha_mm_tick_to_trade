"""NASDAQ ITCH 1.x ASCII format loader (v1.28.43).

Parses NASDAQ's legacy ITCH-1 ASCII feed format (e.g., the historical
S010303-v2 sample from 2003-01-03 hosted at emi.nasdaq.com). Extracts
per-(symbol, side, price) absolute-qty depth updates suitable for
the framework's M0 + hftbacktest cross-validation.

ITCH-1 ASCII format (per-line message, fixed-width columns):
  Pos 0-7 (8 chars):  timestamp in ms since midnight
  Pos 8 (1 char):     message type (A, X, E, D, U, P, S, ...)
  Pos 9+:             type-specific payload

  Type 'A' (Add Order, 42 cols):
    pos 9-17 (9):  order_ref       (left-padded)
    pos 18 (1):    side ('B' bid, 'S' ask)
    pos 19-24 (6): shares
    pos 25-32 (8): symbol           (left-aligned, space-padded)
    pos 33-40 (8): price (in 0.0001 dollars)
    pos 41 (1):    display indicator
  Type 'X' (Cancel, 25 cols):  order_ref(9) + shares(6)  -> pos 9-17, 19-24
  Type 'E' (Execute, 33 cols): order_ref(9) + shares(6) + match_num(8) -> 9-17, 19-24
  Type 'D' (Delete, 18 cols):  order_ref(9)  -> pos 9-17
  Type 'U' (Replace):          old_ref(9) + new_ref(9) + shares(6) + price(8)

The loader maintains per-order state (price + side + remaining shares)
so cancel/execute/delete can subtract the right qty from the right
(symbol, side, price) level. Output: TapeEvent stream sorted by ts.

Format reference: NASDAQ ITCH 1.x specification (legacy, 2003-2009).
Source data: https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/S010303-v2.zip
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterator

from xcheck.lobster_tape_loader import TapeEvent


def _parse_int(s: str) -> int:
    s = s.strip()
    return int(s) if s else 0


def _parse_ts_ms(line: str) -> int:
    return int(line[0:8])


def load_nasdaq_itch1_depth_events(
    path: Path | str,
    *,
    sym_id_for: dict[str, int] | None = None,
    include_symbols: set[str] | None = None,
    max_lines: int | None = None,
) -> Iterator[TapeEvent]:
    """Yield depth-update TapeEvents from a NASDAQ ITCH-1 ASCII file.

    `sym_id_for`: maps NASDAQ symbol string -> framework sym_id. Symbols
    not in the map are dropped. If None, the loader auto-builds the
    mapping in order-of-appearance up to a cap.

    `include_symbols`: if set, only emit events for symbols in this set.

    `max_lines`: optional cap on input lines (for sampling).
    """
    p = Path(path)
    auto_assign = sym_id_for is None
    if auto_assign:
        sym_id_for = {}
    next_id = max(sym_id_for.values()) + 1 if sym_id_for else 0

    # Per-order state: order_ref -> (sym, side, price_ticks, remaining_shares)
    orders: dict[str, tuple[str, int, int, int]] = {}
    # Per-(sym, side, price) running level qty
    levels: dict[tuple[str, int, int], int] = defaultdict(int)

    n = 0
    with p.open("r") as f:
        for line in f:
            n += 1
            if max_lines is not None and n > max_lines:
                return
            line = line.rstrip("\n")
            if len(line) < 9:
                continue
            ts_ms = _parse_ts_ms(line)
            ts_ns = ts_ms * 1_000_000
            mt = line[8]

            if mt == 'A' and len(line) >= 42:
                # Add Order: order_ref(9) side(1) shares(6) symbol(8) price(8) display(1)
                order_ref = line[9:18].strip()
                side_c    = line[18]
                shares    = _parse_int(line[19:25])
                symbol    = line[25:33].strip()
                price     = _parse_int(line[33:41])
                if include_symbols is not None and symbol not in include_symbols:
                    continue
                if symbol not in sym_id_for:
                    if not auto_assign:
                        continue
                    sym_id_for[symbol] = next_id
                    next_id += 1
                sid = sym_id_for[symbol]
                side = 0 if side_c == 'B' else 1
                orders[order_ref] = (symbol, side, price, shares)
                k = (symbol, side, price)
                levels[k] += shares
                yield TapeEvent(
                    timestamp_ns=ts_ns, sym_id=sid, op=1, side=side,
                    price_ticks=price, new_abs_qty=levels[k],
                )

            elif mt in ('X', 'E') and len(line) >= 24:
                # Cancel/Execute: order_ref(9) shares(6) [+ match_num(8) for E].
                # NO side field after order_ref (unlike 'A'); shares is at pos 18.
                order_ref = line[9:18].strip()
                shares    = _parse_int(line[18:24])
                meta = orders.get(order_ref)
                if not meta:
                    continue
                symbol, side, price, remaining = meta
                if include_symbols is not None and symbol not in include_symbols:
                    continue
                if symbol not in sym_id_for:
                    continue
                sid = sym_id_for[symbol]
                new_remaining = max(0, remaining - shares)
                if new_remaining == 0:
                    orders.pop(order_ref, None)
                else:
                    orders[order_ref] = (symbol, side, price, new_remaining)
                k = (symbol, side, price)
                levels[k] = max(0, levels[k] - shares)
                op = 1 if levels[k] > 0 else 2
                yield TapeEvent(
                    timestamp_ns=ts_ns, sym_id=sid, op=op, side=side,
                    price_ticks=price, new_abs_qty=levels[k],
                )

            elif mt == 'D' and len(line) >= 18:
                order_ref = line[9:18].strip()
                meta = orders.pop(order_ref, None)
                if not meta:
                    continue
                symbol, side, price, remaining = meta
                if include_symbols is not None and symbol not in include_symbols:
                    continue
                if symbol not in sym_id_for:
                    continue
                sid = sym_id_for[symbol]
                k = (symbol, side, price)
                levels[k] = max(0, levels[k] - remaining)
                op = 1 if levels[k] > 0 else 2
                yield TapeEvent(
                    timestamp_ns=ts_ns, sym_id=sid, op=op, side=side,
                    price_ticks=price, new_abs_qty=levels[k],
                )

            elif mt == 'U' and len(line) >= 41:
                # Replace: old_ref(9) new_ref(9) shares(6) price(8)
                old_ref   = line[9:18].strip()
                new_ref   = line[18:27].strip()
                shares    = _parse_int(line[27:33])
                new_price = _parse_int(line[33:41])
                meta = orders.pop(old_ref, None)
                if not meta:
                    continue
                symbol, side, old_price, _ = meta
                if include_symbols is not None and symbol not in include_symbols:
                    continue
                if symbol not in sym_id_for:
                    continue
                sid = sym_id_for[symbol]
                # First emit a remove at the old price level
                k_old = (symbol, side, old_price)
                levels[k_old] = max(0, levels[k_old] - meta[3])
                op_old = 1 if levels[k_old] > 0 else 2
                yield TapeEvent(
                    timestamp_ns=ts_ns, sym_id=sid, op=op_old, side=side,
                    price_ticks=old_price, new_abs_qty=levels[k_old],
                )
                # Then add at the new price level
                orders[new_ref] = (symbol, side, new_price, shares)
                k_new = (symbol, side, new_price)
                levels[k_new] += shares
                yield TapeEvent(
                    timestamp_ns=ts_ns, sym_id=sid, op=1, side=side,
                    price_ticks=new_price, new_abs_qty=levels[k_new],
                )


def top_active_symbols(
    path: Path | str,
    n_top: int = 8,
    max_lines: int = 2_000_000,
) -> list[str]:
    """Scan the ITCH-1 file, count Add-message frequency per symbol,
    return the N most-active symbols. Used to pick the multi-symbol
    cross-val targets without prior knowledge of what's in the tape."""
    counts: dict[str, int] = {}
    n = 0
    with Path(path).open("r") as f:
        for line in f:
            n += 1
            if n > max_lines:
                break
            if len(line) < 33 or line[8] != 'A':
                continue
            sym = line[25:33].strip()
            if sym:
                counts[sym] = counts.get(sym, 0) + 1
    return [s for s, _ in sorted(counts.items(), key=lambda x: -x[1])[:n_top]]
