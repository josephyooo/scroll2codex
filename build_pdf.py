#!/usr/bin/env python3
"""Stitch chapter screenshots and assemble the PDF.

Usage:
    python build_pdf.py --captures capture/ --out book.pdf
    python build_pdf.py --captures capture/ --out book.pdf --ocr
    python build_pdf.py --captures capture/ --out book.pdf --split-height 3000
"""
import argparse
import re
import subprocess
from pathlib import Path

import img2pdf
import numpy as np
from PIL import Image


def crop_box(img: Image.Image, crop):
    """crop = (left, top, right, bottom) in pixels; None = no crop."""
    if crop is None:
        return img
    return img.crop(crop)


def find_overlap(prev_tail_gray: np.ndarray, next_gray: np.ndarray, strip_h: int):
    """Find the y-offset in next_gray where prev_tail's last strip_h rows best match.

    Returns (offset, mean_abs_diff).
    """
    strip = prev_tail_gray[-strip_h:].astype(np.int32)
    H = next_gray.shape[0]
    if H < strip_h:
        return 0, float("inf")

    # Slide the strip down next_gray; compute mean abs diff at each offset.
    best_offset = 0
    best_diff = float("inf")
    # Step 1 for accuracy; this loop is cheap for typical sizes.
    for y in range(0, H - strip_h + 1):
        window = next_gray[y:y + strip_h].astype(np.int32)
        diff = float(np.abs(window - strip).mean())
        if diff < best_diff:
            best_diff = diff
            best_offset = y
    return best_offset, best_diff


def stitch_chapter(shot_paths, crop, strip_h=200, diff_threshold=8.0,
                   min_overlap=None):
    """Stitch screenshots top-to-bottom via overlap detection.

    diff_threshold: if best overlap match has mean abs diff larger than this,
    we assume weak overlap (rare) and append with a conservative trim.

    min_overlap: pixels to trim from the top of next_arr in the weak-overlap
    fallback branch. Captures are intentionally overlapping (scroll_delta <
    viewport_height), so appending the untrimmed frame duplicates content.
    Defaults to strip_h -- the width of the band we try to match -- which
    is a conservative lower bound. Users with larger scroll deltas may want
    to raise this via --min-overlap to reduce residual duplication.
    """
    if min_overlap is None:
        min_overlap = strip_h
    imgs = [crop_box(Image.open(p).convert("RGB"), crop) for p in shot_paths]
    if not imgs:
        raise ValueError("no screenshots provided")

    result = np.array(imgs[0])
    for next_img in imgs[1:]:
        next_arr = np.array(next_img)
        tail_gray = np.array(
            Image.fromarray(result[-max(strip_h * 3, 600):]).convert("L")
        )
        next_gray = np.array(next_img.convert("L"))
        offset, diff = find_overlap(tail_gray, next_gray, strip_h)

        if diff > diff_threshold:
            # Weak overlap: trim a conservative minimum instead of appending
            # the entire frame (which would duplicate the intended overlap).
            trim = min(min_overlap, next_arr.shape[0])
            print(f"    [stitch] weak overlap (diff={diff:.1f}); "
                  f"trimming {trim}px conservatively")
            if trim >= next_arr.shape[0]:
                continue  # nothing new to add
            result = np.vstack([result, next_arr[trim:]])
            continue

        new_y = offset + strip_h
        if new_y >= next_arr.shape[0]:
            continue  # nothing new to add
        result = np.vstack([result, next_arr[new_y:]])

    return Image.fromarray(result)


def find_blank_rows(img: Image.Image, bg_threshold: int = 245, dark_frac_max: float = 0.003):
    """Return a boolean array: True where the row is considered whitespace.

    A row counts as blank if fewer than dark_frac_max of its pixels fall below
    bg_threshold in grayscale. Default 0.3% keeps page fine: a single scraggly
    pixel of noise per 333 won't disqualify a line gap, but real glyph rows
    (5-20% dark pixels) will never qualify.
    """
    gray = np.asarray(img.convert("L"))
    dark = gray < bg_threshold
    dark_frac_per_row = dark.mean(axis=1)
    return dark_frac_per_row <= dark_frac_max


def find_snap_point(blank: np.ndarray, target: int, tolerance: int) -> int:
    """Find a blank row within +/-tolerance of target; if multiple, pick the
    center of the widest contiguous blank band. Returns -1 if none found.
    """
    lo = max(0, target - tolerance)
    hi = min(len(blank), target + tolerance + 1)
    window = blank[lo:hi]
    if not window.any():
        return -1
    # Find contiguous runs of True; pick the run whose center is closest to target.
    best_center = -1
    best_score = None
    i = 0
    while i < len(window):
        if not window[i]:
            i += 1
            continue
        j = i
        while j < len(window) and window[j]:
            j += 1
        center = lo + (i + j - 1) // 2
        # Prefer bands close to target, then wider bands.
        dist = abs(center - target)
        width = j - i
        score = (dist, -width)
        if best_score is None or score < best_score:
            best_score = score
            best_center = center
        i = j
    return best_center


def pad_to_height(img: Image.Image, target_h: int, bg=(255, 255, 255)):
    """Return a copy of img padded with whitespace top+bottom to exact target_h.
    If img is already >= target_h, returns img unchanged.
    """
    w, h = img.size
    if h >= target_h:
        return img
    pad_top = (target_h - h) // 2
    canvas = Image.new("RGB", (w, target_h), bg)
    canvas.paste(img, (0, pad_top))
    return canvas


def split_into_pages(img: Image.Image, page_h: int, snap_tolerance: int = 60):
    """Split a very tall image into pages of approximately page_h rows.

    Splits are snapped to inter-line whitespace so no glyph row is bisected.
    snap_tolerance caps how far from the ideal boundary we'll move to find
    whitespace; if no blank row exists in the window, we fall back to a hard
    cut at the target (rare in practice for textbook content).
    """
    w, h = img.size
    if h <= page_h:
        return [img]

    blank = find_blank_rows(img)
    pages = []
    y = 0
    while y < h:
        ideal = y + page_h
        if ideal >= h:
            pages.append(img.crop((0, y, w, h)))
            break
        snap = find_snap_point(blank, ideal, snap_tolerance)
        y2 = snap if snap > y else ideal  # guard against snap going backwards
        pages.append(img.crop((0, y, w, y2)))
        y = y2
    return pages


def parse_crop(s):
    if not s or s.lower() == "none":
        return None
    parts = [int(x) for x in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("crop must be 'left,top,right,bottom'")
    return tuple(parts)


def parse_chapters(s):
    """Parse a chapter spec like '0-8', '9-23', '0-8,15,17-19' into a set of ints.
    Returns None when s is empty (meaning: all chapters).
    """
    if not s:
        return None
    result = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
            if hi < lo:
                raise argparse.ArgumentTypeError(f"bad range: {part}")
            result.update(range(lo, hi + 1))
        else:
            result.add(int(part))
    return result


def ch_index(name: str):
    """Extract the spine index from a chapter dir/file name like 'ch10' -> 10."""
    m = re.match(r"ch(\d+)", name)
    return int(m.group(1)) if m else None


def assemble_pdf(page_images, out_path: Path, ocr: bool, ocr_lang: str):
    print(f"Assembling PDF from {len(page_images)} pages -> {out_path}")
    with open(out_path, "wb") as f:
        f.write(img2pdf.convert([str(p) for p in page_images]))
    if ocr:
        ocr_out = out_path.with_name(out_path.stem + "_ocr.pdf")
        print(f"    OCR -> {ocr_out}")
        subprocess.run(
            ["ocrmypdf", "-l", ocr_lang, "--output-type", "pdf",
             str(out_path), str(ocr_out)],
            check=True,
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--captures", type=Path, default=Path("capture"))
    ap.add_argument("--stitched", type=Path, default=Path("stitched"))
    ap.add_argument("--out", type=Path, default=Path("book.pdf"))
    ap.add_argument(
        "--crop",
        type=parse_crop,
        default=(130, 40, 1780, 1000),
        help="left,top,right,bottom to crop browser chrome (tuned for 1920x1080). "
             "Use 'none' to skip cropping.",
    )
    ap.add_argument("--max-shots", type=int, default=0,
                    help="Cap number of screenshots stitched per chapter (0 = all)")
    ap.add_argument("--strip-height", type=int, default=200,
                    help="Overlap-detection strip height in pixels")
    ap.add_argument("--min-overlap", type=int, default=0,
                    help="Pixels to trim off the top of each frame in the "
                         "weak-overlap fallback branch (0 = use --strip-height). "
                         "Raise this if fallback stitches show duplicated bands.")
    ap.add_argument("--split-height", type=int, default=0,
                    help="Split stitched chapters into pages of ~this height (0 = no split). "
                         "Splits snap to inter-line whitespace so text lines aren't bisected.")
    ap.add_argument("--snap-tolerance", type=int, default=60,
                    help="Max pixels the split point may drift from --split-height to find "
                         "a whitespace row (default 60)")
    ap.add_argument("--uniform-pages", action="store_true",
                    help="Auto-detect a canonical page height from the first few chapters, "
                         "split longer chapters into pages of that height (snapping to "
                         "whitespace), and pad shorter pages with whitespace top+bottom "
                         "to the canonical height. Result: every PDF page is identical size.")
    ap.add_argument("--uniform-sample", type=int, default=4,
                    help="Number of leading chapters sampled to determine canonical height")
    ap.add_argument("--page-height", type=int, default=0,
                    help="Override the canonical page height for --uniform-pages (0 = auto)")
    ap.add_argument("--chapters", type=str, default="",
                    help="Restrict to these chapter indices, e.g. '0-8', '9-23', "
                         "'0-8,15,17-19'. Empty = all captured chapters.")
    ap.add_argument("--per-chapter", action="store_true",
                    help="Emit one PDF per chapter instead of one combined PDF. "
                         "Use {n} in --out for the 1-based position within the "
                         "selected range, or {name} for the chapter dir name "
                         "(e.g. --out 'chapter_{n}.pdf').")
    ap.add_argument("--ocr", action="store_true", help="Run ocrmypdf after assembly")
    ap.add_argument("--ocr-lang", default="eng")
    args = ap.parse_args()

    args.stitched.mkdir(parents=True, exist_ok=True)

    chapter_dirs = sorted(d for d in args.captures.glob("ch*") if d.is_dir())
    if not chapter_dirs:
        print(f"No chapter directories found under {args.captures}")
        return

    allowed = parse_chapters(args.chapters)
    if allowed is not None:
        chapter_dirs = [d for d in chapter_dirs if ch_index(d.name) in allowed]
        if not chapter_dirs:
            print(f"No chapter directories match --chapters {args.chapters!r}")
            return
        print(f"Selected {len(chapter_dirs)} chapter(s): "
              f"{[d.name for d in chapter_dirs]}")

    # Pass 1: stitch every chapter.
    stitched_paths = []
    for ch_dir in chapter_dirs:
        shot_paths = sorted(ch_dir.glob("shot_*.png"))
        if not shot_paths:
            continue
        if args.max_shots > 0:
            shot_paths = shot_paths[: args.max_shots]
        print(f"Stitching {ch_dir.name} ({len(shot_paths)} shots)")
        stitched = stitch_chapter(
            shot_paths, args.crop,
            strip_h=args.strip_height,
            min_overlap=args.min_overlap or None,
        )
        stitched_path = args.stitched / f"{ch_dir.name}.png"
        stitched.save(stitched_path, optimize=True)
        print(f"    -> {stitched_path} ({stitched.size[0]}x{stitched.size[1]})")
        stitched_paths.append(stitched_path)

    # Pass 2: decide page layout and (optionally) split + pad.
    canonical_h = None
    page_h = None
    if args.uniform_pages:
        if args.page_height > 0:
            canonical_h = args.page_height
            print(f"\nUsing --page-height override: {canonical_h}px")
        else:
            sample_n = min(args.uniform_sample, len(stitched_paths))
            sample_heights = []
            for p in stitched_paths[:sample_n]:
                with Image.open(p) as im:
                    sample_heights.append(im.size[1])
            canonical_h = min(sample_heights)
            print(f"\nCanonical page height: {canonical_h}px "
                  f"(min of first {sample_n}: {sample_heights})")
        page_h = canonical_h
    elif args.split_height > 0:
        page_h = args.split_height

    # Produce page images grouped by chapter.
    per_chapter_pages = []  # list of (ch_name, [page_paths])
    for stitched_path in stitched_paths:
        if page_h is None:
            per_chapter_pages.append((stitched_path.stem, [stitched_path]))
            continue
        with Image.open(stitched_path) as stitched:
            stitched = stitched.convert("RGB")
            pages = split_into_pages(stitched, page_h,
                                     snap_tolerance=args.snap_tolerance)
            if args.uniform_pages:
                pages = [pad_to_height(p, canonical_h) for p in pages]
        name = stitched_path.stem
        page_paths = []
        for i, p in enumerate(pages):
            pp = args.stitched / f"{name}_p{i:03d}.png"
            p.save(pp, optimize=True)
            page_paths.append(pp)
        per_chapter_pages.append((name, page_paths))

    print()
    if args.per_chapter:
        template = str(args.out)
        if "{n}" not in template and "{name}" not in template:
            # Auto-append a 1-based counter before the suffix.
            template = str(args.out.with_name(
                args.out.stem + "_{n:02d}" + args.out.suffix))
        for n, (ch_name, page_paths) in enumerate(per_chapter_pages, start=1):
            out = Path(template.format(n=n, name=ch_name))
            assemble_pdf(page_paths, out, args.ocr, args.ocr_lang)
    else:
        all_pages = [p for _, paths in per_chapter_pages for p in paths]
        assemble_pdf(all_pages, args.out, args.ocr, args.ocr_lang)


if __name__ == "__main__":
    main()
