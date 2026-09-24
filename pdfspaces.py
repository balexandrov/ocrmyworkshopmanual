#!/usr/bin/env python3
"""
pdfspaces.py

Turn the boxes that some converters draw at every word gap back into spaces.

    python pdfspaces.py <file-or-dir>                  # report only
    python pdfspaces.py <file-or-dir> --apply          # rewrite in place

WHAT THE BOXES ARE. An Interleaf-era PDF converter writes text two ways. Where it emits a
kerned array the words are separated by numeric offsets and no space glyph is ever asked
for, and those pages are clean:

    [(DTC)-3621.5(22)-2436.6(Compressor)-274.8(Lock)]TJ

Where it draws each word separately it puts a literal space between them and justifies
with word spacing:

    (DIAGNOSTICS)Tj 2.8162 Tw ( )Tj 0 Tw (AIR)Tj 0.2882 Tw ( )Tj 0 Tw (CONDITIONING)Tj

But it subsets its fonts at /FirstChar 37 (or 40), and the subset's cmaps stop at the codes
it kept, so code 32 maps to glyph 0 -- .notdef -- and a strict viewer draws a box at every
word gap. Measured on one repair manual's diagnostics section: page 1 uses the kerned form and
is clean, pages 2-3 use literal spaces and are covered in boxes. Over one make's whole tree
of such manuals, 4,991 of 21,524 files carried the fault (20,407 fonts).

THE FIX, three edits per affected font, no new glyph data:

  1. the descriptor's /Flags: clear Nonsymbolic (32), set Symbolic (4);
  2. drop /Encoding /WinAnsiEncoding (or /MacRomanEncoding) from the font dictionary;
  3. in the embedded TrueType, set the (1,0) format-0 cmap's entry for code 32 to glyph 32.

Together they route the lookup through the flat 256-byte cmap instead of through Unicode,
where glyph 32 is already present and EMPTY -- so it draws nothing, which is what a space
should do. Steps 1-2 alone change nothing (measured); step 3 changes one byte and no table
length, so the font's own offsets stay valid.

WHY THE LAYOUT CANNOT MOVE. Code 32 sits outside /FirstChar..LastChar, so its PDF width is
/MissingWidth -- absent, so 0 -- before and after. All the spacing comes from the Tw word
spacing the converter already emits, which applies to byte 32 whatever glyph it draws.
Only the box goes.

WHAT IS TOUCHED. Only a font that shows the fault: a simple /TrueType with /FirstChar > 32,
an embedded /FontFile2, a (1,0) format-0 cmap, and glyph 32 present, empty and unmapped.
Anything else is left alone, so re-running is a no-op. Fonts are found in page resources and
in the resources of the Form XObjects pages draw, however deep.

WHAT IT MEANS FOR TEXT EXTRACTION, measured on the files it repaired: a whole-page text
extraction still reads those gaps as spaces, but a clipped one (PyMuPDF's get_textbox) now
returns U+FFFD for them. Anything that parses text inside a rectangle must treat U+FFFD as
a gap -- one consumer read "DI-1<U+FFFD>7" as DI-1 until it did.
"""
import os
import struct
import sys

import pikepdf

SYMBOLIC = 4
NONSYMBOLIC = 32
SPACE = 32


# ---------------------------------------------------------------- the embedded TrueType
def _tables(data):
    """tag -> (offset, length) from the TrueType table directory."""
    out = {}
    count = struct.unpack(">H", data[4:6])[0]
    for i in range(count):
        off = 12 + 16 * i
        tag = data[off:off + 4].decode("latin-1", "replace")
        o, n = struct.unpack(">II", data[off + 8:off + 16])
        out[tag] = (o, n)
    return out


def _num_glyphs(data, tabs):
    o, _ = tabs["maxp"]
    return struct.unpack(">H", data[o + 4:o + 6])[0]


def _glyph_is_empty(data, tabs, gid):
    """True when glyph `gid` exists and has no outline, i.e. draws nothing."""
    ho, _ = tabs["head"]
    long_loca = struct.unpack(">h", data[ho + 50:ho + 52])[0]
    lo, _ = tabs["loca"]
    if gid + 1 > _num_glyphs(data, tabs):
        return False

    def at(i):
        if long_loca == 0:
            return struct.unpack(">H", data[lo + i * 2:lo + i * 2 + 2])[0] * 2
        return struct.unpack(">I", data[lo + i * 4:lo + i * 4 + 4])[0]

    try:
        return at(gid) == at(gid + 1)
    except struct.error:
        return False


def _flat_cmap_offset(data, tabs):
    """Absolute offset of the (1,0) format-0 glyph array, or None."""
    if "cmap" not in tabs:
        return None
    co, _ = tabs["cmap"]
    n = struct.unpack(">H", data[co + 2:co + 4])[0]
    for i in range(n):
        pid, eid, off = struct.unpack(">HHI", data[co + 4 + 8 * i:co + 12 + 8 * i])
        so = co + off
        if (pid, eid) == (1, 0) and struct.unpack(">H", data[so:so + 2])[0] == 0:
            return so + 6
    return None


def _fault(font):
    """(fontfile stream, byte offset of cmap[32]) when this font shows the fault, else None."""
    try:
        if font.get("/Subtype") != "/TrueType":
            return None
        first = font.get("/FirstChar")
        if first is None or int(first) <= SPACE:
            return None
        fd = font.get("/FontDescriptor")
        ff = fd.get("/FontFile2") if fd is not None else None
        if ff is None:
            return None
        data = ff.read_bytes()
        tabs = _tables(data)
        if not {"cmap", "loca", "maxp", "head"} <= set(tabs):
            return None
        pos = _flat_cmap_offset(data, tabs)
        if pos is None or data[pos + SPACE] != 0 or not _glyph_is_empty(data, tabs, SPACE):
            return None
        return ff, pos + SPACE
    except (struct.error, KeyError, ValueError, TypeError, pikepdf.PdfError):
        return None             # a font we cannot read is a font we do not touch


def repair_font(font):
    """Apply the three edits to one font -> a note of what changed, or None if it had no fault."""
    got = _fault(font)
    if got is None:
        return None
    ff, at = got
    data = bytearray(ff.read_bytes())
    data[at] = SPACE
    ff.write(bytes(data))
    changed = ["cmap(1,0)[32]=32"]
    fd = font["/FontDescriptor"]
    flags = int(fd.get("/Flags", 0))
    if flags & NONSYMBOLIC:
        fd["/Flags"] = (flags & ~NONSYMBOLIC) | SYMBOLIC
        changed.append("Flags %d->%d" % (flags, int(fd["/Flags"])))
    if "/Encoding" in font and str(font["/Encoding"]) in ("/WinAnsiEncoding", "/MacRomanEncoding"):
        changed.append("drop %s" % str(font["/Encoding"]))
        del font["/Encoding"]
    return "%s: %s" % (str(font.get("/BaseFont", "?")), ", ".join(changed))


def fonts_of(pdf):
    """Every distinct font dictionary a page can draw with: page resources, and the resources
    of Form XObjects reached from them, however deep."""
    seen_fonts, seen_res, out = set(), set(), []

    def walk(res):
        if res is None:
            return
        key = getattr(res, "objgen", None)
        if key and key != (0, 0):
            if key in seen_res:
                return
            seen_res.add(key)
        for _n, font in (res.get("/Font") or {}).items():
            fk = getattr(font, "objgen", None) or id(font)
            if fk not in seen_fonts:
                seen_fonts.add(fk)
                out.append(font)
        for _n, xo in (res.get("/XObject") or {}).items():
            if isinstance(xo, pikepdf.Stream) and xo.get("/Subtype") == "/Form":
                walk(xo.get("/Resources"))

    for page in pdf.pages:
        walk(page.obj.get("/Resources"))
    return out


def detect(pdf):
    """Fonts in an open pdf that show the fault (nothing is changed)."""
    return [f for f in fonts_of(pdf) if _fault(f) is not None]


# ---------------------------------------------------------------- a file
def _fingerprint(pdf):
    """What the repair must leave exactly as it was: page count and every page's content."""
    out = []
    for page in pdf.pages:
        c = page.obj.get("/Contents")
        parts = c if isinstance(c, pikepdf.Array) else ([c] if c is not None else [])
        out.append(b"".join(p.read_bytes() for p in parts))
    return out


def clean_file(src, out):
    """Repair src into out. -> dict(found=n faulty fonts, fonts=[notes], err=str|None).

    `out` is written only when something was repaired AND the self-audit passes: the result is
    reopened and must have the same page count, byte-identical page content streams, the same
    number of fonts and none still faulty. A failed audit is reported, never shipped."""
    res = {"found": 0, "fonts": [], "err": None}
    with pikepdf.open(str(src)) as pdf:
        before = _fingerprint(pdf)
        nfonts = len(fonts_of(pdf))
        for font in fonts_of(pdf):
            note = repair_font(font)
            if note:
                res["fonts"].append(note)
        res["found"] = len(res["fonts"])
        if not res["found"]:
            return res
        pdf.save(str(out))
    with pikepdf.open(str(out)) as chk:
        if len(chk.pages) != len(before):
            res["err"] = f"space fix failed: page count {len(before)} -> {len(chk.pages)}"
        elif _fingerprint(chk) != before:
            res["err"] = "space fix failed: a page's content stream changed"
        elif len(fonts_of(chk)) != nfonts:
            res["err"] = "space fix failed: font count changed"
        elif detect(chk):
            res["err"] = "space fix failed: fonts still faulty after the rewrite"
    if res["err"]:
        try:
            os.remove(str(out))
        except OSError:
            pass
    return res


def note_for(res):
    """The report note for a clean_file result, '' when nothing applied."""
    if not res or not res.get("found"):
        return ""
    if res.get("err"):
        return f" (box spaces found in {res['found']} font(s), left: {res['err']})"
    return f" (box spaces fixed: {res['found']} font(s))"


def _pdfs_under(path):
    if os.path.isfile(path):
        return [path]
    return [os.path.join(d, f) for d, _s, fs in os.walk(path) for f in sorted(fs)
            if f.lower().endswith(".pdf")]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    apply_it = "--apply" in argv
    touched = fonts = failed = 0
    files = _pdfs_under(args[0])
    for path in files:
        try:
            if apply_it:
                tmp = path + ".spaces.part"
                res = clean_file(path, tmp)
                if res["found"] and not res["err"]:
                    os.replace(tmp, path)
            else:
                with pikepdf.open(path) as pdf:
                    res = {"found": len(detect(pdf)), "fonts": [], "err": None}
        except Exception as ex:
            failed += 1
            print(f"   unreadable: {os.path.basename(path)} -- {ex}")
            continue
        if res["found"]:
            touched += 1
            fonts += res["found"]
            if res["err"]:
                failed += 1
                print(f"   {os.path.basename(path)}: {res['err']}")
    print(f"PDFs examined      : {len(files)}")
    print(f"  with the fault   : {touched}")
    print(f"  fonts {'repaired' if apply_it else 'faulty'}   : {fonts}")
    print(f"  failed           : {failed}")
    if not apply_it:
        print("(dry run -- pass --apply to write)")


if __name__ == "__main__":
    main()
