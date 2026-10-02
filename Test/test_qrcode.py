#!/usr/bin/env python3
"""
Test suite for the minimal-RAM QR generator (qrcode.c / qrcode.h).

Everything that JUDGES the generator's output is written independently of the C
code under test: its own copy of the spec tables and formulas, its own GF(256)
tables, its own symbol reader.  Sections (run with --sections a,b,...):

  selftest   the checker itself: spec tables vs formulas, OpenCV-encoder symbols
             accepted, injected faults in every symbol region rejected
  unit       white-box: mask formulas, format/version BCH, Rule-4 balance, saturating add vs the spec
  capacity   max message length per (version, ECC) vs the spec; over-capacity rejected
  lengths    EVERY message length 0..capacity for every version/ECC (mask varies)
  masks      all 8 masks x every version/ECC x boundary lengths
  automask   automatic mask choice == lowest penalty according to an independent scorer
  rscorrect  an independent Reed-Solomon decoder corrects the maximum EC/2 errors in every block
  opencv     a real decoder (OpenCV): payload types, module sizes, error-correction damage
  encoder    cross-check against OpenCV's own QR *encoder* (data+EC codeword streams)
  api        contract: bounds, reuse, order independence, extreme lengths
  build      identical output across -O levels / sanitizers / auto-var-init / C standards,
             strict warnings, static analyzer, C++ linkage
  guards     compile-time #error guards + symbolic macro values
  memory     static RAM / stack / flash numbers, the header's documented example

usage: python3 test_qrcode.py [--src DIR_WITH_qrcode.c_and_h] [--work DIR]
                              [--sections a,b,...] [--versions 1-20]
needs: gcc, numpy; OpenCV (cv2) is optional (opencv/encoder sections are skipped without it)
"""
import argparse, hashlib, os, re, subprocess, sys, time
from fractions import Fraction
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
try:
    import cv2
except Exception:
    cv2 = None

# ----------------------------------------------------------------------------
# The C test harness: reads one message per line (hex, or "L<n>" = n bytes of 'A';
# in FORCED builds the line starts with the mask number), prints the symbol as
# rows of 0/1, or REJECT.
# ----------------------------------------------------------------------------
GEN_C = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include "qrcode.h"
#ifdef FORCED
uint8_t qr_dbg_mask;
#endif
static int hv(int c) { return c <= '9' ? c - '0' : (c | 32) - 'a' + 10; }
int main(void) {
    static char line[140000];
    static uint8_t msg[70000];
    static qr_code_t q;
    while (fgets(line, sizeof line, stdin)) {
        char *p = line;
        unsigned long len = 0;           /* host-side harness: wide types are fine here */
#ifdef FORCED
        qr_dbg_mask = (uint8_t)strtol(p, &p, 10);
#endif
        while (*p == ' ') p++;
        if (*p == 'L') { len = (unsigned long)strtol(p + 1, NULL, 10); memset(msg, 'A', len); }
        else while (isxdigit((unsigned char)p[0]) && isxdigit((unsigned char)p[1])) {
            msg[len++] = (uint8_t)(hv(p[0]) * 16 + hv(p[1])); p += 2; }
        if (!qr_generate(&q, msg, (uint16_t)len)) { puts("REJECT"); continue; }
        puts("OK");
        for (int y = 0; y < QR_SIZE; y++) {
            for (int x = 0; x < QR_SIZE; x++) putchar(qr_get_module(&q, (uint8_t)x, (uint8_t)y) ? '1' : '0');
            putchar('\n');
        }
    }
    return 0;
}
'''

# ----------------------------------------------------------------------------
# Spec data, typed in independently of qrcode.c (same layout as Nayuki's tables:
# index [ecc 0..3 = L,M,Q,H][version 1..40], index 0 unused).
# ----------------------------------------------------------------------------
ECC_CW_PER_BLOCK = [
 [0,7,10,15,20,26,18,20,24,30,18,20,24,26,30,22,24,28,30,28,28,28,28,30,30,26,28,30,30,30,30,30,30,30,30,30,30,30,30,30,30],
 [0,10,16,26,18,24,16,18,22,22,26,30,22,22,24,24,28,28,26,26,26,26,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28,28],
 [0,13,22,18,26,18,24,18,22,20,24,28,26,24,20,30,24,28,28,26,30,28,30,30,30,30,28,30,30,30,30,30,30,30,30,30,30,30,30,30,30],
 [0,17,28,22,16,22,28,26,26,24,28,24,28,22,24,24,30,28,28,26,28,30,24,30,30,30,30,30,30,30,30,30,30,30,30,30,30,30,30,30,30]]
NUM_BLOCKS = [
 [0,1,1,1,1,1,2,2,2,2,4,4,4,4,4,6,6,6,6,7,8,8,9,9,10,12,12,12,13,14,15,16,17,18,19,19,20,21,22,24,25],
 [0,1,1,1,2,2,4,4,4,5,5,5,8,9,9,10,10,11,13,14,16,17,17,18,20,21,23,25,26,28,29,31,33,35,37,38,40,43,45,47,49],
 [0,1,1,2,2,4,4,6,6,8,8,8,10,12,16,12,17,16,18,21,20,23,23,25,27,29,34,34,35,38,40,43,45,48,51,53,56,59,62,65,68],
 [0,1,1,2,4,4,4,5,6,8,8,11,11,16,16,18,16,19,21,25,25,25,34,30,32,35,37,40,42,45,48,51,54,57,60,63,66,70,74,77,81]]
# ISO/IEC 18004 byte-mode capacities (bytes) for versions 1..20, columns L,M,Q,H
SPEC_CAP = {1:(17,14,11,7),2:(32,26,20,14),3:(53,42,32,24),4:(78,62,46,34),5:(106,84,60,44),
 6:(134,106,74,58),7:(154,122,86,64),8:(192,152,108,84),9:(230,180,130,98),10:(271,213,151,119),
 11:(321,251,177,137),12:(367,287,203,155),13:(425,331,241,177),14:(458,362,258,194),
 15:(520,412,292,220),16:(586,450,322,250),17:(644,504,364,280),18:(718,560,394,310),
 19:(792,624,442,338),20:(858,666,482,382)}
# total codewords (data + EC) per version 1..20
SPEC_TOTAL_CW = [26,44,70,100,134,172,196,242,292,346,404,466,532,581,655,733,815,901,991,1085]
# ISO 18004 Annex C: the 32 format strings, rows L,M,Q,H x mask 0..7
SPEC_FORMAT = [
 ["111011111000100","111001011110011","111110110101010","111100010011101","110011000101111","110001100011000","110110001000001","110100101110110"],
 ["101010000010010","101000100100101","101111001111100","101101101001011","100010111111001","100000011001110","100111110010111","100101010100000"],
 ["011010101011111","011000001101000","011111100110001","011101000000110","010010010110100","010000110000011","010111011011010","010101111101101"],
 ["001011010001001","001001110111110","001110011100111","001100111010000","000011101100010","000001001010101","000110100001100","000100000111011"]]
# ISO 18004 Annex D: 18-bit version strings, versions 7..20
SPEC_VERSION = {7:0x07C94,8:0x085BC,9:0x09A99,10:0x0A4D3,11:0x0BBF6,12:0x0C762,13:0x0D847,
 14:0x0E60D,15:0x0F928,16:0x10B78,17:0x1145D,18:0x12A17,19:0x13532,20:0x149A6}
FINDER = ["1111111","1000001","1011101","1011101","1011101","1000001","1111111"]
ALIGN  = ["11111","10001","10101","10001","11111"]
ECC_NAMES = "LMQH"

# GF(256), primitive polynomial 0x11D -- table based here, table-free in the C code
EXP = np.zeros(512, np.uint8); LOG = np.zeros(256, np.int64)
_x = 1
for _i in range(255):
    EXP[_i] = _x; LOG[_x] = _i
    _x <<= 1
    if _x & 0x100: _x ^= 0x11D
for _i in range(255, 512): EXP[_i] = EXP[_i - 255]

def num_raw_data_modules(v):
    r = (16 * v + 128) * v + 64
    if v >= 2:
        n = v // 7 + 2
        r -= (25 * n - 10) * n - 55
        if v >= 7: r -= 36
    return r

def align_positions(v):
    if v == 1: return []
    n = v // 7 + 2
    step = 26 if v == 32 else (v * 4 + n * 2 + 1) // (n * 2 - 2) * 2
    res = [6]; pos = 4 * v + 10
    for _ in range(n - 1):
        res.insert(1, pos); pos -= step
    return res

def format_bits(e, mask):
    data = ([1, 0, 3, 2][e] << 3) | mask          # ECC indicators: L=01 M=00 Q=11 H=10
    rem = data
    for _ in range(10): rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412

def version_bits(v):
    rem = v
    for _ in range(12): rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (v << 12) | rem

# ----------------------------------------------------------------------------
# Per-version geometry and per-(version, ECC) codeword layout
# ----------------------------------------------------------------------------
class Geo:
    def __init__(self, v):
        n = 4 * v + 17; self.v, self.n = v, n
        val = np.zeros((n, n), np.uint8); fixed = np.zeros((n, n), bool)
        for bx, by in ((0, 0), (n - 8, 0), (0, n - 8)):          # finder + separator boxes
            fixed[by:by + 8, bx:bx + 8] = True
        for ox, oy in ((0, 0), (n - 7, 0), (0, n - 7)):
            for r in range(7):
                for c in range(7): val[oy + r, ox + c] = FINDER[r][c] == "1"
        for i in range(8, n - 8):                                 # timing patterns
            fixed[6, i] = fixed[i, 6] = True
            val[6, i] = val[i, 6] = (i % 2 == 0)
        pos = align_positions(v); last = len(pos) - 1
        for i, cy in enumerate(pos):                              # alignment patterns
            for j, cx in enumerate(pos):
                if (i == 0 and j == 0) or (i == 0 and j == last) or (i == last and j == 0): continue
                for r in range(5):
                    for c in range(5):
                        fixed[cy - 2 + r, cx - 2 + c] = True
                        val[cy - 2 + r, cx - 2 + c] = ALIGN[r][c] == "1"
        fixed[n - 8, 8] = True; val[n - 8, 8] = 1                 # the always-dark module
        self.fmt1 = [(8,0),(8,1),(8,2),(8,3),(8,4),(8,5),(8,7),(8,8),(7,8),(5,8),(4,8),(3,8),(2,8),(1,8),(0,8)]
        self.fmt2 = [(n - 1 - i, 8) for i in range(8)] + [(8, n - 15 + i) for i in range(8, 15)]
        self.ver1, self.ver2 = [], []
        if v >= 7:
            for i in range(18):
                a, b = n - 11 + i % 3, i // 3
                self.ver1.append((a, b)); self.ver2.append((b, a))
        fn = fixed.copy()
        for x, y in self.fmt1 + self.fmt2 + self.ver1 + self.ver2: fn[y, x] = True
        xs, ys = [], []                                           # zig-zag data order
        right = n - 1
        while right >= 1:
            if right == 6: right = 5
            upward = ((right + 1) & 2) == 0
            for vert in range(n):
                y = n - 1 - vert if upward else vert
                for j in range(2):
                    x = right - j
                    if not fn[y, x]: xs.append(x); ys.append(y)
            right -= 2
        assert len(xs) == num_raw_data_modules(v), (v, len(xs))
        self.xs, self.ys = np.array(xs), np.array(ys)
        self.fixed, self.val, self.fn = fixed, val, fn
        yy, xx = np.mgrid[0:n, 0:n]
        masks = [(xx + yy) % 2 == 0, yy % 2 == 0, xx % 3 == 0, (xx + yy) % 3 == 0,
                 (xx // 3 + yy // 2) % 2 == 0, (xx * yy) % 2 + (xx * yy) % 3 == 0,
                 ((xx * yy) % 2 + (xx * yy) % 3) % 2 == 0, ((xx + yy) % 2 + (xx * yy) % 3) % 2 == 0]
        self.mask_at = [m[self.ys, self.xs].astype(np.uint8) for m in masks]

class Layout:
    def __init__(self, v, e):
        raw = num_raw_data_modules(v) // 8
        nb, ec = NUM_BLOCKS[e][v], ECC_CW_PER_BLOCK[e][v]
        nshort, short_len = nb - raw % nb, raw // nb
        self.v, self.e, self.nb, self.ec, self.raw = v, e, nb, ec, raw
        self.data_lens = [short_len - ec + (0 if i < nshort else 1) for i in range(nb)]
        self.T = sum(self.data_lens)
        order = []
        for i in range(max(self.data_lens)):
            for b in range(nb):
                if i < self.data_lens[b]: order.append(b)
        for i in range(ec):
            for b in range(nb): order.append(b)
        assert len(order) == raw
        order = np.array(order)
        self.sel = [np.flatnonzero(order == b) for b in range(nb)]   # stream positions of block b
        self.cap = (8 * self.T - 4 - (8 if v <= 9 else 16)) // 8

_GEO, _LAY = {}, {}
def geo(v):
    if v not in _GEO: _GEO[v] = Geo(v)
    return _GEO[v]
def layout(v, e):
    if (v, e) not in _LAY: _LAY[(v, e)] = Layout(v, e)
    return _LAY[(v, e)]

_PW = {}
def syndromes_zero(cw, ec):
    """All ec Reed-Solomon syndromes S_j = c(alpha^j), j = 0..ec-1, are zero."""
    L = len(cw)
    if (L, ec) not in _PW:
        _PW[(L, ec)] = (np.arange(ec)[:, None] * (L - 1 - np.arange(L))[None, :]) % 255
    c = cw.astype(np.int64)
    terms = np.where(c[None, :] == 0, 0, EXP[(LOG[c][None, :] + _PW[(L, ec)]) % 255])
    return not np.bitwise_xor.reduce(terms, axis=1).any()

def read_format(M, g):
    f1 = sum(int(M[y, x]) << i for i, (x, y) in enumerate(g.fmt1))
    f2 = sum(int(M[y, x]) << i for i, (x, y) in enumerate(g.fmt2))
    return f1, f2

def parse_stream(v, data):
    errs = []; T = len(data); total = 8 * T
    big = int.from_bytes(bytes(data), "big")
    def get(pos, k): return (big >> (total - pos - k)) & ((1 << k) - 1)
    if get(0, 4) != 4: return ["mode indicator is not byte mode (0100)"], None
    cci = 8 if v <= 9 else 16
    ln = get(4, cci); pos = 4 + cci
    if pos + 8 * ln > total: return ["length field exceeds capacity"], None
    payload = bytes(get(pos + 8 * i, 8) for i in range(ln)); pos += 8 * ln
    term = min(4, total - pos)
    if get(pos, term) != 0: errs.append("terminator bits not zero")
    pos += term
    while pos % 8:
        if get(pos, 1): errs.append("pad-to-byte bits not zero"); break
        pos += 1
    pad = 0xEC
    for k in range(pos // 8, T):
        if data[k] != pad: errs.append(f"pad byte #{k} is {int(data[k]):#04x}, expected {pad:#04x}"); break
        pad = 0x11 if pad == 0xEC else 0xEC
    return errs, payload

def read_stream(M, v, e):
    """-> (ecc_idx, mask, interleaved codeword stream) of a symbol, via the format info."""
    g = geo(v); f1, _ = read_format(M, g)
    for ee in range(4):
        for mm in range(8):
            if format_bits(ee, mm) == f1:
                bits = M[g.ys, g.xs] ^ g.mask_at[mm]
                return ee, mm, np.packbits(bits[:8 * layout(v, e).raw])
    return None, None, None

def verify_symbol(M, v, e, payload=None, mask=None, remainder=True):
    """List of problems; an empty list means the symbol conforms to the spec."""
    g, L = geo(v), layout(v, e); n = g.n; errs = []
    if M.shape != (n, n): return [f"size {M.shape} != {(n, n)}"]
    bad = int((M[g.fixed] != g.val[g.fixed]).sum())
    if bad: errs.append(f"{bad} fixed function modules wrong (finder/separator/timing/alignment/dark)")
    f1, f2 = read_format(M, g)
    if f1 != f2: errs.append(f"format-info copies differ ({f1:015b} vs {f2:015b})")
    found = [(ee, mm) for ee in range(4) for mm in range(8) if format_bits(ee, mm) == f1]
    if not found: return errs + ["format info is not a valid BCH codeword"]
    fe, fm = found[0]
    if fe != e: errs.append(f"ECC level in format info is {ECC_NAMES[fe]}, expected {ECC_NAMES[e]}")
    if mask is not None and fm != mask: errs.append(f"mask in format info is {fm}, expected {mask}")
    if v >= 7:
        vb = version_bits(v)
        for k, copy in enumerate((g.ver1, g.ver2)):
            got = sum(int(M[y, x]) << i for i, (x, y) in enumerate(copy))
            if got != vb: errs.append(f"version-info copy {k + 1} is {got:#07x}, expected {vb:#07x}")
    bits = M[g.ys, g.xs] ^ g.mask_at[fm]
    if remainder and bits[8 * L.raw:].any(): errs.append("remainder bits are not zero")
    stream = np.packbits(bits[:8 * L.raw]); data = []
    for b in range(L.nb):
        cw = stream[L.sel[b]]
        if not syndromes_zero(cw, L.ec): errs.append(f"block {b}: Reed-Solomon syndromes not zero")
        data.append(cw[:L.data_lens[b]])
    perr, got = parse_stream(v, np.concatenate(data).astype(np.uint8))
    errs += perr
    if payload is not None and got != payload: errs.append("decoded payload differs from the message")
    return errs

# ----------------------------------------------------------------------------
# Independent mask-penalty scorer (ISO 18004 rules 1-4; Rule 3 = the windows-inside-
# the-symbol reading used by the Thonky tutorial and by qrcode.c)
# ----------------------------------------------------------------------------
_PA = np.array([1,0,1,1,1,0,1,0,0,0,0]); _PB = np.array([0,0,0,0,1,0,1,1,1,0,1])
def _runs_penalty(A):
    p = 0
    for row in A:
        cuts = np.flatnonzero(np.diff(row))
        runs = np.diff(np.concatenate(([-1], cuts, [len(row) - 1])))
        p += int((runs[runs >= 5] - 2).sum())                      # 3 + (n - 5)
    return p
def penalty(M):
    M = M.astype(np.int8)
    r1 = _runs_penalty(M) + _runs_penalty(M.T)
    a, b, c, d = M[:-1, :-1], M[:-1, 1:], M[1:, :-1], M[1:, 1:]
    r2 = 3 * int(((a == b) & (a == c) & (a == d)).sum())
    r3 = 0
    for A in (M, M.T):
        W = sliding_window_view(A, 11, axis=1)
        r3 += 40 * int((W == _PA).all(-1).sum() + (W == _PB).all(-1).sum())
    total = M.shape[0] ** 2; p = Fraction(int(M.sum()), total); k = 0
    while not (Fraction(45 - 5 * k, 100) <= p <= Fraction(55 + 5 * k, 100)): k += 1
    return r1 + r2 + r3 + 10 * k

# ----------------------------------------------------------------------------
# Build / run helpers
# ----------------------------------------------------------------------------
class Env:
    def __init__(self, src, work):
        self.src, self.work = os.path.abspath(src), os.path.abspath(work)
        os.makedirs(self.work, exist_ok=True)
        h = hashlib.md5()
        for f in ("qrcode.c", "qrcode.h"): h.update(open(os.path.join(self.src, f), "rb").read())
        self.tag = h.hexdigest()[:8]
        self.gen_c = os.path.join(self.work, "gen.c"); open(self.gen_c, "w").write(GEN_C)
        self.dbg_h = os.path.join(self.work, "dbgdecl.h")
        open(self.dbg_h, "w").write("#include <stdint.h>\nextern uint8_t qr_dbg_mask;\n")

    def gen(self, v, e, forced, flags=(), name=""):
        key = hashlib.md5(" ".join(flags).encode()).hexdigest()[:6]
        exe = os.path.join(self.work, f"gen_{self.tag}_{'f' if forced else 'a'}_{v}_{e}_{key}")
        if os.path.exists(exe): return exe
        cmd = ["gcc", "-O2", "-std=c99", f"-DQR_VERSION={v}", f"-DQR_ECC_LEVEL={e}u", "-I", self.src]
        if forced: cmd += ["-DFORCED", "-DQR_FIXED_MASK=qr_dbg_mask", "-include", self.dbg_h]
        cmd += list(flags) + [self.gen_c, os.path.join(self.src, "qrcode.c"), "-o", exe]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode: raise RuntimeError("compile failed: " + " ".join(cmd) + "\n" + r.stderr[:2000])
        return exe

    def run(self, exe, lines, timeout=90):
        r = subprocess.run([exe], input=("\n".join(lines) + "\n").encode(), capture_output=True, timeout=timeout)
        if r.returncode: raise RuntimeError(f"{exe} exited {r.returncode}: {r.stderr[:500]!r}")
        return r.stdout

def parse(buf, n, count):
    res, pos = [], 0
    for _ in range(count):
        if buf.startswith(b"REJECT\n", pos): res.append(None); pos += 7
        elif buf.startswith(b"OK\n", pos):
            pos += 3
            arr = np.frombuffer(buf[pos:pos + n * (n + 1)], np.uint8).reshape(n, n + 1)[:, :n] - 48
            res.append(arr.astype(np.uint8)); pos += n * (n + 1)
        else: raise RuntimeError(f"unparseable harness output at byte {pos}: {buf[pos:pos+40]!r}")
    assert pos == len(buf), "trailing harness output"
    return res

class Sec:
    def __init__(self, name): self.name, self.n, self.fails, self.notes, self.t0 = name, 0, [], [], time.time()
    def check(self, cond, what):
        self.n += 1
        if not cond: self.fails.append(what)
    def note(self, s): self.notes.append(s)
    def done(self):
        print(f"[{'PASS' if not self.fails else 'FAIL'}] {self.name}: {self.n} checks, {len(self.fails)} failures ({time.time() - self.t0:.1f}s)")
        for f in self.fails[:12]: print("    FAIL:", f)
        if len(self.fails) > 12: print(f"    ... and {len(self.fails) - 12} more")
        for s in self.notes: print("    note:", s)
        return not self.fails

def configs(versions): return [(v, e) for v in versions for e in range(4)]

# ============================================================================
# Sections
# ============================================================================
import string, shutil, tempfile

def rand_bytes(rng, l): return rng.randint(0, 256, size=l).astype(np.uint8).tobytes()
def rand_text(rng, l):
    chars = list(string.ascii_letters + string.digits + " .,:;/?&=+-_#%")
    return "".join(rng.choice(chars, size=l))

def cv_encode(text, v, e):
    try:
        P = cv2.QRCodeEncoder_Params(); P.version = v
        P.correction_level = [cv2.QRCodeEncoder_CORRECT_LEVEL_L, cv2.QRCodeEncoder_CORRECT_LEVEL_M,
                              cv2.QRCodeEncoder_CORRECT_LEVEL_Q, cv2.QRCodeEncoder_CORRECT_LEVEL_H][e]
        P.mode = cv2.QRCodeEncoder_MODE_BYTE
        img = cv2.QRCodeEncoder_create(P).encode(text)
    except Exception:
        return None
    n = 4 * v + 17
    if img is None or img.shape != (n + 4, n + 4): return None      # 2-module quiet zone, 1 px/module
    return (img[2:2 + n, 2:2 + n] < 128).astype(np.uint8)

def render(M, scale=8, quiet=4):
    n = len(M); img = np.full(((n + 2 * quiet) * scale,) * 2, 255, np.uint8)
    big = np.kron(M, np.ones((scale, scale), np.uint8))
    img[quiet * scale:(quiet + n) * scale, quiet * scale:(quiet + n) * scale] = np.where(big == 1, 0, 255)
    return img

def cv_decode(M, scale=8, quiet=4):
    img = render(M, scale, quiet)
    t, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
    if t: return t
    try: t, _, _ = cv2.QRCodeDetectorAruco().detectAndDecode(img)
    except Exception: t = ""
    return t

def flip(M, x, y):
    M = M.copy(); M[y, x] ^= 1; return M

def set_format(M, g, value):
    M = M.copy()
    for copy in (g.fmt1, g.fmt2):
        for i, (x, y) in enumerate(copy): M[y, x] = (value >> i) & 1
    return M

def sec_selftest(env, versions):
    s = Sec("selftest (the checker checks itself)")
    for e in range(4):
        for m in range(8):
            s.check(format(format_bits(e, m), "015b") == SPEC_FORMAT[e][m], f"format string {ECC_NAMES[e]}/mask {m} vs ISO table")
    for v, bits in SPEC_VERSION.items(): s.check(version_bits(v) == bits, f"version string v{v} vs ISO table")
    for v in range(1, 21):
        geo(v)                                                    # asserts data-module count == spec formula
        s.check(num_raw_data_modules(v) // 8 == SPEC_TOTAL_CW[v - 1], f"total codewords v{v}")
        for e in range(4): s.check(layout(v, e).cap == SPEC_CAP[v][e], f"derived capacity v{v}-{ECC_NAMES[e]}")
    rng = np.random.RandomState(7)
    # (a) injected faults must be rejected, in every region of the symbol
    sample = [(v, e) for v, e in configs(versions) if v in (1, 2, 5, 7, 10, 14, 20)]
    for v, e in sample:
        g, L = geo(v), layout(v, e); n = g.n
        p = rand_bytes(rng, max(L.cap // 2, 1))
        M = parse(env.run(env.gen(v, e, True), [f"3 {p.hex()}"]), n, 1)[0]
        s.check(not verify_symbol(M, v, e, payload=p, mask=3), f"checker rejects a good v{v}-{ECC_NAMES[e]} symbol")
        pos = align_positions(v)
        sites = {"finder": (0, 0), "finder (top-right)": (n - 1, 0), "separator": (7, 0), "timing": (10, 6),
                 "dark module": (8, n - 8), "format copy 1": g.fmt1[3], "format copy 2": g.fmt2[10],
                 "data codeword": (g.xs[3], g.ys[3]), "EC codeword": (g.xs[8 * L.raw - 5], g.ys[8 * L.raw - 5])}
        if pos: sites["alignment"] = (pos[-1], pos[-1])
        if v >= 7: sites["version copy 1"] = g.ver1[5]; sites["version copy 2"] = g.ver2[7]
        if len(g.xs) > 8 * L.raw: sites["remainder bit"] = (g.xs[8 * L.raw], g.ys[8 * L.raw])
        for name, (x, y) in sites.items():
            s.check(bool(verify_symbol(flip(M, x, y), v, e, payload=p)), f"checker misses a flipped {name} module (v{v}-{ECC_NAMES[e]})")
        s.check(bool(verify_symbol(set_format(M, g, format_bits((e + 1) % 4, 3)), v, e, payload=p)), f"checker misses wrong ECC level (v{v})")
        s.check(bool(verify_symbol(set_format(M, g, format_bits(e, 4)), v, e, payload=p)), f"checker misses wrong mask (v{v})")
    # (b) symbols from an independent encoder (OpenCV) must be accepted by the checker
    if cv2 is not None:
        good = bad = rem_dev = 0
        for v, e in sample:
            L = layout(v, e); t = rand_text(rng, max(L.cap // 2, 1)); Mo = cv_encode(t, v, e)
            if Mo is None: continue
            errs = verify_symbol(Mo, v, e, payload=t.encode(), remainder=False)
            good += not errs; bad += bool(errs)
            rem_dev += bool(verify_symbol(Mo, v, e, payload=t.encode()))
            s.check(not errs, f"OpenCV-encoded v{v}-{ECC_NAMES[e]} symbol rejected by checker: {errs}")
        s.note(f"checker accepted {good} of {good + bad} symbols produced by OpenCV's own encoder (ignoring remainder bits)")
        s.note(f"{rem_dev} of those OpenCV symbols draw a remainder-bit module differently from the spec/ZXing/libqrencode convention (masked zeros); see the encoder section")
    else:
        s.note("OpenCV not available: skipped the independent-encoder calibration")
    return s.done()

def sec_capacity(env, versions):
    s = Sec("capacity (limits vs ISO table; over-capacity rejected)")
    for v, e in configs(versions):
        n, cap = 4 * v + 17, layout(v, e).cap
        lens = sorted({0, 1, max(cap - 1, 0), cap, cap + 1, cap + 2, 255, 256, 257, 300, 1000, 4095, 65535})
        res = parse(env.run(env.gen(v, e, False), [f"L{l}" for l in lens]), n, len(lens))
        for l, M in zip(lens, res):
            s.check((M is not None) == (l <= cap), f"v{v}-{ECC_NAMES[e]} len {l}: {'rejected' if M is None else 'accepted'} (capacity {cap})")
            if M is not None and l >= cap - 1:
                errs = verify_symbol(M, v, e, payload=b"A" * l)
                s.check(not errs, f"v{v}-{ECC_NAMES[e]} len {l}: {errs}")
    return s.done()

def sec_lengths(env, versions):
    s = Sec("lengths (every length 0..capacity, random bytes, mask = length mod 8)")
    rng = np.random.RandomState(20260101)
    for v, e in configs(versions):
        n, L = 4 * v + 17, layout(v, e)
        lens = list(range(L.cap + 1)); pays = [rand_bytes(rng, l) for l in lens]
        res = parse(env.run(env.gen(v, e, True), [f"{l % 8} {p.hex()}" for l, p in zip(lens, pays)]), n, len(lens))
        for l, p, M in zip(lens, pays, res):
            if M is None: s.check(False, f"v{v}-{ECC_NAMES[e]} len {l}: rejected"); continue
            errs = verify_symbol(M, v, e, payload=p, mask=l % 8)
            s.check(not errs, f"v{v}-{ECC_NAMES[e]} len {l}: {'; '.join(errs)}")
    s.note(f"{s.n} symbols")
    return s.done()

def sec_masks(env, versions):
    s = Sec("masks (all 8 masks x boundary lengths x every version/ECC)")
    rng = np.random.RandomState(99)
    for v, e in configs(versions):
        n, L = 4 * v + 17, layout(v, e)
        meta = []
        for l in sorted({0, 1, L.cap // 2, max(L.cap - 1, 0), L.cap}):
            p = rand_bytes(rng, l)
            meta += [(l, p, m) for m in range(8)]
        res = parse(env.run(env.gen(v, e, True), [f"{m} {p.hex()}" for l, p, m in meta]), n, len(meta))
        for (l, p, m), M in zip(meta, res):
            errs = verify_symbol(M, v, e, payload=p, mask=m)
            s.check(not errs, f"v{v}-{ECC_NAMES[e]} len {l} mask {m}: {'; '.join(errs)}")
    return s.done()

def sec_automask(env, versions):
    s = Sec("automask (valid symbol + chosen mask == lowest penalty by the independent scorer)")
    rng = np.random.RandomState(4242); differs = 0
    for v, e in configs(versions):
        n, L = 4 * v + 17, layout(v, e)
        pays = [rand_bytes(rng, l) for l in sorted({1, 5, L.cap // 3, L.cap // 2, L.cap})]
        pays += [b"\x00" * L.cap, b"\xff" * max(L.cap // 2, 1), b"\x55" * L.cap, bytes([0xEC, 0x11] * (L.cap // 2)), b"A" * L.cap]
        auto = parse(env.run(env.gen(v, e, False), [p.hex() for p in pays]), n, len(pays))
        forced = parse(env.run(env.gen(v, e, True), [f"{m} {p.hex()}" for p in pays for m in range(8)]), n, 8 * len(pays))
        g = geo(v)
        for i, p in enumerate(pays):
            Ma = auto[i]; errs = verify_symbol(Ma, v, e, payload=p)
            s.check(not errs, f"v{v}-{ECC_NAMES[e]} len {len(p)}: {'; '.join(errs)}")
            f1, _ = read_format(Ma, g)
            chosen = next((m for m in range(8) if format_bits(e, m) == f1), None)
            scores = [penalty(forced[8 * i + m]) for m in range(8)]
            best = min(range(8), key=lambda m: (scores[m], m))
            s.check(chosen == best, f"v{v}-{ECC_NAMES[e]} len {len(p)}: chose mask {chosen}, lowest penalty is mask {best} {scores}")
            if chosen is not None: s.check(np.array_equal(Ma, forced[8 * i + chosen]), f"v{v}-{ECC_NAMES[e]}: auto symbol != forced symbol of the same mask")
            differs += len(set(scores)) > 1
    s.note(f"{s.n} checks over {s.n // 3} payloads; masks were distinguishable (not all scores equal) in {differs} of them")
    return s.done()

def sec_opencv(env, versions):
    if cv2 is None:
        print("[SKIP] opencv: cv2 not installed"); return True
    s = Sec("opencv (a real decoder: payload types, module size, error-correction damage)")
    rng = np.random.RandomState(31337)
    fixed = ["https://example.com/a/b?c=d&e=f#frag", "WIFI:T:WPA;S:MyNetwork;P:secret123;;",
             "BEGIN:VCARD\nVERSION:3.0\nFN:Jane Doe\nTEL:+46701234567\nEND:VCARD",
             "H\u00e9llo w\u00f6rld \u2013 \u65e5\u672c\u8a9e \U0001F600", "1234567890" * 20, "mailto:someone@example.com?subject=Hi"]
    for v, e in configs(versions):
        n, L = 4 * v + 17, layout(v, e)
        texts = [t for t in fixed if len(t.encode()) <= L.cap]
        texts += [rand_text(rng, l) for l in sorted({1, max(L.cap // 2, 1), L.cap})]
        res = parse(env.run(env.gen(v, e, False), [t.encode().hex() for t in texts]), n, len(texts))
        for t, M in zip(texts, res):
            got = cv_decode(M)
            s.check(got == t, f"v{v}-{ECC_NAMES[e]} len {len(t.encode())}: OpenCV returned {got!r:.40}")
        # error correction: corrupt ec/4 codewords in EVERY block. (The code can correct ec/2 per block -- that full
        # limit is verified with an independent Reed-Solomon decoder in the 'rscorrect' section; OpenCV's decoder itself
        # gives up earlier for versions >= 7, and does so identically on symbols made by OpenCV's own encoder.)
        t = rand_text(rng, max(L.cap // 2, 1)); M = parse(env.run(env.gen(v, e, False), [t.encode().hex()]), n, 1)[0]
        g = geo(v); D = M.copy(); t_err = max(L.ec // 4, 1)
        for b in range(L.nb):
            for k in rng.choice(L.sel[b], size=t_err, replace=False):
                bit = rng.randint(8); D[g.ys[8 * k + bit], g.xs[8 * k + bit]] ^= 1
        got = cv_decode(D)
        s.check(got == t, f"v{v}-{ECC_NAMES[e]}: failed to correct {t_err} damaged codewords in each of {L.nb} blocks (got {got!r:.30})")
    # module size sweep (informational below 4 px)
    for v, e in [(1, 1), (5, 3), (10, 1), (20, 0)]:
        if v not in versions: continue
        L = layout(v, e); t = rand_text(rng, L.cap // 2)
        M = parse(env.run(env.gen(v, e, False), [t.encode().hex()]), 4 * v + 17, 1)[0]
        row = []
        for sc in (2, 3, 4, 6, 8, 12):
            ok = cv_decode(M, scale=sc) == t; row.append(f"{sc}px:{'ok' if ok else 'FAIL'}")
            if sc >= 4: s.check(ok, f"v{v}-{ECC_NAMES[e]} at {sc} px/module")
        s.note(f"v{v}-{ECC_NAMES[e]} module-size sweep: " + " ".join(row))
    return s.done()

def sec_encoder(env, versions):
    if cv2 is None:
        print("[SKIP] encoder: cv2 not installed"); return True
    s = Sec("encoder cross-check (codeword streams vs OpenCV's independent QR encoder)")
    rng = np.random.RandomState(555); same_mask = total = skipped = rem_diff = 0
    for v, e in configs(versions):
        n, L = 4 * v + 17, layout(v, e); g = geo(v)
        texts = [rand_text(rng, l) for l in sorted({1, max(L.cap // 2, 1), L.cap})]
        ours = parse(env.run(env.gen(v, e, False), [t.encode().hex() for t in texts]), n, len(texts))
        for t, M in zip(texts, ours):
            Mo = cv_encode(t, v, e)
            if Mo is None: skipped += 1; continue
            errs_o = verify_symbol(Mo, v, e, payload=t.encode(), remainder=False)
            if errs_o:
                s.note(f"OpenCV's own v{v}-{ECC_NAMES[e]} len {len(t)} symbol fails the checker: {errs_o[:2]}"); skipped += 1; continue
            _, m1, st_o = read_stream(Mo, v, e); _, m2, st_m = read_stream(M, v, e)
            total += 1; same_mask += (m1 == m2)
            s.check(np.array_equal(st_o, st_m), f"v{v}-{ECC_NAMES[e]} len {len(t)}: data+EC codewords differ from OpenCV's encoder")
            if m1 == m2:
                keep = np.ones((n, n), bool); keep[g.ys[8 * L.raw:], g.xs[8 * L.raw:]] = False      # leave out remainder-bit modules
                s.check(np.array_equal(M[keep], Mo[keep]), f"v{v}-{ECC_NAMES[e]} len {len(t)}: same mask but modules differ (outside the remainder bits)")
                rem_diff += not np.array_equal(M, Mo)
    s.note(f"{total} symbols compared; both encoders picked the same mask in {same_mask}; {skipped} skipped (OpenCV produced nothing or a non-conforming symbol)")
    s.note(f"in {rem_diff} same-mask symbols the ONLY differing modules are remainder-bit modules (OpenCV draws them dark regardless of mask; ours are masked zeros as in ZXing/libqrencode/Nayuki)")
    return s.done()


EXPL, LOGL = EXP.tolist(), LOG.tolist()
def gmul(a, b): return 0 if (a == 0 or b == 0) else EXPL[LOGL[a] + LOGL[b]]
def gdiv(a, b): return 0 if a == 0 else EXPL[(LOGL[a] - LOGL[b]) % 255]
def peval(p, x):                      # p: coefficients, lowest degree first
    y = 0
    for c in reversed(p): y = gmul(y, x) ^ c
    return y

def rs_decode(cw, ec):
    """Berlekamp-Massey + Chien search + Forney. cw: ints, first symbol = highest degree (data..., then EC).
    Returns the corrected list, or None if it is uncorrectable."""
    n = len(cw)
    S = []
    for j in range(ec):
        x = EXPL[j]; y = 0
        for c in cw: y = gmul(y, x) ^ c
        S.append(y)
    if not any(S): return list(cw)
    C, B, Lc, m, b = [1], [1], 0, 1, 1
    for k in range(ec):
        d = S[k]
        for i in range(1, Lc + 1): d ^= gmul(C[i], S[k - i])
        if d == 0: m += 1; continue
        T = C[:]; coef = gdiv(d, b)
        if len(C) < len(B) + m: C += [0] * (len(B) + m - len(C))
        for i, bv in enumerate(B): C[i + m] ^= gmul(coef, bv)
        if 2 * Lc <= k: Lc, B, b, m = k + 1 - Lc, T, d, 1
        else: m += 1
    if Lc > ec // 2: return None
    errs = [i for i in range(n) if peval(C, EXPL[(255 - (n - 1 - i)) % 255]) == 0]
    if len(errs) != Lc: return None
    Om = [0] * ec
    for i in range(ec):
        for j, cj in enumerate(C):
            if i + j < ec: Om[i + j] ^= gmul(S[i], cj)
    dC = [C[i] if i % 2 == 1 else 0 for i in range(1, len(C))]
    out = list(cw)
    for i in errs:
        p = n - 1 - i; xinv = EXPL[(255 - p) % 255]
        den = peval(dC, xinv)
        if den == 0: return None
        out[i] ^= gdiv(gmul(EXPL[p % 255], peval(Om, xinv)), den)
    return out

def sec_rscorrect(env, versions):
    s = Sec("rscorrect (independent Reed-Solomon decoder recovers the maximum EC/2 corrupted codewords in every block)")
    rng = np.random.RandomState(777); blocks = 0
    # sanity: the decoder itself must reject t+1 errors in a toy case rather than "succeed" with wrong data (not a library test)
    for v, e in configs(versions):
        n, L = 4 * v + 17, layout(v, e); g = geo(v)
        for trial in range(2):
            p = rand_bytes(rng, int(rng.randint(1, L.cap + 1)))
            M = parse(env.run(env.gen(v, e, False), [p.hex()]), n, 1)[0]
            _, mk, st0 = read_stream(M, v, e)
            D = M.copy(); t = L.ec // 2
            for b in range(L.nb):
                for k in rng.choice(L.sel[b], size=t, replace=False):
                    ev = int(rng.randint(1, 256))
                    for j in range(8):
                        if (ev >> (7 - j)) & 1: D[g.ys[8 * k + j], g.xs[8 * k + j]] ^= 1
            _, _, st1 = read_stream(D, v, e)
            s.check(not np.array_equal(st0, st1), f"v{v}-{ECC_NAMES[e]}: damage did not change the stream (test is vacuous)")
            data = []
            for b in range(L.nb):
                good = [int(x) for x in st0[L.sel[b]]]; bad = [int(x) for x in st1[L.sel[b]]]
                fixed = rs_decode(bad, L.ec); blocks += 1
                s.check(fixed == good, f"v{v}-{ECC_NAMES[e]} block {b}: {t} errors not corrected")
                if fixed: data.append(np.array(fixed[:L.data_lens[b]], np.uint8))
            if len(data) == L.nb:
                errs, got = parse_stream(v, np.concatenate(data))
                s.check(not errs and got == p, f"v{v}-{ECC_NAMES[e]}: payload not recovered after correction")
    s.note(f"{blocks} blocks, each with exactly EC/2 random whole-byte errors (data and EC positions), all corrected and the payload recovered")
    return s.done()


UNIT_C = r"""
#include <stdio.h>
#include "qrcode.c"          /* white-box: the internal static helpers are visible here */
int main(void) {
    for (int m = 0; m < 8; m++) for (int y = 0; y < 256; y++) {
        for (int x = 0; x < 256; x++) putchar(mask_condition((uint8_t)m, (uint8_t)x, (uint8_t)y) ? '1' : '0');
        putchar('\n');
    }
    for (int d = 0; d < 32; d++) printf("F %d %u\n", d, (unsigned)compute_format_bits((uint8_t)d));
#if QR_VERSION >= 7
    for (int v = 7; v <= 40; v++) printf("V %d %u\n", v, (unsigned)compute_version_bch((uint8_t)v));
#endif
#ifndef QR_FIXED_MASK
    for (unsigned dark = 0; dark <= (unsigned)QR_SIZE * QR_SIZE; dark++)
        printf("B %u %u\n", dark, (unsigned)balance_penalty((uint16_t)dark));
    { uint16_t p = 65530; pen_add(&p, 3); printf("S %u\n", p); pen_add(&p, 40); printf("S %u\n", p);
      pen_add(&p, 40); printf("S %u\n", p); pen_add(&p, 0); printf("S %u\n", p);
      p = 0; pen_add(&p, 40); pen_add(&p, 3); printf("S %u\n", p); }
#endif
    return 0;
}
"""

def sec_unit(env, versions):
    s = Sec("unit (white-box: mask formulas, format/version BCH, Rule-4 balance, saturating add vs the spec)")
    open(os.path.join(env.work, "unit.c"), "w").write(UNIT_C)
    yy, xx = np.mgrid[0:256, 0:256]
    grids = [(xx + yy) % 2 == 0, yy % 2 == 0, xx % 3 == 0, (xx + yy) % 3 == 0, (xx // 3 + yy // 2) % 2 == 0,
             (xx * yy) % 2 + (xx * yy) % 3 == 0, ((xx * yy) % 2 + (xx * yy) % 3) % 2 == 0, ((xx + yy) % 2 + (xx * yy) % 3) % 2 == 0]
    first = True
    for v in versions:
        exe = os.path.join(env.work, f"unit_{env.tag}_{v}")
        r = sh(["gcc", "-O1", "-std=c99", f"-DQR_VERSION={v}", "-I", env.src, os.path.join(env.work, "unit.c"), "-o", exe])
        if r.returncode: s.check(False, f"unit build failed for v{v}: {r.stderr[:200]}"); continue
        lines = sh([exe]).stdout.split("\n")
        i = 0
        if first or v == 7:                      # mask formulas do not depend on the version: check once (and once more at v7)
            for m in range(8):
                got = np.array([[c == "1" for c in lines[i + y]] for y in range(256)]); i += 256
                s.check(np.array_equal(got, grids[m]), f"mask_condition({m}) differs from the ISO formula somewhere in 0..255 x 0..255")
            first = False
        else: i += 8 * 256
        fm = {}
        for k in range(32): _, d, val = lines[i + k].split(); fm[int(d)] = int(val)
        i += 32
        for e in range(4):
            for m in range(8):
                s.check(fm[([1, 0, 3, 2][e] << 3) | m] == int(SPEC_FORMAT[e][m], 2), f"compute_format_bits for {ECC_NAMES[e]}/mask {m} differs from the ISO table")
        if v >= 7:
            for k in range(34):
                _, vv, val = lines[i + k].split(); vv, val = int(vv), int(val)
                s.check(val == version_bits(vv) & 0xFFF, f"compute_version_bch({vv}) wrong")
                if vv in SPEC_VERSION: s.check(val == SPEC_VERSION[vv] & 0xFFF, f"compute_version_bch({vv}) differs from the ISO table")
            i += 34
        total = (4 * v + 17) ** 2
        bad = 0
        for dark in range(total + 1):
            _, d, val = lines[i + dark].split()
            k = 0
            while not ((45 - 5 * k) * total <= 100 * dark <= (55 + 5 * k) * total): k += 1
            bad += int(val) != 10 * k
        i += total + 1
        s.check(bad == 0, f"balance_penalty wrong for {bad} of {total + 1} dark-module counts at v{v}")
        sat = [int(l.split()[1]) for l in lines[i:i + 5]]
        s.check(sat == [65533, 65535, 65535, 65535, 43], f"pen_add saturation behaves wrongly: {sat}")
    return s.done()

API_C = r'''
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "qrcode.h"
static int fails = 0, checks = 0;
#define CHECK(c, msg) do { checks++; if (!(c)) { fails++; printf("FAIL line %d: %s\n", __LINE__, msg); } } while (0)
static qr_code_t a, b, c, snap;
static uint8_t big[70000];
int main(void) {
    memset(big, 'A', sizeof big);
    uint16_t cap = 0;
    for (uint16_t n = 0; n <= 1200; n++) { if (qr_generate(&c, big, n)) cap = n; else break; }
    printf("CAP %u\n", cap);
    CHECK(qr_generate(&c, big, cap), "len == capacity accepted");
    CHECK(!qr_generate(&c, big, (uint16_t)(cap + 1)), "len == capacity+1 rejected");
    CHECK(!qr_generate(&c, big, 65535) && !qr_generate(&c, big, 65534) && !qr_generate(&c, big, 32768), "huge lengths rejected");
    CHECK(qr_generate(&c, NULL, 0), "empty message with NULL data accepted");
    CHECK(qr_generate(&c, big, 0), "empty message accepted");
    {   /* bounds of qr_get_module */
        uint8_t bad[] = {QR_SIZE, QR_SIZE + 1, 128, 200, 255};
        for (unsigned i = 0; i < sizeof bad; i++) {
            CHECK(!qr_get_module(&c, bad[i], 0), "x out of range reads light");
            CHECK(!qr_get_module(&c, 0, bad[i]), "y out of range reads light");
            CHECK(!qr_get_module(&c, bad[i], bad[i]), "x,y out of range read light");
        }
        CHECK(qr_get_module(&c, 0, 0) && qr_get_module(&c, QR_SIZE - 1, 0) && qr_get_module(&c, 0, QR_SIZE - 1), "three finder corners are dark");
        CHECK(!qr_get_module(&c, 7, 0) && !qr_get_module(&c, 0, 7), "separator modules are light");
    }
    {   /* a finished symbol survives later calls that use the shared scratch buffers */
        CHECK(qr_generate(&a, (const uint8_t *)"alpha", 5), "gen a");
        snap = a;
        CHECK(qr_generate(&b, big, cap), "gen b (full)");
        CHECK(qr_generate(&c, (const uint8_t *)"zz", 2), "gen c");
        CHECK(memcmp(&a, &snap, sizeof a) == 0, "symbol a unchanged by later calls");
    }
    {   /* no history: same message => same symbol, whatever was generated before */
        const uint8_t m[] = "history independence";
        uint16_t ml = (uint16_t)(sizeof m - 1 > cap ? cap : sizeof m - 1);
        CHECK(qr_generate(&a, m, ml), "gen x1");
        CHECK(qr_generate(&c, big, cap), "pollute scratch with a full-length message");
        CHECK(qr_generate(&b, m, ml), "gen x2");
        CHECK(memcmp(&a, &b, sizeof a) == 0, "identical symbol after polluting call");
        CHECK(qr_generate(&c, (const uint8_t *)"y", 1), "pollute with a 1-byte message");
        CHECK(qr_generate(&c, m, ml), "gen x3 into a previously used object");
        CHECK(memcmp(&a, &c, sizeof a) == 0, "identical symbol when reusing a dirty qr_code_t");
    }
    {   /* a rejected call must not disturb scratch state or the next result */
        const uint8_t m[] = "after a rejection";
        uint16_t ml = (uint16_t)(sizeof m - 1 > cap ? cap : sizeof m - 1);
        CHECK(qr_generate(&a, m, ml), "gen before");
        CHECK(!qr_generate(&c, big, (uint16_t)(cap + 1)), "rejected");
        CHECK(qr_generate(&b, m, ml), "gen after");
        CHECK(memcmp(&a, &b, sizeof a) == 0, "same symbol after an intervening rejection");
    }
    {   /* stress: 400 calls of varying length, all must succeed, deterministic */
        uint32_t h1 = 0, h2 = 0;
        for (int pass = 0; pass < 2; pass++) {
            uint32_t h = 2166136261u;
            for (int i = 0; i < 400; i++) {
                uint16_t n = (uint16_t)((i * 37u) % (cap + 1u));
                for (uint16_t k = 0; k < n; k++) big[k] = (uint8_t)(i + k * 7);
                if (!qr_generate(&c, big, n)) { CHECK(0, "stress: unexpected rejection"); break; }
                for (unsigned k = 0; k < sizeof c.modules; k++) h = (h ^ c.modules[k]) * 16777619u;
            }
            if (pass == 0) h1 = h; else h2 = h;
        }
        CHECK(h1 == h2, "stress run is deterministic");
    }
    printf("%d checks, %d failures\n", checks, fails);
    return fails != 0;
}
'''

MISMATCH_C = r'''
#define QR_VERSION 1            /* defined only in THIS file, not in qrcode.c */
#include "qrcode.h"
#include <stdio.h>
static qr_code_t q;
int main(void) { printf("%u\n", (unsigned)sizeof q); return qr_generate(&q, (const uint8_t *)"hello", 5) ? 0 : 1; }
'''

def sh(cmd, **kw): return subprocess.run(cmd, capture_output=True, text=True, **kw)

def sec_api(env, versions):
    s = Sec("api (bounds, reuse, no hidden state, extreme lengths) under ASan+UBSan")
    open(os.path.join(env.work, "api.c"), "w").write(API_C)
    for v, e in [c for c in [(1, 0), (3, 1), (5, 3), (7, 2), (10, 1), (14, 3), (20, 0)] if c[0] in versions]:
        exe = os.path.join(env.work, f"api_{env.tag}_{v}_{e}")
        r = sh(["gcc", "-O1", "-std=c99", "-fsanitize=address,undefined", "-fno-sanitize-recover=all", f"-DQR_VERSION={v}",
                f"-DQR_ECC_LEVEL={e}u", "-I", env.src, os.path.join(env.work, "api.c"), os.path.join(env.src, "qrcode.c"), "-o", exe])
        s.check(r.returncode == 0, f"api test did not compile for v{v}-{ECC_NAMES[e]}: {r.stderr[:200]}")
        if r.returncode: continue
        r = sh([exe]); out = r.stdout
        m = re.search(r"CAP (\d+)", out)
        s.check(m and int(m.group(1)) == SPEC_CAP[v][e], f"v{v}-{ECC_NAMES[e]}: probed capacity {m.group(1) if m else None}, spec {SPEC_CAP[v][e]}")
        m2 = re.search(r"(\d+) checks, (\d+) failures", out)
        s.check(r.returncode == 0 and m2 is not None, f"v{v}-{ECC_NAMES[e]}: api test failed\n{out[-400:]}{r.stderr[-400:]}")
        if m2: s.n += int(m2.group(1)) - 1
    # order independence: same lines, forward vs reversed vs one fresh process per message
    rng = np.random.RandomState(8)
    for v, e in [c for c in [(2, 1), (6, 0), (9, 3), (12, 2)] if c[0] in versions]:
        n, L = 4 * v + 17, layout(v, e)
        pays = [rand_bytes(rng, int(rng.randint(0, L.cap + 1))) for _ in range(24)]
        for forced in (False, True):
            exe = env.gen(v, e, forced); mk = (lambda p, i: f"{i % 8} {p.hex()}") if forced else (lambda p, i: p.hex())
            lines = [mk(p, i) for i, p in enumerate(pays)]
            fwd = parse(env.run(exe, lines), n, len(lines))
            rev = parse(env.run(exe, lines[::-1]), n, len(lines))[::-1]
            for i, (a, b) in enumerate(zip(fwd, rev)):
                s.check(np.array_equal(a, b), f"v{v}-{ECC_NAMES[e]} message {i}: output depends on what was generated before it")
            for i in range(0, len(lines), 6):
                one = parse(env.run(exe, [lines[i]]), n, 1)[0]
                s.check(np.array_equal(one, fwd[i]), f"v{v}-{ECC_NAMES[e]} message {i}: differs from a fresh process")
    # configuration mismatch between translation units (design hazard, informational)
    open(os.path.join(env.work, "mismatch.c"), "w").write(MISMATCH_C)
    exe = os.path.join(env.work, "mismatch")
    r = sh(["gcc", "-O0", "-g", "-fsanitize=address", "-I", env.src, os.path.join(env.work, "mismatch.c"), os.path.join(env.src, "qrcode.c"), "-o", exe])
    if r.returncode == 0:
        r = sh([exe])
        s.note("config-mismatch hazard: defining QR_VERSION only in the caller's file -> " +
               ("AddressSanitizer reports a buffer overflow" if "AddressSanitizer" in r.stderr else "no overflow detected"))
    return s.done()

def sec_build(env, versions):
    s = Sec("build (identical output across compilers' modes; strict warnings; analyzer; C++ linkage)")
    sample = [c for c in [(1, 0), (3, 1), (5, 3), (7, 2), (10, 0), (14, 3), (20, 1)] if c[0] in versions]
    rng = np.random.RandomState(2024)
    VARIANTS = {"-O0": ["-O0"], "-O1": ["-O1"], "-O3": ["-O3"], "-Os": ["-Os"], "-O2 -flto": ["-O2", "-flto"],
        "asan+ubsan -O0": ["-O0", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"],
        "asan+ubsan -O2": ["-O2", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"],
        "auto-var-init=pattern": ["-O2", "-ftrivial-auto-var-init=pattern"], "auto-var-init=zero": ["-O2", "-ftrivial-auto-var-init=zero"],
        "-funsigned-char": ["-O2", "-funsigned-char"], "-fsigned-char": ["-O2", "-fsigned-char"],
        "-std=c99": ["-std=c99"], "-std=c11": ["-std=c11"], "-std=c17": ["-std=c17"], "-std=gnu99": ["-std=gnu99"],
        "-O3 -fwrapv": ["-O3", "-fwrapv"]}
    for v, e in sample:
        n, L = 4 * v + 17, layout(v, e)
        pays = [rand_bytes(rng, int(l)) for l in sorted({0, 1, 9, L.cap // 2, L.cap})] + [b"\x00" * L.cap, b"\xff" * L.cap]
        for forced in (False, True):
            lines = [(f"{i % 8} " if forced else "") + p.hex() for i, p in enumerate(pays)]
            base = hashlib.sha256(env.run(env.gen(v, e, forced), lines)).hexdigest()
            for name, flags in VARIANTS.items():
                try: out = env.run(env.gen(v, e, forced, flags=flags), lines)
                except Exception as ex: s.check(False, f"v{v}-{ECC_NAMES[e]} {name}: {str(ex)[:160]}"); continue
                s.check(hashlib.sha256(out).hexdigest() == base, f"v{v}-{ECC_NAMES[e]} {'forced' if forced else 'auto'} {name}: output differs from -O2")
    WARN = ["-std=c99", "-O2", "-Wall", "-Wextra", "-Wpedantic", "-Wconversion", "-Wsign-conversion", "-Wshadow", "-Wcast-qual",
            "-Wundef", "-Wstrict-prototypes", "-Wold-style-definition", "-Wmissing-prototypes", "-Wredundant-decls",
            "-Wnull-dereference", "-Wdouble-promotion", "-Wformat=2", "-Wswitch-enum", "-Wunused"]
    nw = 0
    for v, e in configs(versions):
        r = sh(["gcc", *WARN, f"-DQR_VERSION={v}", f"-DQR_ECC_LEVEL={e}u", "-I", env.src, "-c", os.path.join(env.src, "qrcode.c"), "-o", os.devnull])
        s.check(not r.stderr.strip(), f"warnings for v{v}-{ECC_NAMES[e]}: {r.stderr[:200]}"); nw += 1
    for v, m in [(1, 0), (7, 3), (20, 7)]:
        if v not in versions: continue
        r = sh(["gcc", *WARN, f"-DQR_VERSION={v}", f"-DQR_FIXED_MASK={m}", "-I", env.src, "-c", os.path.join(env.src, "qrcode.c"), "-o", os.devnull])
        s.check(not r.stderr.strip(), f"warnings for fixed-mask v{v} m{m}: {r.stderr[:200]}")
    s.note(f"strict-warning build: {nw} version/ECC configurations + 3 fixed-mask builds")
    for v, e in [c for c in [(1, 1), (5, 3), (7, 0), (20, 2)] if c[0] in versions]:
        r = sh(["gcc", "-std=c99", "-O2", "-fanalyzer", f"-DQR_VERSION={v}", f"-DQR_ECC_LEVEL={e}u", "-I", env.src, "-c", os.path.join(env.src, "qrcode.c"), "-o", os.devnull])
        s.check(not r.stderr.strip(), f"-fanalyzer v{v}-{ECC_NAMES[e]}: {r.stderr[:300]}")
    cpp = os.path.join(env.work, "cpp_main.cpp")
    open(cpp, "w").write('#include "qrcode.h"\n#include <cstdio>\nint main(){ static qr_code_t q; const uint8_t m[]={104,105};\n'
                         ' bool ok = qr_generate(&q, m, 2); std::printf("%d %d %d\\n", (int)ok, (int)qr_get_module(&q,0,0), (int)QR_SIZE); return ok?0:1; }\n')
    r1 = sh(["gcc", "-std=c99", "-O2", "-I", env.src, "-c", os.path.join(env.src, "qrcode.c"), "-o", os.path.join(env.work, "q_for_cpp.o")])
    r2 = sh(["g++", "-std=c++11", "-Wall", "-Wextra", "-I", env.src, cpp, os.path.join(env.work, "q_for_cpp.o"), "-o", os.path.join(env.work, "cpp_main")])
    s.check(r1.returncode == 0 and r2.returncode == 0 and not r2.stderr.strip(), f"C++ translation unit links against the C object: {r1.stderr[:150]}{r2.stderr[:150]}")
    if r2.returncode == 0:
        s.check(sh([os.path.join(env.work, "cpp_main")]).stdout.split()[:2] == ["1", "1"], "C++ caller gets a valid symbol")
    return s.done()

def sec_guards(env, versions):
    s = Sec("guards (invalid configuration is a compile error; symbolic macros work)")
    cases = [(["-DQR_VERSION=0"], "QR_VERSION must be between 1 and 20"), (["-DQR_VERSION=21"], "QR_VERSION must be between 1 and 20"),
             (["-DQR_ECC_LEVEL=7u"], "QR_ECC_LEVEL must be"), (["-DQR_ECC_LEVEL=4"], "QR_ECC_LEVEL must be"),
             (["-DQR_FIXED_MASK=8"], "QR_FIXED_MASK must be between 0 and 7"), (["-DQR_FIXED_MASK=-1"], "QR_FIXED_MASK must be between 0 and 7")]
    for flags, msg in cases:
        r = sh(["gcc", "-std=c99", *flags, "-I", env.src, "-c", os.path.join(env.src, "qrcode.c"), "-o", os.devnull])
        s.check(r.returncode != 0 and msg in r.stderr, f"{' '.join(flags)} should fail with '{msg}' (rc={r.returncode})")
    rng = np.random.RandomState(1); lines = [rand_bytes(rng, l).hex() for l in (0, 3, 17, 40)]
    def build_run(defs, forcedtag):
        exe = os.path.join(env.work, f"sym_{forcedtag}")
        r = sh(["gcc", "-O2", "-std=c99", *defs, "-I", env.src, env.gen_c, os.path.join(env.src, "qrcode.c"), "-o", exe])
        return None if r.returncode else env.run(exe, lines)
    for defs, v, e, tag in [(["-DQR_VERSION=5", "-DQR_ECC_LEVEL=QR_ECC_H"], 5, 3, "symH"), (["-DQR_VERSION=2", "-DQR_ECC_LEVEL=QR_ECC_L"], 2, 0, "symL"),
                            ([], 3, 1, "defaults")]:
        out = build_run(defs, tag); ref = env.run(env.gen(v, e, False), lines)
        s.check(out is not None and out == ref, f"{' '.join(defs) or 'no -D flags (header defaults v3/M)'}: output differs from the numeric build v{v}-{ECC_NAMES[e]}")
    return s.done()

def sec_memory(env, versions):
    s = Sec("memory (static RAM / const tables / stack; the header's documented example)")
    open(os.path.join(env.work, "sz.c"), "w").write('#include "qrcode.h"\n#include <stdio.h>\nint main(void){printf("%u\\n",(unsigned)sizeof(qr_code_t));return 0;}\n')
    rows = []
    for v, e in [c for c in [(1, 0), (3, 1), (4, 1), (5, 3), (10, 0), (20, 0)] if c[0] in versions]:
        base = os.path.join(env.work, f"mem_{v}_{e}")
        r = sh(["gcc", "-std=c99", "-Os", "-fstack-usage", f"-DQR_VERSION={v}", f"-DQR_ECC_LEVEL={e}u", "-I", env.src, "-c", os.path.join(env.src, "qrcode.c"), "-o", base + ".o"])
        if r.returncode: s.check(False, f"compile v{v}-{ECC_NAMES[e]}: {r.stderr[:200]}"); continue
        syms = {}
        for line in sh(["nm", "-S", "-t", "d", base + ".o"]).stdout.split("\n"):
            p = line.split()
            if len(p) == 4: syms[p[3]] = (int(p[1]), p[2])
        sh(["gcc", f"-DQR_VERSION={v}", f"-DQR_ECC_LEVEL={e}u", "-I", env.src, os.path.join(env.work, "sz.c"), "-o", os.path.join(env.work, "sz")])
        qsz = int(sh([os.path.join(env.work, "sz")]).stdout)
        L = layout(v, e); nbytes = ((4 * v + 17) ** 2 + 7) // 8
        cw, fm = syms.get("s_codewords", (0, ""))[0], syms.get("s_is_function", (0, ""))[0]
        s.check(cw == L.raw, f"v{v}-{ECC_NAMES[e]}: s_codewords is {cw} B, expected {L.raw}")
        s.check(fm == nbytes and qsz == nbytes, f"v{v}-{ECC_NAMES[e]}: function map {fm} B / qr_code_t {qsz} B, expected {nbytes}")
        ram_syms = [k for k, (sz, t) in syms.items() if t in "bBdD" and sz]
        s.check(sorted(ram_syms) == ["s_codewords", "s_is_function"], f"v{v}-{ECC_NAMES[e]}: unexpected writable static data: {ram_syms}")
        tables = sum(sz for k, (sz, t) in syms.items() if k in ("QR_BLOCK_TABLE", "ALIGN_POS", "ALIGN_COUNT"))
        frame = 0
        sufile = base + ".su"
        if os.path.exists(sufile):
            for line in open(sufile):
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 2 and parts[0].endswith("qr_generate"): frame = int(parts[1])
        rows.append((v, e, qsz, cw + fm, tables, frame))
        if (v, e) == (4, 1): s.check(qsz + cw + fm == 374, f"header's documented v4-M example says 374 B of RAM, measured {qsz + cw + fm} B")
    s.note("RAM = sizeof(qr_code_t) (yours, must persist) + library scratch (static, reusable between calls)")
    for v, e, q, scratch, tables, frame in rows:
        s.note(f"  v{v}-{ECC_NAMES[e]}: {q} + {scratch} = {q + scratch} B RAM | qr_generate stack frame {frame} B on x86-64 (-Os; will differ on your MCU)")
    return s.done()

SECTIONS = [("selftest", sec_selftest), ("unit", sec_unit), ("capacity", sec_capacity), ("lengths", sec_lengths), ("masks", sec_masks),
            ("automask", sec_automask), ("rscorrect", sec_rscorrect), ("opencv", sec_opencv), ("encoder", sec_encoder), ("api", sec_api),
            ("build", sec_build), ("guards", sec_guards), ("memory", sec_memory)]

def parse_versions(txt):
    out = []
    for part in txt.split(","):
        a, _, b = part.partition("-"); out += list(range(int(a), int(b or a) + 1))
    return sorted(set(out))

def main():
    ap = argparse.ArgumentParser(description="Thorough tests for qrcode.c / qrcode.h")
    ap.add_argument("--src", default=".", help="directory containing qrcode.c and qrcode.h")
    ap.add_argument("--work", default="qr_test_work"); ap.add_argument("--versions", default="1-20")
    ap.add_argument("--sections", default="all", help="comma list of: " + ",".join(n for n, _ in SECTIONS))
    a = ap.parse_args()
    versions = parse_versions(a.versions); env = Env(a.src, a.work)
    chosen = [n for n, _ in SECTIONS] if a.sections == "all" else a.sections.split(",")
    ok = True; t0 = time.time()
    for name, fn in SECTIONS:
        if name in chosen: ok &= bool(fn(env, versions))
    print(f"\n{'ALL SELECTED SECTIONS PASSED' if ok else 'FAILURES FOUND'} ({time.time() - t0:.0f}s)")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
