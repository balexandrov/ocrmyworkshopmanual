# Compression tuning

[← README](../README.md) · [all docs](../README.md#documentation)

| topic | what to know |
|---|---|
| **Interpolated renders** | Ghostscript doesn't average pixels when downscaling unless told to, so a 600→200 dpi reduction keeps 1 pixel in 9 and hairlines vanish. `-dDOINTERPOLATE` makes a hairline arrive as *grey* — what adaptive binarization is for — measured 1,425 line pieces instead of 2,455 and a JBIG2 **7.7% smaller** |
| **Resolution measured two ways** | Effective dpi over the page area is wrong for scans stored as full-width *strips* (68 strips read as 73 dpi against a true 604), so an image ≥4:1 wider than tall also contributes a width-based reading. It informs classification but deliberately doesn't raise render dpi (2× bytes, 2× runtime for no gain) |
| **`--sauvola-k`** | Boldness: lower = thicker ink. A hard ink floor keeps solid-black fills solid, which Sauvola alone hollows out |
| **Photo pages** | Always flat-fielded against a bright-paper envelope (so solid blacks stay black), edges trimmed, soft-levels curve plus a highlight knee so photos stay rich rather than washed. `--photo-descreen` merges halftone grain into smooth tone |
| **Colour detection** | White-balances first, so a yellowed B&W page isn't mistaken for colour and kept as a large yellow JPEG |
| **JBIG2 mode** | Generic only. A shared-dictionary "symbol" mode would be ~30% smaller, but PDFium (Chrome/Edge) renders it as **blank pages** |
| **Never grows a file** | If compression or the pre-check won't beat the original, images are kept untouched and only OCR is added |
| **Windows long paths** | Inputs over 260 chars are opened via the `\\?\` prefix |

Rough size comparison on grayscale line-art scans: **this tool ~8%**, CCITT-G4 ~34%,
grayscale-JPEG ~47%, `ocrmypdf --optimize 3` ~37%.
