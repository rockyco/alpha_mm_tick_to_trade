"""V2-rtl: m0_multi_symbol RTL == cycle pymodel (lockstep cocotb).

Closes the L2==L3 contract for the M0 wrapper (around
priority_array_k_packed). Drives random multi-symbol depth events
into both layers and asserts cycle-by-cycle agreement on every
output port (sym_o, best_bid_*, best_ask_*).
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pymodel.m0_multi_symbol import M0MultiSymbolCycle, LATENCY_CYCLES


N_SYMBOLS = 8
ROI_LB = 19744
ROI_SIZE = 64
VAL_BITS = 24
K_LEVELS = 2


@cocotb.test()
async def test_m0_l2_eq_l3(dut):
    pym = M0MultiSymbolCycle(N_SYMBOLS, ROI_LB, ROI_SIZE, VAL_BITS, K_LEVELS)

    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    for v in ('valid_i', 'sym_i', 'op_i', 'side_i', 'price_tick_i',
              'new_abs_qty_i'):
        getattr(dut, v).value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await FallingEdge(dut.clk)

    rng = random.Random(2026)
    cmds = []
    for _ in range(80):
        sid = rng.randint(0, N_SYMBOLS - 1)
        side = rng.randint(0, 1)
        offset = rng.randint(0, ROI_SIZE - 1)
        cmds.append((sid, 1, side, ROI_LB + offset, rng.randint(1, 10000)))
    for _ in range(100):
        sid = rng.randint(0, N_SYMBOLS - 1)
        side = rng.randint(0, 1)
        op = rng.choice([1, 1, 1, 2])
        offset = rng.randint(0, ROI_SIZE - 1)
        qty = rng.randint(1, 10000) if op == 1 else 0
        cmds.append((sid, op, side, ROI_LB + offset, qty))
    for _ in range(10):
        sid = rng.randint(0, N_SYMBOLS - 1)
        side = rng.randint(0, 1)
        cmds.append((sid, 1, side, ROI_LB - rng.randint(1, 100),
                     rng.randint(1, 100)))
    for _ in range(80):
        sid = rng.randint(0, N_SYMBOLS - 1)
        side = rng.randint(0, 1)
        offset = rng.randint(0, ROI_SIZE - 1)
        cmds.append((sid, 1, side, ROI_LB + offset, rng.randint(1, 10000)))

    rtl_mismatches = []
    cycle = 0

    for sym, op, side, price_tick, qty in cmds:
        dut.valid_i.value = 1
        dut.sym_i.value = sym
        dut.op_i.value = op
        dut.side_i.value = side
        dut.price_tick_i.value = price_tick
        dut.new_abs_qty_i.value = qty
        pym.compute(sym_id=sym, op=op, side=side,
                    price_tick=price_tick, qty=qty)

        await RisingEdge(dut.clk)
        pym.clock()
        cycle += 1
        await FallingEdge(dut.clk)

        if cycle > LATENCY_CYCLES - 1:
            rtl_sym = int(dut.sym_o.value)
            rtl_bid_tick = int(dut.best_bid_tick_o.value)
            rtl_bid_qty  = int(dut.best_bid_qty_o.value)
            rtl_bid_vld  = int(dut.best_bid_valid_o.value)
            rtl_ask_tick = int(dut.best_ask_tick_o.value)
            rtl_ask_qty  = int(dut.best_ask_qty_o.value)
            rtl_ask_vld  = int(dut.best_ask_valid_o.value)
            pym_sym      = pym.sym_o
            pym_bid_tick = pym.best_bid_tick_o
            pym_bid_qty  = pym.best_bid_qty_o
            pym_bid_vld  = int(pym.best_bid_valid_o)
            pym_ask_tick = pym.best_ask_tick_o
            pym_ask_qty  = pym.best_ask_qty_o
            pym_ask_vld  = int(pym.best_ask_valid_o)

            mismatch = False
            if rtl_sym != pym_sym:
                mismatch = True
            if rtl_bid_vld != pym_bid_vld:
                mismatch = True
            if rtl_bid_vld and (rtl_bid_tick != pym_bid_tick or
                                rtl_bid_qty != pym_bid_qty):
                mismatch = True
            if rtl_ask_vld != pym_ask_vld:
                mismatch = True
            if rtl_ask_vld and (rtl_ask_tick != pym_ask_tick or
                                rtl_ask_qty != pym_ask_qty):
                mismatch = True
            if mismatch:
                rtl_mismatches.append(
                    f"cyc {cycle}: rtl=(sym={rtl_sym},bid={rtl_bid_tick}/{rtl_bid_qty}/{rtl_bid_vld},"
                    f"ask={rtl_ask_tick}/{rtl_ask_qty}/{rtl_ask_vld}) "
                    f"pym=(sym={pym_sym},bid={pym_bid_tick}/{pym_bid_qty}/{pym_bid_vld},"
                    f"ask={pym_ask_tick}/{pym_ask_qty}/{pym_ask_vld})"
                )

    if rtl_mismatches:
        for m in rtl_mismatches[:5]:
            dut._log.error(m)
        assert False, f"V2-rtl FAIL: {len(rtl_mismatches)} mismatches"

    dut._log.info(
        f"L2==L3 PASS (m0_multi_symbol): {cycle} cycles, "
        f"N_SYMBOLS={N_SYMBOLS}, ROI_SIZE={ROI_SIZE}, "
        f"RTL == cycle pymodel."
    )
