"""V2-rtl: priority_array_k_packed RTL == cycle pymodel == golden math.

Drives random insert/remove sequences into both the cycle pymodel and
the SystemVerilog RTL in lockstep, comparing top_*_o each cycle.

This is the L2==L3 contract for the M0 underlying primitive
(priority_array_k_packed). The L1==L2 chain is established separately
in tests/test_l1_eq_l2.py (pure Python). Together they prove
L1 == L2 == L3 for this primitive.
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

from pymodel.priority_array_k_packed import PriorityArrayKPackedCycle


N_SYMBOLS = 4
KEY_BITS  = 6
VAL_BITS  = 16
K_LEVELS  = 2


@cocotb.test()
async def test_packed_l2_eq_l3(dut):
    """Cycle-by-cycle: cycle pymodel and RTL produce identical top_*_o."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    for v in ('sym_i', 'op_i', 'key_i', 'val_i', 'read_sym_i', 'read_key_i'):
        getattr(dut, v).value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await FallingEdge(dut.clk)

    pym = PriorityArrayKPackedCycle(
        n_symbols=N_SYMBOLS, max_keys=1 << KEY_BITS, descending=True,
        key_bits=KEY_BITS, val_bits=VAL_BITS, k_levels=K_LEVELS,
    )
    rng = random.Random(12345)

    n_events = 400
    cmds = []
    for _ in range(n_events):
        sid = rng.randint(0, N_SYMBOLS - 1)
        op = rng.choice([1, 1, 1, 2])
        key = rng.randint(0, (1 << KEY_BITS) - 1)
        val = rng.randint(1, 1000) if op == 1 else 0
        cmds.append((sid, op, key, val))

    cycle = 0
    mismatches = 0
    for sid, op, key, val in cmds:
        # Drive RTL
        dut.sym_i.value = sid
        dut.op_i.value = op
        dut.key_i.value = key
        dut.val_i.value = val
        dut.read_sym_i.value = 0
        dut.read_key_i.value = 0
        # Drive pymodel
        pym.compute(sym_id=sid, op_in=op, key_in=key, val_in=val)

        await RisingEdge(dut.clk)
        pym.clock()
        cycle += 1
        await FallingEdge(dut.clk)

        # Compare cycle-by-cycle
        if cycle > 1:  # let the first valid_o stabilize
            r_sym  = int(dut.top_sym_o.value)
            r_key  = int(dut.top_key_o.value)
            r_val  = int(dut.top_val_o.value)
            r_vld  = int(dut.top_valid_o.value)
            p_sym  = pym.top_sym_o
            p_key  = pym.top_key_o
            p_val  = pym.top_val_o
            p_vld  = int(pym.top_valid_o)

            mismatch = (r_sym != p_sym) or (r_vld != p_vld)
            if r_vld and (r_key != p_key or r_val != p_val):
                mismatch = True
            if mismatch:
                mismatches += 1
                if mismatches <= 5:
                    dut._log.error(
                        f"cyc {cycle}: rtl=(sym={r_sym},key={r_key},"
                        f"val={r_val},vld={r_vld}) "
                        f"pym=(sym={p_sym},key={p_key},val={p_val},vld={p_vld})"
                    )

    if mismatches:
        assert False, f"L2 != L3: {mismatches} mismatches in {n_events} events"

    dut._log.info(
        f"L2==L3 PASS: priority_array_k_packed RTL == cycle pymodel "
        f"on {n_events} random multi-symbol events (N_SYMBOLS={N_SYMBOLS}, "
        f"K_LEVELS={K_LEVELS}, KEY_BITS={KEY_BITS})"
    )
