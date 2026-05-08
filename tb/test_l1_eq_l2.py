"""L1 (math golden) == L2 (cycle pymodel) consistency check, pure Python.

Validates that every math primitive's golden model and cycle pymodel
agree on outputs - no RTL involved. This is the FAST baseline that
catches Python-side drift before any RTL/iverilog is run.

Run as a stand-alone script:
    PYTHONPATH=. python3 tb/test_l1_eq_l2.py

Modules covered:
    - priority_array_k          (bounded keyed top-K store)
    - priority_array_k_packed   (multi-symbol packed variant)
    - alpha_mm                  (alpha-driven MM strategy)
    - m0_multi_symbol           (M0 public-book wrapper)
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

# Resolve PYTHONPATH = repo root if run as a script
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from golden.alpha_mm           import AlphaMM, AlphaMMParams
from golden.priority_array_k   import PriorityArrayK
from golden.priority_array_k_packed import PriorityArrayKPacked
from pymodel.alpha_mm          import AlphaMMCycle
from pymodel.m0_multi_symbol   import M0MultiSymbolCycle, LATENCY_CYCLES as M0_LAT
from pymodel.priority_array_k  import PriorityArrayKCycle
from pymodel.priority_array_k_packed import PriorityArrayKPackedCycle


# ---------------------------------------------------------------------------
# alpha_mm: golden compute() == pymodel compute()+clock() output after 2 cyc
# ---------------------------------------------------------------------------


def test_alpha_mm_l1_eq_l2():
    p = AlphaMMParams()
    golden = AlphaMM(p)
    cyc    = AlphaMMCycle(p)
    rng = random.Random(42)
    n_check = 0
    for _ in range(500):
        bid_px  = rng.randint(100, 5000)
        ask_px  = bid_px + rng.randint(1, 8)
        bid_qty = rng.randint(0, 1000)
        ask_qty = rng.randint(0, 1000)
        pos     = rng.randint(-1024, 1024)
        ofi     = rng.randint(-512, 512)

        # Golden: stateless functional reference
        _, _, q_gold = golden.compute(bid_px, bid_qty, ask_px, ask_qty,
                                      pos, ofi)

        # Cycle pymodel: drive once then drain LATENCY_CYCLES=2
        cyc_local = AlphaMMCycle(p)
        cyc_local.compute(valid_i=True, bid_px=bid_px, bid_qty=bid_qty,
                          ask_px=ask_px, ask_qty=ask_qty,
                          position=pos, ofi_signal=ofi)
        cyc_local.clock()
        cyc_local.compute(valid_i=False)
        cyc_local.clock()
        # After 2 clocks: cyc_local.bid_price_o is for the event we drove
        assert cyc_local.valid_o, "pymodel output should be valid 2 cycles in"
        # Mask outputs to PX_BITS=16 signed for apples-to-apples
        def s16(x):
            return ((x & 0xFFFF) ^ 0x8000) - 0x8000
        assert s16(cyc_local.bid_price_o) == s16(q_gold.bid_px), (
            f"alpha_mm L1!=L2 bid: gold={q_gold.bid_px} cyc={cyc_local.bid_price_o}"
        )
        assert s16(cyc_local.ask_price_o) == s16(q_gold.ask_px), (
            f"alpha_mm L1!=L2 ask: gold={q_gold.ask_px} cyc={cyc_local.ask_price_o}"
        )
        n_check += 1
    print(f"  alpha_mm L1==L2: {n_check}/{n_check} match")


# ---------------------------------------------------------------------------
# priority_array_k (bounded K, single symbol): math vs cycle agreement
# ---------------------------------------------------------------------------


def test_priority_array_k_l1_eq_l2():
    """Drive identical insert/remove sequences into golden + cycle pymodel.

    Per Rule 6.5(2), under steady-state input the cycle pymodel converges
    to the math model after the scan FSM walks the keyspace. We assert
    convergence by driving each event then advancing the cycle pymodel
    `KEY_RANGE+8` cycles (worst-case scan duration) before comparing
    peek_top.
    """
    KEY_BITS  = 6
    VAL_BITS  = 16
    K_LEVELS  = 2
    KEY_RANGE = 1 << KEY_BITS
    DRAIN     = KEY_RANGE * 4 + 16

    rng = random.Random(2026)
    golden = PriorityArrayK(
        max_keys=KEY_RANGE, descending=True,
        key_bits=KEY_BITS, val_bits=VAL_BITS,
    )
    cyc = PriorityArrayKCycle(
        max_keys=KEY_RANGE, descending=True,
        key_bits=KEY_BITS, val_bits=VAL_BITS, k_levels=K_LEVELS,
    )

    # Run a sequence of inserts + removes, comparing peek_top after drain
    n_events = 50
    n_check = 0
    for _ in range(n_events):
        op = rng.choice([1, 1, 1, 2])  # bias toward inserts
        key = rng.randint(0, KEY_RANGE - 1)
        val = rng.randint(1, 1000) if op == 1 else 0

        # Golden update
        if op == 1:
            golden.insert(key, val)
        else:
            golden.remove(key)

        # Cycle pymodel: drive event then drain
        cyc.compute(op_in=op, key_in=key, val_in=val)
        cyc.clock()
        for _ in range(DRAIN):
            cyc.compute(op_in=0)
            cyc.clock()

        # Compare
        g_key, g_val = golden.peek_top()
        c_key = cyc.top_key_o if cyc.top_valid_o else 0
        c_val = cyc.top_val_o if cyc.top_valid_o else 0
        assert g_key == c_key and g_val == c_val, (
            f"priority_array_k L1!=L2 after op={op} key={key} val={val}: "
            f"gold=({g_key},{g_val}) cyc=({c_key},{c_val})"
        )
        n_check += 1
    print(f"  priority_array_k L1==L2: {n_check}/{n_check} match (DRAIN={DRAIN})")


# ---------------------------------------------------------------------------
# priority_array_k_packed: multi-symbol math vs cycle
# ---------------------------------------------------------------------------


def test_priority_array_k_packed_l1_eq_l2():
    N_SYMBOLS = 4
    KEY_BITS  = 6
    VAL_BITS  = 16
    K_LEVELS  = 2
    KEY_RANGE = 1 << KEY_BITS
    DRAIN     = KEY_RANGE * 4 + 16

    rng = random.Random(99)
    golden = PriorityArrayKPacked(
        n_symbols=N_SYMBOLS, max_keys=KEY_RANGE, descending=True,
        key_bits=KEY_BITS, val_bits=VAL_BITS,
    )
    cyc = PriorityArrayKPackedCycle(
        n_symbols=N_SYMBOLS, max_keys=KEY_RANGE, descending=True,
        key_bits=KEY_BITS, val_bits=VAL_BITS, k_levels=K_LEVELS,
    )

    n_events = 30
    n_check = 0
    for _ in range(n_events):
        sid = rng.randint(0, N_SYMBOLS - 1)
        op = rng.choice([1, 1, 1, 2])
        key = rng.randint(0, KEY_RANGE - 1)
        val = rng.randint(1, 1000) if op == 1 else 0

        if op == 1:
            golden.insert(sid, key, val)
        else:
            golden.remove(sid, key)

        cyc.compute(sym_id=sid, op_in=op, key_in=key, val_in=val)
        cyc.clock()
        # Drain: drive nops addressed at the SAME sym so the output port
        # stays indexed at sid. Without this, _cmd_sym_reg drifts to 0
        # and top_*_o reads the WRONG symbol's kbest.
        for _ in range(DRAIN):
            cyc.compute(sym_id=sid, op_in=0)
            cyc.clock()

        # Compare peek_top for the actively-updated symbol
        g_key, g_val = (
            golden.peek_top(sid) if len(golden.symbols[sid]) > 0 else (0, 0)
        )
        c_key = cyc.top_key_o if cyc.top_valid_o else 0
        c_val = cyc.top_val_o if cyc.top_valid_o else 0
        assert g_key == c_key and g_val == c_val, (
            f"priority_array_k_packed sym {sid} L1!=L2 after "
            f"op={op} key={key} val={val}: "
            f"gold=({g_key},{g_val}) cyc=({c_key},{c_val})"
        )
        n_check += 1
    print(f"  priority_array_k_packed L1==L2: {n_check}/{n_check} match")


# ---------------------------------------------------------------------------
# m0_multi_symbol: cycle pymodel internal consistency
# ---------------------------------------------------------------------------


def test_m0_multi_symbol_pymodel_runs():
    """Smoke-check that M0 cycle pymodel runs without crashing on a
    realistic event stream. The strict equivalence vs golden is covered
    by the priority_array_k_packed L1==L2 test (M0 is a thin wrapper)."""
    N_SYMBOLS = 4
    ROI_LB = 100_000
    ROI_SIZE = 64
    pym = M0MultiSymbolCycle(
        n_symbols=N_SYMBOLS, roi_lb=ROI_LB, roi_size=ROI_SIZE,
        val_bits=24, k_levels=2,
    )
    rng = random.Random(7)
    for _ in range(200):
        sid = rng.randint(0, N_SYMBOLS - 1)
        side = rng.randint(0, 1)
        op = rng.choice([1, 1, 1, 2])
        offset = rng.randint(0, ROI_SIZE - 1)
        price = ROI_LB + offset
        qty = rng.randint(1, 10000) if op == 1 else 0
        pym.compute(sym_id=sid, op=op, side=side, price_tick=price, qty=qty)
        pym.clock()
    # Drain
    for _ in range(ROI_SIZE * 4):
        pym.compute(sym_id=0, op=0)
        pym.clock()
    # Sanity: outputs are within expected ranges
    assert isinstance(pym.best_bid_tick_o, int)
    assert isinstance(pym.best_ask_tick_o, int)
    assert pym.sym_o in range(N_SYMBOLS)
    print(f"  m0_multi_symbol pymodel: 200 events processed cleanly")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    print("L1 == L2 consistency tests (pure Python):")
    failures = 0
    for fn in [
        test_alpha_mm_l1_eq_l2,
        test_priority_array_k_l1_eq_l2,
        test_priority_array_k_packed_l1_eq_l2,
        test_m0_multi_symbol_pymodel_runs,
    ]:
        try:
            fn()
        except AssertionError as e:
            print(f"  FAIL: {fn.__name__}: {e}")
            failures += 1
    if failures:
        print(f"\nFAILED: {failures} test(s) failed")
        return 1
    print("\nL1 == L2 consistency: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
