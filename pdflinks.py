#!/usr/bin/env python3
"""
pdflinks.py

Make a manual's cross-references work in a browser: rewrite `/GoToR` and `/Launch`
link actions to `/URI`, and leave everything else exactly as it is.

    python pdflinks.py <file-or-dir>             # report only
    python pdflinks.py <file-or-dir> --apply     # rewrite in place

These manuals cross-reference each other constantly — "(See page IN-27)" — and each
of those is a `/GoToR` action: *go to a remote file*, naming a sibling by a relative
filename with a destination of `[0 /FitH 845]`.

    /A << /S /GoToR  /F << /Type /Filespec /F (m_in_0027.pdf) >>  /D [0 /FitH 845] >>

**No browser follows `/GoToR`.** Chrome's PDFium ignores it and Firefox's pdf.js does
not follow it to local files, both deliberately: an action that sends the viewer to an
arbitrary path on the reader's disk is an attack surface. Desktop readers do honour it,
so those links are not broken — they are addressed to a viewer these files are not read
in. They are rewritten to the one action a browser does follow:

    /A << /S /URI  /URI (m_in_0027.pdf#page=1) >>

The URI stays **relative**, so it resolves against wherever the tree is served from and
the files stay portable. `/GoToR` page numbers are 0-based and `#page=` is 1-based, hence
the +1. What is given up is `/FitH 845`, an exact scroll offset a URL fragment cannot
express — the reader lands at the top of the right page instead — and strict `/GoToR`
support in desktop readers, which is an accepted trade.

WHAT IS LEFT ALONE, and why that makes this safe to re-run:
  * internal `/GoTo` — already works in every browser, so there is nothing to fix
  * an existing `/URI` — already the target form
  * any link whose target cannot be found beside the linking file (below)

WHY THERE IS NO SEARCH FOR A TARGET THAT IS NOT A SIBLING. An earlier tool walked up
the tree looking for a same-named file and accepted it when exactly one candidate
survived a trailing-path-segment test. Measured on the master collection before writing
this — 1,200 files sampled out of 347,821, of which 31 carry these links, 370 links in
total:

    resolved as a sibling                    273  (74%)
    resolved as a given relative path          2
    walking up: resolved, same model           0
    walking up: resolved, ANOTHER model        5
    walking up: ambiguous or nowhere          92

The walk added **nothing correct and five links into a different car's manual.** On one
brand's tree alone it produced 767 unique hits of which all 767 were cross-model: a 2001
Prius wiring diagram's `../../../../ewdsourc/2001/01priuse/electric/parts.pdf` resolving
into the 2000 Land Cruiser EWD. The cause is that section names repeat across the whole
collection — under one brand `gi.pdf` is 436 files, `ec.pdf` 396, `fwd.pdf` 422 — so only
0.1% of files have a same-named rival in their own folder while 88.8% have one a single
level up. A quoted "96% of non-sibling links resolve to a unique file" is a UNIQUENESS
rate, not a correctness rate; uniqueness is not evidence, and here it was mostly wrong.

No audit can recover from that either: a wrong-car link points at a file that really
exists, so nothing downstream can tell it from a right one. Hence the rule — a target is
either beside the linking file (or at the relative path the link itself gives) or the
link is left as `/GoToR` and counted. A link that quietly does nothing beats a link that
confidently opens the wrong car.

THE NAME A LINK CARRIES IS NOT TRUSTED FOR CASE. These files were authored on Windows,
where `FWD.pdf` opens `fwd.pdf`. Windows `os.path.exists` says yes and would put that
spelling straight into the URL, which then 404s on a case-sensitive server — turning an
inert link into a visible error, the one outcome this exists to avoid. Every path segment
is matched against the real directory listing and the on-disk spelling is what goes in.

LINKS LIVE IN TWO PLACES and both are rewritten: the page's `/Annots` and the `/Outlines`
tree, which is the sidebar menu. They are separate objects even when they say the same
thing, so fixing only the annotations leaves a contents page whose body works and whose
menu beside it is dead.

MOST DESTINATIONS ARE NAMES, not page numbers: `/D (E.B0010439)`, a symbolic anchor the
authoring tool wrote. A URL fragment cannot carry a name, so the target is opened once,
its name tree read, and the name reduced to the page it points at. Without that the
reader lands on page 1 of a 400-page section instead of on the procedure they clicked.
"""
import os
import re
import sys
import urllib.parse
from collections import namedtuple

try:
    import pikepdf
except ImportError:                                     # pragma: no cover
    pikepdf = None

# converted : /GoToR|/Launch actions rewritten to /URI
# named     : of those, ones whose symbolic destination was resolved to a real page
# clamped   : page numbers that pointed past the end of the target and were pulled back
# outline   : of `converted`, how many were bookmark (sidebar menu) entries
# unresolved: left as /GoToR because the target is not beside the linking file
# noname    : left alone because the action names no target file at all
LinkFix = namedtuple('LinkFix', 'converted named clamped outline unresolved noname')
_ZERO = LinkFix(0, 0, 0, 0, 0, 0)


class Destinations:
    """Page number for a named destination inside another file, and that file's length.

    The target is opened once, its name tree read, and every destination reduced to a page.
    Files are cached, so a folder whose 60 sections all cross-reference each other opens
    each target once. A target that cannot be opened, or does not hold the name, gives None
    and the link is written with no fragment — which is where it would have landed anyway.
    """

    def __init__(self):
        self._maps = {}
        self._counts = {}

    @staticmethod
    def _page_of(dest, page_index):
        # A destination is [page /Fit ...]; the dict form wraps it in /D. Tested on the TYPE
        # and not on `hasattr(dest, 'get')`: a pikepdf Array answers True to that, and
        # `'/D' in <Array>` then raises TypeError out of here and into the caller's
        # except-Exception, which threw away the WHOLE file's name tree — every symbolic
        # destination in it silently unresolved, and every link written with no #page at all.
        if isinstance(dest, pikepdf.Dictionary) and '/D' in dest:
            dest = dest['/D']
        try:
            first = dest[0]
        except (TypeError, IndexError, KeyError):
            return None
        try:
            return int(first) + 1
        except (TypeError, ValueError):
            pass
        try:
            return page_index.get(first.objgen)
        except AttributeError:
            return None

    def _map_for(self, path):
        key = os.path.normcase(os.path.abspath(path))
        if key not in self._maps:
            out = {}
            try:
                with pikepdf.open(str(path)) as pdf:
                    index = {p.obj.objgen: i + 1 for i, p in enumerate(pdf.pages)}
                    self._counts[key] = len(index)
                    root = pdf.Root
                    trees = []
                    # /Names /Dests is the modern name tree; /Dests is the PDF 1.1 dict.
                    # Both occur in this collection, so both are read.
                    if '/Names' in root and '/Dests' in root.Names:
                        trees.append(pikepdf.NameTree(root.Names.Dests))
                    if '/Dests' in root:
                        trees.append(root.Dests)
                    for tree in trees:
                        for name, dest in tree.items():
                            try:
                                page = self._page_of(dest, index)
                            except Exception:
                                # one malformed entry loses one name, not every
                                # name in the file -- the failure mode the old
                                # `'/D' in <Array>` test had
                                continue
                            if page:
                                out[str(name).lstrip('/')] = page
            except Exception:
                out = {}
                self._counts.setdefault(key, None)
            self._maps[key] = out
        return self._maps[key]

    def page(self, path, name):
        return self._map_for(path).get(str(name).lstrip('/'))

    def count(self, path):
        """How many pages the target has, or None if it cannot be opened.

        Used to keep a page number inside its file. A `/D [6 ...]` — a 0-based index one
        past the end of a 6-page foldout — was wrong in the source before anything was
        converted; passed through as `#page=7` Chrome gives up on the fragment and opens
        page 1, the wrong end of the document, while clamped to the last page it lands
        where the author meant. Shares the name tree's cache, so this is free on a target
        already opened."""
        key = os.path.normcase(os.path.abspath(path))
        if key not in self._counts:
            self._map_for(path)         # records the count on its way through
        return self._counts.get(key)


def target_of(action):
    """(filename, page number 1-based or None, destination name or None).

    A `/GoToR` or `/Launch` action reduced to where it wants to go. `/Launch` carries no
    destination, so it yields a filename and nothing else."""
    spec = action.get('/F')
    if spec is None:
        # PDF 1.7 12.6.4.5 also allows /Launch << /Win << /F (...) >> >>. Handled because
        # it is cheap and unambiguous; not seen in the sampled files.
        win = action.get('/Win')
        spec = win.get('/F') if hasattr(win, 'get') else None
    if spec is None:
        return None, None, None
    if isinstance(spec, pikepdf.String) or not hasattr(spec, 'get'):
        name = str(spec)
    else:
        # /UF is the Unicode form and wins over /F when both are present
        name = next((str(spec[k]) for k in ('/UF', '/F') if k in spec), '')
    if not name:
        return None, None, None
    name = name.replace('\\', '/')

    page = dest_name = None
    dest = action.get('/D')
    if isinstance(dest, (pikepdf.String, pikepdf.Name)):
        dest_name = str(dest)           # a name into the target's own destination tree
    elif dest is not None:
        try:
            page = int(dest[0]) + 1     # a remote destination's page is a 0-based index
        except (TypeError, ValueError, IndexError, AttributeError, KeyError):
            page = None
    return name, page, dest_name


def to_uri(rel, page=None):
    """Relative URL for the target, with the page fragment when we know it."""
    url = '/'.join(urllib.parse.quote(part) for part in rel.split('/'))
    return url + ('#page=%d' % page if page else '')


def by_section_code(entries, part):
    """`GI.pdf` -> `gi-general_information.pdf`, when that is unambiguous.

    The newest sets renamed every section from its bare code to `<code>-<description>.pdf`
    but left their LINKS naming the old short form: one 2016 manual holds 10,691 references
    to `GI.pdf`, `EM.pdf`, `DAS.pdf` in a folder containing none of those names, which left
    its whole cross-reference system dead.

    Accepted only when exactly one file carries the code AND the code is the whole stem
    before the separator — not a prefix of a longer one, or `EC.pdf` would match `ECU.pdf`.
    Checked on that folder: 62 files, 62 distinct codes, no collisions."""
    stem, dot, ext = part.rpartition('.')
    if not dot or not stem:
        return None
    hits = [e for e in entries
            if e.lower().endswith('.' + ext.lower())
            and re.match(r'^%s[-_]' % re.escape(stem.lower()), e.lower())]
    if len(hits) == 1:
        return hits[0]
    # The other direction. Mitsubishi CD manuals link `GR00005000-52B.pdf` -- a document
    # id and the group code -- in folders whose files were renamed to the bare code,
    # `52B.pdf`: 1,558 dead links in the 2004 Outlander alone (measured 2026-09-14 over
    # the whole archive). Only the exact bare code is accepted. A DIFFERENT document id
    # carrying the same code (`GR00005200-52B.pdf` where only `GR00001300-52B.pdf`
    # exists, 788 links across the Evo X and Lancer sets) is left alone: the name cannot
    # say whether it is the same document, and a wrong link that exists is uncatchable.
    m = re.match(r'^gr\d+[a-z]?-(.+)$', stem.lower())
    if m:
        bare = m.group(1) + '.' + ext.lower()
        hits = [e for e in entries if e.lower() == bare]
        if len(hits) == 1:
            return hits[0]
    return None


def sibling_path(folder, name, exact=False):
    """The real on-disk relative path for `name` under `folder`, or None.

    Matched segment by segment against the actual directory listing, so the case that goes
    into the URL is the case the file has on disk. `os.path.exists` cannot do this job on
    Windows — it answers yes for `FWD.pdf` when the file is `fwd.pdf` — and the URL has to
    survive a case-sensitive server.

    `exact` is the verification mode: only a byte-identical segment counts, no
    case-insensitive fallback and no section-code aliasing. That is what proves a URL this
    module already wrote will resolve on the server, rather than merely on Windows."""
    cur = folder
    parts = [p for p in name.replace('\\', '/').split('/') if p not in ('', '.')]
    if not parts:
        return None
    out = []
    for part in parts:
        if part == '..':
            # Upward relative paths the link itself supplies are honoured — the collection
            # files loose consolidated PDFs beside the folders they reference. What is NOT
            # done is inventing a walk the link did not ask for (see the module docstring).
            cur = os.path.dirname(cur)
            out.append('..')
            continue
        try:
            entries = os.listdir(cur)
        except OSError:
            return None
        hit = next((e for e in entries if e == part), None)
        if hit is None and not exact:
            hit = next((e for e in entries if e.lower() == part.lower()), None)
            if hit is None:
                hit = by_section_code(entries, part)
        if hit is None:
            return None
        out.append(hit)
        cur = os.path.join(cur, hit)
    return '/'.join(out)


def action_holders(pdf):
    """Every object in the file that carries a link action.

    Two places, not one. Page `/Annots` are the links *on* the page; the `/Outlines` tree is
    the sidebar menu and holds its own actions. Contents pages have both, pointing at the
    same sections, so walking only the annotations fixes the page and leaves the menu beside
    it dead — 765 outline entries across 207 files in one brand. Cycles in a malformed
    outline are guarded with a seen-set."""
    for page in pdf.pages:
        for annot in (page.get('/Annots') or []):
            # an /Annots array can hold a null entry. Calling .get() on it raises, and
            # because that happens inside the walk the whole file was abandoned: every link
            # in a 1,274-page section left unconverted over one null in an array.
            if not hasattr(annot, 'get'):
                continue
            if str(annot.get('/Subtype', '')) == '/Link':
                yield annot
    if '/Outlines' not in pdf.Root:
        return
    outlines = pdf.Root.Outlines
    if not hasattr(outlines, 'get'):
        return
    seen = set()
    stack = [outlines.get('/First')]
    while stack:
        node = stack.pop()
        while node is not None:
            if not hasattr(node, 'get'):
                break
            try:
                key = node.objgen
            except AttributeError:
                break
            if key in seen:
                break
            seen.add(key)
            yield node
            kid = node.get('/First')
            if kid is not None:
                stack.append(kid)
            node = node.get('/Next')


def written_uris(pdf):
    """Every `/URI` string in the file, from both annotations and the menu.

    The verification side of the rewrite: what a reader will actually try to open."""
    out = []
    for holder in action_holders(pdf):
        action = holder.get('/A')
        if action is None or not hasattr(action, 'get'):
            continue
        if str(action.get('/S', '')) == '/URI' and '/URI' in action:
            out.append(str(action.get('/URI')))
    return out


def fix_links(pdf, folder, dests=None) -> LinkFix:
    """Rewrite this open PDF's `/GoToR` and `/Launch` actions to `/URI`. Returns counts.

    `folder` is the directory the LINKING file lives in — targets are resolved relative to
    it, so when the output goes somewhere else the URLs are still built from the source
    tree's real spelling. Nothing is written; the caller saves."""
    if dests is None:
        dests = Destinations()
    converted = named = clamped = outline = unresolved = noname = 0
    for holder in action_holders(pdf):
        action = holder.get('/A')
        if action is None or not hasattr(action, 'get'):
            continue
        if str(action.get('/S', '')) not in ('/GoToR', '/Launch'):
            continue                     # /GoTo and /URI both already work in a browser
        name, pageno, dest_name = target_of(action)
        if not name:
            noname += 1
            continue
        rel = sibling_path(folder, name)
        if rel is None:
            unresolved += 1              # left as /GoToR, deliberately (see the docstring)
            continue
        target = os.path.join(folder, rel.replace('/', os.sep))
        if pageno is None and dest_name:
            pageno = dests.page(target, dest_name)
            if pageno:
                named += 1
        if pageno:
            count = dests.count(target)
            if count and pageno > count:
                pageno = count
                clamped += 1
        holder['/A'] = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name('/Action'), S=pikepdf.Name('/URI'),
            URI=pikepdf.String(to_uri(rel, pageno))))
        converted += 1
        if '/Subtype' not in holder:     # no /Subtype -> an outline node, not an annotation
            outline += 1
    return LinkFix(converted, named, clamped, outline, unresolved, noname)


def verify_uris(path, folder) -> str:
    """'' if every `/URI` in `path` resolves, else why not.

    Re-read off disk CASE-SENSITIVELY, which is the whole point: a URL that opens on
    Windows and 404s on a Linux origin is worse than the inert link it replaced. Absolute
    and non-PDF URLs (`http:`, `mailto:`) are somebody else's business and are skipped;
    a `#page=N` past the end of its target is a defect this module must not introduce."""
    dests = Destinations()
    try:
        with pikepdf.open(str(path)) as pdf:
            uris = written_uris(pdf)
    except Exception as exc:
        return f'cannot re-open to verify links: {exc}'
    for uri in uris:
        rel, _, frag = uri.partition('#')
        if not rel or ':' in rel.split('/')[0]:
            continue                     # absolute / scheme-qualified: not ours
        rel = urllib.parse.unquote(rel)
        if not rel.lower().endswith('.pdf'):
            continue
        real = sibling_path(folder, rel, exact=True)
        if real is None:
            return f'link target does not resolve case-sensitively: {rel}'
        page = re.match(r'page=(\d+)$', frag)
        if page:
            count = dests.count(os.path.join(folder, real.replace('/', os.sep)))
            if count and int(page.group(1)) > count:
                return (f'link points past the end of its target: {rel}#page='
                        f'{page.group(1)} of {count} pages')
    return ''


def predict(path, folder=None, dests=None) -> tuple:
    """(LinkFix, error) for what `fix_links_file` WOULD do. Writes nothing.

    The rewrite happens on an in-memory object that is then dropped, so the prediction runs
    the real resolver over the real directory listings rather than a second, drifting
    estimate of it."""
    if pikepdf is None:
        return _ZERO, 'pikepdf is not installed'
    src = os.path.abspath(str(path))
    folder = os.path.dirname(src) if folder is None else str(folder)
    try:
        with pikepdf.open(src) as pdf:
            return fix_links(pdf, folder, dests), ''
    except Exception as exc:
        return _ZERO, f'cannot inspect: {exc}'


def fix_links_file(path, folder=None, out=None, dests=None) -> tuple:
    """Rewrite one file's links. Returns (LinkFix, error).

    The file is only written when something changed, and only after the result verifies:
    same page count, no link annotation lost, and every URL resolving case-sensitively on
    disk. A failure leaves the input byte-identical — pikepdf writes a temp file and
    renames, so the original is not touched until the new one is complete, and this returns
    the reason instead of shipping it.

    `out=None` rewrites in place. `folder` defaults to the file's own directory and is what
    targets resolve against, so an output written elsewhere still gets the source tree's
    real spelling."""
    if pikepdf is None:
        return _ZERO, 'pikepdf is not installed'
    src = os.path.abspath(str(path))
    folder = os.path.dirname(src) if folder is None else str(folder)
    dst = src if out is None else os.path.abspath(str(out))
    try:
        pdf = pikepdf.open(src, allow_overwriting_input=True)
    except Exception as exc:
        return _ZERO, f'cannot open: {exc}'
    try:
        try:
            before = len(pdf.pages), len(written_uris(pdf))
            stats = fix_links(pdf, folder, dests)
        except Exception as exc:
            # A malformed outline or annotation in one file must not abandon the rest of a
            # run, and must never leave a half-converted file behind: nothing is written on
            # this path, so the input stands as it was.
            return _ZERO, f'unconvertible: {exc}'
        if not stats.converted:
            if dst != src:
                import shutil
                shutil.copyfile(src, dst)
            return stats, ''
        part = dst + '.linkfix'
        try:
            pdf.save(part)
        except Exception as exc:
            _unlink(part)
            return _ZERO, f'unsaveable: {exc}'
    finally:
        pdf.close()
    try:
        with pikepdf.open(part) as chk:
            pages, uris = len(chk.pages), len(written_uris(chk))
    except Exception as exc:
        _unlink(part)
        return _ZERO, f'result will not open: {exc}'
    if pages != before[0]:
        _unlink(part)
        return _ZERO, f'result has {pages} pages, expected {before[0]}'
    # every link converted must ARRIVE as a /URI: before + converted is exactly how many
    # there should now be, so a link silently dropped by the rewrite is caught here rather
    # than showing up as a missing menu entry months later.
    if uris != before[1] + stats.converted:
        _unlink(part)
        return _ZERO, (f'result carries {uris} URI links, expected '
                       f'{before[1] + stats.converted}')
    bad = verify_uris(part, folder)
    if bad:
        _unlink(part)
        return _ZERO, bad
    try:
        os.replace(part, dst)
    except Exception as exc:
        _unlink(part)
        return _ZERO, f'cannot place result: {exc}'
    return stats, ''


def _unlink(p):
    try:
        os.unlink(p)
    except OSError:
        pass


def note_for(stats: LinkFix) -> str:
    """One report fragment, or '' when this file had no such links.

    The `/GoToR` -> `/URI` swap is a semantic change to the file, so a run must never hand
    one back without saying so."""
    if not stats or not (stats.converted or stats.unresolved):
        return ''
    if not stats.converted:
        return f' ({stats.unresolved} cross-file links left as /GoToR: target not beside the file)'
    bits = [f'{stats.converted} cross-file links -> /URI']
    if stats.outline:
        bits.append(f'{stats.outline} in the menu')
    if stats.named:
        bits.append(f'{stats.named} named dests resolved')
    if stats.clamped:
        bits.append(f'{stats.clamped} clamped')
    if stats.unresolved:
        bits.append(f'{stats.unresolved} left as /GoToR (target not beside the file)')
    return ' (' + ', '.join(bits) + ')'


def add(a: LinkFix, b: LinkFix) -> LinkFix:
    return LinkFix(*(x + y for x, y in zip(a, b)))


def _pdfs_under(path):
    if os.path.isfile(path):
        return [path]
    out = []
    for dirpath, _dirs, files in os.walk(path):
        out += [os.path.join(dirpath, f) for f in sorted(files)
                if f.lower().endswith('.pdf')]
    return out


def main(argv=None):
    # Manual filenames are not ASCII and neither is this output; on Windows the console's
    # cp1252 default mangles an em dash and raises outright on a Japanese filename.
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:                                   # pragma: no cover
        pass
    argv = sys.argv[1:] if argv is None else argv
    args = [a for a in argv if not a.startswith('--')]
    apply_it = '--apply' in argv
    if not args:
        print(__doc__)
        return 2
    if pikepdf is None:
        print('ERROR: pikepdf is required (pip install pikepdf)', file=sys.stderr)
        return 1
    files = _pdfs_under(os.path.abspath(args[0]))
    dests = Destinations()
    total, touched, failed, shown = _ZERO, 0, 0, 0
    for path in files:
        folder = os.path.dirname(path)
        if apply_it:
            stats, err = fix_links_file(path, folder, dests=dests)
        else:
            try:
                with pikepdf.open(path) as pdf:
                    stats, err = fix_links(pdf, folder, dests), ''
            except Exception as exc:
                stats, err = _ZERO, f'cannot open: {exc}'
        if err:
            failed += 1
            print(f'   {os.path.basename(path)}: {err}')
            continue
        total = add(total, stats)
        if stats.converted:
            touched += 1
            if shown < 5:
                print(f'   {os.path.basename(path):<28}{note_for(stats).strip()}')
                shown += 1
    print(f'PDFs examined        : {len(files)}')
    print(f'  with cross-file links: {touched}')
    print(f'  links -> /URI        : {total.converted}')
    print(f'    of those, menu entries      : {total.outline}')
    print(f'    named dests resolved to page: {total.named}')
    print(f'    page numbers clamped        : {total.clamped}')
    print(f'  left as /GoToR (not a sibling): {total.unresolved}')
    print(f'  actions naming no file        : {total.noname}')
    print(f'  failed                        : {failed}')
    if not apply_it:
        print('(dry run — pass --apply to write)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
