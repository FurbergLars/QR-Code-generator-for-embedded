#!/usr/bin/env python3
"""
Mutation testing for qrcode.c: seed one deliberate bug at a time into a copy of the library and check
that test_qrcode.py's sections notice it. A mutant that survives means a gap in the tests.

  python3 mutants.py            run all mutants (about 6 minutes) and print a summary table
  python3 mutants.py LO HI      run mutants LO..HI-1 only (results are appended to mutants.jsonl)

Needs qrcode.c, qrcode.h and test_qrcode.py in the same directory. The mutation patterns are written against
this exact revision of qrcode.c; if you edit the library they may no longer match (the script says so).
A decode-only baseline (OpenCV decoding one mid-capacity message per configuration) is evaluated for each
mutant to show which bugs a plain "does it scan?" test would have missed.
"""
import sys, os, io, shutil, tempfile, time, contextlib, json
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import numpy as np
import test_qrcode as T

T.Env.run.__defaults__ = (25,)   # seconds: a hang in a mutant counts as a kill
SRC = open(os.path.join(HERE, "qrcode.c")).read()
# (id, description, old, new, mode)  mode: one = must occur once, all = replace all, first = first occurrence
M = [
 ("M01", "GF(256) reduction constant 0x1D -> 0x1C", "if (hiBitSet) a ^= 0x1Du;", "if (hiBitSet) a ^= 0x1Cu;", "one"),
 ("M02", "RS generator starts at alpha^1 instead of alpha^0", "uint8_t root = 1; /* alpha^0 */", "uint8_t root = 2; /* alpha^0 */", "one"),
 ("M03", "pad bytes 0xEC/0x11 swapped", "useEC ? 0xECu : 0x11u", "useEC ? 0x11u : 0xECu", "one"),
 ("M04", "mode indicator 0100 -> 0010", "bw_write(&bw, 0x4u /* binary/byte mode */, 4);", "bw_write(&bw, 0x2u /* binary/byte mode */, 4);", "one"),
 ("M05", "16-bit length field starts at v11 instead of v10", "uint8_t cciBits = (QR_VERSION <= 9) ? 8 : 16;", "uint8_t cciBits = (QR_VERSION <= 10) ? 8 : 16;", "one"),
 ("M06", "capacity check admits one byte too many (v1-9)", "((cciBits == 8) ? 2u : 3u)", "((cciBits == 8) ? 1u : 3u)", "one"),
 ("M07", "EC codewords interleaved transposed", "(uint16_t)((uint16_t)bs->block * bs->ecPerBlock) + bs->col);", "(uint16_t)((uint16_t)bs->col * bs->ecPerBlock) + bs->block);", "one"),
 ("M08", "timing pattern parity flipped", "bool dark = (i & 1u) == 0;", "bool dark = (i & 1u) == 1;", "one"),
 ("M09", "timing pattern one module short", "for (uint8_t i = 8; i <= QR_SIZE - 9; i++) {", "for (uint8_t i = 8; i <= QR_SIZE - 10; i++) {", "one"),
 ("M10", "always-dark module one row off", "uint8_t y = 4 * QR_VERSION + 9, x = 8;", "uint8_t y = 4 * QR_VERSION + 8, x = 8;", "one"),
 ("M11", "finder pattern ring at wrong radius", "ring_dist(r, c, 3) != 2", "ring_dist(r, c, 3) != 1", "one"),
 ("M12", "alignment pattern ring at wrong radius", "ring_dist(r, c, 2) != 1", "ring_dist(r, c, 2) != 0", "one"),
 ("M13", "data scan skips the wrong column", "if (col == 6) col--;", "if (col == 5) col--;", "one"),
 ("M14", "2nd format-info copy shifted by one module", "y2 = (uint8_t)(QR_SIZE - 15 + i); }", "y2 = (uint8_t)(QR_SIZE - 14 + i); }", "one"),
 ("M15", "version-info: BCH/number boundary at bit 11", "if (i < 12) { bit = (bch & 1u) != 0; bch >>= 1; }", "if (i < 11) { bit = (bch & 1u) != 0; bch >>= 1; }", "one"),
 ("M16", "ECC level indicators L and M swapped", "{1, 0, 3, 2}", "{0, 1, 3, 2}", "one"),
 ("M17", "version-info BCH polynomial 0x1F25 -> 0x1F24", "0x1F25u", "0x1F24u", "one"),
 ("M18", "mask 5 formula wrong (| becomes &)", "case 5: return ((x & y & 1u) | prod_mod3(x, y)) == 0;", "case 5: return ((x & y & 1u) & prod_mod3(x, y)) == 0;", "one"),
 ("M19", "mask 3 formula wrong", "return s == 0 || s == 3;", "return s == 0;", "one"),
 ("M20", "remainder bits are 1 instead of 0", "if (bitsource_next_bit(bs, &bit) && bit) {", "if (bitsource_next_bit(bs, &bit) ? bit : true) {", "one"),
 ("M21", "penalty: format bits NOT written before scoring (the bug fixed earlier)", "reserve_and_maybe_write_format(qr, true, compute_format_bits(format_data(m)));", "(void)format_data(m);", "one"),
 ("M22", "penalty rule 2 weight 3 -> 2", "pen_add(&penalty, 3);", "pen_add(&penalty, 2);", "one"),
 ("M23", "penalty rule 1 run cost n-2 -> n-3", "(uint8_t)(runLen - 2)", "(uint8_t)(runLen - 3)", "all"),
 ("M24", "penalty rule 3: typo in one pattern (rows only)", "m[4] && !m[5] && m[6] && m[7] && m[8] && !m[9] && m[10]", "m[4] && !m[5] && m[6] && m[7] && m[8] && m[9] && m[10]", "first"),
 ("M25", "penalty rule 4 weight 10 -> 5", "return (uint8_t)(k * 10u);", "return (uint8_t)(k * 5u);", "one"),
 ("M26", "qr_get_module bounds check removed", "if (x >= QR_SIZE || y >= QR_SIZE) return false;", "", "one"),
 ("M27", "bit-writer position narrowed to 8 bits", "uint16_t bitPos; /* <= 8 * QR_TOTAL_CODEWORDS */", "uint8_t bitPos; /* <= 8 * QR_TOTAL_CODEWORDS */", "one"),
 ("M28", "codeword offset narrowed to 8 bits (data columns)", "uint16_t offset = (bs->block < bs->g1n)", "uint8_t offset = (bs->block < bs->g1n)", "one"),
 ("M29", "qr->modules not cleared between calls", "memset(qr->modules, 0, sizeof(qr->modules));", "", "one"),
]

def mutate(src, old, new, mode):
    n = src.count(old)
    assert n >= 1, f"pattern not found: {old!r}"
    if mode == "one": assert n == 1, f"pattern occurs {n}x: {old!r}"; return src.replace(old, new)
    if mode == "all": return src.replace(old, new)
    if mode == "first": return src.replace(old, new, 1)

ORDER = ["unit", "capacity", "masks", "lengths", "automask", "api"]
def run_mutant(mid, desc, old, new, mode, versions):
    os.makedirs(os.path.join(HERE, "mutwork"), exist_ok=True)
    t0 = time.time(); d = tempfile.mkdtemp(prefix=mid + "_", dir=os.path.join(HERE, "mutwork"))
    open(d + "/qrcode.c", "w").write(mutate(SRC, old, new, mode)); shutil.copy(os.path.join(HERE, "qrcode.h"), d)
    env = T.Env(d, d + "/work"); killed_by, detail = None, ""
    secs = dict(T.SECTIONS)
    for name in ORDER:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf): ok = secs[name](env, versions)
        except Exception as ex:
            ok = False; buf.write(f"    FAIL: {type(ex).__name__}: {str(ex)[:140]}\n")
        if not ok:
            killed_by = name
            fl = [l.strip() for l in buf.getvalue().split("\n") if "FAIL:" in l]
            detail = fl[0][:150] if fl else "(failed)"
            break
    # would a decode-only test (real decoder, no strict checks) have noticed?
    dec_killed = None
    if T.cv2 is not None:
        rng = np.random.RandomState(3); dec_killed = False
        for v, e in T.configs(versions):
            L = T.layout(v, e); t = T.rand_text(rng, max(L.cap // 2, 1))
            try:
                M_ = T.parse(env.run(env.gen(v, e, False), [t.encode().hex()]), 4 * v + 17, 1)[0]
                if M_ is None or T.cv_decode(M_) != t: dec_killed = True; break
            except Exception: dec_killed = True; break
    shutil.rmtree(d, ignore_errors=True)
    return dict(id=mid, desc=desc, killed_by=killed_by, detail=detail, decoder_only_kills=dec_killed, secs=round(time.time() - t0, 1))

if __name__ == "__main__":
    versions = [2, 7, 10, 14]
    if len(sys.argv) == 3: lo, hi = int(sys.argv[1]), int(sys.argv[2])
    else: lo, hi = 0, len(M)
    results = []
    for mid, desc, old, new, mode in M[lo:hi]:
        r = run_mutant(mid, desc, old, new, mode, versions); results.append(r)
        print(json.dumps(r), flush=True)
        open(os.path.join(HERE, "mutants.jsonl"), "a").write(json.dumps(r) + "\n")
    if len(sys.argv) != 3:
        k = sum(bool(r["killed_by"]) for r in results)
        print(f"\n{k} of {len(results)} mutants detected; survivors: {[r['id'] for r in results if not r['killed_by']] or 'none'}")
        miss = [r["id"] for r in results if r["killed_by"] and r["decoder_only_kills"] is False]
        print(f"a decode-only baseline would have missed {len(miss)}: {miss}")
