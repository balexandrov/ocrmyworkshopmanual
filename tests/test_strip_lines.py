"""Caller-named lines: a printout service's running header and credit lines, which carry no
marker for `detect` to find.

THE SHAPE, measured on a 23,970-page repair manual that reached the archive as a third-party
information service's printout of the maker's pages:

  * every page opens with the service's two-line header, "<year> <make> <model>" over
    "<year> <GROUP> <article title>", each drawn in two to four show-text pieces -- split at
    a space ("... Brake (Front)" | "(Preparation) - RX") and mid-word ("Can Co" | "mmunication")
  * on some pages the whole header is drawn TWICE, over itself, on the same baselines
  * under every figure, a credit line "Courtesy of <maker's sales company>", split mid-word,
    at whatever height the figure ends
  * the manual's own text says "Courtesy Light Switch Assembly Connector", and each article's
    first page repeats "<year> <GROUP>" as a title in the body

A rule may only ever take a WHOLE displayed line. These fixtures reproduce that shape.
"""
import collections

import pikepdf
import pytest

import pdfwatermark as W
import _util as U

HEADER = [(251, 763, ['2010 Make Model X']),
          (192, 743, ['2010 BRAKES Brake (Front)', '(Preparation) - MX'])]
CREDIT = ['Courtesy of MAKER MO', 'TOR SALES, U.S.A., INC.']


def _esc(s):
    return s.replace('\\', r'\\').replace('(', r'\(').replace(')', r'\)')


def _line(x, y, pieces, size=12):
    """One displayed line drawn as separate pieces, the way the printout draws it."""
    ops, cx = '', x
    for p in pieces:
        ops += f' BT /F1 {size} Tf 1 0 0 1 {cx:.1f} {y:.1f} Tm ({_esc(p)}) Tj ET\n'
        cx += len(p) * size * 0.5
    return ops


def make_printout_pdf(path, npages=4, doubled=(1,)):
    pdf = pikepdf.Pdf.new()
    for k in range(npages):
        page = pdf.add_blank_page(page_size=(612, 792))
        res = U._sub_dict(page.obj, '/Resources')
        U._add_font(pdf, res, '/F1')
        body = ''
        if k == 0:
            body += _line(266, 698, ['2010 BRAKES'])                  # the article's own title
        body += _line(72, 660, [f'Step {k + 1}: torque the caliper bolt to {30 + k} Nm.'])
        body += _line(72, 420 - 40 * k, ['Fig. 1: Front brake components'])
        body += _line(72, 400 - 40 * k, CREDIT)                         # credit, moving down
        body += _line(72, 200, ['Courtesy Light Switch Assembly Connector'])
        body += _line(72, 180, ['Check the courtesy switch circuit. Courtesy of the dealer.'])
        head = ''.join(_line(x, y, p) for x, y, p in HEADER)
        if k in doubled:
            head += head
        page.contents_add(pikepdf.Stream(pdf, (body + head).encode('latin1')), prepend=False)
    pdf.save(str(path))
    pdf.close()
    return path


def _texts(path):
    from pypdf import PdfReader
    return [p.extract_text() or '' for p in PdfReader(str(path)).pages]


HEADER_RULES = [W.LineRule(r'2010 Make Model X', ('top', 0.08)),
                W.LineRule(r'2010 [A-Z].*', ('top', 0.08))]
CREDIT_RULE = [W.LineRule(r'Courtesy of MAKER MOTOR SALES, U\.S\.A\., INC\.')]


def test_the_header_goes_from_every_page_and_nothing_else(tmp_path):
    src = make_printout_pdf(tmp_path / 'p.pdf')
    out = tmp_path / 'out.pdf'
    res = W.strip_lines(src, out, HEADER_RULES, render_sample=4)
    assert res['err'] is None, res
    assert res['pages'] == 4
    for i, t in enumerate(_texts(out)):
        assert 'Make Model X' not in t and '(Preparation)' not in t, f'page {i + 1} kept its header'
        assert f'bolt to {30 + i} Nm' in t, f'page {i + 1} lost its body'
    assert '2010 BRAKES' in _texts(out)[0], 'the article title is body text, outside the band'


def test_a_header_drawn_twice_over_itself_goes_entirely(tmp_path):
    """247 of the measured pages draw the header twice on the same baselines, so a line reads
    as two copies of the rule's text. Taking one copy would leave the header on the page."""
    src = make_printout_pdf(tmp_path / 'p.pdf', doubled=(1, 2))
    out = tmp_path / 'out.pdf'
    res = W.strip_lines(src, out, [W.LineRule(r'2010 Make Model X', ('top', 0.08))],
                        render_sample=4)
    assert res['err'] is None, res
    assert res['lines']['2010 Make Model X 2010 Make Model X'] == 2, res['lines']
    assert all('Make Model X' not in t for t in _texts(out))


def test_the_credit_goes_wherever_it_sits_but_body_text_about_courtesy_stays(tmp_path):
    """The credit is split mid-word ("MO" | "TOR"), so the pieces only match when joined with
    nothing between them; and it sits at a different height on every page, which is why this
    mode cannot use the stamp audit's same-hole-on-every-page test."""
    src = make_printout_pdf(tmp_path / 'p.pdf')
    out = tmp_path / 'out.pdf'
    res = W.strip_lines(src, out, CREDIT_RULE, render_sample=4)
    assert res['err'] is None, res
    assert sum(res['lines'].values()) == 4
    for i, t in enumerate(_texts(out)):
        assert 'MAKER' not in t, f'page {i + 1} kept its credit'
        assert 'Courtesy Light Switch Assembly Connector' in t
        assert 'Courtesy of the dealer.' in t, 'a phrase inside a longer line is not a line'
        assert 'Fig. 1: Front brake components' in t


def test_a_piece_after_another_in_one_text_block_starts_where_that_one_ended(tmp_path):
    """The walker advances the text position by the font's /Widths, as a viewer does. Before
    it did, a header's last piece reported its BT block's start, the line's box stopped short
    of its ink, and the render audit failed a correct rewrite over a 1.5 pt sliver."""
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica,
        FirstChar=65, LastChar=68, Widths=[667, 667, 722, 722]))           # A B C D
    page.obj['/Resources'] = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    page.contents_add(pikepdf.Stream(pdf, b'BT /F1 10 Tf 2 Tc 1 0 0 1 100 700 Tm (AB) Tj (CD) Tj ET'))
    runs = W.page_runs(pdf.pages[0], 0, collections.defaultdict(set))
    ab, cd = runs
    # (667 + 667) / 1000 * 10 pt + 2 codes * 2 pt of character spacing
    assert ab.x == pytest.approx(100) and cd.x == pytest.approx(100 + 13.34 + 4)
    assert ab.ex == pytest.approx(cd.x) and cd.ex == pytest.approx(cd.x + 14.44 + 4)


def test_the_text_audit_does_not_guess_which_copy_went():
    """Measured on one page of the 23,970-page manual: its body opens with a title that
    flattens to the same characters as the removed header, and pypdf extracts the header
    first. Deleting in drawing order, or at the first occurrence, both took the body's copy
    and failed a correct rewrite. Any choice of occurrences that yields the rewrite is it."""
    header = W._flat('2010 Make Model')
    src = W._flat('2010 MAKE / MODEL buzzers relays timers') + header + W._flat('more body')
    got = W._flat('2010 MAKE / MODEL buzzers relays timers more body')
    assert W._removable(src, got, [header])
    # one piece removed means one copy: a rewrite that lost the body title too is refused
    assert not W._removable(src, W._flat('buzzers relays timers more body'), [header])
    # and any other loss is refused
    assert not W._removable(src, got.replace('relays', ''), [header])


def test_a_credit_sharing_its_baseline_with_body_text_stays(tmp_path):
    """Found by this suite's own first draft: a credit that moved down onto a body line's
    baseline made one line of both, which matches no rule -- and must not, because taking
    the credit's pieces out of it is taking part of a line."""
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    U._add_font(pdf, U._sub_dict(page.obj, '/Resources'), '/F1')
    ops = _line(72, 280, CREDIT) + _line(400, 280, ['See step 4.'])
    page.contents_add(pikepdf.Stream(pdf, ops.encode('latin1')), prepend=False)
    pdf.save(str(tmp_path / 'p.pdf'))
    res = W.strip_lines(tmp_path / 'p.pdf', tmp_path / 'o.pdf', CREDIT_RULE, apply_=False)
    assert res['pages'] == 0 and not res['lines'], res


def test_a_band_keeps_a_rule_off_the_body(tmp_path):
    """Without the band, the header rule `2010 [A-Z].*` would also take the article's own
    title from the body of its first page."""
    src = make_printout_pdf(tmp_path / 'p.pdf')
    unbanded = W.strip_lines(src, tmp_path / 'a.pdf', [W.LineRule(r'2010 [A-Z].*')], apply_=False)
    banded = W.strip_lines(src, tmp_path / 'b.pdf', [W.LineRule(r'2010 [A-Z].*', ('top', 0.08))],
                           apply_=False)
    assert unbanded['lines']['2010 BRAKES'] == 1
    assert '2010 BRAKES' not in banded['lines']


def test_the_band_reports_what_it_held_that_no_rule_matched(tmp_path):
    src = make_printout_pdf(tmp_path / 'p.pdf')
    res = W.strip_lines(src, tmp_path / 'o.pdf', [W.LineRule(r'2010 Make Model X', ('top', 0.08))],
                        apply_=False)
    assert res['left_in_band']['2010 BRAKES Brake (Front) (Preparation) - MX'] == 3, res['left_in_band']


def test_the_audit_rejects_a_removal_that_took_anything_else(tmp_path, monkeypatch):
    """Damage has to be caught by the audit, not prevented only by the rule: flag one body
    line as well and the file must be refused, with nothing written."""
    src = make_printout_pdf(tmp_path / 'p.pdf')
    out = tmp_path / 'out.pdf'
    real = W.page_lines

    def greedy(page, page_no, painters):
        lines = real(page, page_no, painters)
        for ln in lines:
            if ln['spaced'].startswith('Step '):
                ln['spaced'] = ln['joined'] = '2010 Make Model X'
                ln['ry'] = 0.99
        return lines

    monkeypatch.setattr(W, 'page_lines', greedy)
    res = W.strip_lines(src, out, [W.LineRule(r'2010 Make Model X', ('top', 0.08))],
                        render_sample=0)
    assert res['err'] and 'no rule names' in res['err'], res
    assert not out.exists()


def test_the_audit_rejects_half_a_line(tmp_path, monkeypatch):
    """A line goes whole or not at all. Make the line grouping forget one piece of the
    header's second line and the audit -- which walks the source's runs itself -- must see
    the piece left standing on a baseline it removed."""
    src = make_printout_pdf(tmp_path / 'p.pdf')
    out = tmp_path / 'out.pdf'
    real = W.page_lines

    def forgetful(page, page_no, painters):
        lines = real(page, page_no, painters)
        for ln in lines:
            if ln['spaced'].startswith('2010 BRAKES Brake (Front) (Preparation)'):
                ln['runs'] = ln['runs'][:1]
        return lines

    monkeypatch.setattr(W, 'page_lines', forgetful)
    res = W.strip_lines(src, out, [W.LineRule(r'2010 [A-Z].*', ('top', 0.08))], render_sample=0)
    assert res['err'] and 'part of a line' in res['err'], res
    assert not out.exists()


def test_the_render_audit_sees_ink_outside_the_removed_lines(tmp_path, monkeypatch):
    """The text check alone cannot see a glyph with no /ToUnicode; the render check must fail
    a page where ink changed outside the rectangles of the lines removed."""
    if not W._gs():
        pytest.skip('Ghostscript not installed')
    src = make_printout_pdf(tmp_path / 'p.pdf')
    out = tmp_path / 'out.pdf'
    real = W.page_lines

    def lying_rects(page, page_no, painters):
        lines = real(page, page_no, painters)
        for ln in lines:
            ln['rect'] = (0.0, 0.0, 0.01, 0.01)       # claim the header was drawn elsewhere
        return lines

    monkeypatch.setattr(W, 'page_lines', lying_rects)
    res = W.strip_lines(src, out, [W.LineRule(r'2010 Make Model X', ('top', 0.08))],
                        render_sample=2)
    assert res['err'] and 'outside the removed lines' in res['err'], res
    assert not out.exists()


# ── the header's frame ────────────────────────────────────────────────────────

def make_framed_pdf(path, npages=3):
    """The printout's page furniture as vector paths: a border round the whole page (whose
    top edge crosses the header band), the header's frame -- an outer box and a shaded bar,
    one edge stroked under its own clip -- and a figure in the body."""
    pdf = pikepdf.Pdf.new()
    for k in range(npages):
        page = pdf.add_blank_page(page_size=(612, 792))
        U._add_font(pdf, U._sub_dict(page.obj, '/Resources'), '/F1')
        ops = (
            'q 0 g 1 3 m 609 3 l 609 791 l 1 791 l h 2 4 m 608 4 l 608 790 l 2 790 l h f* Q\n'
            'q 0.85 g 37 736 543 55 re f Q\n'
            'q 0.95 g 40 740 537 16 re f Q\n'
            'q 37 736 5 46 re W n 0 G 1 w 37 736 m 37 778 l S Q\n'
            'q 0 G 2 w 100 300 m 400 500 l 400 300 l h S Q\n'
            + _line(72, 660, [f'Step {k + 1}: torque the caliper bolt to {30 + k} Nm.'])
        )
        page.contents_add(pikepdf.Stream(pdf, ops.encode('latin1')), prepend=False)
    pdf.save(str(path))
    pdf.close()
    return path


def _paths_left(path):
    with pikepdf.open(str(path)) as pdf:
        return [[str(i.operator) for i in W._instructions(p)] for p in pdf.pages]


def test_the_frame_goes_and_the_border_figure_and_clip_stay(tmp_path):
    src = make_framed_pdf(tmp_path / 'p.pdf')
    out = tmp_path / 'o.pdf'
    res = W.strip_band_graphics(src, out, ('top', 0.09), render_sample=3)
    assert res['err'] is None, res
    assert res['pages'] == 3
    with pikepdf.open(str(out)) as pdf:
        ins = W._instructions(pdf.pages[0])
    res_ = [(str(i.operator), [float(v) for v in i.operands]) for i in ins if str(i.operator) == 're']
    assert [37.0, 736.0, 543.0, 55.0] not in [a for _, a in res_], 'the frame box stayed'
    assert [40.0, 740.0, 537.0, 16.0] not in [a for _, a in res_], 'the shaded bar stayed'
    assert [37.0, 736.0, 5.0, 46.0] in [a for _, a in res_], 'a clipping path must never go'
    ops = [str(i.operator) for i in ins]
    assert ops.count('f*') == 1, 'the page border reaches the side margins and is not the header'
    assert any(str(i.operator) == 'l' and [float(v) for v in i.operands] == [400.0, 500.0]
               for i in ins), 'the body figure is not in the band'
    assert _texts(out) == _texts(src)


def test_the_render_audit_refuses_a_band_graphics_drop_outside_the_band(tmp_path, monkeypatch):
    if not W._gs():
        pytest.skip('Ghostscript not installed')
    src = make_framed_pdf(tmp_path / 'p.pdf')
    out = tmp_path / 'o.pdf'
    real = W._band_paths

    def greedy(page, band):
        ins, drop = real(page, band)
        for i, x in enumerate(ins):          # also take the body figure's stroke
            if str(x.operator) == 'l' and [float(v) for v in x.operands] == [400.0, 500.0]:
                drop |= {i - 1, i, i + 1, i + 2, i + 3}
        return ins, drop

    monkeypatch.setattr(W, '_band_paths', greedy)
    res = W.strip_band_graphics(src, out, ('top', 0.09), render_sample=3)
    assert res['err'] and 'outside the band' in res['err'], res
    assert not out.exists()


def test_the_command_line_reports_without_writing(tmp_path, capsys):
    src = make_printout_pdf(tmp_path / 'p.pdf')
    before = src.read_bytes()
    assert W.main([str(src), '--strip-line', r'2010 Make Model X', '--band', 'top:0.08']) == 0
    assert 'would remove 4 lines' in capsys.readouterr().out
    assert src.read_bytes() == before, 'report-only mode must not touch the file'
