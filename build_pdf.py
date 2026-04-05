#!/usr/bin/env python3
"""Stitch chapter screenshots and assemble the PDF.

Usage:
    python build_pdf.py --captures capture/ --out book.pdf
    python build_pdf.py --captures capture/ --out book.pdf --ocr
    python build_pdf.py --captures capture/ --out book.pdf --split-height 3000
"""
import argparse
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


def stitch_chapter(shot_paths, crop, strip_h=200, diff_threshold=8.0):
    """Stitch screenshots top-to-bottom via overlap detection.

    diff_threshold: if best overlap match has mean abs diff larger than this,
    we assume no overlap (rare) and append with a separator line.
    """
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
            # fall back: append entire next image minus small overlap guess
            print(f"    [stitch] weak overlap (diff={diff:.1f}); appending conservatively")
            result = np.vstack([result, next_arr])
            continue

        new_y = offset + strip_h
        if new_y >= next_arr.shape[0]:
            continue  # nothing new to add
        result = np.vstack([result, next_arr[new_y:]])

    return Image.fromarray(result)


def split_into_pages(img: Image.Image, page_h: int):
    """Split a very tall image into multiple pages of at most page_h rows."""
    w, h = img.size
    if h <= page_h:
        return [img]
    pages = []
    y = 0
    while y < h:
        y2 = min(y + page_h, h)
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
    ap.add_argument("--split-height", type=int, default=0,
                    help="Split stitched chapters into pages of this height (0 = no split)")
    ap.add_argument("--ocr", action="store_true", help="Run ocrmypdf after assembly")
    ap.add_argument("--ocr-lang", default="eng")
    args = ap.parse_args()

    args.stitched.mkdir(parents=True, exist_ok=True)

    chapter_dirs = sorted(d for d in args.captures.glob("ch*") if d.is_dir())
    if not chapter_dirs:
        print(f"No chapter directories found under {args.captures}")
        return

    page_images = []
    for ch_dir in chapter_dirs:
        shot_paths = sorted(ch_dir.glob("shot_*.png"))
        if not shot_paths:
            continue
        if args.max_shots > 0:
            shot_paths = shot_paths[: args.max_shots]
        print(f"Stitching {ch_dir.name} ({len(shot_paths)} shots)")
        stitched = stitch_chapter(shot_paths, args.crop, strip_h=args.strip_height)
        stitched_path = args.stitched / f"{ch_dir.name}.png"
        stitched.save(stitched_path, optimize=True)
        print(f"    -> {stitched_path} ({stitched.size[0]}x{stitched.size[1]})")

        if args.split_height > 0:
            pages = split_into_pages(stitched, args.split_height)
            for i, p in enumerate(pages):
                pp = args.stitched / f"{ch_dir.name}_p{i:03d}.png"
                p.save(pp, optimize=True)
                page_images.append(pp)
        else:
            page_images.append(stitched_path)

    print(f"\nAssembling PDF from {len(page_images)} pages...")
    with open(args.out, "wb") as f:
        f.write(img2pdf.convert([str(p) for p in page_images]))
    print(f"    -> {args.out}")

    if args.ocr:
        ocr_out = args.out.with_name(args.out.stem + "_ocr.pdf")
        print(f"\nRunning OCR -> {ocr_out}")
        subprocess.run(
            ["ocrmypdf", "-l", args.ocr_lang, "--output-type", "pdf",
             str(args.out), str(ocr_out)],
            check=True,
        )
        print("OCR complete.")


if __name__ == "__main__":
    main()
