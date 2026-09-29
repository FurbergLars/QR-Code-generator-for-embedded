/*
 * qrcode.h -- Minimal-RAM QR Code generator for 8-bit MCUs.
 *
 * Scope / design goals
 * ---------------------
 *   - Only "byte" (binary) encoding mode is supported.
 *   - QR version and error correction level are BUILD-TIME constants
 *     (see below). Nothing about the symbol's structure is decided at
 *     runtime, so no per-instance configuration struct is needed and
 *     no RAM is spent storing settings that never change.
 *   - The ONLY thing that can change after the firmware is built is the
 *     message string (and its length) passed into qr_generate().
 *   - All large lookup tables live in `const` arrays so a normal
 *     embedded toolchain places them in flash/ROM, not RAM.
 *   - Reed-Solomon math is done with a table-free GF(256) multiply
 *     (see qrcode.c), so no 256-byte log/antilog tables are needed.
 *
 * How to configure
 * -----------------
 * Define these before including this header (or pass them on the
 * compiler command line, e.g. `-DQR_VERSION=4 -DQR_ECC_LEVEL=QR_ECC_M`):
 *
 *   QR_VERSION     1..20   (symbol size = 4*QR_VERSION + 17 modules)
 *   QR_ECC_LEVEL   one of QR_ECC_L, QR_ECC_M, QR_ECC_Q, QR_ECC_H
 *   QR_FIXED_MASK  optional, 0..7. If defined, that mask pattern is used
 *                  unconditionally and the mask-penalty scoring code is
 *                  compiled out (saves flash + a bit of RAM headroom on
 *                  the stack). If left undefined (default), the encoder
 *                  tries all 8 masks and keeps the best one, as the spec
 *                  recommends.
 *
 * If you don't define QR_VERSION / QR_ECC_LEVEL, sensible defaults are
 * used so the library still compiles out of the box.
 *
 * Versions above 20 are not included in the shipped tables. See the
 * comment above QR_BLOCK_TABLE in qrcode.c for how to extend this.
 *
 * Memory usage (approximate, all static, no malloc/free anywhere)
 * -----------------------------------------------------------------
 *   Persistent (part of qr_code_t, must live as long as you need the
 *   symbol, e.g. to keep redrawing it to a display):
 *       modules[]       = ceil(QR_SIZE * QR_SIZE / 8) bytes
 *
 *   Transient (internal `static` buffers in qrcode.c, only need to be
 *   valid during qr_generate() -- see the comment above them if you
 *   want to overlay this memory with something else in your linker
 *   script):
 *       function-module map   = ceil(QR_SIZE * QR_SIZE / 8) bytes
 *       codeword buffer       = (total data + EC codewords for the
 *                                chosen version/ECC level) bytes
 *       a few small block-offset arrays (a handful of bytes)
 *
 *   Example: version 4, level M -> 33x33 symbol -> bitmap = 137 bytes,
 *   100 data+EC codewords total -> roughly 137 + 137 + 100 = 374 bytes
 *   of static RAM altogether (measured; see the project README).
 */

#ifndef QRCODE_H
#define QRCODE_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ */
/* Error correction level constants (values match the order used      */
/* internally; do not change without updating qrcode.c's tables).     */
/* ------------------------------------------------------------------ */
#define QR_ECC_L 0u  /*  ~7% of codewords can be restored */
#define QR_ECC_M 1u  /* ~15% of codewords can be restored */
#define QR_ECC_Q 2u  /* ~25% of codewords can be restored */
#define QR_ECC_H 3u  /* ~30% of codewords can be restored */

/* ------------------------------------------------------------------ */
/* Build-time configuration (override before including this header,   */
/* or with -D compiler flags).                                        */
/* ------------------------------------------------------------------ */
#ifndef QR_VERSION
#define QR_VERSION 3
#endif

#ifndef QR_ECC_LEVEL
#define QR_ECC_LEVEL QR_ECC_M
#endif

#if (QR_VERSION < 1) || (QR_VERSION > 20)
#error "QR_VERSION must be between 1 and 20 (see qrcode.c to extend the tables to larger versions)"
#endif

#if (QR_ECC_LEVEL != QR_ECC_L) && (QR_ECC_LEVEL != QR_ECC_M) && \
    (QR_ECC_LEVEL != QR_ECC_Q) && (QR_ECC_LEVEL != QR_ECC_H)
#error "QR_ECC_LEVEL must be QR_ECC_L, QR_ECC_M, QR_ECC_Q or QR_ECC_H"
#endif

#if defined(QR_FIXED_MASK) && ((QR_FIXED_MASK) < 0 || (QR_FIXED_MASK) > 7)
#error "QR_FIXED_MASK must be between 0 and 7"
#endif

/* Side length of the symbol, in modules. */
#define QR_SIZE (4 * (QR_VERSION) + 17)

/* Number of bytes needed to bit-pack one QR_SIZE x QR_SIZE bitmap. */
#define QR_BITMAP_BYTES (((QR_SIZE) * (QR_SIZE) + 7u) / 8u)

/* ------------------------------------------------------------------ */
/* Public types                                                        */
/* ------------------------------------------------------------------ */

/*
 * Holds one generated symbol. This is the only piece of state that
 * needs to outlive the qr_generate() call. Size is fixed at compile
 * time by QR_VERSION.
 */
typedef struct {
    uint8_t modules[QR_BITMAP_BYTES]; /* 1 = dark module, 0 = light */
} qr_code_t;

/* ------------------------------------------------------------------ */
/* Public API                                                          */
/* ------------------------------------------------------------------ */

/*
 * Encode `len` bytes from `data` (binary/byte mode) into *qr.
 *
 * Returns true on success. Returns false if the message is too long to
 * fit in the data capacity of QR_VERSION/QR_ECC_LEVEL -- in that case
 * *qr is left in an undefined state and must not be used.
 *
 * Not reentrant / not thread-safe: internal working buffers are shared
 * `static` storage (see qrcode.c). Do not call this from more than one
 * task/ISR context at a time, and don't call it again to build a
 * second symbol before you're done reading out the first one, unless
 * you're fully done with qr_generate's side effects (the result in
 * *qr is safe to keep around; only the internal scratch buffers are
 * reused on the next call).
 */
bool qr_generate(qr_code_t *qr, const uint8_t *data, uint16_t len);

/*
 * Read back one module. x = column, y = row, both 0-based, 0..QR_SIZE-1.
 * Returns true for a dark module, false for a light one.
 * (0,0) is the top-left corner, matching the finder pattern there.
 */
bool qr_get_module(const qr_code_t *qr, uint8_t x, uint8_t y);

#ifdef __cplusplus
}
#endif

#endif /* QRCODE_H */
