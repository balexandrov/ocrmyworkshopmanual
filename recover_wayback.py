#!/usr/bin/env python3
"""Recover the broken .pdf downloads (saved Google-search HTML pages) by pulling
the originals from the Wayback Machine. Reads reports/junk_recovery_urls.csv,
saves verified PDFs to ./recovered/<group>/ (override with argv[1]), writes
reports/junk_recovery_results.csv.

Robust vs the first attempt: tries www/non-www + query-stripped URL variants,
prefers captures whose mimetype is application/pdf (picks the largest = most
complete), verifies the %PDF- header, and backs off on archive.org throttling.
Never touches M:\\ originals.
"""
import csv, json, os, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
IN = os.path.join(HERE, "reports", "junk_recovery_urls.csv")
OUT = os.path.join(HERE, "reports", "junk_recovery_results.csv")
DEST = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "recovered")
UA = "Mozilla/5.0 (recovery-bot; archive fetch)"


def get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def url_variants(u):
    """Candidate URLs to look up in Wayback: the original, a query-stripped form,
    and www/non-www toggles."""
    out = []
    def add(x):
        if x and x not in out:
            out.append(x)
    add(u)
    base = u.split("?", 1)[0]
    add(base)
    for v in (u, base):
        p = urllib.parse.urlsplit(v)
        if p.netloc.startswith("www."):
            add(urllib.parse.urlunsplit((p.scheme, p.netloc[4:], p.path, p.query, "")))
        else:
            add(urllib.parse.urlunsplit((p.scheme, "www." + p.netloc, p.path, p.query, "")))
    return out


def cdx_lookup(u):
    """Return list of (timestamp, original, mimetype, length) rows, 200-only."""
    q = ("http://web.archive.org/cdx/search/cdx?url=" + urllib.parse.quote(u, safe="")
         + "&output=json&filter=statuscode:200&collapse=digest&limit=60")
    for attempt in range(4):
        try:
            data = get(q, timeout=60)
            rows = json.loads(data.decode("utf-8", "replace")) if data.strip() else []
            if not rows:
                return []
            hdr = rows[0]
            idx = {name: hdr.index(name) for name in
                   ("timestamp", "original", "mimetype", "length") if name in hdr}
            res = []
            for row in rows[1:]:
                res.append((row[idx["timestamp"]], row[idx["original"]],
                            row[idx.get("mimetype", 0)],
                            int(row[idx["length"]]) if row[idx.get("length", -1)].isdigit() else 0))
            return res
        except Exception as e:
            if "429" in str(e) or "throttl" in str(e).lower():
                time.sleep(10 * (attempt + 1))
                continue
            return []
    return []


def pick(rows):
    """Prefer application/pdf captures (largest first); else largest of any."""
    pdfs = [r for r in rows if r[2] == "application/pdf"]
    pool = pdfs or rows
    return max(pool, key=lambda r: r[3]) if pool else None


def fetch_pdf(timestamp, original):
    """Download raw archived bytes; return bytes if it starts with %PDF-, else None.
    Backs off on throttle interstitials."""
    wb = f"http://web.archive.org/web/{timestamp}id_/{original}"
    for attempt in range(4):
        try:
            b = get(wb, timeout=150)
        except Exception as e:
            if "429" in str(e):
                time.sleep(12 * (attempt + 1)); continue
            return None
        if b[:5] == b"%PDF-":
            return b
        # throttle interstitial or wrong capture -> back off and retry
        if b"too many requests" in b[:2000].lower() or b"<html" in b[:200].lower():
            time.sleep(12 * (attempt + 1)); continue
        return None
    return None


def main():
    rows = list(csv.DictReader(open(IN, encoding="utf-8")))
    results = []
    recovered = 0
    total_bytes = 0
    todo = [r for r in rows if r["recoverable"] == "yes"]
    print(f"{len(todo)} recoverable of {len(rows)} total", flush=True)
    for i, r in enumerate(rows):
        if r["recoverable"] != "yes":
            results.append([r["junk_path"], r["target_url"], r["group"], "skipped", "", "", 0])
            continue
        tgt = r["target_url"]
        group = r["group"]
        base = os.path.basename(urllib.parse.urlsplit(tgt).path) or "recovered.pdf"
        status, source, saved, nbytes = "not_on_wayback", "", "", 0
        best = None
        for cand in url_variants(tgt):
            got = cdx_lookup(cand)
            time.sleep(1.5)               # polite pacing
            p = pick(got)
            if p and (best is None or p[3] > best[3]):
                best = p
            if p and p[2] == "application/pdf":
                break
        if best:
            b = fetch_pdf(best[0], best[1])
            if b:
                d = os.path.join(DEST, group)
                os.makedirs(d, exist_ok=True)
                out = os.path.join(d, base)
                n = 2
                while os.path.exists(out):
                    stem, ext = os.path.splitext(base)
                    out = os.path.join(d, f"{stem}_{n}{ext}"); n += 1
                with open(out, "wb") as fh:
                    fh.write(b)
                status, source, saved, nbytes = "recovered", best[0], out, len(b)
                recovered += 1; total_bytes += len(b)
            else:
                status, source = "wayback_not_pdf", best[0]
        results.append([r["junk_path"], tgt, group, status, source, saved, nbytes])
        print(f"  [{i+1}/{len(rows)}] {status:16} {base[:45]}", flush=True)

    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["junk_path", "target_url", "group", "status", "source", "saved_to", "bytes"])
        w.writerows(results)
    from collections import Counter
    print("\nDONE. recovered", recovered, "files,",
          round(total_bytes / 1048576, 1), "MB")
    print(Counter(x[3] for x in results))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
