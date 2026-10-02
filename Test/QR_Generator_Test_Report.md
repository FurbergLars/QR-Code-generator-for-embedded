# QR Code Generator — Test Report

> **Performed by Claude Sonnet 5.5 Max (Anthropic).** I did all of the analysis, the fixes, the test design, the test code, the execution and the write-up described in this report.

| | |
|---|---|
| **Subject** | `qrcode.c` / `qrcode.h` — a minimal-RAM QR Code generator for 8-bit MCUs (byte mode, version 1–20 and ECC level chosen at build time) |
| **Revision tested** | `qrcode.c` 777 lines, MD5 `aba4a6ed2765ebd4a2ad580cac21d1e7` (mask-penalty fix + 8/16-bit integer types).<br>`qrcode.h` MD5 `b8dd4bdc31bacde246fdd160070b7fa1` (unchanged from the upload) |
| **Report date** | 2 October 2026 |
| **Overall result** | **PASS.** About 31,700 automated checks with 0 failures on the final code, and 29 of 29 deliberately seeded bugs detected. Two real defects in the original code were found and fixed along the way (section 8). |
| **Delivered with this report** | `qrcode.c` (fixed and narrowed), `test_qrcode.py` (999 lines, MD5 `608891fa0eaf392ed51e01729288612e`), `mutants.py` (106 lines, MD5 `b64c55de5791ab1c62ea737220316097`) |

## Contents

1. Executive summary
2. Scope
3. Environment and approach
4. Work performed
5. Test architecture
6. Results
7. Mutation testing
8. Findings
9. Problems in my own tests, and how I resolved them
10. Limitations
11. Recommendations
12. Reproducing the tests
- Appendix A — Capacities checked
- Appendix B — Memory footprint
- Appendix C — Compiler modes and warning flags
- Credits

---

## 1. Executive summary

I took the uploaded generator through four phases of work:

1. **Functional test of the original.** I built all 80 version/ECC combinations, decoded 240 generated symbols with OpenCV, checked every capacity limit against the ISO table, exercised all 8 masks, and ran AddressSanitizer/UBSan. No decoding defect.
2. **Audit and fix of the mask-penalty scoring.** I found a real flaw: the eight candidate masks were scored *before* the format-information bits (and, from version 7, the version-information bits) were written, so those modules were scored as light. In my sample the generator picked a different mask than complete scoring would in 86 of 240 symbols (36%). Every one of those symbols still scanned, which is why decode tests can never see this class of bug. I also replaced a Rule 4 formula that scored mirror-image cases differently (for example 40% and 60% dark).
3. **Reduction of every integer to 8 or 16 bits** for the 8-bit target. No `int`, `int8_t` or 32-bit type remains (30 `uint32_t` and 78 `int` occurrences removed). The output is bit-for-bit identical to the previous version in all 1,360 comparisons.
4. **A thorough, independent test campaign** — a 999-line test suite whose oracles share no code or tables with the library, plus mutation testing to prove the suite can actually fail.

**Headline results**

- **About 31,700 automated checks, 0 failures** against the final code.
- **19,366 symbols** covering every message length from 0 to the maximum for all 80 version/ECC combinations, each passing a strict ISO/IEC 18004 conformance check that I wrote independently of the library.
- **Error correction verified at full capacity:** an independent Reed–Solomon decoder corrected exactly EC/2 corrupted bytes in each of **1,238 blocks**.
- **Mask selection verified:** the chosen mask equalled the lowest-penalty mask of an independent scorer in **798 of 798** payloads.
- **Independent encoder cross-check:** the data and error-correction bytes were identical to OpenCV's own QR encoder in **240 of 240** symbols.
- **Build robustness:** identical output in 16 compiler modes (optimisation levels, sanitizers, auto-variable-init, C standards, LTO); no warnings under strict flags in all 80 configurations; clean under the GCC static analyzer; links from C++.
- **Mutation testing:** **29 of 29** seeded bugs detected. A plain "does it scan?" check would have missed 15 of them.

**What this work does not cover:** the sandbox had no 8-bit compiler or hardware, so size, speed and behaviour on the real MCU were not measured; OpenCV was the only real-world decoder available; there was no camera or display scanning (section 10).

---

## 2. Scope

**In scope**

- Correctness of `qr_generate()` and `qr_get_module()` for every supported configuration: versions 1–20 × ECC L/M/Q/H, byte mode, message lengths 0…capacity, all 8 masks, and automatic mask selection.
- Conformance to ISO/IEC 18004 for the symbol structure, format and version information, codeword layout, Reed–Solomon coding, padding and remainder bits.
- The mask-penalty scoring; the safety of the 8/16-bit integer widths; the API contract; memory safety; determinism; build robustness; and the memory claims documented in the header.

**Out of scope** (the library does not support them by design, or the sandbox could not provide them)

- Numeric, alphanumeric, kanji and ECI modes; versions 21–40; Micro QR.
- Execution on an 8-bit target; camera or screen scanning.

---

## 3. Environment and approach

| Item | Value |
|---|---|
| Tester | Claude Sonnet 5.5 Max (Anthropic) |
| Platform | Ubuntu 24.04 sandbox, x86-64, one CPU core, no network |
| Compiler | GCC 13.3.0 |
| Languages / libraries | Python 3 with NumPy; OpenCV 4.13.0 (QR decoder, Aruco-based QR detector and QR *encoder*) |
| Not available | avr-gcc, sdcc, clang, valgrind, 32-bit multilib |

**Principles I followed**

1. **Independence.** The oracle that judges the generator shares no code or tables with it. It has its own copy of the spec tables, a table-based GF(256) (the library's is table-free), its own symbol reader, penalty scorer and Reed–Solomon decoder — and a third-party reference (OpenCV) on top.
2. **Strictness.** Real decoders ignore many things a conforming encoder must still get right: remainder bits, the second copy of the format information, version information, padding bytes and alignment-pattern positions. My checker verifies all of them, so a code that merely scans is not enough to pass.
3. **Exhaustiveness where it is cheap.** Every message length, every mask, every version/ECC combination, and exhaustive loops in the unit tests.
4. **Testing the tester.** The checker is calibrated against ISO tables, against an independent encoder's symbols and against injected faults, and the whole suite is then validated by mutation testing.
5. **No offline copy of the standard.** With no network access, the ISO/IEC 18004 tables were entered from memory. I cross-validated them several ways (section 5.4), so a mistake in my copy would have shown up as a failure rather than a silent pass.

All random inputs use fixed seeds, so every run is reproducible.

---

## 4. Work performed

### 4.1 Phase 1 — Functional test of the original

- Read all of `qrcode.c` and `qrcode.h` against the spec: block table, alignment positions, format/version bit arithmetic and placement, GF(256) and Reed–Solomon, interleaving, placement scan, mask formulas and the penalty rules.
- Built all 80 configurations with AddressSanitizer and UBSan, generated messages of 1 byte, half capacity and full capacity (240 symbols), rendered each at 8 px per module with a 4-module quiet zone, and decoded with OpenCV (the Aruco-based detector as fallback). All 240 decoded to the exact bytes.
- Found the maximum message length of every configuration by bisection: all 80 equal the ISO byte-mode capacity, and one byte more is rejected.
- Forced each of the 8 masks on versions 1, 2, 5, 7, 10, 14 and 20 at all four ECC levels (448 symbols): all decoded.
- Compiled with `-Wall -Wextra -Wpedantic -Wconversion -Wshadow`: no warnings.

**Outcome:** no decoding defect. I recorded one observation for deeper study — mask penalties were computed before the format bits existed — because a decode test cannot reveal it. Other notes: the generator is not re-entrant (documented in the header) and the header defaults to version 3 / ECC M.

### 4.2 Phase 2 — Mask-penalty scoring: audit and fixes

**Method**

1. I wrote an independent scorer in Python straight from the four ISO penalty rules, operating on a *finished* symbol (format and version information included).
2. I instrumented a copy of the library to print its eight per-mask scores, and obtained the finished symbol for each mask by forcing that mask.
3. I compared both across all 80 configurations × 3 messages (240 symbols, 1,920 per-mask scores).

**Findings**

- **Rules 1–4 were coded exactly as intended.** With the format/version modules blanked (and the original's own Rule 4 formula emulated), my scorer reproduced the library's scores in 240 of 240 symbols — the only deviation was *what* was scored.
- **Defect:** the 30 format-information modules (and from version 7 the 36 version-information modules) were still unwritten when the masks were scored, so they counted as light. All 1,920 per-mask scores differed from the complete-symbol scores, and in 86 of 240 symbols (36%) the library chose a different mask than complete scoring would. Every such symbol still decoded: the choice among masks affects robustness, not validity.
- **Defect:** the Rule 4 formula used a floored percentage and mis-scored exact multiples of 5% above 50% — 28 of 80,280 (symbol size, dark-module count) combinations (exactly 60%, 80% and 100% dark). For example 40% and 60% dark were penalised 10 and 20 instead of 10 and 10. Practically unreachable in real symbols, but wrong.

**Fixes**

The real format bits for each candidate mask, and the version bits, are now written into the grid before that candidate is scored:

```c
#if QR_VERSION >= 7
    reserve_and_maybe_write_version(qr, true, compute_version_bch(QR_VERSION));
#endif
    uint8_t best = 0;
    uint16_t bestScore = 0xFFFFu;
    for (uint8_t m = 0; m < 8; m++) {
        reserve_and_maybe_write_format(qr, true, compute_format_bits(format_data(m)));
        uint16_t score = evaluate_penalty(qr, m);
        if (score < bestScore) { bestScore = score; best = m; }
    }
```

Rule 4 is now exact integer arithmetic: the penalty is 10·k, where k is the smallest integer ≥ 0 such that |2·dark − total| ≤ (k+1)·total/10 (written in 16 bits in Phase 3).

**Verification**

| Measure | Original | Fixed |
|---|---|---|
| Per-mask scores equal to the complete-symbol reference | 0 / 1,920 | 1,920 / 1,920 |
| Chosen mask equals the reference's best mask | 154 / 240 (64%) | 240 / 240 |
| Rule 4 vs exact arithmetic | 28 mismatches in 80,280 cases | 0 mismatches |

After the fix, all 80 configurations and all 8 forced masks still decoded, and the sanitizer runs and warning checks stayed clean.

**A judgement call I left alone.** Rule 3 (the finder-like 1:1:3:1:1 pattern) follows the Thonky-tutorial reading: it only looks at windows inside the symbol and counts each side separately. Reference implementations differ here. As far as I recall, libqrencode and Nayuki's library treat the quiet zone as light, while ZXing does not. In my experiment, switching conventions changed the chosen mask in 70 of 240 symbols (29%). Both choices are valid, so I did not change it.

### 4.3 Phase 3 — 8/16-bit integer types

You asked whether integer types could be narrowed for an 8-bit target. They could, completely: no `int`, `int8_t` or 32-bit type remains in `qrcode.c` (30 `uint32_t` occurrences, 78 `int` occurrences and one `int8_t` removed).

| Quantity | Range needed | Solution |
|---|---|---|
| Coordinates, loop counters, run lengths, block and codeword bytes | ≤ 97 (≤ 255 in general) | `uint8_t` |
| Flat bit index into the packed bitmaps | ≤ 9,408 | `uint16_t` |
| Codeword offsets, bit-writer position, message length, dark-module count | ≤ 1,085 / 8,680 / 858 / 9,409 | `uint16_t` |
| Penalty score | ≤ 21,753 observed; 16-bit limit 65,535 | `uint16_t` with a saturating add (`pen_add`) |
| Version information (18 bits) | 18 bits | 12-bit BCH remainder plus the 6-bit version number (`compute_version_bch`) |
| Capacity check | 4 + 16 + 8·len bits | rewritten in whole bytes: len ≤ T−2 (or T−3) |
| Rule 4 (naively 20·dark ≈ 188,000) | exceeds 16 bits | exact 16-bit form using floor((k+1)·total/10) = (k+1)·(total/10) + (k+1)·(total mod 10)/10 |
| Mask formulas | products up to ~9,200 | parity as `x ^ y`, `(x·y) mod 2` as `x & y & 1`, mod 3 via residues |

```c
static uint8_t balance_penalty(uint16_t dark) {
    const uint16_t total = (uint16_t)((uint16_t)QR_SIZE * QR_SIZE);
    const uint16_t q = (uint16_t)(total / 10u);
    const uint8_t  r = (uint8_t)(total % 10u);
    uint16_t twice = (uint16_t)(dark << 1);
    uint16_t dev = (twice > total) ? (uint16_t)(twice - total) : (uint16_t)(total - twice);
    uint8_t k = 0;
    while (dev > (uint16_t)((k + 1u) * q + ((k + 1u) * r) / 10u)) k++;
    return (uint8_t)(k * 10u);
}
```

I also replaced loops that would have wrapped around once their counter became unsigned, and added three compile-time assertions (`coord_fits_u8`, `index_fits_u16`, `bits_fit_u16`) that fail the build if the tables are ever extended past what the narrow types can hold.

**Verification**

- The new file produced identical results to the previous version in **1,360 of 1,360** comparisons across every version and ECC level: accept/reject, all eight per-mask scores, the chosen mask and the full symbol, including degenerate inputs (all-`0x00`, all-`0xFF`, repeating patterns).
- Exhaustive checks: mask formulas for all 524,288 (mask, x, y) combinations; the 32 format strings; the version BCH for versions 7–40; and the 16-bit Rule 4 against exact arithmetic for 477,360 dark-module counts covering every symbol size from version 1 to version 40.
- Highest penalty observed: 21,753 for a single mask and 6,479 for the best mask, both at version 20 (the largest symbol; the inputs included degenerate messages), far below the 65,535 saturation point.
- 340 cases under ASan + UBSan; OpenCV decoded all 80 configurations and all 8 forced masks on versions 2, 7 and 20; clean under `-Wconversion -Wsign-conversion -Wpedantic` (C99).

**Not verifiable here:** no 8-bit compiler was available. I reasoned through every place where a 16-bit `int` could behave differently from the 32-bit `int` on x86 (promotions, products near 32,767) and found none, but this is reasoning, not measurement.

### 4.4 Phase 4 — Thorough test campaign

I designed and wrote a self-contained test suite (`test_qrcode.py`) and a mutation-testing script (`mutants.py`), ran every section against the final code, investigated every unexpected result, and iterated on the tests themselves (section 9). The architecture is described in section 5, the results in section 6 and the mutation results in section 7.

---

## 5. Test architecture

### 5.1 C harness

A small C program (embedded in the suite) reads one message per line and prints each finished symbol as rows of `0`/`1`, or `REJECT`. It is built once per (version, ECC level) in two flavours:

- **auto** — the library exactly as shipped, with automatic mask selection;
- **forced** — compiled with `-DQR_FIXED_MASK=qr_dbg_mask`, so the mask is chosen *at run time* per line without recompiling.

One process handles hundreds of messages in sequence with a single reused `qr_code_t`, which also exercises state reuse.

### 5.2 Independent conformance checker

For every symbol, `verify_symbol()` checks:

1. **Fixed patterns** — three finders, separators, timing patterns, alignment patterns (positions derived from a formula, not from the library's table) and the always-dark module, compared module-by-module with patterns drawn from literal ASCII art.
2. **Format information** — both copies equal, a valid BCH codeword, ECC level equal to the build configuration, mask equal to the expected one.
3. **Version information** (version ≥ 7) — both 18-bit copies equal the spec value.
4. **Codeword extraction** — unmask and read in the spec's zig-zag order; the data-module count equals the spec formula for every version.
5. **Remainder bits** are zero after unmasking.
6. **Reed–Solomon** — de-interleave into blocks using block structure derived from the spec's formulas, then require all syndromes S₀…S(ec−1) to be zero in every block. Because the code is systematic, this proves every EC byte is correct.
7. **Data stream** — mode `0100`, 8- or 16-bit length, payload equal to the message, 4-bit terminator, zero bits to the byte boundary, then `0xEC`/`0x11` alternating to fill the capacity.

### 5.3 Independent oracles

| Oracle | Purpose |
|---|---|
| Python penalty scorer (four ISO rules, exact rational Rule 4) | Judges mask selection |
| Python Reed–Solomon decoder (Berlekamp–Massey, Chien search, Forney) | Proves the full EC/2 correction capacity |
| OpenCV `QRCodeDetector` + Aruco-based detector | Real decoder: payload types, damage, module sizes |
| OpenCV `QRCodeEncoder` (byte mode, explicit version and ECC level) | Independent encoder: data + EC codeword streams compared byte for byte |

### 5.4 Calibrating the checker (`selftest`, 582 checks)

- **Spec cross-validation:** 32 format strings and 14 version strings computed by BCH equal the ISO tables; derived total codeword counts equal the published totals for all 20 versions; derived byte capacities equal the published capacities for all 80 configurations; the zig-zag data-module count equals the spec formula for all 20 versions.
- **Independent encoder:** the checker accepted 28 of 28 symbols produced by OpenCV's own encoder (remainder bits excluded, see F-6).
- **Fault injection:** for 7 versions × 4 ECC levels I flipped one module in each region — finder, top-right finder, separator, timing, alignment, dark module, format copy 1 and 2, version copy 1 and 2, a data codeword, an EC codeword, a remainder bit — and required the checker to reject every one. A wrong ECC level and a wrong mask in the format information were also rejected.

---

## 6. Results

| Section | What it verifies | Oracle | Size | Result | Time |
|---|---|---|---|---|---|
| `selftest` | The checker itself | ISO tables, OpenCV encoder, fault injection | 582 checks | PASS | 35 s |
| `unit` | Internal helpers: mask formulas, format/version BCH, Rule 4, saturating add | ISO tables, exact arithmetic | 1,368 checks | PASS | 5 s |
| `capacity` | Maximum length per configuration; over-capacity rejected | Capacity derived from spec formulas (= ISO table) | 80 configs, 1,199 checks | PASS | 35 s |
| `lengths` | **Every** message length 0…capacity | Strict checker | 19,366 symbols | PASS | 29 s |
| `masks` | All 8 masks × boundary lengths | Strict checker | 3,200 symbols | PASS | 2 s |
| `automask` | Symbol validity and mask choice | Independent penalty scorer | 798 payloads, 2,394 checks | PASS | 21 s |
| `rscorrect` | Error correction at full capacity | Independent BM decoder | 1,238 blocks, 1,558 checks | PASS | 2 s |
| `opencv` | A real decoder | OpenCV | 716 checks | PASS | 76 s |
| `encoder` | Independent encoder agreement | OpenCV encoder | 240 symbols, 457 checks | PASS | 380 s |
| `api` | Contract, reuse, no hidden state, under ASan + UBSan | C test programs | 504 checks | PASS | 52 s |
| `build` | Output identical across compiler modes; warnings; analyzer; C++ | Byte-identical hashes, compiler diagnostics | 317 checks | PASS | 120 s |
| `guards` | Invalid configuration is a compile error; symbolic macros | Compiler | 9 checks | PASS | 1 s |
| `memory` | Static RAM; the header's documented example | `nm -S`, `sizeof` | 19 checks | PASS | 2 s |
| **Total** | | | **about 31,700 checks, 0 failures** | **PASS** | **about 13 min** |

(The full run is dominated by the encoder cross-check; the rest takes about 6 minutes.)

### Notable detail

- **capacity.** Lengths probed per configuration: 0, 1, cap−1, cap, cap+1, cap+2, 255, 256, 257, 300, 1000, 4095 and 65535. Accepted exactly when ≤ capacity; the symbols at cap−1 and cap fully verified.
- **lengths.** Random bytes (including `0x00` and high bytes); the mask is varied as *length mod 8*, so all lengths and all masks are covered together.
- **masks.** Lengths {0, 1, cap/2, cap−1, cap} × all 8 masks × 80 configurations.
- **automask.** Payloads: random data at five lengths plus all-`0x00`, all-`0xFF`, all-`0x55`, repeating `0xEC 0x11` and all-`A`. For each payload the symbol was generated with the automatic mask and with each of the 8 masks forced; the independent scorer ranked the eight and the library's choice matched the lowest-penalty mask (lowest index on ties) in 798 of 798. The automatic symbol was also identical to the forced symbol of the same mask.
- **rscorrect.** Exactly EC/2 random whole-byte errors (data and EC positions) in every block, for 80 configurations × 2 messages. All 1,238 blocks were corrected and every payload recovered.
- **opencv.** Payloads: a URL, a WiFi configuration string, a vCard, UTF-8 text with accented, CJK and emoji characters, 200 digits, a `mailto:` link, and random printable text at 1 byte, half and full capacity. Damage test: EC/4 corrupted codewords per block were corrected (see F-7 for why not EC/2). Module-size sweep: versions 1-M, 5-H and 10-M decoded at every size from 2 to 12 px per module; version 20-L failed at 2 px and decoded at 3 px and above.
- **encoder.** Data + EC codeword streams identical in 240 of 240 symbols (the two encoders chose the same mask in 217; mask choice is the one place where two correct encoders may differ). OpenCV's symbols were also run through my checker.
- **api.** `qr_get_module` returns light for out-of-range coordinates (QR_SIZE, QR_SIZE+1, 128, 200, 255); finder corners dark and separators light; a finished symbol is unchanged by later calls; the same message gives the same symbol regardless of what was generated before (including after a full-length message, into a dirty `qr_code_t`, and after a rejected call); lengths 65535, 65534 and 32768 are rejected; an empty message with a NULL data pointer is accepted; a 400-call stress run is deterministic; and forward, reversed and fresh-process runs agree. All under ASan + UBSan on seven configurations.
- **build.** 16 compiler modes × 7 sample configurations × (auto, forced) = 224 byte-for-byte output comparisons against `-O2` (modes listed in Appendix C); strict warnings on all 80 configurations plus fixed-mask builds; `-fanalyzer` on four configurations; a C++ translation unit links against the C object.
- **guards.** Version 0 and 21, ECC level 4 and 7, and mask 8 and −1 each fail to compile with the documented message; `-DQR_ECC_LEVEL=QR_ECC_H` and the header defaults produce output identical to the numeric builds.
- **memory.** Measured static RAM matches the formula exactly; the header's documented version 4 / level M example (374 bytes) is exact (Appendix B).

---

## 7. Mutation testing

To prove the suite can fail, I seeded **29 bugs**, one at a time, into a copy of `qrcode.c` and ran the suite against versions 2, 7, 10 and 14 (all four ECC levels).

**Result: 29 of 29 detected. A decode-only check would have missed 15.**

| ID | Seeded bug | First section to fail | Would a decode-only check have caught it? |
|---|---|---|---|
| M01 | GF(256) reduction constant 0x1D → 0x1C | `capacity` | yes |
| M02 | RS generator starts at alpha^1 instead of alpha^0 | `capacity` | yes |
| M03 | pad bytes 0xEC/0x11 swapped | `capacity` | **no** |
| M04 | mode indicator 0100 → 0010 | `capacity` | yes |
| M05 | 16-bit length field starts at v11 instead of v10 | `capacity` | yes |
| M06 | capacity check admits one byte too many (v1-9) | `capacity` | **no** |
| M07 | EC codewords interleaved transposed | `capacity` | yes |
| M08 | timing pattern parity flipped | `capacity` | **no** |
| M09 | timing pattern one module short | `capacity` | yes |
| M10 | always-dark module one row off | `capacity` | **no** |
| M11 | finder pattern ring at wrong radius | `capacity` | yes |
| M12 | alignment pattern ring at wrong radius | `capacity` | **no** |
| M13 | data scan skips the wrong column | `capacity` | yes |
| M14 | 2nd format-info copy shifted by one module | `capacity` | yes |
| M15 | version-info: BCH/number boundary at bit 11 | `capacity` | **no** |
| M16 | ECC level indicators L and M swapped | `capacity` | yes |
| M17 | version-info BCH polynomial 0x1F25 → 0x1F24 | `capacity` | **no** |
| M18 | mask 5 formula wrong (OR becomes AND) | `masks` | yes |
| M19 | mask 3 formula wrong | `masks` | yes |
| M20 | remainder bits are 1 instead of 0 | `capacity` | **no** |
| M21 | penalty: format bits NOT written before scoring (the bug fixed earlier) | `automask` | **no** |
| M22 | penalty rule 2 weight 3 → 2 | `automask` | **no** |
| M23 | penalty rule 1 run cost n-2 → n-3 | `automask` | **no** |
| M24 | penalty rule 3: typo in one pattern (rows only) | `automask` | **no** |
| M25 | penalty rule 4 weight 10 → 5 | `unit` | **no** |
| M26 | qr_get_module bounds check removed | `api` | **no** |
| M27 | bit-writer position narrowed to 8 bits | `capacity` | yes |
| M28 | codeword offset narrowed to 8 bits (data columns) | `capacity` | yes |
| M29 | `qr->modules` not cleared between calls | `capacity` | **no** |

Notes:

- *First section to fail* means the first section that reported a failure when run in the order `capacity`, `masks`, `lengths`, `automask`, `api` (the `unit` section was added later and ran first for M25–M29). Most mutants are caught by several sections.
- *Decode-only check* means: OpenCV decodes one mid-capacity message per configuration, with no strict checks. It is a deliberately simple baseline.
- M14 as seeded also writes one module out of range (its last position is row `n`), so it breaks decoding too.
- M18 mutates `|` to `&` in the mask 5 formula.
- **M25 initially survived.** Rule 4 almost never contributes to the score (real symbols sit within 45–55% dark), so no end-to-end section could notice its weight changing. I added the white-box `unit` section, which checks the Rule 4 function exhaustively, and it now detects M25.
- M27 (an 8-bit bit-writer position) made the harness loop forever; a hang counts as a detection, and I added subprocess timeouts so it fails fast.
- I did not seed mutants that cannot change the output. For example, by reasoning (not by running it), dropping the 4-bit terminator changes nothing in byte mode: the data always ends 4 bits short of a byte boundary, so the pad-to-byte step writes the same four zero bits.

---

## 8. Findings

| ID | Type | Status | Summary |
|---|---|---|---|
| F-1 | Defect (medium-low) | **Fixed** | Mask scoring ignored format and version information; a non-optimal mask was chosen in 36% of my samples. Symbols remained valid and scannable. |
| F-2 | Defect (low) | **Fixed** | Rule 4 scored exact 60/80/100% dark inconsistently (asymmetric with 40/20/0%). Practically unreachable in real symbols. |
| F-3 | Design-goal gap | **Done** | 30 `uint32_t`, 78 `int` and one `int8_t` occurrence removed; output unchanged. |
| F-4 | Hazard | Open (documentation) | The build configuration must be identical in every translation unit. If `QR_VERSION` is defined only in the caller's file before including `qrcode.h`, `qrcode.c` is still compiled with the header defaults; the caller's `qr_code_t` is then smaller than what `qr_generate` writes, and AddressSanitizer reports a buffer overflow (demonstrated in the `api` section). Define the macros in compiler flags that apply to every file, or edit the defaults in `qrcode.h`. |
| F-5 | Observation | Open (judgement) | Rule 3 convention differs between reference implementations (section 4.2). Both choices are valid. |
| F-6 | Observation | None needed | OpenCV's encoder draws some of the final remainder-bit modules dark regardless of the mask. In 39 of the compared symbols (all within versions 1–10) this is the *only* difference from ours. Ours are masked zeros, which as far as I recall is also what ZXing, libqrencode and Nayuki's library do. No decoder reads these bits. |
| F-7 | Observation | None needed | OpenCV's decoder gives up before the theoretical EC/2 limit for versions ≥ 7, and does so identically on symbols made by OpenCV's own encoder. Full capacity is verified with the independent decoder instead. At 2 px per module, version 20 failed in OpenCV. |
| F-8 | Observation | Open | By code review (not tested): a NULL `qr`, or a NULL `data` with `len` > 0, is not validated. `len` = 0 with NULL `data` is accepted (tested). |

---

## 9. Problems in my own tests, and how I resolved them

I treated every unexpected failure as something to understand, not to silence.

| What happened | What I found | Resolution |
|---|---|---|
| First `selftest` rejected 3 of 28 OpenCV-encoder symbols for non-zero remainder bits | Compared with our symbol for the same mask, only remainder-bit modules differed and the codeword streams were identical. OpenCV's encoder deviates from the masked-zero convention (F-6) | Library and checker kept as they were; remainder bits are excluded when judging OpenCV's symbols, and the deviation is reported |
| First `opencv` damage test failed at EC/2 for versions ≥ 7 | A control experiment gave identical failures on symbols made by OpenCV's own encoder, so it is a decoder limit (F-7) | OpenCV damage test lowered to EC/4; the full EC/2 limit is verified by the new independent `rscorrect` section |
| First `api` run failed on version 1 | My test message was longer than version 1-L's 17-byte capacity | Test fixed |
| First `memory` run measured more than the 374 bytes the header documents | x86 rounds `.bss` object sizes up; the library was right | Switched to exact symbol sizes from `nm -S`; the 374-byte example is now confirmed exactly |
| Mutant M27 hung the harness | An 8-bit bit-writer position loops forever | Subprocess timeouts added; a hang counts as detection |
| Mutant M25 survived | Rule 4's weight is invisible to end-to-end tests | Added the `unit` section (white-box, exhaustive) |

---

## 10. Limitations

- **No 8-bit target.** Everything ran on x86-64 with GCC 13.3. RAM figures are exact (they are array sizes), but stack usage, code size and speed on the target were not measured. The x86-64 `qr_generate` frame of 192–208 bytes in Appendix B is not representative of an 8-bit compiler.
- **One real decoder.** OpenCV was the only third-party decoder available (two detectors, one decoding core). No phone, camera or LCD scanning was done.
- **Spec from memory.** I had no copy of ISO/IEC 18004. My tables were cross-validated (section 5.4), but statements about how other libraries behave are from memory.
- **Rule 3 convention.** Mask choice is checked against the same reading of Rule 3 that the library uses; another encoder may choose a different, equally valid mask.
- **Versions 21–40.** Not supported by the library. Only the 16-bit Rule 4 arithmetic and the version-information BCH were checked up to version 40; nothing else was.
- **Other platforms.** The suite assumes GCC and a Linux-like environment (sanitizers, `-ftrivial-auto-var-init`, `nm`).

---

## 11. Recommendations

1. **Fix the build configuration in one place** (compiler flags or the header defaults) so every translation unit agrees (F-4).
2. **Run a short on-target smoke test.** Generate a handful of fixed messages on the MCU, hash `qr.modules`, and compare with the host output of the harness in `test_qrcode.py`. This is the single biggest remaining gap.
3. **Render with a 4-module quiet zone and at least 3 px per module.** The full matrix was decoded at 8 px per module; the four configurations I swept (1-M, 5-H, 10-M, 20-L) all decoded at 3 px and above, so 4 px or more is a comfortable margin.
4. **Optional speed-up (not needed for correctness):** Rule 3 re-reads 11 modules at every window position. A sliding 16-bit window would remove that cost without needing wider types.
5. **If untrusted callers are possible,** add NULL checks to `qr_generate` (F-8).
6. **Keep the suite in your build.** `python3 test_qrcode.py` after any change to `qrcode.c`; `python3 mutants.py` when you want to re-validate the tests themselves.

---

## 12. Reproducing the tests

Put `qrcode.c`, `qrcode.h`, `test_qrcode.py` and `mutants.py` in one directory. Needs `gcc` and NumPy; OpenCV is optional (the `opencv` and `encoder` sections are skipped without it).

```
python3 test_qrcode.py                                    # everything, about 13 minutes
python3 test_qrcode.py --sections unit,capacity,masks     # a quick run
python3 test_qrcode.py --sections lengths --versions 1-6  # one section, some versions
python3 mutants.py                                        # all 29 mutants, about 6 minutes
```

Sections: `selftest`, `unit`, `capacity`, `lengths`, `masks`, `automask`, `rscorrect`, `opencv`, `encoder`, `api`, `build`, `guards`, `memory`. The mutation patterns are written against this exact revision of `qrcode.c`; if you edit the library they may need updating (the script tells you when a pattern no longer matches).

---

## Appendix A — Capacities checked

Maximum message length in bytes (byte mode). The suite derives these from spec formulas and the generator matches all 80.

| Version | Size | L | M | Q | H |
|---|---|---|---|---|---|
| 1 | 21×21 | 17 | 14 | 11 | 7 |
| 2 | 25×25 | 32 | 26 | 20 | 14 |
| 3 | 29×29 | 53 | 42 | 32 | 24 |
| 4 | 33×33 | 78 | 62 | 46 | 34 |
| 5 | 37×37 | 106 | 84 | 60 | 44 |
| 6 | 41×41 | 134 | 106 | 74 | 58 |
| 7 | 45×45 | 154 | 122 | 86 | 64 |
| 8 | 49×49 | 192 | 152 | 108 | 84 |
| 9 | 53×53 | 230 | 180 | 130 | 98 |
| 10 | 57×57 | 271 | 213 | 151 | 119 |
| 11 | 61×61 | 321 | 251 | 177 | 137 |
| 12 | 65×65 | 367 | 287 | 203 | 155 |
| 13 | 69×69 | 425 | 331 | 241 | 177 |
| 14 | 73×73 | 458 | 362 | 258 | 194 |
| 15 | 77×77 | 520 | 412 | 292 | 220 |
| 16 | 81×81 | 586 | 450 | 322 | 250 |
| 17 | 85×85 | 644 | 504 | 364 | 280 |
| 18 | 89×89 | 718 | 560 | 394 | 310 |
| 19 | 93×93 | 792 | 624 | 442 | 338 |
| 20 | 97×97 | 858 | 666 | 482 | 382 |

## Appendix B — Memory footprint

RAM is `sizeof(qr_code_t)` (yours, must persist as long as you want to draw the symbol) plus the library's static scratch (codeword buffer + function-module map, reusable between calls). There is no initialised data (`.data`) and no other writable static.

| Configuration | `qr_code_t` | Library scratch | Total RAM |
|---|---|---|---|
| Version 1, ECC L | 56 B | 82 B | 138 B |
| Version 3, ECC M (header default) | 106 B | 176 B | 282 B |
| Version 4, ECC M (header's example) | 137 B | 237 B | **374 B** (matches the header) |
| Version 5, ECC H | 172 B | 306 B | 478 B |
| Version 10, ECC L | 407 B | 753 B | 1,160 B |
| Version 20, ECC L | 1,177 B | 2,262 B | 3,439 B |

## Appendix C — Compiler modes and warning flags

**Output-identity modes** (each compared byte-for-byte with the default `-O2` build): `-O0`, `-O1`, `-O3`, `-Os`, `-O2 -flto`, ASan + UBSan at `-O0` and `-O2`, `-ftrivial-auto-var-init=pattern`, `-ftrivial-auto-var-init=zero`, `-funsigned-char`, `-fsigned-char`, `-std=c99`, `-std=c11`, `-std=c17`, `-std=gnu99`, `-O3 -fwrapv`.

**Strict warning build** (all 80 version/ECC configurations, no output allowed): `-std=c99 -O2 -Wall -Wextra -Wpedantic -Wconversion -Wsign-conversion -Wshadow -Wcast-qual -Wundef -Wstrict-prototypes -Wold-style-definition -Wmissing-prototypes -Wredundant-decls -Wnull-dereference -Wdouble-promotion -Wformat=2 -Wswitch-enum -Wunused`.

---

## Credits

All of the work in this report was done by **Claude Sonnet 5.5 Max** (Anthropic): reading and auditing the original code, finding and fixing the mask-penalty defects, narrowing the integer types, designing and writing the independent checker, the penalty scorer, the Reed–Solomon decoder, the C harness, the 13-section test suite and the mutation tests, running every test, investigating every unexpected result, and writing this report.
