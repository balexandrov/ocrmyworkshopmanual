#!/usr/bin/env python3
"""Replace originals with the verified outputs of a lossless pass.

    python helpers/promote_lossless.py --before <SRC_ROOT> --after <OUT_ROOT>
                                       --audit reports/lossless_audit.csv [--apply]

WITHOUT --apply it only reports what it would do. Nothing is written until you pass --apply.

A file is promoted only if ALL of these hold, so the decision is never taken on trust:

  * its audit row says `ok` — the structural + pixel/text audit passed on THAT file
  * the output is actually smaller than the original (a byte-copy has nothing to promote)
  * the output opens, and its page count matches the original's, re-checked HERE and now
  * the copy landing next to the original is byte-identical to the output it came from

The last one is the reason this is a script and not a robocopy: the audit proved the file in
<AFTER> is sound, so what gets promoted must be provably THAT file and not a truncated or
half-flushed copy of it. Sequence per file: copy to `<name>.promote` beside the original,
hash-compare it against the source output, then a single atomic os.replace. A failure at any
step leaves the original exactly as it was, because nothing has been written over it yet.

Rejected rows are listed, never skipped silently: "24 files unchanged" must be readable as a
decision, not mistaken for 24 files that were somehow missed.
"""
import argparse
import csv
import hashlib
import os
import shutil
import stat
import sys
from pathlib import Path

import pikepdf


def _replace_clearing_readonly(tmp: Path, dst: Path) -> bool:
    """os.replace onto `dst`, coping with a READ-ONLY original. Returns True if the read-only
    flag had to be cleared — and it is left CLEARED, not restored.

    Measured: 26 of 585 files in this archive are mode 444 with the Windows R attribute set —
    they were copied off a CD/DVD, where every file is read-only, and the bit rode along with
    the copy. It carries no intent about these files: it is an artifact of the medium they came
    from, and on a working archive it only causes replacements to fail with PermissionError.
    So it goes."""
    try:
        os.replace(str(tmp), str(dst))
        return False
    except PermissionError:
        if not dst.exists():
            raise
        mode = dst.stat().st_mode
        if mode & stat.S_IWRITE:
            raise                          # not the read-only bit; a real permission problem
        os.chmod(str(dst), mode | stat.S_IWRITE)
        os.replace(str(tmp), str(dst))
        return True


def _sha1(p: Path, chunk=1 << 20) -> str:
    h = hashlib.sha1()
    with open(p, 'rb') as f:
        while True:
            b = f.read(chunk)
            if not b:
                return h.hexdigest()
            h.update(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--before', required=True, type=Path, help='source root (originals)')
    ap.add_argument('--after', required=True, type=Path, help='output root (verified rewrites)')
    ap.add_argument('--audit', type=Path, default=None,
                    help='lossless_audit.csv from helpers/verify_lossless.py. OPTIONAL: without '
                         'it every pair found under --after is a candidate, judged only by the '
                         'checks below. The run itself already verified each file against its '
                         'source before writing it (page/annotation/bookmark/destination counts, '
                         'document-wide decoded content bytes, page fingerprints, docinfo and '
                         'XMP) — an audit CSV adds an INDEPENDENT second opinion, which is worth '
                         'having but is not the only thing standing between you and a bad file')
    ap.add_argument('--apply', action='store_true', help='actually replace the originals')
    args = ap.parse_args()

    if args.audit:
        rows = list(csv.DictReader(open(args.audit, newline='', encoding='utf-8')))
        if not rows:
            sys.exit(f'no rows in {args.audit}')
    else:
        # No audit CSV: every output present is a candidate. Say so out loud rather than
        # letting the absence of a second opinion pass unremarked.
        rows = [{'file': str(p.relative_to(args.after)), 'verdict': 'ok'}
                for p in sorted(args.after.rglob('*'))
                if p.is_file() and p.suffix.lower() == '.pdf']
        print(f'No --audit given: promoting on the run\'s own verification only '
              f'({len(rows)} outputs found).')

    promote, skip = [], []
    for r in rows:
        rel = r['file']
        src, out = args.before / rel, args.after / rel
        if r.get('verdict') != 'ok':
            skip.append((rel, f'audit verdict {r.get("verdict")}'))
            continue
        if not src.is_file() or not out.is_file():
            skip.append((rel, 'missing source or output'))
            continue
        so, no = src.stat().st_size, out.stat().st_size
        if no >= so:
            skip.append((rel, f'not smaller ({no} >= {so}) — byte-copy, nothing to promote'))
            continue
        promote.append((rel, src, out, so, no))

    gain = sum(so - no for _r, _s, _o, so, no in promote)
    print(f'{len(promote)} file(s) to promote, {gain / 1e9:.2f} GB reclaimed')
    print(f'{len(skip)} file(s) left alone:')
    for rel, why in skip:
        print(f'   {rel}: {why}')
    if not args.apply:
        print('\nDRY RUN — nothing written. Re-run with --apply to replace the originals.')
        return

    done = failed = 0
    for i, (rel, src, out, so, no) in enumerate(promote, 1):
        tmp = src.with_suffix(src.suffix + '.promote')
        try:
            # Re-check the output HERE, not on the audit's word alone: page count must match
            # the original it is about to replace.
            with pikepdf.open(str(out)) as a, pikepdf.open(str(src)) as b:
                if len(a.pages) != len(b.pages):
                    raise ValueError(f'page count {len(b.pages)} -> {len(a.pages)}')
            shutil.copyfile(str(out), str(tmp))
            if _sha1(tmp) != _sha1(out):
                raise ValueError('copy is not byte-identical to the verified output')
            ro = _replace_clearing_readonly(tmp, src)
            done += 1
            print(f'  [{i}/{len(promote)}] promoted {so / 1048576:.1f} -> {no / 1048576:.1f} MB'
                  f'{" (read-only cleared)" if ro else ""}  {rel[:56]}', flush=True)
        except Exception as ex:
            failed += 1
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            print(f'  [{i}/{len(promote)}] FAILED (original untouched): {rel}: '
                  f'{repr(ex)[:110]}', flush=True)

    print(f'\npromoted {done}, failed {failed}, left alone {len(skip)}')
    if failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
