"""Real Binance SPOT depthUpdate WebSocket-capture loader (v1.28.40).

Parses the format hftbacktest publishes in
https://github.com/nkaz001/hftbacktest/tree/master/examples/spot
(each line: `<local_ts_ns> <combined_stream_json>`).

The JSON `data.e == "depthUpdate"` payload contains:
  E: event-time (ms)
  s: symbol
  U: first update ID
  u: final update ID
  b: bid level updates [(price, qty), ...]
  a: ask level updates [(price, qty), ...]

For our M0 cross-validation we only care about (price, qty, side, ts).
qty == 0 means "remove this level". Each level update emits ONE TapeEvent
in the framework's canonical schema (which mirrors hftbacktest's depth
absolute-qty semantics).

The Binance spot format does NOT include T (trade-time) or pu (prev
update). hftbacktest's `binancefutures.convert` requires those, so it
can't parse this format - hence this loader.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Iterator

from xcheck.lobster_tape_loader import TapeEvent


def _open(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt")
    return path.open("r")


def load_binance_spot_depth(
    path: Path | str,
    sym_id: int = 0,
    *,
    tick_size: float = 0.01,
    lot_size: float = 1e-8,
    max_events: int | None = None,
) -> Iterator[TapeEvent]:
    """Parse a Binance SPOT depthUpdate capture; yield TapeEvents.

    `tick_size` is the price quantization (Binance BTCUSDT spot uses
    $0.01). `lot_size` is the qty quantization (BTCUSDT is 1e-8 BTC =
    1 satoshi).

    px_ticks = round(price_dollars / tick_size).
    new_abs_qty = round(qty_coins / lot_size).
    """
    p = Path(path)
    n = 0
    with _open(p) as f:
        for line in f:
            try:
                local_ts_str, body = line.strip().split(" ", 1)
                local_ts_ns = int(local_ts_str)
                obj = json.loads(body)
            except (ValueError, json.JSONDecodeError):
                continue
            data = obj.get("data") if isinstance(obj, dict) else obj
            if not isinstance(data, dict) or data.get("e") != "depthUpdate":
                continue
            try:
                exch_ts_ms = int(data["E"])
            except (KeyError, ValueError, TypeError):
                continue
            exch_ts_ns = exch_ts_ms * 1_000_000
            ts = max(exch_ts_ns, local_ts_ns)  # use later of the two

            for side, levels in (("b", data.get("b", [])), ("a", data.get("a", []))):
                side_int = 0 if side == "b" else 1
                for lvl in levels:
                    try:
                        price = float(lvl[0])
                        qty   = float(lvl[1])
                    except (IndexError, ValueError, TypeError):
                        continue
                    px_ticks = int(round(price / tick_size))
                    abs_qty  = int(round(qty / lot_size))
                    op = 1 if abs_qty > 0 else 2
                    yield TapeEvent(
                        timestamp_ns=ts, sym_id=sym_id, op=op,
                        side=side_int, price_ticks=px_ticks,
                        new_abs_qty=abs_qty,
                    )
                    n += 1
                    if max_events is not None and n >= max_events:
                        return


def merge_multi_asset(
    sources: list[tuple[int, Path | str, float, float]],
    max_events_per_sym: int | None = None,
) -> list[TapeEvent]:
    """Merge multiple per-asset capture files into a single time-sorted tape.

    `sources`: list of (sym_id, path, tick_size, lot_size).
    Returns a single list of TapeEvents sorted by timestamp_ns.

    Re-timestamps each event with strictly-monotone ns values so
    hftbacktest's `wait_next_feed` advances exactly one event per
    call. The original timestamp ordering is preserved (sort by
    original ts first, then re-stamp).
    """
    import dataclasses as _dc
    all_events: list[TapeEvent] = []
    for sym_id, path, tick, lot in sources:
        for ev in load_binance_spot_depth(
            path, sym_id=sym_id, tick_size=tick, lot_size=lot,
            max_events=max_events_per_sym,
        ):
            all_events.append(ev)
    all_events.sort(key=lambda e: e.timestamp_ns)
    # Re-stamp: monotonically increasing by 1 ns per event (preserves order
    # so hftbacktest processes one event per wait_next_feed call).
    if all_events:
        base = all_events[0].timestamp_ns
        all_events = [
            _dc.replace(ev, timestamp_ns=base + i)
            for i, ev in enumerate(all_events)
        ]
    return all_events
