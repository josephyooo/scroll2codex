# scroll2codex

> **Note**: This project was built entirely with [Claude Code](https://claude.ai/claude-code). Code, documentation, and commit messages were AI-generated with human direction and review.

Turn a scrolling ebook reader into a uniform, searchable PDF.

Many ebook platforms render books into an infinite-scroll web reader rather
than discrete pages. `scroll2codex` drives a real browser with Playwright,
scrolls each chapter top-to-bottom, captures overlapping screenshots, stitches
them into a continuous image, then splits the result into uniformly-sized
pages at inter-line whitespace (so text is never bisected). OCR gives you a
searchable text layer.

Originally written against VitalSource Bookshelf; the only provider-specific
bits are the RPC bridge used to navigate chapters and the crop rectangle used
to trim the reader chrome. Both are configurable.

## Install

```bash
conda env create -f environment.yml
conda activate scroll2codex
playwright install chromium

# OCR pipeline (macOS):
brew install ocrmypdf
```

## Usage

### 1. Capture

```bash
python capture.py \
    --url 'https://reader.example.com/books/.../some-chapter' \
    --pause
```

- `--pause` opens the reader and waits for you to log in, close the sidebar,
  tune font/line-height/margins, and press Enter.
- The script walks every chapter, scrolls top-to-bottom, and saves
  `capture/chNN/shot_000.png`, `shot_001.png`, … with `.done` markers so you
  can resume after interruptions.
- `--start N --end M` limits to a spine range (useful for testing).
- `--force` re-captures chapters that already have `.done` markers.

### 2. Assemble

```bash
python build_pdf.py --captures capture/ --out book.pdf --uniform-pages
```

- `--uniform-pages` auto-picks a canonical page height from the first few
  chapters, splits longer chapters at inter-line whitespace so no text is
  bisected, and pads shorter pages to the canonical height. Every PDF page
  is the same size.
- `--crop left,top,right,bottom` trims reader chrome from each screenshot
  (default tuned for 1920×1080 VitalSource).
- `--split-height N` (alternative to `--uniform-pages`) uses a fixed target.
- `--max-shots N` stitches only the first N screenshots per chapter, for
  quick iteration on crop/split tuning.

### 3. OCR

```bash
ocrmypdf -l eng book.pdf book_searchable.pdf
```

Adds an invisible text layer, leaving the page images untouched.

## Adapting to other providers

Two things are provider-specific:

1. **Chapter navigation.** `capture.py` calls `window.Viewer.callMethod(...)`
   which is VitalSource's mosaic-reader bridge. For another provider, replace
   the `rpc(...)` calls and `jump_to_chapter(...)` with that provider's
   equivalent (most scrolling readers expose something similar, or you can
   drive plain URL navigation + `page.evaluate` on an exposed global).
2. **Reader chrome crop.** `build_pdf.py --crop` should be retuned for the
   new reader's layout. Run a one-chapter capture first, open a screenshot,
   and measure the content bounds.

The scroll-capture-and-stitch loop, overlap detection, whitespace-snapped
splitting, and uniform-page padding are all provider-agnostic.

## How the stitching works

- **Overlap detection:** each screenshot overlaps the previous one by
  `viewport_height − scroll_delta` pixels. The stitcher takes the last 200px
  of the running result, slides it down the top of the next screenshot row
  by row, and picks the offset with the lowest mean absolute grayscale
  difference. Everything below that offset is new content and gets appended.
- **Whitespace splitting:** blank rows are rows where <0.3% of pixels are
  darker than 245 (grayscale). Around each ideal split point, the splitter
  searches ±60px for a whitespace band and cuts at its center, preferring
  wider bands over narrower ones. If no whitespace is found in that window
  (rare for textbook prose), it falls back to a hard cut.

## Files

- `capture.py` — Playwright capture loop, resume-aware
- `build_pdf.py` — stitch + split + assemble + (optional) pad to uniform
- `environment.yml` — conda env spec
