#!/usr/bin/env python3
"""Drive an in-place compress+OCR run over the TOP-LEVEL candidate folders from
reports/scan_candidates.csv, biggest first, RECURSIVELY (so a root also covers its
nested candidate subfolders — each subtree is processed once).

Language is left to the tool: `--language auto` detects each file's script from the
image (Tesseract OSD) and picks eng / rus+eng per file. No per-folder guessing here.

Reads the candidate list produced by scan_candidates.py (reports/scan_candidates.csv).
Resumable: roots already in reports/batch_done.txt are skipped.
Progress -> reports/batch_progress.csv (flushed per root).

  python batch_compress.py [--skip FOLDER ...] [--tessdata DIR]

--skip     a candidate folder to leave alone (repeatable), e.g. one already done
--tessdata TESSDATA_PREFIX for OCR language packs (else the current env / system default)
"""
import argparse, csv, os, re, subprocess, sys, time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument('--skip', action='append', default=[], help='candidate folder to skip (repeatable)')
ap.add_argument('--tessdata', help='TESSDATA_PREFIX dir for OCR language packs')
args = ap.parse_args()

REPO = Path(__file__).resolve().parent
PY = sys.executable
TOOL = str(REPO / "ocrmyworkshopmanual.py")
CAND = REPO / "reports" / "scan_candidates.csv"
PROG = REPO / "reports" / "batch_progress.csv"
DONE = REPO / "reports" / "batch_done.txt"
SKIP = set(args.skip)

if args.tessdata:
    os.environ["TESSDATA_PREFIX"] = args.tessdata

def fnum(r, k):
    try: return float(r[k])
    except Exception: return 0.0

rows = {r['folder']: r for r in csv.DictReader(open(CAND, encoding='utf-8'))}
cands = list(rows)
cset = set(cands)
# common parent of all candidate folders — only for trimming paths in the console log
COMMON = os.path.dirname(os.path.commonpath(cands)) if cands else ""

def has_ancestor(c):
    p = os.path.dirname(c)
    while True:
        if p in cset:
            return True
        parent = os.path.dirname(p)
        if parent == p:          # reached a drive/filesystem root
            return False
        p = parent

roots = [c for c in cands if not has_ancestor(c)]
roots.sort(key=lambda c: fnum(rows[c], 'compressible_MB'), reverse=True)

done = set()
if DONE.exists():
    done = set(l.strip() for l in DONE.open(encoding='utf-8') if l.strip())

new_prog = not PROG.exists()
prog = PROG.open('a', newline='', encoding='utf-8')
pw = csv.writer(prog)
if new_prog:
    pw.writerow(['n', 'root', 'orig_MB', 'new_MB', 'saved_MB', 'pct',
                 'processed', 'failed', 'status', 'seconds'])
    prog.flush()

SUM = re.compile(r'Total:\s*([\d.]+)\s*MB\s*->\s*([\d.]+)\s*MB\s*\(([\d.]+)%')
PROC = re.compile(r'processed\s+(\d+)\b.*?failed\s+(\d+)', re.S)

todo = [r for r in roots if r not in SKIP and r not in done]
print(f"{len(roots)} top-level roots | {len(done)} done | {len(todo)} to run "
      f"(collapsed {len(cands)-len(roots)} nested)", flush=True)
tot_orig = tot_new = 0.0
t_all = time.time()
for i, root in enumerate(roots, 1):
    if root in SKIP or root in done:
        continue
    t0 = time.time()
    try:
        r = subprocess.run(
            [PY, TOOL, root, "--in-place", "--no-log", "--language", "auto"],
            capture_output=True, timeout=None)
        out = r.stdout.decode('utf-8', 'replace') + r.stderr.decode('utf-8', 'replace')
        m, p = SUM.search(out), PROC.search(out)
        orig = float(m.group(1)) if m else 0.0
        new = float(m.group(2)) if m else 0.0
        pct = m.group(3) if m else ''
        proc = p.group(1) if p else '?'
        failed = p.group(2) if p else '?'
        status = 'ok' if r.returncode == 0 else f'exit{r.returncode}'
        tot_orig += orig; tot_new += new
    except Exception as e:
        orig = new = 0.0; pct = ''; proc = failed = '?'; status = f'ERROR:{str(e)[:40]}'
    secs = round(time.time() - t0)
    pw.writerow([i, root, round(orig, 1), round(new, 1), round(orig - new, 1),
                 pct, proc, failed, status, secs])
    prog.flush()
    with DONE.open('a', encoding='utf-8') as d:
        d.write(root + '\n')
    rel = root[len(COMMON):].lstrip("\\/") if COMMON and root.startswith(COMMON) else root
    print(f"[{i}/{len(roots)}] {status:6} {orig:7.0f}->{new:7.0f}MB "
          f"({pct}%) f={failed} {secs}s | cum saved {(tot_orig-tot_new)/1024:.2f} GB | {rel[:66]}",
          flush=True)

prog.close()
print(f"\nBATCH DONE in {(time.time()-t_all)/3600:.2f} h. "
      f"cumulative {tot_orig/1024:.1f} GB -> {tot_new/1024:.1f} GB, "
      f"saved {(tot_orig-tot_new)/1024:.1f} GB")
