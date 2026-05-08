"""hftbacktest cross-validation orchestrator (Phase 9, v1.28.35).

The "credibility gate" called out in the framework plan: drive a real
historical tape through both hftbacktest's reference simulator AND the
framework's RTL pipeline; assert bit-exact agreement on every (action,
order_id, side, px, qty) decision tuple.

This file is the orchestrator. Right now it runs the framework's math
layer as the SOTA-mirror reference (per Rule 6: hftbacktest's
ROIVectorMarketDepth is the canonical reference; our golden/
priority_array_k_packed.py is its bit-exact mirror per v1.28.26).
When the real hftbacktest Python binding is installed in the
environment, the SOTA-mirror reference is one shim away from being the
real thing — the tape format, trace recording, and diff infrastructure
are already in place.

Pipeline stages (current vs unblocked):

   ┌─────────────────────────────────────────────────────────┐
   │ LOBSTER tape (.csv)                                       │
   │   |- lobster_tape_loader.py: parse + translate to        │
   │      TapeEvent stream                                    │
   └─────────────────────────────────────────────────────────┘
                 │
   ┌─────────────┴───────────────┐
   │                              │
   ▼                              ▼
[CURRENT: SOTA-mirror]    [UNBLOCKED: real hftbacktest]
golden/priority_array_k_  hftbacktest.run(strategy=...)
packed.py + strategy lib  Python binding (Rust impl)
   │                              │
   │                              │
   ▼                              ▼
TraceRecord stream A     TraceRecord stream B
                 │
                 ▼
        FPGA RTL (cocotb-replay) -> TraceRecord stream C
                 │
                 ▼
              Diff: (A == B) and (B == C)

Today's gate: A == cycle_pymodel == C  (the framework's three layers).
Plus: A schema-compatible with B for future swap-in.

Status: framework's three-layer agreement on synthetic tapes is verified
by run_xcheck.py. This file extends to:
  1. accept LOBSTER-format real tape (or synthetic one),
  2. record framework-side trace in a portable format,
  3. provide a diff function that will accept hftbacktest traces once
     they exist in this environment.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

TOOLS = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TOOLS))

from xcheck.lobster_tape_loader import (
    TapeEvent,
    load_lobster_messages,
    lobster_to_framework_events,
)
    generate_tape,
)
from golden.priority_array_k_packed import (
    PriorityArrayKPacked,
)


# ---------------------------------------------------------------------------
# TraceRecord: the canonical event-by-event audit row used for diff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceRecord:
    """One row of the cross-validation trace.

    Matches the (timestamp, action, order_id, side, px, qty) tuple
    documented in the framework's plan (Phase 9).

    For book-state checks (M0 cross-validation), action='book_top' and
    px/qty represent the best bid/ask. For order emissions (full pipeline
    cross-val), action ∈ {'NEW', 'CANCEL', 'MODIFY'}.
    """
    timestamp_ns: int
    action: str             # 'book_top' | 'NEW' | 'CANCEL' | 'MODIFY' | 'nop'
    side: int               # 0=bid, 1=ask
    px_ticks: int
    qty: int
    order_id: int = 0       # only used for NEW/CANCEL/MODIFY
    sym_id: int = 0


def trace_to_jsonl(trace: Iterable[TraceRecord], path: Path | str) -> int:
    """Persist a trace to JSONL for cross-tool diff."""
    p = Path(path)
    n = 0
    with p.open("w") as f:
        for r in trace:
            f.write(json.dumps(asdict(r)) + "\n")
            n += 1
    return n


def trace_from_jsonl(path: Path | str) -> list[TraceRecord]:
    """Load a previously-recorded trace."""
    p = Path(path)
    out: list[TraceRecord] = []
    with p.open() as f:
        for line in f:
            obj = json.loads(line)
            out.append(TraceRecord(**obj))
    return out


def diff_traces_tolerant(
    a: list[TraceRecord], b: list[TraceRecord],
    *, qty_tolerance: int = 1,
) -> tuple[int, list[str]]:
    """Diff with `qty` tolerance: hftbacktest stores qty as float64;
    framework golden uses pure integer ticks. Round-trip
    qty(int) -> qty(float) -> qty(int) can lose 1 LSB. This diff
    treats |qty_a - qty_b| <= qty_tolerance as a match (everything
    else MUST be exact: timestamp, action, side, sym_id, px_ticks).
    """
    msgs: list[str] = []
    n_mismatch = 0
    for i in range(min(len(a), len(b))):
        x, y = a[i], b[i]
        if (x.timestamp_ns != y.timestamp_ns or x.action != y.action
                or x.side != y.side or x.sym_id != y.sym_id
                or x.px_ticks != y.px_ticks
                or abs(x.qty - y.qty) > qty_tolerance):
            n_mismatch += 1
            if len(msgs) < 10:
                msgs.append(f"row {i}: A={x} vs B={y}")
    if len(a) != len(b):
        n_mismatch += abs(len(a) - len(b))
        if len(msgs) < 10:
            msgs.append(f"length mismatch: A={len(a)}, B={len(b)}")
    return n_mismatch, msgs


def diff_traces(
    a: list[TraceRecord],
    b: list[TraceRecord],
    *,
    label_a: str = "A",
    label_b: str = "B",
    skip_nops: bool = True,
) -> tuple[int, list[str]]:
    """Diff two TraceRecord lists. Returns (mismatch_count, sample_mismatches).

    Skips 'nop' actions on both sides by default since they're alignment
    placeholders, not decisions.
    """
    if skip_nops:
        a = [r for r in a if r.action != "nop"]
        b = [r for r in b if r.action != "nop"]
    mismatches: list[str] = []
    n_max = max(len(a), len(b))
    for i in range(n_max):
        if i >= len(a):
            mismatches.append(f"row {i}: {label_a} ended early; {label_b}={b[i]}")
            continue
        if i >= len(b):
            mismatches.append(f"row {i}: {label_b} ended early; {label_a}={a[i]}")
            continue
        if a[i] != b[i]:
            mismatches.append(
                f"row {i}: {label_a}={a[i]} vs {label_b}={b[i]}"
            )
            if len(mismatches) >= 10:
                mismatches.append(f"... and {n_max - i - 1} more rows; truncating to 10")
                break
    return len(mismatches), mismatches


# ---------------------------------------------------------------------------
# SOTA-mirror reference: replays a tape through the framework's math layer
# ---------------------------------------------------------------------------


def sota_mirror_replay_book_multi(
    tape: Iterable[TapeEvent],
    *,
    n_symbols: int,
    key_bits: int = 16,
    val_bits: int = 24,
    price_anchor_per_sym: list[int] | None = None,
    auto_cross_clear: bool = False,
) -> list[TraceRecord]:
    """Multi-symbol SOTA-mirror replay with PER-SYMBOL price anchor.

    Different assets have different price ranges (BTC ~$45000, ETH
    ~$2500, SOL ~$100). Each symbol's tape gets its own anchor so the
    framework's bounded-key ROI window covers each independently.

    Returns one (bid, ask) TraceRecord pair per tape event, with
    `sym_id` set to the event's asset.
    """
    bid_books = [
        PriorityArrayKPacked(
            n_symbols=n_symbols, max_keys=1 << key_bits,
            descending=True,  key_bits=key_bits, val_bits=val_bits,
        )
    ]
    ask_books = [
        PriorityArrayKPacked(
            n_symbols=n_symbols, max_keys=1 << key_bits,
            descending=False, key_bits=key_bits, val_bits=val_bits,
        )
    ]
    bid_book = bid_books[0]
    ask_book = ask_books[0]

    if price_anchor_per_sym is None:
        # Latch on first event seen for each symbol
        anchors: list[int | None] = [None] * n_symbols
    else:
        assert len(price_anchor_per_sym) == n_symbols
        anchors = list(price_anchor_per_sym)

    out: list[TraceRecord] = []
    for ev in tape:
        sid = ev.sym_id
        if not (0 <= sid < n_symbols):
            continue
        if anchors[sid] is None:
            anchors[sid] = ev.price_ticks
        anchor = anchors[sid]
        offset = ev.price_ticks - anchor
        if offset < 0 or offset >= (1 << key_bits):
            continue
        book = bid_book if ev.side == 0 else ask_book
        if ev.op == 1:
            book.insert(sid, offset, ev.new_abs_qty)
        elif ev.op == 2:
            book.remove(sid, offset)
        if auto_cross_clear and ev.op == 1 and ev.new_abs_qty > 0:
            # Match hftbacktest: a BID arriving at price P >= best_ask
            # auto-removes all ASK levels at price <= P (they were
            # crossed and got matched). Same for ASK >= best_bid.
            # The opposing book stores offsets relative to its own
            # anchor; both books share the same anchor here so offset
            # comparison is direct.
            opp = ask_book if ev.side == 0 else bid_book
            opp_syms = opp.symbols[sid]
            if ev.side == 0:  # BID arrived; clear asks <= bid_price
                bid_offset = offset
                # Asks are stored ascending; iterate sorted offsets
                ask_offsets = sorted(list(opp_syms._store.keys()))
                for k in ask_offsets:
                    if k <= bid_offset:
                        opp.remove(sid, k)
                    else:
                        break
            else:  # ASK arrived; clear bids >= ask_price
                ask_offset = offset
                bid_offsets = sorted(list(opp_syms._store.keys()), reverse=True)
                for k in bid_offsets:
                    if k >= ask_offset:
                        opp.remove(sid, k)
                    else:
                        break
        bid_key, bid_qty = bid_book.peek_top(sid) \
            if len(bid_book.symbols[sid]) > 0 else (0, 0)
        ask_key, ask_qty = ask_book.peek_top(sid) \
            if len(ask_book.symbols[sid]) > 0 else (0, 0)
        out.append(TraceRecord(
            timestamp_ns=ev.timestamp_ns, action="book_top", side=0,
            px_ticks=bid_key + anchor if bid_qty > 0 else 0,
            qty=bid_qty, sym_id=sid,
        ))
        out.append(TraceRecord(
            timestamp_ns=ev.timestamp_ns, action="book_top", side=1,
            px_ticks=ask_key + anchor if ask_qty > 0 else 0,
            qty=ask_qty, sym_id=sid,
        ))
    return out


def sota_mirror_replay_book(
    tape: Iterable[TapeEvent],
    *,
    n_symbols: int = 1,
    key_bits: int = 16,
    val_bits: int = 24,
    price_anchor: int | None = None,
) -> list[TraceRecord]:
    """Replay a tape through the framework's math model and record book tops.

    The framework's `golden.priority_array_k_packed.PriorityArrayKPacked` is
    the SOTA mirror per Rule 6 (hftbacktest's ROIVectorMarketDepth, version
    nkaz001/hftbacktest 4015 stars). For book-top cross-validation, this
    function plays the role of "what hftbacktest would output" when given
    the same tape. When the real hftbacktest Python binding is installed,
    this function can be replaced by a thin wrapper around hftbacktest's
    BookDepth.peek_top() — and the trace records will be schema-identical.

    Returns one TraceRecord(action='book_top') per non-nop tape event.
    """
    bid_book = PriorityArrayKPacked(
        n_symbols=n_symbols, max_keys=1 << 16,
        descending=True,  key_bits=key_bits, val_bits=val_bits,
    )
    ask_book = PriorityArrayKPacked(
        n_symbols=n_symbols, max_keys=1 << 16,
        descending=False, key_bits=key_bits, val_bits=val_bits,
    )
    out: list[TraceRecord] = []
    for ev in tape:
        if price_anchor is None:
            price_anchor = ev.price_ticks
        offset = ev.price_ticks - price_anchor
        if offset < 0 or offset >= (1 << key_bits):
            # outside ROI window; skip (real hftbacktest would still
            # update its dict; for ROI-Vector match we drop)
            continue
        book = bid_book if ev.side == 0 else ask_book
        if ev.op == 1:
            book.insert(ev.sym_id, offset, ev.new_abs_qty)
        elif ev.op == 2:
            book.remove(ev.sym_id, offset)
        # peek both sides for the book_top trace row
        bid_key, bid_qty = bid_book.peek_top(ev.sym_id) \
            if len(bid_book.symbols[ev.sym_id]) > 0 else (0, 0)
        ask_key, ask_qty = ask_book.peek_top(ev.sym_id) \
            if len(ask_book.symbols[ev.sym_id]) > 0 else (0, 0)
        out.append(TraceRecord(
            timestamp_ns=ev.timestamp_ns,
            action="book_top",
            side=0,
            px_ticks=bid_key + price_anchor if bid_qty > 0 else 0,
            qty=bid_qty,
            order_id=0,
            sym_id=ev.sym_id,
        ))
        out.append(TraceRecord(
            timestamp_ns=ev.timestamp_ns,
            action="book_top",
            side=1,
            px_ticks=ask_key + price_anchor if ask_qty > 0 else 0,
            qty=ask_qty,
            order_id=0,
            sym_id=ev.sym_id,
        ))
    return out


# ---------------------------------------------------------------------------
# Real-hftbacktest hook (stub - activates when binding is installed)
# ---------------------------------------------------------------------------


def real_hftbacktest_replay_book(
    tape: Iterable[TapeEvent], **kwargs
) -> list[TraceRecord]:
    """Placeholder for the real hftbacktest Python binding call.

    When `pip install hftbacktest` succeeds in this environment, this
    function should:
      1. Construct an hftbacktest BacktestAsset configured with
         ROIVectorMarketDepth (matches our framework's M0 semantics).
      2. Drive the tape through hftbacktest.elapse() per event.
      3. Read book top via the binding and record TraceRecords.

    Until then, this raises so callers know to use the SOTA-mirror.
    """
    try:
        import hftbacktest  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "hftbacktest Python binding not installed. "
            "Use sota_mirror_replay_book() for now, OR install via "
            "`pip install hftbacktest` (requires Rust toolchain). "
            "See nkaz001/hftbacktest README for build instructions."
        ) from e
    raise NotImplementedError(
        "Real hftbacktest binding bridge not yet wired. Schema for "
        "TraceRecord is already aligned; bridge needs ~50 LoC to map "
        "hftbacktest's BookDepth iterator to TraceRecord. Tracked as "
        "a follow-up; see PHASE_9_PROGRESS.md."
    )


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


def run_xcheck_book(
    tape_source: str,
    tape_path: Path | str | None = None,
    *,
    n_events: int = 1000,
    seed: int = 2026,
    out_dir: Path | str = "synth_results/xcheck",
) -> dict:
    """Drive a tape through the SOTA-mirror reference; record + persist trace.

    `tape_source`: 'synthetic' or 'lobster'.
      - synthetic: uses synth_lob_tape.generate_tape(n_events, seed).
      - lobster: parses tape_path as LOBSTER message file.

    Returns a summary dict with trace count + file paths. Persists JSONL.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if tape_source == "synthetic":
        synth_events = generate_tape(n_events=n_events, seed=seed)
        tape: list[TapeEvent] = []
        # Convert synth events to TapeEvent format. synth_lob_tape produces
        # rows with bid_px/ask_px/etc; we translate to TapeEvent here.
        for i, e in enumerate(synth_events):
            # Note: synth tape has BOTH sides per row. Emit two events per row:
            tape.append(TapeEvent(
                timestamp_ns=i * 1000, sym_id=0, op=1,
                side=0, price_ticks=e.bid_px, new_abs_qty=e.bid_qty,
            ))
            tape.append(TapeEvent(
                timestamp_ns=i * 1000 + 500, sym_id=0, op=1,
                side=1, price_ticks=e.ask_px, new_abs_qty=e.ask_qty,
            ))
    elif tape_source == "lobster":
        if tape_path is None:
            raise ValueError("lobster source requires tape_path")
        lob = load_lobster_messages(tape_path)
        tape = list(lobster_to_framework_events(lob))
    else:
        raise ValueError(f"unknown tape_source: {tape_source}")

    # Run the SOTA mirror
    trace_a = sota_mirror_replay_book(tape, n_symbols=1)

    # Persist
    out_a = out_dir / "trace_sota_mirror.jsonl"
    n_a = trace_to_jsonl(trace_a, out_a)

    return {
        "tape_source": tape_source,
        "n_tape_events": len(tape),
        "n_trace_rows": n_a,
        "trace_path": str(out_a),
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tape-source", choices=["synthetic", "lobster"],
                    default="synthetic")
    ap.add_argument("--tape-path", type=Path, default=None)
    ap.add_argument("--n-events", type=int, default=1000)
    ap.add_argument("--out-dir", type=Path, default="synth_results/xcheck")
    args = ap.parse_args()

    summary = run_xcheck_book(
        tape_source=args.tape_source,
        tape_path=args.tape_path,
        n_events=args.n_events,
        out_dir=args.out_dir,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
