# QR code console demo for Visual Studio

It's a plain C console app -- `qrcode.c`/`qrcode.h` are compiled 
directly into the same project, so there's no P/Invoke, no separate 
native DLL, and no interop shim file needed anymore. One project, 
open it in Visual Studio, hit run.

## Layout

```
QrConsoleApp.sln
QrConsoleApp/
  QrConsoleApp.vcxproj
  QrConsoleApp.vcxproj.filters
  main.c        <- console demo usage. Prompts for a message, calls qr_generate(), renders it
  qrcode.c      <- unmodified
  qrcode.h      <- unmodified
```

`qrcode.c` and `qrcode.h` are intended for embedded 8-bit MCU.
`main.c` is a console demo, rendering half blocks and full blocks.

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
  *two* module rows using the Unicode half- or full-block characters
  `▀`/`▄`/`█` (plus a plain space where both halves match). Roughly
  half the vertical space for the same resolution. This needs a font
  in the Windows Terminal / console that includes the block-drawing
  Unicode range, which is true of the default fonts on any current
  Windows install.

If you push `QR_VERSION` a lot higher (say, 20) and print full-size at
a large font, some scanning apps can struggle with how physically big
the result gets on screen -- that's just an inherent property of
bigger QR codes needing more resolution to scan reliably, not
something specific to this renderer. Half-size mode, or a smaller
console font, generally scans easier at high versions.
