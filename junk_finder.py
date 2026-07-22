#!/usr/bin/env python3
"""Find .pdf-named files that are NOT real PDFs (HTML error pages, download
stubs, etc.) under a tree, and sniff what each one actually is so the originals
can be tracked down and re-downloaded.

Read-only. Writes reports/junk_html_pdfs.csv (path, size, kind, title, sniff).
"""
import concurrent.futures as cf
import csv
import os
import re
import sys
from pathlib import Path

ROOT = sys.argv[1] if len(sys.argv) > 1 else "."
OUT = Path(__file__).resolve().parent / 'reports' / 'junk_html_pdfs.csv'

_TITLE = re.compile(rb'<title[^>]*>(.*?)</title>', re.I | re.S)


def win_long(p):
    if os.name == 'nt':
        ap = os.path.abspath(str(p))
        return ap if ap.startswith('\\\\?\\') else '\\\\?\\' + ap
    return str(p)


def sniff(path):
    """Return (is_junk, kind, title, snippet). is_junk=False for real PDFs."""
    try:
        with open(win_long(path), 'rb') as fh:
            head = fh.read(2048)
    except Exception as ex:
        return True, 'unreadable', '', str(ex)[:200]
    if b'%PDF-' in head[:1024]:
        return False, '', '', ''
    low = head.lower()
    kind = 'unknown'
    if b'<!doctype html' in low or b'<html' in low:
        kind = 'html'
    elif low.startswith(b'{') or low.startswith(b'['):
        kind = 'json'
    elif low.startswith(b'<?xml'):
        kind = 'xml'
    elif b'http/1.' in low[:20] or b'404' in head[:64] or b'403' in head[:64]:
        kind = 'http-error'
    m = _TITLE.search(head)
    title = ''
    if m:
        try:
            title = m.group(1).decode('utf-8', 'replace')
        except Exception:
            title = ''
        title = re.sub(r'\s+', ' ', title).strip()[:200]
    try:
        snip = head.decode('utf-8', 'replace')
    except Exception:
        snip = repr(head[:200])
    snip = re.sub(r'\s+', ' ', snip).strip()[:300]
    return True, kind, title, snip


def main():
    pdfs = []
    for dp, _dn, fns in os.walk(ROOT):
        for fn in fns:
            if fn.lower().endswith('.pdf'):
                pdfs.append(os.path.join(dp, fn))
    print(f'{len(pdfs)} .pdf files to check ...', flush=True)
    junk = []
    done = 0
    with cf.ThreadPoolExecutor(max_workers=64) as ex:
        futs = {ex.submit(sniff, p): p for p in pdfs}
        for fut in cf.as_completed(futs):
            p = futs[fut]
            done += 1
            if done % 20000 == 0:
                print(f'  checked {done}/{len(pdfs)} ({len(junk)} junk so far)', flush=True)
            try:
                is_junk, kind, title, snip = fut.result()
            except Exception as ex:
                is_junk, kind, title, snip = True, 'error', '', str(ex)[:200]
            if is_junk:
                try:
                    size = os.path.getsize(win_long(p))
                except Exception:
                    size = 0
                junk.append((p, size, kind, title, snip))
    junk.sort()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open('w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(['path', 'size_bytes', 'kind', 'html_title', 'snippet'])
        w.writerows(junk)
    print(f'\nDONE: {len(junk)} junk .pdf files of {len(pdfs)} total.')
    from collections import Counter
    for k, c in Counter(j[2] for j in junk).most_common():
        print(f'  {k:12} {c}')
    print(f'Wrote {OUT}')


if __name__ == '__main__':
    main()
