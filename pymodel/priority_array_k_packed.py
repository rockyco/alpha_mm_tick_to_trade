"""PriorityArrayKPackedCycle - cycle-accurate model (canonical, v1.28.23).

Mirrors rtl/priority_array_k_packed.sv exactly. All registered state is
snapshotted at clock() entry; combinational logic is derived from the
snapshot; all register updates commit at clock() exit. This matches the
posedge-triggered semantics of the RTL bit-for-bit.

State (all registered):
  cmd_*_reg                  : S0 latched cmd
  mem[(sym, key)]            : full ROI table (BRAM)
  kbest_key/val/vld[sym][k]  : per-symbol K-cache (LUTRAM)
  size_reg[sym]              : per-symbol counter
  refill_pending[sym]        : per-symbol "scan needed" bit
  scan_state, scan_sym, scan_key   : shared scan FSM
  scan_sym_d1, scan_key_d1, scan_active_d1 : scan address pipeline reg
  read_*_reg                 : debug read port output

Convergence (Rule 6.5(2)): under steady-state input the scan FSM walks
any pending symbol's keyspace (worst-case KEY_RANGE cycles) and refills
slot 0 from mem. Then top_*_o = math.peek_top bit-exact.

Liveness (Rule 6.5(5)): the debug read port + scan port together provide
KEY_RANGE * (VAL_BITS+1) bits of read-side consumption per symbol, so
Vivado retains the BRAM. RAMB36 utilisation in V4 satisfies
RAMB36 * 36864 >= N_SYMBOLS * KEY_RANGE * (VAL_BITS + 1).
"""

from __future__ import annotations


LATENCY_CYCLES = 2


class PriorityArrayKPackedCycle:
    def __init__(
        self,
        n_symbols: int,
        max_keys: int,
        descending: bool,
        key_bits: int = 8,
        val_bits: int = 32,
        k_levels: int = 2,
    ) -> None:
        self.n_symbols = int(n_symbols)
        self.max_keys = int(max_keys)
        self.descending = bool(descending)
        self.key_bits = int(key_bits)
        self.val_bits = int(val_bits)
        self.k_levels = int(k_levels)
        self.key_mask = (1 << self.key_bits) - 1
        self.val_mask = (1 << self.val_bits) - 1
        self.key_high = self.key_mask
        self.key_zero = 0
        self.key_range = 1 << self.key_bits

        # Inputs (next-state from compute())
        self._cmd_sym_next = 0
        self._cmd_op_next = 0
        self._cmd_key_next = 0
        self._cmd_val_next = 0
        self._read_sym_next = 0
        self._read_key_next = 0

        # Registered: S0 cmd
        self._cmd_sym_reg = 0
        self._cmd_op_reg = 0
        self._cmd_key_reg = 0
        self._cmd_val_reg = 0

        # Registered: storage
        self._mem: dict[tuple[int, int], int] = {}

        # Registered: K-best per-symbol cache
        self._kbest_key = [[0] * self.k_levels for _ in range(self.n_symbols)]
        self._kbest_val = [[0] * self.k_levels for _ in range(self.n_symbols)]
        self._kbest_vld = [[False] * self.k_levels for _ in range(self.n_symbols)]
        self._size_reg = [0] * self.n_symbols

        # Registered: refill arbitration (v1.28.24 FIFO arbiter)
        # enqueued_q[sym] is the dedup flag; read by single index (LUTRAM-friendly).
        # pending_fifo is the work queue of pending sym IDs.
        self._enqueued = [False] * self.n_symbols
        self._fifo: list[int] = []  # list-as-FIFO; head = self._fifo[0]

        # Registered: scan FSM
        self._scan_state = "S_IDLE"
        self._scan_sym = 0
        self._scan_key = self.key_high if self.descending else self.key_zero
        self._scan_sym_d1 = 0
        self._scan_key_d1 = 0
        self._scan_active_d1 = False

        # Registered: debug read port
        self._read_val_reg = 0
        self._read_vld_reg = False

    def compute(
        self,
        sym_id: int = 0,
        op_in: int = 0,
        key_in: int = 0,
        val_in: int = 0,
        read_sym: int = 0,
        read_key: int = 0,
    ) -> None:
        if self.n_symbols > 1:
            self._cmd_sym_next = int(sym_id) % self.n_symbols
            self._read_sym_next = int(read_sym) % self.n_symbols
        else:
            self._cmd_sym_next = 0
            self._read_sym_next = 0
        self._cmd_op_next = int(op_in)
        self._cmd_key_next = int(key_in) & self.key_mask
        self._cmd_val_next = int(val_in) & self.val_mask
        self._read_key_next = int(read_key) & self.key_mask

    def _better(self, a: int, b: int) -> bool:
        return (a > b) if self.descending else (a < b)

    def clock(self) -> None:
        # ---------- 1. SNAPSHOT all registered state ----------
        cmd_sym  = self._cmd_sym_reg
        cmd_op   = self._cmd_op_reg
        cmd_key  = self._cmd_key_reg
        cmd_val  = self._cmd_val_reg
        is_insert = (cmd_op == 1) and (cmd_val != 0)
        is_remove = (cmd_op == 2) or ((cmd_op == 1) and (cmd_val == 0))
        cmd_we    = is_insert or is_remove

        kk_old = list(self._kbest_key[cmd_sym])
        kv_old = list(self._kbest_val[cmd_sym])
        kvd_old = list(self._kbest_vld[cmd_sym])
        size_old = self._size_reg[cmd_sym]
        was_valid = (cmd_sym, cmd_key) in self._mem

        scan_state_old = self._scan_state
        scan_sym_old = self._scan_sym
        scan_key_old = self._scan_key
        scan_sym_d1_old = self._scan_sym_d1
        scan_key_d1_old = self._scan_key_d1
        scan_active_d1_old = self._scan_active_d1
        enqueued_old = list(self._enqueued)
        fifo_old = list(self._fifo)

        # ---------- 2. CMD-driven kbest update (combinational) ----------
        kk_new, kv_new, kvd_new = list(kk_old), list(kv_old), list(kvd_old)
        match0 = kvd_old[0] and kk_old[0] == cmd_key
        better0 = (not kvd_old[0]) or self._better(cmd_key, kk_old[0])
        if self.k_levels > 1:
            match1 = kvd_old[1] and kk_old[1] == cmd_key
            better1 = (not kvd_old[1]) or self._better(cmd_key, kk_old[1])
        else:
            match1 = False
            better1 = False

        if is_insert:
            if match0:
                kv_new[0] = cmd_val
            elif better0:
                if self.k_levels > 1:
                    kk_new[1] = kk_old[0]
                    kv_new[1] = kv_old[0]
                    kvd_new[1] = kvd_old[0]
                kk_new[0] = cmd_key
                kv_new[0] = cmd_val
                kvd_new[0] = True
            elif self.k_levels > 1 and match1:
                kv_new[1] = cmd_val
            elif self.k_levels > 1 and better1:
                kk_new[1] = cmd_key
                kv_new[1] = cmd_val
                kvd_new[1] = True
        elif is_remove:
            if match0:
                if self.k_levels > 1:
                    kk_new[0] = kk_old[1]
                    kv_new[0] = kv_old[1]
                    kvd_new[0] = kvd_old[1]
                    kk_new[1] = 0
                    kv_new[1] = 0
                    kvd_new[1] = False
                else:
                    kk_new[0] = 0
                    kv_new[0] = 0
                    kvd_new[0] = False
            elif self.k_levels > 1 and match1:
                kk_new[1] = 0
                kv_new[1] = 0
                kvd_new[1] = False

        # next size (combinational)
        if is_insert and not was_valid:
            size_new = size_old + 1
        elif is_remove and was_valid:
            size_new = size_old - 1
        else:
            size_new = size_old

        # cmd-driven refill SET (combinational; FIFO push gated by dedup flag)
        refill_set = cmd_we and (not kvd_new[0]) and (size_new > 0)
        enq_w = refill_set and not enqueued_old[cmd_sym]

        # ---------- 3. SCAN-driven refill (combinational) ----------
        # scan_data this cycle = mem at (scan_sym_d1, scan_key_d1) registered last cycle
        scan_cell_val = 0
        scan_cell_vld = False
        if scan_active_d1_old:
            cell = self._mem.get((scan_sym_d1_old, scan_key_d1_old), 0)
            if cell != 0:
                scan_cell_val = cell & self.val_mask
                scan_cell_vld = True

        scan_hit = (scan_state_old == "S_SCAN") and scan_active_d1_old \
            and scan_cell_vld and (scan_sym_d1_old == scan_sym_old)
        # Cmd has priority over scan on the per-LUTRAM single write port.
        # Broader than same-sym only: cmd-write blocks scan-write even at
        # different addresses (single-port LUTRAM constraint).
        scan_collision = scan_hit and cmd_we
        scan_commit = scan_hit and not scan_collision

        # scan-driven kbest update target (slot 0 of scan_sym_d1_old)
        if scan_commit:
            target = scan_sym_d1_old
            sk_old = self._kbest_key[target]
            sv_old = self._kbest_val[target]
            svd_old = self._kbest_vld[target]
            sk_new, sv_new, svd_new = list(sk_old), list(sv_old), list(svd_old)
            # Clear slot 1 if it duplicates the new top key
            if self.k_levels > 1 and svd_old[1] and sk_old[1] == scan_key_d1_old:
                sk_new[1] = 0
                sv_new[1] = 0
                svd_new[1] = False
            sk_new[0] = scan_key_d1_old
            sv_new[0] = scan_cell_val
            svd_new[0] = True
        else:
            target = -1
            sk_new = sv_new = svd_new = None

        # FIFO dequeue (combinational): only fires in S_IDLE with non-empty FIFO.
        deq_w = (scan_state_old == "S_IDLE") and (len(fifo_old) > 0)
        deq_sym = fifo_old[0] if deq_w else -1

        # scan FSM next-state (combinational)
        # On collision-suppressed hit, STALL: stay in S_SCAN with scan_key unchanged
        # so the next cycle re-issues the same scan_addr and retries the commit.
        # This guarantees Rule 6.5(2) convergence without needing a re-enqueue path.
        if scan_state_old == "S_IDLE":
            if deq_w:
                scan_state_next = "S_SCAN"
                scan_sym_next = deq_sym
                scan_key_next = self.key_high if self.descending else self.key_zero
            else:
                scan_state_next = "S_IDLE"
                scan_sym_next = scan_sym_old
                scan_key_next = scan_key_old
        else:  # S_SCAN
            exhausted = (scan_key_old == self.key_zero) if self.descending \
                else (scan_key_old == self.key_high)
            if scan_hit:
                if scan_collision:
                    # STALL: keep S_SCAN, hold scan_key for retry next cycle
                    scan_state_next = "S_SCAN"
                    scan_sym_next = scan_sym_old
                    scan_key_next = scan_key_old
                else:
                    scan_state_next = "S_IDLE"
                    scan_sym_next = scan_sym_old
                    scan_key_next = scan_key_old
            elif exhausted:
                scan_state_next = "S_IDLE"
                scan_sym_next = scan_sym_old
                scan_key_next = scan_key_old
            else:
                scan_state_next = "S_SCAN"
                scan_sym_next = scan_sym_old
                if self.descending:
                    scan_key_next = (scan_key_old - 1) & self.key_mask
                else:
                    scan_key_next = (scan_key_old + 1) & self.key_mask

        # scan_finish_w: this cycle the FSM transitions S_SCAN -> S_IDLE
        # (commit OR exhaust; collision-stall does NOT count).
        scan_finish_w = (scan_state_old == "S_SCAN") and (scan_state_next == "S_IDLE")

        # d1 latches next-state (always)
        scan_sym_d1_next = scan_sym_old
        scan_key_d1_next = scan_key_old
        scan_active_d1_next = (scan_state_old == "S_SCAN")

        # mem next state (insert/remove)
        # debug read next-state
        debug_addr = (self._read_sym_next, self._read_key_next)
        # NOTE: in RTL the debug read returns mem at (read_sym_i, read_key_i)
        # registered to next cycle. We model with mem AFTER cmd update because
        # the BRAM write of cmd commits at posedge same as the read latch; the
        # registered read sees post-write data only if same address. We assume
        # different address (debug not racing cmd). Use post-write mem.

        # ---------- 4. COMMIT all registered state ----------
        # mem
        if is_insert:
            self._mem[(cmd_sym, cmd_key)] = cmd_val
        elif is_remove:
            if was_valid:
                self._mem.pop((cmd_sym, cmd_key), None)

        # cmd-driven kbest commit
        if cmd_we:
            self._kbest_key[cmd_sym] = kk_new
            self._kbest_val[cmd_sym] = kv_new
            self._kbest_vld[cmd_sym] = kvd_new
            self._size_reg[cmd_sym] = size_new

        # scan-driven kbest commit (different sym OR no cmd)
        if scan_commit and target >= 0:
            self._kbest_key[target] = sk_new
            self._kbest_val[target] = sv_new
            self._kbest_vld[target] = svd_new

        # FIFO + dedup-bit commit (registered):
        #   enq_w: push cmd_sym to FIFO tail; enqueued_q[cmd_sym] := True
        #   deq_w: pop FIFO head (already used as scan_sym_next above)
        #   scan_finish_w: enqueued_q[scan_sym_old] := False  (clear dedup)
        if deq_w:
            # pop head
            self._fifo.pop(0)
        if enq_w:
            self._fifo.append(cmd_sym)
            self._enqueued[cmd_sym] = True
        if scan_finish_w:
            self._enqueued[scan_sym_old] = False

        # scan FSM commit
        self._scan_state = scan_state_next
        self._scan_sym = scan_sym_next
        self._scan_key = scan_key_next
        self._scan_sym_d1 = scan_sym_d1_next
        self._scan_key_d1 = scan_key_d1_next
        self._scan_active_d1 = scan_active_d1_next

        # debug read commit (post-mem-write)
        cell = self._mem.get(debug_addr, 0)
        if cell != 0:
            self._read_val_reg = cell & self.val_mask
            self._read_vld_reg = True
        else:
            self._read_val_reg = 0
            self._read_vld_reg = False

        # cmd register advance
        self._cmd_sym_reg = self._cmd_sym_next
        self._cmd_op_reg = self._cmd_op_next
        self._cmd_key_reg = self._cmd_key_next
        self._cmd_val_reg = self._cmd_val_next

    @property
    def top_sym_o(self) -> int:
        return self._cmd_sym_reg

    @property
    def top_key_o(self) -> int:
        return self._kbest_key[self._cmd_sym_reg][0]

    @property
    def top_val_o(self) -> int:
        return self._kbest_val[self._cmd_sym_reg][0]

    @property
    def top_valid_o(self) -> bool:
        return self._kbest_vld[self._cmd_sym_reg][0]

    @property
    def size_o(self) -> int:
        return self._size_reg[self._cmd_sym_reg]

    @property
    def read_val_o(self) -> int:
        return self._read_val_reg

    @property
    def read_valid_o(self) -> bool:
        return self._read_vld_reg
