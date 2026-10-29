#!/usr/bin/env python3
"""x_likes.py — archive and categorize your X/Twitter likes.

  scrape          log in (first run only) and scroll x.com/<you>/likes, saving every tweet the page
                  receives -> data/likes.jsonl (raw GraphQL responses are kept in data/raw/)
  import-archive  merge like.js from an official X data export (tweet IDs + text only)
  enrich          visit archive-only tweets in the browser to fill in author / date / media
  categorize      classify tweets with Claude using categories.json -> data/categories.jsonl
  media           download photos (and --video) -> media/<tweet_id>/
  export          write out/: one markdown file per category, likes.csv, likes.json, index.html
  stats           print a summary of what is on disk
  reparse         rebuild likes.jsonl from data/raw/ after a parser change (no network)

Everything lives next to this file; override the location with XLIKES_HOME.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("XLIKES_HOME") or Path(__file__).resolve().parent)
# Your likes, media and exports live under XLIKES_DATA_HOME (default: next to the script). The demo
# (`./xlikes-view --demo`) points it at demo/, which ships made-up likes.
DATA_HOME = Path(os.environ.get("XLIKES_DATA_HOME") or HOME).resolve()
DATA = DATA_HOME / "data"
RAW = DATA / "raw"
MEDIA = DATA_HOME / "media"
OUT = DATA_HOME / "out"
LIKES_FILE = DATA / "likes.jsonl"
CATS_FILE = DATA / "categories.jsonl"
ARCHIVE_FILE = DATA / "archive_likes.json"
TAXONOMY_FILE = HOME / "categories.json"
PROFILE_DIR = HOME / ".browser-profile"
VIEWER_FILE = HOME / "viewer.html"  # the browsable page; export copies it to out/index.html

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
# Claude Code sets these in its shell; a nested `claude -p` refuses to start while they are present.
NESTED_CLAUDE_VARS = {
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_PID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_JOB_DIR",
    "CLAUDE_EFFORT", "CLAUDE_CODE_EXECPATH",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- storage

_write_lock = threading.Lock()


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # tolerate a torn last line from an interrupted run
    return out


def append_jsonl(path: Path, records) -> None:
    records = list(records)
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock, open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_jsonl(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def load_likes() -> dict[str, dict]:
    """id -> tweet. Later lines win, so appending an updated record upserts it."""
    likes: dict[str, dict] = {}
    for r in read_jsonl(LIKES_FILE):
        likes[r["id"]] = r
    return likes


def load_classifications() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in read_jsonl(CATS_FILE):
        out[r["id"]] = r
    return out


# --------------------------------------------------------------------------- GraphQL parsing

def graphql_op(url: str) -> str:
    return url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def find_instructions(obj):
    """Timeline responses differ in nesting (timeline_v2 vs timeline, user vs conversation);
    just find the first `instructions` list wherever it is."""
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            ins = o.get("instructions")
            if isinstance(ins, list):
                yield from ins
                continue
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)


def iter_timeline_entries(data):
    """Yield ("tweet", entry_id, result) or ("cursor", cursor_type, value) for top-level entries.
    Quoted / retweeted posts stay nested inside their parent result."""
    for instr in find_instructions(data):
        kind = instr.get("type")
        if kind == "TimelineAddEntries":
            entries = instr.get("entries") or []
        elif kind in ("TimelinePinEntry", "TimelineReplaceEntry"):
            entries = [instr.get("entry")]
        else:
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            eid, content = e.get("entryId", ""), e.get("content") or {}
            if eid.startswith("cursor-") or content.get("entryType") == "TimelineTimelineCursor":
                yield "cursor", content.get("cursorType"), content.get("value")
                continue
            item_contents = []
            if content.get("itemContent"):
                item_contents.append(content["itemContent"])
            for it in content.get("items") or []:
                ic = ((it or {}).get("item") or {}).get("itemContent")
                if ic:
                    item_contents.append(ic)
            for ic in item_contents:
                res = (ic.get("tweet_results") or {}).get("result")
                if res:
                    yield "tweet", eid, res


def parse_created_at(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").isoformat()
    except ValueError:
        return s


def user_of(res: dict) -> dict:
    u = ((res.get("core") or {}).get("user_results") or {}).get("result") or {}
    ul, uc = u.get("legacy") or {}, u.get("core") or {}  # X moved screen_name/name from legacy to core in 2025
    return {
        "id": u.get("rest_id"),
        "handle": uc.get("screen_name") or ul.get("screen_name") or "",
        "name": uc.get("name") or ul.get("name") or "",
        "bio": ul.get("description") or (u.get("profile_bio") or {}).get("description") or "",
        "verified": bool(u.get("is_blue_verified") or ul.get("verified")),
        "followers": ul.get("followers_count"),
    }


def media_entities(legacy: dict) -> list:
    return (legacy.get("extended_entities") or {}).get("media") or (legacy.get("entities") or {}).get("media") or []


def expand_text(text: str, legacy: dict, note: dict):
    """Replace t.co links with their targets, drop the trailing media links, unescape HTML."""
    urls = list((legacy.get("entities") or {}).get("urls") or [])
    urls += list((note.get("entity_set") or {}).get("urls") or [])
    links: list[str] = []
    for u in urls:
        short, full = u.get("url"), u.get("expanded_url")
        if short and full:
            text = text.replace(short, full)
            if full not in links:
                links.append(full)
    for m in media_entities(legacy):
        if m.get("url"):
            text = text.replace(m["url"], "")
    return html.unescape(text).strip(), links


def parse_media(legacy: dict) -> list[dict]:
    out = []
    for m in media_entities(legacy):
        mtype = {"photo": "photo", "video": "video", "animated_gif": "gif"}.get(m.get("type"), m.get("type") or "media")
        base = m.get("media_url_https") or ""
        item = {"type": mtype, "url": base, "thumb": base, "alt": m.get("ext_alt_text") or ""}
        if mtype == "photo" and "." in base.rsplit("/", 1)[-1]:
            stem, ext = base.rsplit(".", 1)
            item["url"] = f"{stem}?format={ext}&name=orig"
            item["thumb"] = f"{stem}?format={ext}&name=small"
        vi = m.get("video_info") or {}
        variants = [v for v in vi.get("variants") or [] if v.get("content_type") == "video/mp4" and v.get("url")]
        if variants:
            item["video_url"] = max(variants, key=lambda v: v.get("bitrate") or 0)["url"]
            if vi.get("duration_millis"):
                item["duration_s"] = round(vi["duration_millis"] / 1000, 1)
        out.append(item)
    return out


def normalize_tweet(res, depth: int = 0):
    """Flatten one GraphQL tweet result into our record shape; None for tombstones/unavailable."""
    if not isinstance(res, dict):
        return None
    if res.get("__typename") == "TweetWithVisibilityResults":
        res = res.get("tweet") or {}
    legacy = res.get("legacy")
    if not legacy:
        return None
    rt = (legacy.get("retweeted_status_result") or {}).get("result")
    if rt and depth == 0:  # a liked repost -> archive the original post
        inner = normalize_tweet(rt, depth)
        if inner:
            inner["retweeted_by"] = user_of(res).get("handle")
            return inner
    author = user_of(res)
    tid = str(res.get("rest_id") or legacy.get("id_str") or "")
    if not tid:
        return None
    note = ((res.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
    text, links = expand_text(note.get("text") or legacy.get("full_text") or "", legacy, note)
    quoted = None
    if depth == 0:
        q = normalize_tweet((res.get("quoted_status_result") or {}).get("result"), depth + 1)
        if q:
            quoted = {"id": q["id"], "url": q["url"], "author": q["author"]["handle"], "name": q["author"]["name"],
                      "text": q["text"], "media_count": len(q["media"])}
            # the client hides the quoted post's own link; it is just a pointer to `quoted`
            links = [l for l in links if q["id"] not in l]
            text = re.sub(r"\s*https?://\S*" + re.escape(q["id"]) + r"\S*", "", text)
    text = re.sub(r"[ \t]+\n", "\n", re.sub(r" {2,}", " ", text)).strip()  # gaps left by removed links
    views = (res.get("views") or {}).get("count")
    return {
        "id": tid,
        "url": f"https://x.com/{author['handle'] or 'i/web'}/status/{tid}",
        "created_at": parse_created_at(legacy.get("created_at")),
        "author": author,
        "text": text,
        "lang": legacy.get("lang"),
        "in_reply_to": legacy.get("in_reply_to_screen_name"),
        "media": parse_media(legacy),
        "links": links,
        "quoted": quoted,
        "metrics": {
            "likes": legacy.get("favorite_count"), "reposts": legacy.get("retweet_count"),
            "replies": legacy.get("reply_count"), "quotes": legacy.get("quote_count"),
            "bookmarks": legacy.get("bookmark_count"),
            "views": int(views) if str(views or "").isdigit() else None,
        },
    }


# --------------------------------------------------------------------------- browser

def browser_executable():
    if os.environ.get("XLIKES_BROWSER"):
        return os.environ["XLIKES_BROWSER"]
    if (Path.home() / ".cache" / "ms-playwright").exists():
        return None  # Playwright's own Chromium is installed; let it pick
    for c in ("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
              "/usr/bin/google-chrome-stable", "/usr/bin/brave"):
        if os.path.exists(c):
            return c
    return None


class Browser:
    """Chromium with a persistent profile in .browser-profile/, so the X login survives between runs.

    The `response` handler only queues responses; bodies are read on the main thread via drain()."""

    OPS = ("Likes", "TweetDetail")

    def __init__(self, headless: bool = False):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            sys.exit("Playwright is not installed. Run ./xlikes (it creates the venv) or: pip install playwright")
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(PROFILE_DIR, 0o700)
        self._pw = sync_playwright().start()
        kwargs = dict(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        if headless:
            kwargs["viewport"] = {"width": 1200, "height": 1700}
        else:
            kwargs["no_viewport"] = True  # a normal-looking window that follows the window manager's size
        exe = browser_executable()
        if exe:
            kwargs["executable_path"] = exe
        self._launch(kwargs)
        if headless:
            ua = self.page.evaluate("navigator.userAgent")
            if "Headless" in ua:  # x.com answers HeadlessChrome with an empty 4xx; relaunch as plain Chrome
                self.ctx.close()
                kwargs["user_agent"] = ua.replace("HeadlessChrome", "Chrome")
                self._launch(kwargs)
        self._pending: list = []
        self.page.on("response", lambda r: self._pending.append(r) if "/i/api/graphql/" in r.url else None)
        # A KeyboardInterrupt raised inside Playwright's sync machinery kills its event-loop greenlet and the
        # following close() spins forever. So Ctrl-C only sets a flag here; sleep()/wait_for() raise
        # KeyboardInterrupt between browser calls, where it is safe. A second Ctrl-C quits immediately.
        self._sigints = 0
        self._prev_sigint = signal.signal(signal.SIGINT, self._on_sigint)

    def _on_sigint(self, _signum, _frame) -> None:
        self._sigints += 1
        if self._sigints > 1:
            os._exit(130)
        log("\nStopping… (press Ctrl-C again to quit immediately)")

    def _check_interrupt(self) -> None:
        if self._sigints:
            raise KeyboardInterrupt

    def _launch(self, kwargs) -> None:
        self.ctx = self._pw.chromium.launch_persistent_context(**kwargs)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def close(self) -> None:
        for f in (self.ctx.close, self._pw.stop):
            try:
                f()
            except Exception:
                pass
        signal.signal(signal.SIGINT, self._prev_sigint)

    def drain(self):
        """Yield (op, status, json_or_None, headers) for the GraphQL responses we care about."""
        pending, self._pending = self._pending, []
        for r in pending:
            op = graphql_op(r.url)
            if op not in self.OPS:
                continue
            data = None
            if r.status == 200:
                try:
                    data = r.json()
                except Exception as e:
                    log(f"  ! could not read {op} response body: {e}")
            yield op, r.status, data, r.headers

    def wait_for(self, pred, timeout_s: float, poll_ms: int = 250) -> bool:
        deadline = time.time() + timeout_s
        while True:
            self._check_interrupt()
            if pred():
                return True
            if time.time() >= deadline:
                return False
            self.page.wait_for_timeout(poll_ms)

    def sleep(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while True:
            self._check_interrupt()
            left = deadline - time.time()
            if left <= 0:
                return
            # wait_for_timeout keeps Playwright's event loop running, unlike time.sleep; short chunks keep Ctrl-C snappy
            self.page.wait_for_timeout(int(min(left, 0.5) * 1000))

    def current_handle(self):
        try:
            el = self.page.query_selector('a[data-testid="AppTabBar_Profile_Link"]')
            href = el.get_attribute("href") if el else None
        except Exception:
            return None
        return href.strip("/").split("/")[0] if href else None

    def ensure_login(self, timeout_min: int) -> str:
        self.page.goto("https://x.com/home", wait_until="domcontentloaded")
        told = False
        deadline = time.time() + timeout_min * 60
        while time.time() < deadline:
            h = self.current_handle()
            if h:
                return h
            if not told:
                log(f"→ Not logged in. Log in to X in the browser window (this script never sees your password). "
                    f"Waiting up to {timeout_min} min…")
                told = True
            self.sleep(2)
        sys.exit("Timed out waiting for login.")

    def scroll_to_bottom(self) -> None:
        self.page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")

    def nudge(self) -> None:
        """X's virtual list sometimes needs a scroll-up/down to fire the next fetch."""
        self.page.evaluate("window.scrollBy(0, -2500)")
        self.sleep(0.5)
        self.scroll_to_bottom()

    def click_retry(self) -> bool:
        """After a failed fetch X shows 'Something went wrong' with a Retry button; scrolling alone does nothing."""
        try:
            btn = self.page.get_by_role("button", name=re.compile(r"^retry$", re.I))
            if btn.count():
                btn.first.click(timeout=2000)
                return True
        except Exception:
            pass
        return False


def rate_limit_wait(headers) -> float:
    reset = float(headers.get("x-rate-limit-reset") or 0)
    return max(30.0, min(reset - time.time() + 5, 16 * 60)) if reset else 120.0


# --------------------------------------------------------------------------- commands: browser

def cmd_login(args):
    b = Browser(headless=args.headless)
    try:
        h = b.ensure_login(args.login_timeout)
        log(f"✓ Logged in as @{h}. Session saved in {PROFILE_DIR} — scrape will not ask again.")
    finally:
        b.close()


def cmd_scrape(args):
    likes = load_likes()
    known = set(likes)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    RAW.mkdir(parents=True, exist_ok=True)
    raw_path = RAW / f"likes-{run_id}.jsonl.gz"
    st = {"pages": 0, "new": 0, "idx": 0, "empty": 0, "known_pages": 0, "rate_wait": 0.0, "errors": 0}
    t0 = time.time()

    def elapsed() -> str:
        return time.strftime("%H:%M:%S", time.gmtime(time.time() - t0))

    def process(raw) -> bool:
        """Consume queued Likes responses. True if any arrived (even an error)."""
        got = False
        for op, status, data, headers in b.drain():
            if op != "Likes":
                continue
            got = True
            if status == 429:
                st["rate_wait"] = rate_limit_wait(headers)
                continue
            if status != 200 or data is None:
                st["errors"] += 1
                log(f"  ! Likes → HTTP {status}")
                continue
            raw.write(json.dumps({"ts": now_iso(), "run": run_id, "op": op, "data": data}) + "\n")
            new, total = [], 0
            for kind, _eid, res in iter_timeline_entries(data):
                if kind != "tweet":
                    continue
                total += 1
                t = normalize_tweet(res)
                if not t:
                    continue
                t.update(source="scrape", run=run_id, liked_index=st["idx"], captured_at=now_iso())
                st["idx"] += 1
                if t["id"] not in known:
                    known.add(t["id"])
                    new.append(t)
            append_jsonl(LIKES_FILE, new)
            st["pages"] += 1
            st["new"] += len(new)
            st["empty"] = st["empty"] + 1 if total == 0 else 0
            st["known_pages"] = st["known_pages"] + 1 if (total and not new) else 0
            log(f"  page {st['pages']:>4} · {len(new):>3} new of {total:>2} · saved {len(known):,} · {elapsed()}")
        return got

    b = Browser(headless=args.headless)
    try:
        handle = args.handle or b.ensure_login(args.login_timeout)
        log(f"✓ Logged in as @{handle} · {len(known):,} likes already on disk · run {run_id}")
        with gzip.open(raw_path, "at", encoding="utf-8") as raw:
            b.page.goto(f"https://x.com/{handle}/likes", wait_until="domcontentloaded")
            if not b.wait_for(lambda: process(raw), 30):
                sys.exit("The Likes timeline never loaded — check the browser window (wrong --handle? logged out?).")
            idle = 0
            while True:
                if st["rate_wait"]:
                    log(f"  ⏳ rate limited by X — pausing {int(st['rate_wait'])}s")
                    b.sleep(st["rate_wait"])
                    st["rate_wait"] = 0.0
                    b.click_retry()
                if st["empty"] >= 2:
                    log("✓ Reached the end of the Likes timeline.")
                    break
                if args.incremental and st["known_pages"] >= 3:
                    log("✓ Caught up with previously saved likes.")
                    break
                if args.limit and st["new"] >= args.limit:
                    log(f"✓ Reached --limit {args.limit}.")
                    break
                b.scroll_to_bottom()
                if b.wait_for(lambda: process(raw), args.wait):
                    idle = 0
                else:
                    idle += 1
                    if idle >= args.max_idle:
                        log(f"✓ Nothing new after {idle} scrolls — assuming the end of the timeline.")
                        break
                    if not b.click_retry() and idle % 2 == 0:
                        b.nudge()
                b.sleep(args.delay)
    except KeyboardInterrupt:
        log("\nInterrupted — progress is saved; run scrape again to continue.")
    finally:
        b.close()
    log(f"Done: {st['new']:,} new likes this run · {len(known):,} total in {LIKES_FILE}")


def cmd_enrich(args):
    likes = load_likes()
    todo = [t for t in likes.values() if t.get("source") == "archive" and not t.get("unavailable")]
    todo.sort(key=lambda t: t.get("archive_index", 0))
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        log("Nothing to enrich — no archive-only records without author data.")
        return
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    RAW.mkdir(parents=True, exist_ok=True)
    b = Browser(headless=args.headless)
    filled = gone = 0
    try:
        b.ensure_login(args.login_timeout)
        log(f"Enriching {len(todo):,} archive-only likes by visiting each post…")
        with gzip.open(RAW / f"detail-{run_id}.jsonl.gz", "at", encoding="utf-8") as raw:
            i = 0
            while i < len(todo):
                t = todo[i]
                list(b.drain())  # discard anything left over from the previous page
                b.page.goto(f"https://x.com/i/web/status/{t['id']}", wait_until="domcontentloaded")
                found = {"tweet": None, "answered": False, "rate_wait": 0.0}

                def check() -> bool:
                    for op, status, data, headers in b.drain():
                        if op != "TweetDetail":
                            continue
                        if status == 429:
                            found["rate_wait"] = rate_limit_wait(headers)
                            return True
                        if status != 200 or data is None:
                            continue
                        raw.write(json.dumps({"ts": now_iso(), "run": run_id, "op": op, "target": t["id"], "data": data}) + "\n")
                        found["answered"] = True
                        for kind, _e, res in iter_timeline_entries(data):
                            if kind == "tweet":
                                n = normalize_tweet(res)
                                if n and n["id"] == t["id"]:
                                    found["tweet"] = n
                        return True
                    return False

                b.wait_for(check, 12)
                if found["rate_wait"]:
                    log(f"  ⏳ rate limited by X — pausing {int(found['rate_wait'])}s")
                    b.sleep(found["rate_wait"])
                    continue  # retry the same tweet
                if found["tweet"]:
                    rec = found["tweet"]
                    rec.update(source="enrich", archive_index=t.get("archive_index"), archive_text=t.get("text"),
                               captured_at=now_iso())
                    append_jsonl(LIKES_FILE, [rec])
                    filled += 1
                elif found["answered"]:  # X answered but the post is gone or protected
                    append_jsonl(LIKES_FILE, [dict(t, unavailable=True, checked_at=now_iso())])
                    gone += 1
                else:
                    log(f"  ! no TweetDetail response for {t['id']} — will retry next run")
                i += 1
                if i % 10 == 0 or i == len(todo):
                    log(f"  {i:,}/{len(todo):,} · {filled:,} filled · {gone:,} unavailable")
                b.sleep(args.delay)
    except KeyboardInterrupt:
        log("\nInterrupted — progress is saved; run enrich again to continue.")
    finally:
        b.close()
    log(f"Done: {filled:,} enriched, {gone:,} unavailable.")


# --------------------------------------------------------------------------- commands: archive import

def read_archive_likes(path: Path) -> list[dict]:
    blobs = []
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if re.search(r"(^|/)like(-part\d+)?\.js$", n):
                    blobs.append(z.read(n).decode("utf-8"))
    elif path.is_dir():
        blobs = [p.read_text(encoding="utf-8") for p in sorted(path.rglob("like*.js"))]
    else:
        blobs = [path.read_text(encoding="utf-8")]
    if not blobs:
        sys.exit(f"No like.js found in {path}")
    out = []
    for blob in blobs:
        payload = blob[blob.index("=") + 1:]  # strip the "window.YTD.like.part0 =" prefix
        for item in json.loads(payload):
            out.append(item.get("like", item))
    return out


def archive_record(entry: dict, index: int) -> dict:
    tid = str(entry.get("tweetId") or "")
    return {
        "id": tid,
        "url": entry.get("expandedUrl") or f"https://x.com/i/web/status/{tid}",
        "created_at": None, "author": None,
        "text": html.unescape(entry.get("fullText") or ""),
        "media": [], "links": [], "quoted": None,
        "source": "archive", "archive_index": index, "captured_at": now_iso(),
    }


def cmd_import_archive(args):
    entries = read_archive_likes(Path(args.path))
    previous = json.loads(ARCHIVE_FILE.read_text(encoding="utf-8")) if ARCHIVE_FILE.exists() else []
    merged = {str(e.get("tweetId")): e for e in previous}
    merged.update({str(e.get("tweetId")): e for e in entries})
    DATA.mkdir(parents=True, exist_ok=True)
    ARCHIVE_FILE.write_text(json.dumps(list(merged.values()), ensure_ascii=False), encoding="utf-8")
    likes = load_likes()
    added = [archive_record(e, i) for i, e in enumerate(entries)
             if str(e.get("tweetId") or "") and str(e.get("tweetId")) not in likes]
    append_jsonl(LIKES_FILE, added)
    log(f"Archive lists {len(entries):,} likes; {len(added):,} were not on disk yet and were added as text-only records.")
    if added:
        log("Run `enrich` to fill in author / date / media for them.")


# --------------------------------------------------------------------------- commands: categorize

def load_taxonomy():
    if not TAXONOMY_FILE.exists():
        sys.exit(f"{TAXONOMY_FILE} is missing")
    raw = json.loads(TAXONOMY_FILE.read_text(encoding="utf-8"))
    cats = raw["categories"] if isinstance(raw, dict) else raw
    notes = raw.get("notes", []) if isinstance(raw, dict) else []
    names = [c["name"] for c in cats]
    tax_hash = hashlib.sha1(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:10]
    return cats, notes, names, tax_hash


def compact(t: dict) -> dict:
    """The few fields the classifier needs, trimmed to keep batches small."""
    a = t.get("author") or {}
    d = {"id": t["id"]}
    if a.get("handle"):
        d["author"] = f"@{a['handle']} ({a.get('name', '')})".strip()
    if a.get("bio"):
        d["bio"] = a["bio"][:120]
    d["text"] = (t.get("text") or "")[:600]
    media = t.get("media") or []
    if media:
        kinds = Counter(m["type"] for m in media)
        desc = ", ".join(f"{n} {k}{'s' if n > 1 else ''}" for k, n in kinds.items())
        alts = [m["alt"] for m in media if m.get("alt")]
        if alts:
            desc += "; alt: " + " | ".join(alts)[:300]
        d["media"] = desc
    if t.get("quoted"):
        d["quoted"] = f"@{t['quoted']['author']}: {t['quoted']['text'][:250]}"
    if t.get("links"):
        d["links"] = [l[:90] for l in t["links"][:4]]
    return d


def build_prompt(cats, notes, batch) -> str:
    lines = [
        "You are organizing a personal archive of posts someone liked on X/Twitter.",
        "Assign each post exactly ONE category from this list (use the name verbatim):",
        "",
        *[f"- {c['name']}: {c['description']}" for c in cats],
    ]
    if notes:
        lines += ["", "Tie-break rules:", *[f"- {n}" for n in notes]]
    lines += [
        "",
        'Also give up to 3 short lowercase tags per post (specific topics, e.g. "shaders", "llm", "typography") '
        "and a confidence between 0 and 1.",
        "Use the author's handle and bio, alt text, quoted post and link domains as extra context — "
        "many art and graphics posts have little text.",
        "Do not use any tools. Respond with ONLY a JSON array — no prose, no code fences — one object per post, "
        "every id exactly once:",
        '[{"id": "123", "category": "AI", "tags": ["llm", "agents"], "confidence": 0.9}]',
        "",
        "Posts:",
        json.dumps([compact(t) for t in batch], ensure_ascii=False),
    ]
    return "\n".join(lines)


def parse_classification(text: str, ids: set, names: list, fallback: str) -> dict:
    s, e = text.find("["), text.rfind("]")
    if s < 0 or e <= s:
        raise ValueError(f"no JSON array in model output: {text[:200]!r}")
    lower = {n.lower(): n for n in names}
    out = {}
    for item in json.loads(text[s:e + 1]):
        if not isinstance(item, dict) or str(item.get("id")) not in ids:
            continue
        cat = lower.get(str(item.get("category", "")).strip().lower()) or fallback
        tags = [str(x).strip().lower()[:30] for x in (item.get("tags") or []) if str(x).strip()][:3]
        conf = item.get("confidence")
        out[str(item["id"])] = {
            "id": str(item["id"]), "category": cat, "tags": tags,
            "confidence": round(float(conf), 2) if isinstance(conf, (int, float)) else None,
        }
    return out


class ClaudeCLI:
    """Uses your Claude Code login via `claude -p`; no API key needed."""
    name = "claude -p"

    def __init__(self, model):
        self.model = model
        if not shutil.which("claude"):
            sys.exit("`claude` is not on PATH; install Claude Code or set ANTHROPIC_API_KEY to use the API instead.")

    def complete(self, prompt: str) -> str:
        # no tools (pure text classification) and no saved session per batch
        cmd = ["claude", "-p", "--output-format", "json", "--tools", "", "--no-session-persistence"]
        if self.model:
            cmd += ["--model", self.model]
        env = {k: v for k, v in os.environ.items() if k not in NESTED_CLAUDE_VARS}
        r = subprocess.run(cmd, input=prompt, text=True, capture_output=True, env=env, timeout=900)
        if r.returncode != 0:
            raise RuntimeError(f"claude exited {r.returncode}: {(r.stderr or r.stdout).strip()[:400]}")
        try:
            out = json.loads(r.stdout)
        except json.JSONDecodeError:
            return r.stdout
        if isinstance(out, dict) and out.get("is_error"):
            raise RuntimeError(f"claude error: {str(out.get('result'))[:400]}")
        return out.get("result", "") if isinstance(out, dict) else r.stdout


class AnthropicAPI:
    name = "Anthropic API"

    def __init__(self, model, key):
        self.model, self.key = model, key

    def complete(self, prompt: str) -> str:
        body = json.dumps({"model": self.model, "max_tokens": 8192, "temperature": 0,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    data = json.loads(resp.read())
                return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            except urllib.error.HTTPError as e:
                if e.code in (429, 529) or e.code >= 500:
                    time.sleep(3 * 2 ** attempt)
                    continue
                raise RuntimeError(f"API HTTP {e.code}: {e.read()[:300]!r}")
        raise RuntimeError("API kept returning 429/5xx")


class NVInference:
    """NVIDIA Inference Hub (OpenAI-compatible chat API), same endpoint and key file as pi's
    nv-inference provider (~/.pi/agent/models.json). Key file: $NV_INFERENCE_KEY_FILE or the default."""
    name = "nv-inference"
    url = os.environ.get("NV_INFERENCE_URL", "https://inference-api.nvidia.com/v1") + "/chat/completions"

    def __init__(self, model):
        self.model = model or "aws/anthropic/bedrock-claude-haiku-5-5"
        kf = os.path.expanduser(os.environ.get("NV_INFERENCE_KEY_FILE", "~/.config/nemoclaw-secrets/nvidia_inference_hub_key"))
        try:
            self.key = open(kf).read().strip()
        except OSError:
            sys.exit(f"nv-inference key file not found: {kf} (or use --backend cli)")

    def complete(self, prompt: str) -> str:
        body = json.dumps({"model": self.model, "max_tokens": 8192, "temperature": 0,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request(self.url, data=body, headers={
            "authorization": f"Bearer {self.key}", "content-type": "application/json"})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    data = json.loads(resp.read())
                return data["choices"][0]["message"]["content"] or ""
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    time.sleep(3 * 2 ** attempt)
                    continue
                raise RuntimeError(f"nv-inference HTTP {e.code}: {e.read()[:300]!r}")
        raise RuntimeError("nv-inference kept returning 429/5xx")


def pick_backend(args):
    if args.backend in ("nv", "auto"):
        return NVInference(args.model)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if args.backend == "api" or (args.backend == "auto" and key):
        if not key:
            sys.exit("ANTHROPIC_API_KEY is not set")
        return AnthropicAPI(args.model or "claude-sonnet-5", key)
    return ClaudeCLI(args.model)


def cmd_categorize(args):
    cats, notes, names, tax_hash = load_taxonomy()
    likes, done = load_likes(), load_classifications()
    todo = [t for t in likes.values()
            if (args.all or done.get(t["id"], {}).get("taxonomy") != tax_hash)
            and (t.get("text") or t.get("media") or t.get("quoted"))]
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        log("Nothing to categorize — everything is classified under the current categories.json.")
        return
    backend = pick_backend(args)
    batches = [todo[i:i + args.batch_size] for i in range(0, len(todo), args.batch_size)]
    log(f"Categorizing {len(todo):,} tweets in {len(batches)} batches via {backend.name}"
        f"{' (' + backend.model + ')' if backend.model else ''} into: {', '.join(names)}")
    if args.dry_run:
        print(build_prompt(cats, notes, batches[0]))
        return
    fallback = "Other" if "Other" in names else names[-1]
    workers = args.workers or (4 if isinstance(backend, AnthropicAPI) else 3)
    counts: Counter = Counter()
    n_done = failures = 0

    def work(batch):
        return parse_classification(backend.complete(build_prompt(cats, notes, batch)), {t["id"] for t in batch}, names, fallback)

    with ThreadPoolExecutor(workers) as ex:
        futures = {ex.submit(work, b): b for b in batches}
        for fut in as_completed(futures):
            batch = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                failures += 1
                log(f"  ! batch of {len(batch)} failed: {str(e)[:300]}")
                continue
            recs = [dict(result[t["id"]], taxonomy=tax_hash, model=backend.model or "default", at=now_iso())
                    for t in batch if t["id"] in result]
            append_jsonl(CATS_FILE, recs)
            counts.update(r["category"] for r in recs)
            n_done += len(recs)
            missing = len(batch) - len(recs)
            log(f"  {n_done:>6,}/{len(todo):,} classified" + (f" · {missing} missing in this batch (retried next run)" if missing else ""))
    summary = ", ".join(f"{k} {v}" for k, v in counts.most_common())
    log(f"Done: {summary}" + (f" · {failures} failed batch(es) — re-run to retry" if failures else ""))


# --------------------------------------------------------------------------- commands: media

def download(url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as resp, open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f)
            os.replace(tmp, dest)
            return True
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return False  # gone; don't retry
            time.sleep(5 * (attempt + 1))
        except Exception:
            time.sleep(2 * (attempt + 1))
    tmp.unlink(missing_ok=True)
    return False


def cmd_media(args):
    likes = load_likes()
    jobs = []
    for t in likes.values():
        d = MEDIA / t["id"]
        for i, m in enumerate(t.get("media") or [], 1):
            if m["type"] == "photo":
                fmt = re.search(r"format=(\w+)", m["url"])
                jobs.append((m["url"], d / f"{i}.{fmt.group(1) if fmt else 'jpg'}"))
            else:
                if m.get("url"):
                    jobs.append((m["url"], d / f"{i}_thumb.jpg"))
                if args.video and m.get("video_url"):
                    jobs.append((m["video_url"], d / f"{i}.mp4"))
    jobs = [(u, p) for u, p in jobs if u and not p.exists()]
    if not jobs:
        log("All media already downloaded.")
        return
    log(f"Downloading {len(jobs):,} files to {MEDIA} with {args.workers} workers…")
    ok = fail = 0
    with ThreadPoolExecutor(args.workers) as ex:
        for i, success in enumerate(ex.map(lambda j: download(*j), jobs), 1):
            ok, fail = ok + success, fail + (not success)
            if i % 100 == 0 or i == len(jobs):
                log(f"  {i:,}/{len(jobs):,} · {fail} failed")
    log(f"Done: {ok:,} downloaded, {fail:,} failed.")


# --------------------------------------------------------------------------- commands: export / stats

def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "Other"


def local_media(tid: str, i: int):
    """(main, thumb) paths relative to DATA_HOME for media item i, or None if not downloaded."""
    d = MEDIA / tid
    if not d.is_dir():
        return None, None
    main = next(iter(sorted(d.glob(f"{i}.*"))), None)
    thumb = next(iter(sorted(d.glob(f"{i}_thumb.*"))), None)
    return (main.relative_to(DATA_HOME) if main else None), (thumb.relative_to(DATA_HOME) if thumb else None)


def merged_rows(sort: str):
    likes, cats = load_likes(), load_classifications()
    rows = [(t, cats.get(t["id"])) for t in likes.values()]
    if sort == "liked":  # newest run first, then position in the timeline; archive-only records last
        rows.sort(key=lambda tc: (-int((tc[0].get("run") or "0").replace("-", "")),
                                  tc[0].get("liked_index", 10 ** 9), tc[0].get("archive_index", 10 ** 9)))
    else:
        rows.sort(key=lambda tc: tc[0].get("created_at") or "", reverse=True)
    return rows


def md_tweet(t: dict, c) -> str:
    a = t.get("author") or {}
    date = (t.get("created_at") or "")[:10]
    head = f"### [@{a.get('handle') or '?'}]({t['url']})"
    if a.get("name"):
        head += f" · {a['name']}"
    if date:
        head += f" · {date}"
    lines = [head, ""]
    if t.get("text"):
        lines += ["> " + l for l in t["text"].splitlines()] + [""]
    if t.get("quoted"):
        q = t["quoted"]
        lines += [f"> ↳ **@{q['author']}**: {q['text'].replace(chr(10), ' ')[:300]}", ""]
    for i, m in enumerate(t.get("media") or [], 1):
        main, thumb = local_media(t["id"], i)
        alt = (m.get("alt") or "").replace("]", ")").replace("\n", " ")[:80]
        if m["type"] == "photo":
            lines.append(f"![{alt}]({'../' + str(main) if main else m.get('thumb') or m['url']})")
        else:
            link = ("../" + str(main)) if main else (m.get("video_url") or m["url"])
            lines.append(f"🎬 [{m['type']}]({link})" + (f" ![]({'../' + str(thumb)})" if thumb else ""))
    if t.get("media"):
        lines.append("")
    meta = []
    if c:
        if c.get("tags"):
            meta.append(" ".join(f"`{x}`" for x in c["tags"]))
        if c.get("confidence") is not None:
            meta.append(f"conf {c['confidence']:.2f}")
    meta += [f"🔗 <{l}>" for l in t.get("links") or []]
    if meta:
        lines += [" · ".join(meta), ""]
    lines += ["---", ""]
    return "\n".join(lines)


def viewer_row(t: dict, c) -> dict:
    a = t.get("author") or {}
    image = video = None
    for i, m in enumerate(t.get("media") or [], 1):
        main, thumb = local_media(t["id"], i)
        if m["type"] == "photo":
            image = image or (f"../{main}" if main else m.get("thumb"))
        else:
            image = image or (f"../{thumb}" if thumb else m.get("thumb"))
            video = video or (f"../{main}" if main else m.get("video_url"))
    return {
        "id": t["id"], "url": t["url"], "created_at": t.get("created_at"),
        "author": a.get("handle") or "", "name": a.get("name") or "", "text": t.get("text") or "",
        "liked_index": t.get("liked_index"),
        "category": (c or {}).get("category") or "Uncategorized", "tags": (c or {}).get("tags") or [],
        "confidence": (c or {}).get("confidence"),
        "image": image, "video": video, "media_count": len(t.get("media") or []),
        "quoted": {"author": t["quoted"]["author"], "text": t["quoted"]["text"]} if t.get("quoted") else None,
        "links": t.get("links") or [],
        "source": t.get("site") or "x",
    }


def cmd_export(args):
    rows = merged_rows(args.sort)
    if not rows:
        sys.exit("No likes on disk yet — run scrape first.")
    OUT.mkdir(parents=True, exist_ok=True)
    by_cat: dict[str, list] = defaultdict(list)
    for t, c in rows:
        by_cat[(c or {}).get("category") or "Uncategorized"].append((t, c))

    for cat, items in by_cat.items():
        body = [f"# {cat} — {len(items):,} likes", ""] + [md_tweet(t, c) for t, c in items]
        (OUT / f"{safe_name(cat)}.md").write_text("\n".join(body), encoding="utf-8")

    n_cat = sum(1 for _, c in rows if c)
    n_media = sum(1 for t, _ in rows if t.get("media"))
    dates = sorted(t["created_at"][:10] for t, _ in rows if t.get("created_at"))
    authors = Counter((t.get("author") or {}).get("handle") for t, _ in rows if (t.get("author") or {}).get("handle"))
    index = [f"# X likes archive", "",
             f"Generated {datetime.now():%Y-%m-%d %H:%M} · {len(rows):,} likes · {n_cat:,} categorized · "
             f"{n_media:,} with media" + (f" · posts from {dates[0]} to {dates[-1]}" if dates else ""), "",
             "Open [index.html](index.html) for a browsable gallery.", "",
             "| Category | Likes | |", "|---|---:|---|"]
    for cat, items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        index.append(f"| {cat} | {len(items):,} | [{safe_name(cat)}.md]({safe_name(cat)}.md) |")
    index += ["", "## Most-liked authors", "", "| Author | Likes |", "|---|---:|"]
    index += [f"| [@{h}](https://x.com/{h}) | {n:,} |" for h, n in authors.most_common(25)]
    (OUT / "README.md").write_text("\n".join(index) + "\n", encoding="utf-8")

    with open(OUT / "likes.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "url", "created_at", "author", "name", "category", "tags", "confidence",
                    "text", "media", "links", "source"])
        for t, c in rows:
            a = t.get("author") or {}
            w.writerow([t["id"], t["url"], t.get("created_at") or "", a.get("handle") or "", a.get("name") or "",
                        (c or {}).get("category") or "", " ".join((c or {}).get("tags") or []),
                        (c or {}).get("confidence") if c else "",
                        (t.get("text") or "").replace("\n", " "),
                        " ".join(m.get("video_url") or m["url"] for m in t.get("media") or []),
                        " ".join(t.get("links") or []), t.get("source") or ""])

    with open(OUT / "likes.json", "w", encoding="utf-8") as f:
        json.dump([dict(t, classification=c) for t, c in rows], f, ensure_ascii=False, indent=1)

    # the browsable page: viewer.html (static, edit freely) + likes.js (the data). Two files rather than one
    # so the page stays readable; <script src> works from file://, unlike fetch().
    if not VIEWER_FILE.exists():
        sys.exit(f"{VIEWER_FILE} is missing; it is the page that {OUT}/index.html is copied from.")
    viewer = {"generated": now_iso(), "categories": load_taxonomy()[2], "rows": [viewer_row(t, c) for t, c in rows]}
    payload = json.dumps(viewer, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    (OUT / "likes.js").write_text("window.XLIKES = " + payload + ";\n", encoding="utf-8")
    shutil.copyfile(VIEWER_FILE, OUT / "index.html")
    log(f"Wrote {OUT}/: README.md, {len(by_cat)} category .md files, likes.csv, likes.json, index.html + likes.js "
        f"({len(rows):,} likes, {len(rows) - n_cat:,} uncategorized)")


def cmd_stats(args):
    likes, cats = load_likes(), load_classifications()
    if not likes:
        log(f"No likes on disk yet ({LIKES_FILE}).")
        return
    src = Counter(t.get("source") for t in likes.values())
    log(f"{len(likes):,} likes in {LIKES_FILE}  ({', '.join(f'{k}: {v:,}' for k, v in src.items())})")
    with_media = sum(1 for t in likes.values() if t.get("media"))
    n_video = sum(1 for t in likes.values() if any(m["type"] != "photo" for m in t.get("media") or []))
    log(f"  {with_media:,} with media ({n_video:,} with video/gif) · "
        f"{sum(1 for t in likes.values() if t.get('unavailable')):,} unavailable")
    dates = sorted(t["created_at"][:10] for t in likes.values() if t.get("created_at"))
    if dates:
        log(f"  posts dated {dates[0]} → {dates[-1]}")
    labelled = {i: c for i, c in cats.items() if i in likes}
    log(f"{len(labelled):,} categorized · {len(likes) - len(labelled):,} not yet")
    for cat, n in Counter(c["category"] for c in labelled.values()).most_common():
        log(f"  {cat:<12} {n:>6,}")
    authors = Counter((t.get("author") or {}).get("handle") for t in likes.values() if t.get("author"))
    if authors:
        log("Top authors: " + ", ".join(f"@{h} ({n})" for h, n in authors.most_common(10)))


def cmd_reparse(args):
    existing = load_likes()
    rebuilt: dict[str, dict] = {}
    for path in sorted(RAW.glob("*.jsonl.gz")):
        idx = 0
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for kind, _e, res in iter_timeline_entries(rec.get("data") or {}):
                    if kind != "tweet":
                        continue
                    t = normalize_tweet(res)
                    if not t:
                        continue
                    if rec.get("op") == "Likes":
                        t.update(source="scrape", run=rec.get("run"), liked_index=idx, captured_at=rec.get("ts"))
                        idx += 1
                        rebuilt.setdefault(t["id"], t)  # earliest capture keeps the original like order
                    elif rec.get("op") == "TweetDetail" and t["id"] == rec.get("target"):
                        old = existing.get(t["id"]) or {}
                        t.update(source="enrich", archive_index=old.get("archive_index"),
                                 archive_text=old.get("archive_text") or old.get("text"), captured_at=rec.get("ts"))
                        rebuilt.setdefault(t["id"], t)
    if ARCHIVE_FILE.exists():
        for i, e in enumerate(json.loads(ARCHIVE_FILE.read_text(encoding="utf-8"))):
            tid = str(e.get("tweetId") or "")
            if tid and tid not in rebuilt:
                old = existing.get(tid) or {}
                rec = archive_record(e, i)
                if old.get("unavailable"):
                    rec.update(unavailable=True, checked_at=old.get("checked_at"))
                rebuilt[tid] = rec
    for tid, t in existing.items():  # never drop something we cannot rebuild
        rebuilt.setdefault(tid, t)
    if LIKES_FILE.exists():
        shutil.copy2(LIKES_FILE, LIKES_FILE.with_suffix(".jsonl.bak"))
    write_jsonl(LIKES_FILE, rebuilt.values())
    log(f"Rebuilt {len(rebuilt):,} likes from {RAW} (previous file kept as likes.jsonl.bak).")


# --------------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def browser_opts(p):
        p.add_argument("--headless", action="store_true", help="no browser window (only once you have logged in)")
        p.add_argument("--login-timeout", type=int, default=15, metavar="MIN", help="minutes to wait for login (15)")
        p.add_argument("--handle", help="your @handle; auto-detected from the page when omitted")

    p = sub.add_parser("login", help="open the browser, log in once, print your handle")
    browser_opts(p)
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("scrape", help="scroll your Likes page and save every tweet")
    browser_opts(p)
    p.add_argument("--incremental", action="store_true", help="stop once we reach already-saved likes")
    p.add_argument("--limit", type=int, default=0, help="stop after N new tweets (0 = all)")
    p.add_argument("--delay", type=float, default=1.2, help="seconds between scrolls (1.2)")
    p.add_argument("--wait", type=float, default=6.0, help="seconds to wait for X to answer a scroll (6)")
    p.add_argument("--max-idle", type=int, default=8, help="give up after N silent scrolls (8)")
    p.set_defaults(func=cmd_scrape)

    import reddit_likes
    reddit_likes.add_commands(sub)

    p = sub.add_parser("import-archive", help="merge like.js from an X data export (file, folder or .zip)")
    p.add_argument("path")
    p.set_defaults(func=cmd_import_archive)

    p = sub.add_parser("enrich", help="fill in author/date/media for archive-only likes via the browser")
    browser_opts(p)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--delay", type=float, default=2.0, help="seconds between posts (2)")
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("categorize", help="classify likes into categories.json with Claude")
    p.add_argument("--backend", choices=["auto", "nv", "cli", "api"], default="auto",
                   help="auto/nv = NVIDIA Inference Hub (default model aws/anthropic/bedrock-claude-haiku-5-5); cli = `claude -p`; api = Anthropic API")
    p.add_argument("--model", help="model name (API default claude-sonnet-5; CLI uses your default)")
    p.add_argument("--batch-size", type=int, default=40)
    p.add_argument("--workers", type=int, default=0, help="parallel requests (API 4, CLI 3)")
    p.add_argument("--all", action="store_true", help="re-classify everything, not just new/changed")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry-run", action="store_true", help="print the first batch's prompt and exit")
    p.set_defaults(func=cmd_categorize)

    p = sub.add_parser("media", help="download photos (and --video) into media/<tweet_id>/")
    p.add_argument("--video", action="store_true", help="also download mp4s (large)")
    p.add_argument("--workers", type=int, default=6)
    p.set_defaults(func=cmd_media)

    p = sub.add_parser("export", help="write out/ (markdown per category, CSV, JSON, HTML gallery)")
    p.add_argument("--sort", choices=["created", "liked"], default="created",
                   help="created = newest post first; liked = order you liked them in")
    p.set_defaults(func=cmd_export)

    sub.add_parser("stats", help="summarize what is on disk").set_defaults(func=cmd_stats)
    sub.add_parser("reparse", help="rebuild likes.jsonl from data/raw/").set_defaults(func=cmd_reparse)

    args = ap.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        # scrape/enrich handle Ctrl-C themselves (they report saved progress); this covers the rest.
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        # Playwright raises TargetClosedError when the window is closed under us; no traceback needed.
        # (The class is not exported from playwright.sync_api in every version, so match on name/message.)
        if type(e).__module__.startswith("playwright") and (
                type(e).__name__ == "TargetClosedError" or "has been closed" in str(e)):
            sys.exit("\nThe browser window was closed. Nothing is lost — run the same command again when you're ready.")
        raise


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # `./xlikes stats | head` should not traceback
    main()
