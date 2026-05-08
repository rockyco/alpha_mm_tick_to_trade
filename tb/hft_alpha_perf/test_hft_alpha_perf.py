"""Tick-to-trade RTL measurement: hft_alpha_pipeline on real NASDAQ data."""
from __future__ import annotations

import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge

TOOLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOLS))

from xcheck.nasdaq_itch1_loader import (
    load_nasdaq_itch1_depth_events,
)


ROI_LB    = 533_504
ROI_SIZE  = 1024


@cocotb.test()
async def test_alpha_tick_to_trade_nasdaq(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    for v in ('valid_i', 'sym_i', 'op_i', 'side_i', 'price_tick_i',
              'new_abs_qty_i', 'fill_valid_i', 'fill_sym_i',
              'fill_delta_i', 'ofi_signal_i', 'kill_switch_i'):
        getattr(dut, v).value = 0
    for _ in range(5):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    await FallingEdge(dut.clk)

    import os, hashlib
    nasdaq_path = "/tmp/nasdaq_real/S010303.itch"
    sz = os.path.getsize(nasdaq_path)
    h = hashlib.md5()
    with open(nasdaq_path, "rb") as _f:
        for _chunk in iter(lambda: _f.read(1<<20), b""):
            h.update(_chunk)
    dut._log.info(f"INPUT NASDAQ FILE: {nasdaq_path} size={sz} md5={h.hexdigest()}")

    raw = list(load_nasdaq_itch1_depth_events(
        nasdaq_path, sym_id_for={'MSFT': 0},
        include_symbols={'MSFT'}, max_lines=200_000,
    ))
    events = [
        ev for ev in raw
        if ROI_LB <= ev.price_ticks < ROI_LB + ROI_SIZE
    ]
    dut._log.info(f"REAL NASDAQ MSFT events parsed: raw={len(raw)} in_ROI={len(events)}")
    dut._log.info(f"First 5 events:")
    for ev in events[:5]:
        dut._log.info(f"  ts={ev.timestamp_ns} sym={ev.sym_id} op={ev.op} "
                      f"side={'BID' if ev.side==0 else 'ASK'} px={ev.price_ticks} qty={ev.new_abs_qty}")

    first_valid_i_cycle = -1
    first_m0_both_sides = -1
    first_strat_valid = -1
    first_qe_valid = -1
    first_rg_valid = -1
    first_frame_o_cycle = -1
    n_frames = 0
    cycle = 0
    last_input_cycle = -1

    # Optional: drive a low-amplitude OFI signal to exercise alpha
    # Hold OFI signal at 0 to keep alpha small and conservative
    # (real production would drive this from the OFI primitive).
    dut.ofi_signal_i.value = 0

    n_passed = 0
    n_rejected = 0

    for i, ev in enumerate(events):
        dut.valid_i.value = 1
        dut.sym_i.value = ev.sym_id
        dut.op_i.value = ev.op
        dut.side_i.value = ev.side
        dut.price_tick_i.value = ev.price_ticks
        dut.new_abs_qty_i.value = ev.new_abs_qty
        await RisingEdge(dut.clk)
        cycle += 1
        last_input_cycle = cycle
        if first_valid_i_cycle < 0:
            first_valid_i_cycle = cycle
        await FallingEdge(dut.clk)
        try:
            mb = int(dut.m0_bid_valid_w.value)
            ma = int(dut.m0_ask_valid_w.value)
            if mb and ma and first_m0_both_sides < 0:
                first_m0_both_sides = cycle
        except Exception:
            pass
        try:
            if int(dut.strat_valid_w.value) == 1 and first_strat_valid < 0:
                first_strat_valid = cycle
        except Exception:
            pass
        try:
            if int(dut.qe_valid_w.value) == 1 and first_qe_valid < 0:
                first_qe_valid = cycle
        except Exception:
            pass
        try:
            if int(dut.rg_valid_w.value) == 1:
                if first_rg_valid < 0:
                    first_rg_valid = cycle
                d = int(dut.rg_decision_w.value)
                if d == 0:
                    n_passed += 1
                else:
                    n_rejected += 1
        except Exception:
            pass
        if int(dut.frame_valid_o.value) == 1:
            n_frames += 1
            if first_frame_o_cycle < 0:
                first_frame_o_cycle = cycle
                # Dump first frame contents to PROVE the pipeline produced
                # a real, well-formed encoded order frame (not garbage).
                frame_bits = int(dut.frame_o.value)
                bytes_le = [(frame_bits >> (i*8)) & 0xFF for i in range(12)]
                soh = bytes_le[0]
                op  = bytes_le[1]
                side = bytes_le[2]
                px = sum(bytes_le[3+i] << (i*8) for i in range(2))
                qty = sum(bytes_le[5+i] << (i*8) for i in range(2))
                checksum = bytes_le[11]
                dut._log.info(
                    f"FIRST FRAME @ cyc {cycle}: "
                    f"SOH=0x{soh:02x} op=0x{op:02x}('{chr(op) if 32<=op<127 else '?'}') "
                    f"side=0x{side:02x} px={px} qty={qty} chk=0x{checksum:02x}"
                )

    dut.valid_i.value = 0
    dut.op_i.value = 0
    for _ in range(64):
        await RisingEdge(dut.clk)
        cycle += 1
        await FallingEdge(dut.clk)
        if int(dut.frame_valid_o.value) == 1:
            n_frames += 1
            if first_frame_o_cycle < 0:
                first_frame_o_cycle = cycle

    dut._log.info("=" * 70)
    dut._log.info("ALPHA-PIPELINE TICK-TO-TRADE (REAL NASDAQ MSFT data)")
    dut._log.info("=" * 70)
    dut._log.info(f"  Input events: {len(events)}")
    dut._log.info(f"  Total sim cycles: {cycle}")
    dut._log.info(f"  Frames emitted: {n_frames}")
    dut._log.info(f"  Per-stage cycle breakdown:")
    dut._log.info(f"    First valid_i:        cyc {first_valid_i_cycle}")
    dut._log.info(f"    First M0 both-sides:  cyc {first_m0_both_sides}  "
                  f"(+{first_m0_both_sides - first_valid_i_cycle if first_m0_both_sides>=0 else '-'})")
    dut._log.info(f"    First strat_valid:    cyc {first_strat_valid}  "
                  f"(+{first_strat_valid - first_m0_both_sides if first_strat_valid>=0 and first_m0_both_sides>=0 else '-'})")
    dut._log.info(f"    First qe_valid:       cyc {first_qe_valid}")
    dut._log.info(f"    First rg_valid:       cyc {first_rg_valid}")
    dut._log.info(f"    First frame_valid_o:  cyc {first_frame_o_cycle}")

    if first_valid_i_cycle >= 0 and first_frame_o_cycle >= 0:
        ttt_first_frame = first_frame_o_cycle - first_valid_i_cycle
        # PIPELINE DEPTH (deterministic, data-independent):
        # measured from when the strategy data path is fed
        # (M0 both-sides ready) to when the encoder COULD emit
        # if the risk gateway passes. This is rg_valid + 1 cycle for
        # the encoder's output register.
        if first_m0_both_sides >= 0 and first_rg_valid >= 0:
            pipeline_depth = (first_rg_valid + 1) - first_m0_both_sides
        else:
            pipeline_depth = -1
        # FIRST-FRAME-AFTER-WARMUP:
        # how long until the FIRST risk-passing quote actually emerges.
        # This depends on the ENTIRE configuration (risk limits, alpha
        # tuning, real data shape). NOT pure pipeline depth.
        if first_m0_both_sides >= 0:
            first_frame_after_warmup = first_frame_o_cycle - first_m0_both_sides
        else:
            first_frame_after_warmup = -1
        dut._log.info(f"")
        dut._log.info(f"  PIPELINE DEPTH (deterministic): {pipeline_depth} cycles")
        dut._log.info(f"    M0 ready -> rg_valid -> encoder out reg")
        dut._log.info(f"  FIRST-FRAME-AFTER-WARMUP (config-dep): {first_frame_after_warmup} cycles")
        dut._log.info(f"    risk gateway must accept the quote")
        dut._log.info(f"  COLD-START first-frame from valid_i: {ttt_first_frame} cycles")
        for fmax_label, f in [("333 MHz", 333), ("403 MHz", 403)]:
            dut._log.info(f"    @ {fmax_label}: pipeline_depth={pipeline_depth*1000.0/f:.2f} ns, "
                          f"first_frame_after_warmup={first_frame_after_warmup*1000.0/f:.2f} ns")

    if last_input_cycle > 0 and n_frames > 0:
        rate = n_frames / cycle
        dut._log.info(f"  Frame emission rate: {n_frames}/{cycle} = {rate:.3f} frames/cycle")
        dut._log.info(f"  @ 333 MHz: {rate * 333:.1f} M frames/sec")
        dut._log.info(f"  Effective II: {last_input_cycle/len(events):.3f} cycles/event")
    total_rg = n_passed + n_rejected
    if total_rg > 0:
        dut._log.info(f"  Risk gateway: {n_passed}/{total_rg} passed "
                      f"({100*n_passed/total_rg:.1f}%), "
                      f"{n_rejected}/{total_rg} rejected "
                      f"({100*n_rejected/total_rg:.1f}%)")
    dut._log.info("=" * 70)

    assert first_frame_o_cycle >= 0, "no frame ever emitted"
