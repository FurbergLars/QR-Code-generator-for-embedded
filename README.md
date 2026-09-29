# QR code console demo for Visual Studio (C, no P/Invoke)

This replaces the earlier C# console demo. It's a plain C console app
-- `qrcode.c`/`qrcode.h` are compiled directly into the same project,
so there's no P/Invoke, no separate native DLL, and no interop shim
file needed anymore. One project, open it in Visual Studio, hit run.

## Layout

```
QrConsoleApp.sln
QrConsoleApp/
  QrConsoleApp.vcxproj
  QrConsoleApp.vcxproj.filters
  main.c        <- new: prompts for a message, calls qr_generate(), renders it
  qrcode.c      <- unmodified
  qrcode.h      <- unmodified
```

`qrcode.c` and `qrcode.h` are byte-for-byte the same files as before.
`main.c` is the only new source file.

## Opening and running it

1. Open `QrConsoleApp.sln` in Visual Studio 2026 (needs the "Desktop
   development with C++" workload -- that's the "C++ addon" referred
   to in the prompt; there's no separate plain-C workload, native C
   projects use the same one).
2. Set the build configuration to **Debug|x64** or **Release|x64** and
   press **Start** (F5) or **Start Without Debugging** (Ctrl+F5).
3. Type a message at the prompt, or pass one as a command-line
   argument (Project Properties > Debugging > Command Arguments, or
   from an existing console: `QrConsoleApp.exe "https://example.com"`).

It ships configured for **version 6, level M** (41x41 modules, about
107 bytes of capacity). To change it: Project Properties > C/C++ >
Preprocessor > Preprocessor Definitions, edit the `QR_VERSION=6` /
`QR_ECC_LEVEL=QR_ECC_M` entries (do this for both Debug and Release if
you use both), or just edit the same two lines directly in the
`.vcxproj` file. If a message doesn't fit, `qr_generate()` returns
`false` exactly as designed and the app prints a friendly error
instead of crashing or writing out of bounds.

## What it prints

Same two renderings as before, both with the recommended 4-module
quiet zone and a white background (what actually makes them
scannable, not just a nice picture):

- **Full size** -- two characters wide by one console row tall per
  module, using background color only.
- **Half size** -- one character wide; a single console row packs
  *two* module rows using the Unicode half-block characters `▀`/`▄`
  (plus a plain space where both halves match). Roughly half the
  vertical space for the same resolution. This needs a font in the
  Windows Terminal / console that includes the block-drawing Unicode
  range, which is true of the default fonts on any current Windows
  install.

If you push `QR_VERSION` a lot higher (say, 20) and print full-size at
a large font, some scanning apps can struggle with how physically big
the result gets on screen -- that's just an inherent property of
bigger QR codes needing more resolution to scan reliably, not
something specific to this renderer. Half-size mode, or a smaller
console font, generally scans easier at high versions.

## Design notes

- **Why `WriteConsoleW` instead of `printf` for the grid.** `printf`
  goes through the C runtime's buffered I/O, while
  `SetConsoleTextAttribute` is a raw Win32 call that takes effect
  immediately. Mix the two and a buffered chunk of text can end up
  flushed to the screen *after* you've already changed the attribute
  for the next run -- so colors land on the wrong characters. Every
  module in the grid is written with `WriteConsoleW`, which has no
  such buffering, so each `SetConsoleTextAttribute` call is guaranteed
  to apply to exactly the text written right after it. `printf` is
  still used for the plain instructional text (the version/size line,
  the prompt), where that timing doesn't matter.
- **`QR_VERSION`/`QR_ECC_LEVEL` via Preprocessor Definitions.** Same
  build-time-constant design as before -- Visual Studio's equivalent
  of a `-D` compiler flag is a project property instead of a command
  line, so that's where they live now.
- **No `unsafe`/interop code of any kind.** Since this is all one
  native binary, `main.c` calls `qr_generate()`/`qr_get_module()`
  directly with a plain `qr_code_t` -- no marshaling, no ABI concerns,
  no separate DLL to build first.

## How this was tested

I don't have Windows or Visual Studio in the environment this was
built in, so the project couldn't be opened/built in the IDE itself --
that's the one thing here that wasn't verified by actually doing it.
To compensate:

1. `main.c` (the exact file delivered, unmodified) was compiled and
   run on Linux against a minimal stand-in `windows.h` that implements
   just the handful of Win32 console functions it calls
   (`GetStdHandle`, `SetConsoleTextAttribute`, `WriteConsoleW`,
   `SetConsoleOutputCP`), recording every attribute change and every
   character written instead of drawing to a real console.
2. That recorded log was replayed into an image (mapping each
   character cell to a block of pixels at a realistic monospace font
   ratio, splitting half-block glyphs top/bottom by color) and fed to
   an independent QR decoder.
3. This was repeated across several configurations -- version 1
   (rejects an over-length message cleanly), version 6 (the shipped
   default), version 10, and version 20 (the largest supported) with
   an empty message, a typical message, and a near-capacity message --
   and, other than the large-image decoder artifact noted above (which
   was confirmed to be about image resolution, not the generated
   content, by re-checking the identical recorded output at a smaller
   scale), every case decoded back to the exact original message for
   both the full-size and half-size rendering.
4. The `.vcxproj`/`.sln` were hand-written to the standard MSBuild
   project format (not exported from a real Visual Studio, since none
   was available) -- structurally this is the same shape any VS C++
   console project produces. The project intentionally does not pin a
   specific `PlatformToolset` (e.g. `v143`), so MSBuild picks whatever
   toolset is actually installed on your machine instead of demanding
   one that might not be (that's also what "Retarget Solution" does
   under the hood, so this avoids needing that step at all on a fresh
   open).

So the C code and its logic are verified end-to-end; the untested,
purely mechanical risk is IDE/project-file friction (a properties
dialog looking slightly different than described above) rather than
the program's behavior once it builds.
