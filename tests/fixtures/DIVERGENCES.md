# Known human-vs-classifier divergences (found during fixture verification)

Pages where my visual judgment differed from classify_page(). Kept OUT of the
pass/fail corpus (the test asserts folder==classifier, so a divergent page
can't live in either folder cleanly). Re-examine if the colour test is retuned.

## 2F Engine Repair Manual (cruisercult 40-55), page 1 — cover
- **classify_page:** photo_gray  |  **my read:** photo_color (weak)
- Muted **sage-green** cover with reddish foxing stains; low saturation.
- The cast-robust `_is_color` test white-balances the uniform green away and the
  residual chroma falls under its threshold (>45 on >6% of marks), so it lands gray.
- Defensible either way, but a coloured cover compressed as grayscale loses the
  green + stains. Flag for `_is_color` threshold tuning; it's the failure mode
  opposite to the sepia trap (there: correctly NOT colour; here: arguably IS colour).
- source: `M:\...\Cruiser Cult Manuals (cruisercult.com)\40-55 Series\2F Engine Repair Manual.pdf` p1
