/*
 * main.c -- Windows console demo for the embedded-C QR code generator.
 *
 * Calls qr_generate()/qr_get_module() straight from qrcode.h (compiled
 * in the same project, completely unmodified) and prints the result
 * two ways:
 *
 *   - "full size": two characters wide by one console row tall per
 *     module (solid blocks via console background color only).
 *   - "half size": one character wide, with a single console row
 *     packing TWO module rows using the Unicode block characters
 *     U+2580 (upper half), U+2584 (lower half) and U+2588 (full
 *     block) -- about half the vertical space for the same resolution.
 *
 * Both include the spec-recommended 4-module quiet zone and use a
 * white background, which is what makes the printed result actually
 * scannable rather than just a nice picture.
 *
 * At startup (before anything is printed, whichever rendering is
 * used) the console is switched to black text on a white background
 * and the whole screen buffer is cleared in those colors, so the white
 * backdrop covers the entire window rather than just the printed grid.
 * The console is left in those colors when the program exits; type
 * COLOR (no arguments) in cmd.exe to restore the defaults.
 *
 * QR_VERSION / QR_ECC_LEVEL are the same build-time constants qrcode.h
 * always used -- see the Preprocessor Definitions in this project's
 * properties (or edit qrcode.h's defaults directly) to change them.
 *
 * Console output uses the raw Win32 console API (WriteConsoleW /
 * SetConsoleTextAttribute) rather than printf for the actual grid:
 * mixing buffered CRT output with SetConsoleTextAttribute is a classic
 * source of colors landing on the wrong characters, because the CRT
 * may not flush a printf'd run before the next attribute change takes
 * effect. WriteConsoleW has no such buffering, so each attribute
 * change is guaranteed to apply to exactly the text written after it.
 */

#define _CRT_SECURE_NO_WARNINGS /* plain strcat/fgets below, not the _s variants */
#define WIN32_LEAN_AND_MEAN     /* skip parts of windows.h this program doesn't need */

#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include "qrcode.h"

#define QUIET_ZONE 4
#define ROW_MODULES (QR_SIZE + 2 * QUIET_ZONE) /* modules per printed row, quiet zone included */

/* Win32 console attribute bits. Black is simply "no bits set" for either
 * ground. "White" is the bright white palette entry (BACKGROUND_INTENSITY
 * set), not the light gray that RED|GREEN|BLUE alone gives -- true white
 * gives a scanner noticeably more contrast against the black modules. */
#define BG_WHITE (BACKGROUND_RED | BACKGROUND_GREEN | BACKGROUND_BLUE | BACKGROUND_INTENSITY)
#define BG_BLACK (0)
#define FG_BLACK (0)
/* Block glyphs paint their "ink" in the foreground color and leave the
 * rest of the cell as the background color, so a black module on a
 * white background is: black foreground text on a white cell. */
#define ATTR_GLYPH_ON_WHITE ((WORD)(FG_BLACK | BG_WHITE))
/* The console-wide default for this program: black text on white. */
#define ATTR_DEFAULT ATTR_GLYPH_ON_WHITE

static HANDLE g_hConsole;

/* Makes black-on-white apply to the whole console window: sets it as the
 * current text attribute, then fills the ENTIRE screen buffer with blanks
 * in that attribute and puts the cursor back at the top-left. Setting the
 * attribute alone would only affect text written afterwards; the fill is
 * what repaints everything already on screen (the "clear screen"). */
static void prepare_console(void) {
    CONSOLE_SCREEN_BUFFER_INFO info;
    COORD home = {0, 0};
    DWORD cells, written;

    SetConsoleTextAttribute(g_hConsole, ATTR_DEFAULT);
    if (!GetConsoleScreenBufferInfo(g_hConsole, &info)) {
        return; /* not an interactive console (e.g. output redirected): nothing to clear */
    }
    cells = (DWORD)info.dwSize.X * (DWORD)info.dwSize.Y;
    FillConsoleOutputCharacterW(g_hConsole, L' ', cells, home, &written);
    FillConsoleOutputAttribute(g_hConsole, ATTR_DEFAULT, cells, home, &written);
    SetConsoleCursorPosition(g_hConsole, home);
}

static void reset_attr(void) {
    SetConsoleTextAttribute(g_hConsole, ATTR_DEFAULT);
}

static void write_run(const wchar_t *text, int count, WORD attr) {
    DWORD written;
    SetConsoleTextAttribute(g_hConsole, attr);
    WriteConsoleW(g_hConsole, text, (DWORD)count, &written, NULL);
}

static void newline(void) {
    DWORD written;
    WriteConsoleW(g_hConsole, L"\r\n", 2, &written, NULL);
}

/* True for a dark module; treats the quiet zone (and, for the half-size
 * renderer, one row past the last real row) as light. */
static bool is_dark(const qr_code_t *qr, int x, int y) {
    if (x < 0 || y < 0 || x >= QR_SIZE || y >= QR_SIZE) return false;
    return qr_get_module(qr, (uint8_t)x, (uint8_t)y);
}

static void render_full(const qr_code_t *qr) {
    wchar_t buf[ROW_MODULES * 2];
    for (int y = -QUIET_ZONE; y < QR_SIZE + QUIET_ZONE; y++) {
        int i = 0;
        while (i < ROW_MODULES) {
            bool dark = is_dark(qr, i - QUIET_ZONE, y);
            int len = 0;
            while (i < ROW_MODULES && is_dark(qr, i - QUIET_ZONE, y) == dark) {
                buf[len++] = L' ';
                buf[len++] = L' ';
                i++;
            }
            write_run(buf, len, dark ? BG_BLACK : BG_WHITE);
        }
        newline();
    }
    reset_attr();
}

/* Picks the glyph + attribute for one half-size cell (two module rows,
 * "upper" and "lower", packed into one printed character). */
static void cell_for(bool upper, bool lower, wchar_t *glyph, WORD *attr) {
    if (upper && lower) {
        *glyph = (wchar_t)0x2588; /* full block, black ink on the white cell */
        *attr = ATTR_GLYPH_ON_WHITE;
    } else if (!upper && !lower) {
        *glyph = L' ';
        *attr = BG_WHITE;
    } else if (upper) {
        *glyph = (wchar_t)0x2580; /* upper half block */
        *attr = ATTR_GLYPH_ON_WHITE;
    } else {
        *glyph = (wchar_t)0x2584; /* lower half block */
        *attr = ATTR_GLYPH_ON_WHITE;
    }
}

static void render_half(const qr_code_t *qr) {
    wchar_t buf[ROW_MODULES];
    for (int y = -QUIET_ZONE; y < QR_SIZE + QUIET_ZONE; y += 2) {
        int i = 0;
        while (i < ROW_MODULES) {
            bool upper = is_dark(qr, i - QUIET_ZONE, y);
            bool lower = is_dark(qr, i - QUIET_ZONE, y + 1);
            wchar_t glyph;
            WORD attr;
            cell_for(upper, lower, &glyph, &attr);

            int len = 0;
            for (;;) {
                if (i >= ROW_MODULES) break;
                bool u2 = is_dark(qr, i - QUIET_ZONE, y);
                bool l2 = is_dark(qr, i - QUIET_ZONE, y + 1);
                wchar_t g2;
                WORD a2;
                cell_for(u2, l2, &g2, &a2);
                if (g2 != glyph || a2 != attr) break;
                buf[len++] = glyph;
                i++;
            }
            write_run(buf, len, attr);
        }
        newline();
    }
    reset_attr();
}

int main(int argc, char **argv) {
    g_hConsole = GetStdHandle(STD_OUTPUT_HANDLE);
    prepare_console(); /* black on white + clear screen, before anything is printed */
    SetConsoleOutputCP(CP_UTF8); /* harmless even though rendering uses WriteConsoleW, not codepage-dependent output */

    printf("Native encoder: version %d, ECC level %c, %dx%d modules.\n",
           QR_VERSION, "LMQH"[QR_ECC_LEVEL], QR_SIZE, QR_SIZE);

    char message[2048];
    if (argc > 1) {
        message[0] = '\0';
        for (int i = 1; i < argc; i++) {
            if (i > 1) strcat(message, " ");
            strcat(message, argv[i]);
        }
    } else {
        printf("Enter a message to encode: ");
        fflush(stdout);
        if (!fgets(message, sizeof(message), stdin)) message[0] = '\0';
        size_t len = strlen(message);
        while (len > 0 && (message[len - 1] == '\n' || message[len - 1] == '\r')) {
            message[--len] = '\0';
        }
    }

    static qr_code_t qr; /* static: keep this off the stack */
    size_t msgLen = strlen(message);
    if (!qr_generate(&qr, (const uint8_t *)message, (uint16_t)msgLen)) {
        fprintf(stderr,
                "\nThat message is %zu bytes, which doesn't fit in a version %d "
                "level %c QR code. Try a shorter message, or rebuild with a "
                "larger QR_VERSION or lower QR_ECC_LEVEL (see qrcode.h).\n",
                msgLen, QR_VERSION, "LMQH"[QR_ECC_LEVEL]);
        return 1;
    }

    printf("\nFull size (2 characters wide x 1 row tall per module):\n\n");
    render_full(&qr);

    printf("\nHalf size (1 character wide, 2 module rows per text row):\n\n");
    render_half(&qr);

    printf("\n");
    return 0;
}
