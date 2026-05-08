"""V2-rtl: alpha_mm RTL == cycle pymodel == golden math (3-layer)."""
from __future__ import annotations
import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge

TOOLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOLS))

from golden.alpha_mm import (
    AlphaMM, AlphaMMParams,
)
from pymodel.alpha_mm import (
    AlphaMMCycle, LATENCY_CYCLES,
)


PARAMS = AlphaMMParams(
    half_spread=2, default_qty=10,
    alpha_imb_shift=6, alpha_ofi_shift=4,
    gamma_shift=7, spread_alpha_shift=4,
    confident_threshold=4,
)


@cocotb.test()
async def test_alpha_mm_rtl_eq_pymodel(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    for v in ('valid_i', 'bid_px_i', 'bid_qty_i', 'ask_px_i', 'ask_qty_i',
              'position_i', 'ofi_signal_i'):
        getattr(dut, v).value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await FallingEdge(dut.clk)

    pym = AlphaMMCycle(PARAMS)
    rng = random.Random(2026)

    # 200 random book + position + ofi inputs
    n = 200
    cmds = []
    for _ in range(n):
        bid_px = rng.randint(100, 5000)
        ask_px = bid_px + rng.randint(1, 8)
        bid_qty = rng.randint(0, 1000)
        ask_qty = rng.randint(0, 1000)
        pos = rng.randint(-1024, 1024)
        ofi = rng.randint(-512, 512)
        cmds.append((bid_px, bid_qty, ask_px, ask_qty, pos, ofi))

    mismatches = 0
    cycle = 0
    for bp, bq, ap, aq, ps, of in cmds:
        dut.valid_i.value = 1
        dut.bid_px_i.value = bp
        dut.bid_qty_i.value = bq
        dut.ask_px_i.value = ap
        dut.ask_qty_i.value = aq
        dut.position_i.value = ps
        dut.ofi_signal_i.value = of & 0xFFFFFFFF  # signed 32-bit

        pym.compute(valid_i=True, bid_px=bp, bid_qty=bq,
                    ask_px=ap, ask_qty=aq, position=ps, ofi_signal=of)
        await RisingEdge(dut.clk)
        pym.clock()
        cycle += 1
        await FallingEdge(dut.clk)
        if cycle > LATENCY_CYCLES - 1:
            r_v = int(dut.valid_o.value)
            r_bp = int(dut.bid_price_o.value.signed_integer)
            r_ap = int(dut.ask_price_o.value.signed_integer)
            r_bq = int(dut.bid_qty_o.value)
            r_aq = int(dut.ask_qty_o.value)
            p_v = int(pym.valid_o)
            p_bp = pym.bid_price_o
            p_ap = pym.ask_price_o
            p_bq = pym.bid_qty_o
            p_aq = pym.ask_qty_o
            # Mask to 16-bit signed for comparison
            p_bp16 = ((p_bp & 0xFFFF) ^ 0x8000) - 0x8000
            p_ap16 = ((p_ap & 0xFFFF) ^ 0x8000) - 0x8000
            if (r_v != p_v) or (r_v and (r_bp != p_bp16 or r_ap != p_ap16
                                          or r_bq != p_bq or r_aq != p_aq)):
                mismatches += 1
                if mismatches <= 5:
                    dut._log.error(
                        f"cyc {cycle}: rtl=(v={r_v},bp={r_bp},bq={r_bq},"
                        f"ap={r_ap},aq={r_aq}) "
                        f"pym=(v={p_v},bp={p_bp16},bq={p_bq},ap={p_ap16},aq={p_aq})"
                    )

    assert mismatches == 0, f"V2-rtl FAIL: {mismatches} mismatches"
    dut._log.info(
        f"V2-rtl PASS: alpha_mm RTL == cycle pymodel on {n} random inputs."
    )


@cocotb.test()
async def test_alpha_mm_rtl_eq_golden(dut):
    """L1 == L3 (golden math == RTL) modulo cycle delay."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    for v in ('valid_i', 'bid_px_i', 'bid_qty_i', 'ask_px_i', 'ask_qty_i',
              'position_i', 'ofi_signal_i'):
        getattr(dut, v).value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await FallingEdge(dut.clk)

    golden = AlphaMM(PARAMS)
    rng = random.Random(99)
    cmds = [
        (rng.randint(100, 5000), rng.randint(0, 1000),
         0, 0,    # ask filled below
         rng.randint(-512, 512),
         rng.randint(-256, 256))
        for _ in range(50)
    ]
    cmds = [
        (bp, bq, bp + rng.randint(1, 6), rng.randint(0, 1000), ps, of)
        for (bp, bq, _, _, ps, of) in cmds
    ]

    expected_quotes = [
        golden.compute(bp, bq, ap, aq, ps, of)[2]
        for (bp, bq, ap, aq, ps, of) in cmds
    ]
    rtl_quotes = []
    cycle = 0
    for bp, bq, ap, aq, ps, of in cmds:
        dut.valid_i.value = 1
        dut.bid_px_i.value = bp
        dut.bid_qty_i.value = bq
        dut.ask_px_i.value = ap
        dut.ask_qty_i.value = aq
        dut.position_i.value = ps
        dut.ofi_signal_i.value = of & 0xFFFFFFFF
        await RisingEdge(dut.clk)
        cycle += 1
        await FallingEdge(dut.clk)
        if cycle > LATENCY_CYCLES - 1 and int(dut.valid_o.value):
            rtl_quotes.append((
                int(dut.bid_price_o.value.signed_integer),
                int(dut.bid_qty_o.value),
                int(dut.ask_price_o.value.signed_integer),
                int(dut.ask_qty_o.value),
            ))

    # Compare
    n_match = 0
    for i, (exp, rtl) in enumerate(zip(expected_quotes, rtl_quotes)):
        exp_bp16 = ((exp.bid_px & 0xFFFF) ^ 0x8000) - 0x8000
        exp_ap16 = ((exp.ask_px & 0xFFFF) ^ 0x8000) - 0x8000
        if (rtl[0] == exp_bp16 and rtl[1] == exp.bid_qty
                and rtl[2] == exp_ap16 and rtl[3] == exp.ask_qty):
            n_match += 1

    assert n_match == len(rtl_quotes), (
        f"L1==L3 FAIL: {len(rtl_quotes) - n_match} mismatches "
        f"out of {len(rtl_quotes)}"
    )
    dut._log.info(
        f"L1==L3 PASS: alpha_mm RTL == golden math on {len(rtl_quotes)} cases"
    )
