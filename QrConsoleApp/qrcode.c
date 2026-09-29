/*
 * qrcode.c -- see qrcode.h for the public API and configuration.
 *
 * Implementation notes
 * ---------------------
 * - Reed-Solomon math uses a table-free GF(256) multiply (gf_mul) instead
 *   of the usual 256-byte log/antilog tables, trading a handful of extra
 *   CPU cycles per multiply for ~512 bytes of RAM (or ROM) that an 8-bit
 *   MCU project would rather spend elsewhere. QR codes are generated
 *   rarely (usually once per message), so this is a good trade.
 * - The interleaved codeword stream (data blocks + EC blocks, per
 *   ISO/IEC 18004 "Structure Final Message") is produced on the fly by
 *   qr_bitsource_t instead of being materialized in a second buffer.
 *   Only one codeword buffer (s_codewords) ever exists.
 * - All working state lives in `static` storage, not on the stack, so
 *   this won't blow a tiny call stack. It also means qr_generate() is
 *   NOT reentrant -- see qrcode.h.
 *
 * Extending to versions above 20
 * --------------------------------
 * Everything here is data-driven except for two 20-entry tables:
 *   QR_BLOCK_TABLE   -- from https://www.thonky.com/qr-code-tutorial/error-correction-table
 *                        (equivalent to Table 9 of ISO/IEC 18004)
 *   ALIGN_COUNT / ALIGN_POS -- from
 *                        https://www.thonky.com/qr-code-tutorial/alignment-pattern-locations
 * and one macro chain (QR__TOTAL_CW_*) giving the total codeword count
 * per version (independent of ECC level -- it only depends on the
 * symbol's module geometry). To support versions 21-40, extend all
 * three tables using the linked pages (or ISO/IEC 18004 Annex tables)
 * and bump the range check in qrcode.h. Everything downstream (Reed-
 * Solomon, placement, masking, format/version info) already works for
 * any version because it is computed algorithmically, not tabulated.
 */

#include "qrcode.h"
#include <string.h>

/* ==================================================================== */
/* Total codeword count per version (data + EC codewords combined).     */
/* This is fixed by the symbol's module geometry alone and does NOT     */
/* depend on the ECC level (only the data/EC split within this total    */
/* does). Verified by summing QR_BLOCK_TABLE rows below.                */
/* ==================================================================== */
#define QR__TOTAL_CW_1    26
#define QR__TOTAL_CW_2    44
#define QR__TOTAL_CW_3    70
#define QR__TOTAL_CW_4   100
#define QR__TOTAL_CW_5   134
#define QR__TOTAL_CW_6   172
#define QR__TOTAL_CW_7   196
#define QR__TOTAL_CW_8   242
#define QR__TOTAL_CW_9   292
#define QR__TOTAL_CW_10  346
#define QR__TOTAL_CW_11  404
#define QR__TOTAL_CW_12  466
#define QR__TOTAL_CW_13  532
#define QR__TOTAL_CW_14  581
#define QR__TOTAL_CW_15  655
#define QR__TOTAL_CW_16  733
#define QR__TOTAL_CW_17  815
#define QR__TOTAL_CW_18  901
#define QR__TOTAL_CW_19  991
#define QR__TOTAL_CW_20 1085

#define QR__CAT_(a, b) a##b
#define QR__CAT(a, b) QR__CAT_(a, b)
#define QR_TOTAL_CODEWORDS QR__CAT(QR__TOTAL_CW_, QR_VERSION)

#define QR_MAX_EC_PER_BLOCK 30

/* ==================================================================== */
/* Error-correction block structure table, versions 1-20, one row per   */
/* version, four columns per row in the order L, M, Q, H (matching      */
/* QR_ECC_L..QR_ECC_H). Source: see file header comment.                */
/* ==================================================================== */
typedef struct {
    uint8_t ec_per_block;
    uint8_t g1_blocks;
    uint8_t g1_len;
    uint8_t g2_blocks;
    uint8_t g2_len;
} qr_block_info_t;

static const qr_block_info_t QR_BLOCK_TABLE[20][4] = {
    /* v1  */ {{7,1,19,0,0},   {10,1,16,0,0},  {13,1,13,0,0},  {17,1,9,0,0}},
    /* v2  */ {{10,1,34,0,0},  {16,1,28,0,0},  {22,1,22,0,0},  {28,1,16,0,0}},
    /* v3  */ {{15,1,55,0,0},  {26,1,44,0,0},  {18,2,17,0,0},  {22,2,13,0,0}},
    /* v4  */ {{20,1,80,0,0},  {18,2,32,0,0},  {26,2,24,0,0},  {16,4,9,0,0}},
    /* v5  */ {{26,1,108,0,0}, {24,2,43,0,0},  {18,2,15,2,16}, {22,2,11,2,12}},
    /* v6  */ {{18,2,68,0,0},  {16,4,27,0,0},  {24,4,19,0,0},  {28,4,15,0,0}},
    /* v7  */ {{20,2,78,0,0},  {18,4,31,0,0},  {18,2,14,4,15}, {26,4,13,1,14}},
    /* v8  */ {{24,2,97,0,0},  {22,2,38,2,39}, {22,4,18,2,19}, {26,4,14,2,15}},
    /* v9  */ {{30,2,116,0,0},{22,3,36,2,37}, {20,4,16,4,17}, {24,4,12,4,13}},
    /* v10 */ {{18,2,68,2,69}, {26,4,43,1,44}, {24,6,19,2,20}, {28,6,15,2,16}},
    /* v11 */ {{20,4,81,0,0},  {30,1,50,4,51}, {28,4,22,4,23}, {24,3,12,8,13}},
    /* v12 */ {{24,2,92,2,93}, {22,6,36,2,37}, {26,4,20,6,21}, {28,7,14,4,15}},
    /* v13 */ {{26,4,107,0,0},{22,8,37,1,38}, {24,8,20,4,21}, {22,12,11,4,12}},
    /* v14 */ {{30,3,115,1,116},{24,4,40,5,41},{20,11,16,5,17},{24,11,12,5,13}},
    /* v15 */ {{22,5,87,1,88}, {24,5,41,5,42}, {30,5,24,7,25}, {24,11,12,7,13}},
    /* v16 */ {{24,5,98,1,99}, {28,7,45,3,46}, {24,15,19,2,20},{30,3,15,13,16}},
    /* v17 */ {{28,1,107,5,108},{28,10,46,1,47},{28,1,22,15,23},{28,2,14,17,15}},
    /* v18 */ {{30,5,120,1,121},{26,9,43,4,44},{28,17,22,1,23},{28,2,14,19,15}},
    /* v19 */ {{28,3,113,4,114},{26,3,44,11,45},{26,17,21,4,22},{26,9,13,16,14}},
    /* v20 */ {{28,3,107,5,108},{26,3,41,13,42},{30,15,24,5,25},{28,15,15,10,16}},
};

/* ==================================================================== */
/* Alignment pattern center coordinates, versions 1-20. The same list   */
/* is used for both row and column; see draw_all_alignment_patterns().  */
/* ==================================================================== */
static const uint8_t ALIGN_COUNT[20] = {
    0, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4, 4, 4
};
static const uint8_t ALIGN_POS[20][4] = {
    {0,0,0,0},    {6,18,0,0},   {6,22,0,0},   {6,26,0,0},   {6,30,0,0},
    {6,34,0,0},   {6,22,38,0},  {6,24,42,0},  {6,26,46,0},  {6,28,50,0},
    {6,30,54,0},  {6,32,58,0},  {6,34,62,0},  {6,26,46,66}, {6,26,48,70},
    {6,26,50,74}, {6,30,54,78}, {6,30,56,82}, {6,30,58,86}, {6,34,62,90},
};

/* ==================================================================== */
/* Working storage. `static` (not on the stack, not part of qr_code_t)  */
/* because it is only needed transiently, during qr_generate().         */
/* ==================================================================== */
static uint8_t s_codewords[QR_TOTAL_CODEWORDS];
static uint8_t s_is_function[QR_BITMAP_BYTES];

/* ==================================================================== */
/* Bit-packed grid helpers. x = column, y = row, both 0-based.          */
/* ==================================================================== */
static inline uint16_t xy_idx(int x, int y) {
    return (uint16_t)((uint16_t)y * QR_SIZE + (uint16_t)x);
}
static inline bool bmp_get(const uint8_t *bmp, uint16_t idx) {
    return (bmp[idx >> 3] >> (7 - (idx & 7))) & 1u;
}
static inline void bmp_set(uint8_t *bmp, uint16_t idx, bool v) {
    uint8_t mask = (uint8_t)(1u << (7 - (idx & 7)));
    if (v) bmp[idx >> 3] |= mask;
    else   bmp[idx >> 3] &= (uint8_t)~mask;
}
static inline bool is_function(int x, int y) {
    return bmp_get(s_is_function, xy_idx(x, y));
}
static inline void set_function(int x, int y) {
    bmp_set(s_is_function, xy_idx(x, y), true);
}
static inline bool get_module_bit(const qr_code_t *qr, int x, int y) {
    return bmp_get(qr->modules, xy_idx(x, y));
}
static inline void set_module_bit(qr_code_t *qr, int x, int y, bool v) {
    bmp_set(qr->modules, xy_idx(x, y), v);
}

/* ==================================================================== */
/* GF(256) arithmetic, table-free (see file header comment).            */
/* Uses the primitive polynomial required by the QR spec: x^8+x^4+x^3   */
/* +x^2+1 (0x11D); 0x1D is that polynomial with the leading term        */
/* dropped, used as the reduction constant.                             */
/* ==================================================================== */
static uint8_t gf_mul(uint8_t a, uint8_t b) {
    uint8_t result = 0;
    for (int i = 0; i < 8; i++) {
        if (b & 1) result ^= a;
        uint8_t hiBitSet = (uint8_t)(a & 0x80u);
        a = (uint8_t)(a << 1);
        if (hiBitSet) a ^= 0x1Du;
        b >>= 1;
    }
    return result;
}

/* Builds the degree-`ecLen` Reed-Solomon generator polynomial into
 * gen[0..ecLen] (leading coefficient first, gen[0] is always 1). */
static void build_generator(uint8_t *gen, uint8_t ecLen) {
    for (int i = 0; i <= ecLen; i++) gen[i] = 0;
    gen[0] = 1;
    uint8_t degree = 0;
    uint8_t root = 1; /* alpha^0 */
    for (int k = 0; k < ecLen; k++) {
        gen[degree + 1] = gf_mul(gen[degree], root);
        for (int j = degree; j >= 1; j--) {
            gen[j] = (uint8_t)(gen[j] ^ gf_mul(gen[j - 1], root));
        }
        /* gen[0] stays 1: the polynomial stays monic. */
        degree++;
        root = gf_mul(root, 2); /* alpha = 2 for the QR spec's field */
    }
}

/* Computes ecLen EC codewords for one block of dataLen data codewords,
 * via polynomial long division by the precomputed generator. */
static void rs_encode(const uint8_t *data, uint8_t dataLen,
                       const uint8_t *gen, uint8_t ecLen, uint8_t *ecOut) {
    for (int i = 0; i < ecLen; i++) ecOut[i] = 0;
    for (int i = 0; i < dataLen; i++) {
        uint8_t factor = (uint8_t)(data[i] ^ ecOut[0]);
        for (int j = 0; j < ecLen - 1; j++) ecOut[j] = ecOut[j + 1];
        ecOut[ecLen - 1] = 0;
        if (factor != 0) {
            for (int j = 0; j < ecLen; j++) {
                ecOut[j] = (uint8_t)(ecOut[j] ^ gf_mul(gen[j + 1], factor));
            }
        }
    }
}

/* ==================================================================== */
/* Byte-mode data encoding (mode indicator + length + data + padding).  */
/* ==================================================================== */
typedef struct {
    uint8_t *buf;
    uint32_t bitPos;
} bitwriter_t;

static void bw_write(bitwriter_t *bw, uint32_t value, uint8_t numBits) {
    for (int i = numBits - 1; i >= 0; i--) {
        uint32_t bp = bw->bitPos;
        uint8_t bit = (uint8_t)((value >> i) & 1u);
        uint8_t mask = (uint8_t)(1u << (7 - (bp & 7)));
        if (bit) bw->buf[bp >> 3] |= mask;
        else     bw->buf[bp >> 3] &= (uint8_t)~mask;
        bw->bitPos++;
    }
}

/* ==================================================================== */
/* Finder patterns (+ separators), alignment patterns, timing patterns. */
/* Uses a Chebyshev-distance rule instead of hardcoded 2D pixel arrays: */
/* a 7x7 finder is dark everywhere except the ring at distance 2 from   */
/* its center; a 5x5 alignment pattern is dark everywhere except the    */
/* ring at distance 1.                                                  */
/* ==================================================================== */
static void draw_finder_and_separator(qr_code_t *qr, int box_top, int box_left,
                                       int f_row_off, int f_col_off) {
    /* 8x8 box: finder + separator. Pre-clear to light so whichever part
     * of the box the 7x7 finder does NOT cover is left as the (light)
     * separator automatically -- works for all 3 corners regardless of
     * which sides need the separator. */
    for (int r = 0; r < 8; r++) {
        for (int c = 0; c < 8; c++) {
            int y = box_top + r, x = box_left + c;
            set_function(x, y);
            set_module_bit(qr, x, y, false);
        }
    }
    for (int r = 0; r < 7; r++) {
        for (int c = 0; c < 7; c++) {
            int y = box_top + f_row_off + r, x = box_left + f_col_off + c;
            int dr = r - 3, dc = c - 3;
            int ad = dr < 0 ? -dr : dr, adc = dc < 0 ? -dc : dc;
            int d = ad > adc ? ad : adc;
            set_module_bit(qr, x, y, d != 2);
        }
    }
}

static void draw_alignment(qr_code_t *qr, int cx, int cy) {
    for (int dr = -2; dr <= 2; dr++) {
        for (int dc = -2; dc <= 2; dc++) {
            int x = cx + dc, y = cy + dr;
            set_function(x, y);
            int ad = dr < 0 ? -dr : dr, adc = dc < 0 ? -dc : dc;
            int d = ad > adc ? ad : adc;
            set_module_bit(qr, x, y, d != 1);
        }
    }
}

static void draw_all_alignment_patterns(qr_code_t *qr) {
    uint8_t n = ALIGN_COUNT[QR_VERSION - 1];
    if (n == 0) return;
    const uint8_t *pos = ALIGN_POS[QR_VERSION - 1];
    for (int i = 0; i < n; i++) {
        for (int j = 0; j < n; j++) {
            /* Skip the 3 combinations that would overlap a finder
             * pattern + separator (top-left, top-right, bottom-left). */
            if ((i == 0 && j == 0) || (i == 0 && j == n - 1) ||
                (i == n - 1 && j == 0)) {
                continue;
            }
            draw_alignment(qr, pos[j] /* column */, pos[i] /* row */);
        }
    }
}

static void draw_timing(qr_code_t *qr) {
    for (int i = 8; i <= QR_SIZE - 9; i++) {
        set_function(i, 6);
        set_module_bit(qr, i, 6, (i % 2) == 0);
        set_function(6, i);
        set_module_bit(qr, 6, i, (i % 2) == 0);
    }
}

static void place_dark_module(qr_code_t *qr) {
    int y = 4 * QR_VERSION + 9, x = 8;
    set_function(x, y);
    set_module_bit(qr, x, y, true);
}

/* ==================================================================== */
/* Format info (always) and version info (version >= 7 only).           */
/* Coordinate layout and BCH generator polynomials per ISO/IEC 18004;   */
/* cross-checked against the worked examples in Thonky's QR tutorial.   */
/* `write` false = reservation pass (mark function, value irrelevant);  */
/* `write` true = final pass (mark function AND set the real bits).     */
/* ==================================================================== */
static void reserve_and_maybe_write_format(qr_code_t *qr, bool write, uint16_t bits) {
    static const uint8_t tl_row[15] = {0,1,2,3,4,5,7,8,8,8,8,8,8,8,8};
    static const uint8_t tl_col[15] = {8,8,8,8,8,8,8,8,7,5,4,3,2,1,0};
    for (int i = 0; i < 15; i++) {
        bool bit = (bool)((bits >> i) & 1u);

        int x1 = tl_col[i], y1 = tl_row[i];
        set_function(x1, y1);
        if (write) set_module_bit(qr, x1, y1, bit);

        int x2, y2;
        if (i < 7) { y2 = QR_SIZE - 1 - i; x2 = 8; }
        else       { y2 = 8; x2 = QR_SIZE - 15 + i; }
        set_function(x2, y2);
        if (write) set_module_bit(qr, x2, y2, bit);
    }
}

#if QR_VERSION >= 7
static void reserve_and_maybe_write_version(qr_code_t *qr, bool write, uint32_t bits) {
    for (int i = 0; i < 18; i++) {
        int a = (QR_SIZE - 11) + (i % 3);
        int b = i / 3;
        bool bit = (bool)((bits >> i) & 1u);

        /* bottom-left block: row = a, col = b */
        set_function(b, a);
        if (write) set_module_bit(qr, b, a, bit);
        /* top-right block: row = b, col = a */
        set_function(a, b);
        if (write) set_module_bit(qr, a, b, bit);
    }
}
#endif

/* 15-bit format string: 5 data bits (2-bit ECC indicator + 3-bit mask
 * number) BCH(15,5)-encoded with generator 0x537, then XORed with the
 * fixed mask 0x5412 (both values are constants defined by the spec). */
static uint16_t compute_format_bits(uint8_t data5) {
    uint32_t rem = data5;
    for (int i = 0; i < 10; i++) {
        rem = (rem << 1) ^ ((rem >> 9) * 0x537u);
    }
    uint16_t bits = (uint16_t)(((uint32_t)data5 << 10) | rem);
    return (uint16_t)(bits ^ 0x5412u);
}

#if QR_VERSION >= 7
/* 18-bit version string: 6-bit version number, BCH(18,6)-encoded with
 * generator 0x1F25 (no extra XOR mask for version info). */
static uint32_t compute_version_bits(uint8_t version) {
    uint32_t rem = version;
    for (int i = 0; i < 12; i++) {
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25u);
    }
    return ((uint32_t)version << 12) | rem;
}
#endif

/* ==================================================================== */
/* Data placement: the interleaved codeword stream is generated on the  */
/* fly (see file header comment) and fed into the classic "two columns  */
/* at a time, snaking up/down, skip the timing column" placement scan.  */
/* ==================================================================== */
typedef struct {
    const uint8_t *codewords;
    uint16_t totalDataCW;
    uint8_t ecPerBlock;
    uint8_t g1n, g1len, g2n, g2len;
    uint8_t totalBlocks;
    uint8_t maxLen;
    uint8_t phase; /* 0 = data columns, 1 = EC columns, 2 = exhausted */
    uint8_t col;
    uint8_t block;
    uint8_t curByte;
    int8_t bitsLeft;
} qr_bitsource_t;

static bool bitsource_next_byte(qr_bitsource_t *bs) {
    for (;;) {
        if (bs->phase == 0) {
            if (bs->block >= bs->totalBlocks) {
                bs->block = 0;
                bs->col++;
                if (bs->col >= bs->maxLen) { bs->phase = 1; bs->col = 0; bs->block = 0; }
                continue;
            }
            uint8_t len = (bs->block < bs->g1n) ? bs->g1len : bs->g2len;
            if (bs->col < len) {
                uint16_t offset = (bs->block < bs->g1n)
                    ? (uint16_t)bs->block * bs->g1len
                    : (uint16_t)bs->g1n * bs->g1len + (uint16_t)(bs->block - bs->g1n) * bs->g2len;
                bs->curByte = bs->codewords[offset + bs->col];
                bs->block++;
                return true;
            }
            bs->block++;
        } else if (bs->phase == 1) {
            if (bs->col >= bs->ecPerBlock) { bs->phase = 2; return false; }
            if (bs->block >= bs->totalBlocks) { bs->block = 0; bs->col++; continue; }
            uint16_t offset = (uint16_t)(bs->totalDataCW +
                (uint16_t)((uint16_t)bs->block * bs->ecPerBlock) + bs->col);
            bs->curByte = bs->codewords[offset];
            bs->block++;
            return true;
        } else {
            return false;
        }
    }
}

static bool bitsource_next_bit(qr_bitsource_t *bs, bool *outBit) {
    if (bs->bitsLeft <= 0) {
        if (!bitsource_next_byte(bs)) return false;
        bs->bitsLeft = 8;
    }
    bs->bitsLeft--;
    *outBit = (bs->curByte >> bs->bitsLeft) & 1u;
    return true;
}

static void place_data(qr_code_t *qr, qr_bitsource_t *bs) {
    bool upward = true;
    for (int col = QR_SIZE - 1; col >= 1; col -= 2) {
        if (col == 6) col--; /* never place data in the vertical timing column */
        for (int i = 0; i < QR_SIZE; i++) {
            int row = upward ? (QR_SIZE - 1 - i) : i;
            for (int c = 0; c < 2; c++) {
                int x = col - c;
                if (!is_function(x, row)) {
                    bool bit = false;
                    if (bitsource_next_bit(bs, &bit) && bit) {
                        set_module_bit(qr, x, row, true);
                    }
                    /* stream exhausted -> cell keeps its default 0 value;
                     * this IS the spec's "remainder bits", with no
                     * separate table needed. */
                }
            }
        }
        upward = !upward;
    }
}

/* ==================================================================== */
/* Masking. Trial masks are scored without materializing a second grid: */
/* the masked value of a data module is computed on the fly from the    */
/* unmasked grid + the mask formula, so only one grid ever exists.      */
/* ==================================================================== */
static bool mask_condition(uint8_t mask, int x, int y) {
    switch (mask) {
        case 0: return ((x + y) % 2) == 0;
        case 1: return (y % 2) == 0;
        case 2: return (x % 3) == 0;
        case 3: return ((x + y) % 3) == 0;
        case 4: return (((y / 2) + (x / 3)) % 2) == 0;
        case 5: return (((x * y) % 2) + ((x * y) % 3)) == 0;
        case 6: return ((((x * y) % 2) + ((x * y) % 3)) % 2) == 0;
        default: return ((((x + y) % 2) + ((x * y) % 3)) % 2) == 0; /* case 7 */
    }
}

static inline bool masked_value(const qr_code_t *qr, uint8_t mask, int x, int y) {
    bool v = get_module_bit(qr, x, y);
    if (is_function(x, y)) return v;
    return v ^ mask_condition(mask, x, y);
}

#ifndef QR_FIXED_MASK
static uint32_t evaluate_penalty(const qr_code_t *qr, uint8_t mask) {
    uint32_t penalty = 0;

    /* Rule 1: runs of 5+ same-color modules, per row and per column. */
    for (int y = 0; y < QR_SIZE; y++) {
        int runLen = 1;
        bool prev = masked_value(qr, mask, 0, y);
        for (int x = 1; x < QR_SIZE; x++) {
            bool v = masked_value(qr, mask, x, y);
            if (v == prev) {
                runLen++;
            } else {
                if (runLen >= 5) penalty += (uint32_t)(runLen - 5 + 3);
                runLen = 1;
                prev = v;
            }
        }
        if (runLen >= 5) penalty += (uint32_t)(runLen - 5 + 3);
    }
    for (int x = 0; x < QR_SIZE; x++) {
        int runLen = 1;
        bool prev = masked_value(qr, mask, x, 0);
        for (int y = 1; y < QR_SIZE; y++) {
            bool v = masked_value(qr, mask, x, y);
            if (v == prev) {
                runLen++;
            } else {
                if (runLen >= 5) penalty += (uint32_t)(runLen - 5 + 3);
                runLen = 1;
                prev = v;
            }
        }
        if (runLen >= 5) penalty += (uint32_t)(runLen - 5 + 3);
    }

    /* Rule 2: 2x2 blocks of one color. */
    for (int y = 0; y < QR_SIZE - 1; y++) {
        for (int x = 0; x < QR_SIZE - 1; x++) {
            bool v = masked_value(qr, mask, x, y);
            if (v == masked_value(qr, mask, x + 1, y) &&
                v == masked_value(qr, mask, x, y + 1) &&
                v == masked_value(qr, mask, x + 1, y + 1)) {
                penalty += 3;
            }
        }
    }

    /* Rule 3: finder-like 1:1:3:1:1 pattern with 4 light modules attached. */
    for (int y = 0; y < QR_SIZE; y++) {
        for (int x = 0; x + 10 < QR_SIZE; x++) {
            bool m[11];
            for (int k = 0; k < 11; k++) m[k] = masked_value(qr, mask, x + k, y);
            if ((m[0] && !m[1] && m[2] && m[3] && m[4] && !m[5] && m[6] && !m[7] && !m[8] && !m[9] && !m[10]) ||
                (!m[0] && !m[1] && !m[2] && !m[3] && m[4] && !m[5] && m[6] && m[7] && m[8] && !m[9] && m[10])) {
                penalty += 40;
            }
        }
    }
    for (int x = 0; x < QR_SIZE; x++) {
        for (int y = 0; y + 10 < QR_SIZE; y++) {
            bool m[11];
            for (int k = 0; k < 11; k++) m[k] = masked_value(qr, mask, x, y + k);
            if ((m[0] && !m[1] && m[2] && m[3] && m[4] && !m[5] && m[6] && !m[7] && !m[8] && !m[9] && !m[10]) ||
                (!m[0] && !m[1] && !m[2] && !m[3] && m[4] && !m[5] && m[6] && m[7] && m[8] && !m[9] && m[10])) {
                penalty += 40;
            }
        }
    }

    /* Rule 4: overall dark/light balance. */
    uint32_t dark = 0;
    for (int y = 0; y < QR_SIZE; y++) {
        for (int x = 0; x < QR_SIZE; x++) {
            if (masked_value(qr, mask, x, y)) dark++;
        }
    }
    uint32_t total = (uint32_t)QR_SIZE * (uint32_t)QR_SIZE;
    uint32_t percent = dark * 100u / total;
    uint32_t prev5 = (percent / 5u) * 5u;
    uint32_t next5 = prev5 + 5u;
    uint32_t a = prev5 > 50u ? prev5 - 50u : 50u - prev5;
    uint32_t b = next5 > 50u ? next5 - 50u : 50u - next5;
    penalty += (a < b ? a : b) / 5u * 10u;

    return penalty;
}
#endif /* !QR_FIXED_MASK */

static void apply_mask_final(qr_code_t *qr, uint8_t mask) {
    for (int y = 0; y < QR_SIZE; y++) {
        for (int x = 0; x < QR_SIZE; x++) {
            if (!is_function(x, y) && mask_condition(mask, x, y)) {
                set_module_bit(qr, x, y, !get_module_bit(qr, x, y));
            }
        }
    }
}

/* ==================================================================== */
/* Public API                                                           */
/* ==================================================================== */
bool qr_generate(qr_code_t *qr, const uint8_t *data, uint16_t len) {
    const qr_block_info_t *bi = &QR_BLOCK_TABLE[QR_VERSION - 1][QR_ECC_LEVEL];
    uint16_t totalData = (uint16_t)((uint16_t)(bi->g1_blocks * bi->g1_len) +
                                     (uint16_t)(bi->g2_blocks * bi->g2_len));
    uint16_t totalBlocks = (uint16_t)bi->g1_blocks + bi->g2_blocks;

    uint8_t cciBits = (QR_VERSION <= 9) ? 8 : 16;
    uint32_t neededBits = 4u + cciBits + (uint32_t)len * 8u;
    if (neededBits > (uint32_t)totalData * 8u) return false; /* message too long */
    if (cciBits == 8 && len > 255u) return false;

    memset(s_codewords, 0, sizeof(s_codewords));
    memset(s_is_function, 0, sizeof(s_is_function));
    memset(qr->modules, 0, sizeof(qr->modules));

    /* --- Encode: mode indicator, length, data, terminator, padding --- */
    bitwriter_t bw = { s_codewords, 0 };
    bw_write(&bw, 0x4u /* binary/byte mode */, 4);
    bw_write(&bw, len, cciBits);
    for (uint16_t i = 0; i < len; i++) bw_write(&bw, data[i], 8);

    uint32_t totalDataBits = (uint32_t)totalData * 8u;
    uint32_t remaining = totalDataBits - bw.bitPos;
    uint8_t term = (uint8_t)(remaining < 4u ? remaining : 4u);
    bw_write(&bw, 0, term);
    while (bw.bitPos % 8u != 0u) bw_write(&bw, 0, 1);
    {
        bool useEC = true;
        while ((bw.bitPos / 8u) < totalData) {
            bw_write(&bw, useEC ? 0xECu : 0x11u, 8);
            useEC = !useEC;
        }
    }

    /* --- Error correction --- */
    uint8_t gen[QR_MAX_EC_PER_BLOCK + 1];
    build_generator(gen, bi->ec_per_block);
    for (uint16_t b = 0; b < totalBlocks; b++) {
        uint8_t blen = (b < bi->g1_blocks) ? bi->g1_len : bi->g2_len;
        uint16_t off = (b < bi->g1_blocks)
            ? (uint16_t)b * bi->g1_len
            : (uint16_t)bi->g1_blocks * bi->g1_len + (uint16_t)(b - bi->g1_blocks) * bi->g2_len;
        uint8_t ec[QR_MAX_EC_PER_BLOCK];
        rs_encode(&s_codewords[off], blen, gen, bi->ec_per_block, ec);
        memcpy(&s_codewords[totalData + (uint16_t)b * bi->ec_per_block], ec, bi->ec_per_block);
    }

    /* --- Function patterns --- */
    draw_finder_and_separator(qr, 0, 0, 0, 0);
    draw_finder_and_separator(qr, 0, QR_SIZE - 8, 0, 1);
    draw_finder_and_separator(qr, QR_SIZE - 8, 0, 1, 0);
    draw_all_alignment_patterns(qr);
    draw_timing(qr);
    reserve_and_maybe_write_format(qr, false, 0);
#if QR_VERSION >= 7
    reserve_and_maybe_write_version(qr, false, 0);
#endif
    place_dark_module(qr);

    /* --- Data placement --- */
    qr_bitsource_t bs = {
        .codewords = s_codewords,
        .totalDataCW = totalData,
        .ecPerBlock = bi->ec_per_block,
        .g1n = bi->g1_blocks, .g1len = bi->g1_len,
        .g2n = bi->g2_blocks, .g2len = bi->g2_len,
        .totalBlocks = (uint8_t)totalBlocks,
        .maxLen = (uint8_t)(bi->g1_len > bi->g2_len ? bi->g1_len : bi->g2_len),
        .phase = 0, .col = 0, .block = 0, .curByte = 0, .bitsLeft = 0
    };
    place_data(qr, &bs);

    /* --- Masking --- */
#ifdef QR_FIXED_MASK
    uint8_t best = QR_FIXED_MASK;
#else
    uint8_t best = 0;
    uint32_t bestScore = 0xFFFFFFFFu;
    for (uint8_t m = 0; m < 8; m++) {
        uint32_t score = evaluate_penalty(qr, m);
        if (score < bestScore) { bestScore = score; best = m; }
    }
#endif
    apply_mask_final(qr, best);

    /* --- Format & version info (written last: never masked) --- */
    static const uint8_t ECC_INDICATOR[4] = {1, 0, 3, 2}; /* L=01 M=00 Q=11 H=10 */
    uint16_t fbits = compute_format_bits((uint8_t)((ECC_INDICATOR[QR_ECC_LEVEL] << 3) | best));
    reserve_and_maybe_write_format(qr, true, fbits);
#if QR_VERSION >= 7
    uint32_t vbits = compute_version_bits(QR_VERSION);
    reserve_and_maybe_write_version(qr, true, vbits);
#endif

    return true;
}

bool qr_get_module(const qr_code_t *qr, uint8_t x, uint8_t y) {
    if (x >= QR_SIZE || y >= QR_SIZE) return false;
    return get_module_bit(qr, x, y);
}
