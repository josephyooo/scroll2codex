#!/usr/bin/env python3
"""Capture VitalSource book pages via Playwright-driven browser.

First run: log in manually in the launched browser. Profile is persisted
to ~/.vs2pdf_profile so subsequent runs stay logged in.

Usage:
    python capture.py --url 'https://bookshelf.vitalsource.com/reader/books/...'
    python capture.py --url ... --start 2 --end 3   # only chapter index 2
"""
import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote, urlparse
from playwright.async_api import async_playwright

PROFILE_DIR = Path.home() / ".vs2pdf_profile"

JS_WAIT_READY = """
() => !!(window.Viewer && typeof window.Viewer.callMethod === 'function')
"""

JS_RPC = """
async ([method, args]) => {
    try {
        const r = await window.Viewer.callMethod(method, args);
        return { ok: true, data: r };
    } catch (e) {
        return { ok: false, error: String(e && e.message || e) };
    }
}
"""


async def rpc(page, method, args=None):
    if args is None:
        args = {}
    r = await page.evaluate(JS_RPC, [method, args])
    if not r["ok"]:
        raise RuntimeError(f"RPC {method} failed: {r.get('error')}")
    return r["data"]


async def settle(page, ms=400):
    await page.wait_for_timeout(ms)


JS_TOC_OPEN = """
() => {
    // Reliable signal: the content iframe's left edge. When TOC is closed the
    // iframe starts at roughly x<80 (just past the always-visible toolbar).
    // When TOC is open, the iframe is pushed right to x>180.
    const iframes = Array.from(document.querySelectorAll('iframe'))
        .filter(f => !(f.id || '').startsWith('claude'));
    let best = null, bestW = 0;
    for (const f of iframes) {
        const r = f.getBoundingClientRect();
        if (r.width > bestW) { bestW = r.width; best = r; }
    }
    if (!best) return false;
    return best.left > 180;
}
"""

JS_FIND_HAMBURGER = """
() => {
    const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
    const score = (b) => {
        const r = b.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return -1;
        const label = ((b.getAttribute('aria-label') || '') + ' ' +
                       (b.getAttribute('title') || '') + ' ' +
                       (b.getAttribute('data-testid') || '')).toLowerCase();
        let s = 0;
        if (/(table of contents|toc|contents|sidebar|table_of_contents|tableofcontents)/.test(label)) s += 1000;
        if (/(menu|hamburger|panel|drawer|navigation|collapse|expand)/.test(label)) s += 300;
        if (r.left < 120 && r.top < 140 && r.width < 80 && r.height < 80) s += 200;
        const ar = r.width / r.height;
        if (ar > 0.6 && ar < 1.8) s += 40;
        return s;
    };
    let best = null, bestScore = -1;
    for (const b of buttons) {
        const s = score(b);
        if (s > bestScore) { bestScore = s; best = b; }
    }
    if (!best || bestScore <= 100) return null;
    const r = best.getBoundingClientRect();
    return {
        score: bestScore,
        label: best.getAttribute('aria-label') || best.getAttribute('title') || '',
        x: Math.round(r.left + r.width / 2),
        y: Math.round(r.top + r.height / 2),
    };
}
"""

JS_DEBUG_BUTTONS = """
() => {
    const btns = Array.from(document.querySelectorAll('button, [role="button"]'));
    return btns
        .map(b => {
            const r = b.getBoundingClientRect();
            return {
                label: b.getAttribute('aria-label') || '',
                title: b.getAttribute('title') || '',
                testid: b.getAttribute('data-testid') || '',
                text: (b.innerText || '').trim().slice(0, 30),
                x: Math.round(r.left), y: Math.round(r.top),
                w: Math.round(r.width), h: Math.round(r.height),
            };
        })
        .filter(b => b.w > 0 && b.h > 0 && b.x < 200 && b.y < 200);
}
"""


async def _check_toc_open(page):
    try:
        return await page.evaluate(JS_TOC_OPEN)
    except Exception:
        return False


async def close_toc_if_open(page, seek_ms=2000, poll_ms=300, max_closes=5):
    """Close the left TOC sidebar if visible. Handles late-rendering TOC too.

    Phase 1 (seek, up to seek_ms): poll for the TOC to appear. This covers the
    case where the reader chrome renders after we think loading is done.
    Phase 2 (close, up to max_closes clicks): click hamburger until TOC is gone.

    Returns True if the TOC was closed.
    """
    # Phase 1: seek
    deadline = seek_ms
    elapsed = 0
    is_open = False
    while elapsed <= deadline:
        if await _check_toc_open(page):
            is_open = True
            break
        await page.wait_for_timeout(poll_ms)
        elapsed += poll_ms
    if not is_open:
        return False

    # Phase 2: close via real mouse click (React components need a synthetic
    # event, not element.click()).
    last_target = None
    for _ in range(max_closes):
        try:
            target = await page.evaluate(JS_FIND_HAMBURGER)
        except Exception:
            target = None
        if target:
            last_target = target
            try:
                await page.mouse.click(target["x"], target["y"])
            except Exception:
                pass
        else:
            try:
                await page.mouse.click(32, 86)
            except Exception:
                pass
        await page.wait_for_timeout(poll_ms)
        if not await _check_toc_open(page):
            return True
    try:
        diag = await page.evaluate(JS_DEBUG_BUTTONS)
        print(f"  [warn] failed to close TOC. last target={last_target}")
        print(f"  [warn] top-left buttons: {diag}")
    except Exception:
        print("  [warn] failed to close TOC after retries")
    return False


def png_hash(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def build_reader_url(book_url: str, cfi: str) -> str | None:
    """Construct a reader URL that jumps directly to a given cfi."""
    m = re.search(r"/books/(\d+)/", book_url)
    if not m:
        return None
    isbn = m.group(1)
    parsed = urlparse(book_url)
    return f"{parsed.scheme}://{parsed.netloc}/reader/books/{isbn}/epubcfi/{quote(cfi, safe='')}"


async def jump_to_chapter(page, book_url, chapters, target_idx, resolve_idx, force_reload=False):
    """Navigate directly to chapter target_idx via URL + cfi. Returns True on success."""
    if target_idx >= len(chapters):
        return False
    cfi = chapters[target_idx].get("cfi") or chapters[target_idx].get("cfiWithoutAssertions")
    if not cfi:
        return False
    url = build_reader_url(book_url, cfi)
    if not url:
        return False
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_function(JS_WAIT_READY, timeout=60_000)
    await settle(page, 1500)
    await close_toc_if_open(page)
    cur = await rpc(page, "Book.getCurrentPage")
    landed = resolve_idx(cur)
    return landed == target_idx


async def capture_chapter(page, ch_idx, out_dir, scroll_delta, settle_ms, max_scrolls, force):
    ch_dir = out_dir / f"ch{ch_idx:02d}"
    done_marker = ch_dir / ".done"
    if done_marker.exists() and not force:
        print(f"  ch{ch_idx:02d} already captured, skipping (use --force to redo)")
        return 0
    ch_dir.mkdir(parents=True, exist_ok=True)

    meta = await rpc(page, "Book.getCurrentPage")
    (ch_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"  ch{ch_idx:02d}: {meta.get('chapterTitle', '?')}  path={meta.get('path')}")

    # Defensive: ensure TOC sidebar isn't eating content width
    await close_toc_if_open(page)

    # Scroll to top of chapter
    await rpc(page, "Viewport.scrollToTop")
    await settle(page, 600)

    # Move mouse into content area to receive wheel events
    vp = page.viewport_size
    cx = vp["width"] // 2
    cy = vp["height"] // 2
    await page.mouse.move(cx, cy)

    start_path = meta.get("path")
    prev_hash = None
    n_shots = 0

    for i in range(max_scrolls):
        png = await page.screenshot(type="png", full_page=False)
        h = png_hash(png)
        if h == prev_hash:
            break  # screenshot stopped changing -> bottom reached
        (ch_dir / f"shot_{i:03d}.png").write_bytes(png)
        n_shots += 1
        prev_hash = h

        await page.mouse.wheel(0, scroll_delta)
        await settle(page, settle_ms)

        # Guard: if we accidentally scrolled into next chapter, stop
        cur = await rpc(page, "Book.getCurrentPage")
        if cur.get("path") != start_path:
            break

    done_marker.touch()
    print(f"    captured {n_shots} screenshots")
    return n_shots


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="VitalSource reader URL (any page in the book)")
    ap.add_argument("--out", default="capture", type=Path, help="Output directory")
    ap.add_argument("--start", type=int, default=0, help="Start chapter spine index")
    ap.add_argument("--end", type=int, default=None, help="End chapter spine index (exclusive)")
    ap.add_argument("--viewport-width", type=int, default=1920)
    ap.add_argument("--viewport-height", type=int, default=1080)
    ap.add_argument("--scroll-delta", type=int, default=700,
                    help="Pixels to scroll per step (keep < viewport height for overlap)")
    ap.add_argument("--settle-ms", type=int, default=350, help="Wait after each scroll")
    ap.add_argument("--max-scrolls", type=int, default=300, help="Safety cap per chapter")
    ap.add_argument("--force", action="store_true", help="Recapture chapters that already have .done marker")
    ap.add_argument("--pause", action="store_true",
                    help="After reader loads, pause until you press Enter in the terminal. "
                         "Use this to configure reader preferences (font, margins, line height) manually.")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": args.viewport_width, "height": args.viewport_height},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        print(f"Navigating to {args.url}")
        await page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)

        print("Waiting for reader to initialize...")
        print("If you need to log in, do it now. Script will wait up to 5 minutes.")
        await page.wait_for_function(JS_WAIT_READY, timeout=300_000)
        await settle(page, 1500)
        await close_toc_if_open(page)

        if args.pause:
            print("\n=== PAUSED ===")
            print("Configure reader preferences in the browser now")
            print("(Close TOC, set Text Size / Font / Margin / Line Height).")
            input("Press Enter here when ready to begin capture... ")
            await settle(page, 500)

        spine = await rpc(page, "Book.getSpine")
        chapters = spine.get("data") or []
        print(f"Spine has {len(chapters)} items")

        def resolve_idx(cur):
            """Return current chapter spine index, falling back to path match."""
            idx = cur.get("index")
            if idx is not None:
                return idx
            path = cur.get("path")
            for i, ch in enumerate(chapters):
                url = ch.get("url") or ""
                if url == path or url.endswith(path or ""):
                    return i
            return 0

        # Navigate to chapter at args.start
        cur = await rpc(page, "Book.getCurrentPage")
        cur_idx = resolve_idx(cur)
        print(f"Current chapter index: {cur_idx}, target start: {args.start}")

        if cur_idx != args.start:
            # Try URL jump first (instant), fall back to walking
            print(f"Jumping to chapter {args.start} via URL...")
            jumped = await jump_to_chapter(page, args.url, chapters, args.start, resolve_idx)
            if jumped:
                cur_idx = args.start
                print(f"  landed at index {cur_idx}")
            else:
                print("  URL jump failed, walking with goToNextPage")
                while cur_idx < args.start:
                    await rpc(page, "Book.goToNextPage")
                    await settle(page, 800)
                    cur = await rpc(page, "Book.getCurrentPage")
                    new_idx = resolve_idx(cur)
                    if new_idx == cur_idx:
                        print("  [WARN] goToNextPage did not advance; stopping")
                        break
                    cur_idx = new_idx
                    print(f"  advanced to index {cur_idx}")

        end = args.end if args.end is not None else len(chapters)
        while cur_idx < end:
            await capture_chapter(
                page, cur_idx, args.out,
                scroll_delta=args.scroll_delta,
                settle_ms=args.settle_ms,
                max_scrolls=args.max_scrolls,
                force=args.force,
            )
            if cur_idx + 1 >= end:
                break
            await rpc(page, "Book.goToNextPage")
            await settle(page, 1000)
            cur = await rpc(page, "Book.getCurrentPage")
            new_idx = resolve_idx(cur)
            if new_idx == cur_idx:
                print("  [WARN] goToNextPage did not advance; stopping")
                break
            cur_idx = new_idx

        print(f"\nDone. Screenshots under {args.out}/")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
