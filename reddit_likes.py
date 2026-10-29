"""Reddit upvoted + saved posts/comments, stored as likes (site=reddit) next to the X likes.

Uses your Brave's Reddit login via brave-copy (a headless copy of your Brave profile, deleted afterwards;
`--cdp PORT`), or else its own Chromium profile (.browser-profile-reddit/, `reddit-login`), and reads Reddit's own
/user/<you>/upvoted.json and /saved.json listings with that session's cookies."""
import os
import sys
import time
from datetime import datetime, timezone

import x_likes as X

PROFILE_DIR = X.HOME / ".browser-profile-reddit"
LAPSED_EXIT = 3


def open_ctx(headless: bool):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright is not installed. Run ./xlikes (it creates the venv).")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(PROFILE_DIR, 0o700)
    pw = sync_playwright().start()
    kw = dict(user_data_dir=str(PROFILE_DIR), headless=headless, user_agent=X.UA,
              args=["--disable-blink-features=AutomationControlled"], ignore_default_args=["--enable-automation"])
    if not headless:
        kw["no_viewport"] = True
    exe = X.browser_executable()
    if exe:
        kw["executable_path"] = exe
    return pw, pw.chromium.launch_persistent_context(**kw)


def open_cdp(port: int):
    """Attach to an already running (headless) Brave, e.g. brave-copy's copy of your Brave profile."""
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    br = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    return pw, br.contexts[0]


def get_json(ctx, url: str):
    """(status, json or None) fetched with the session's cookies."""
    r = ctx.request.get(url, headers={"accept": "application/json"})
    try:
        return r.status, (r.json() if r.status == 200 else None)
    except Exception:
        return r.status, None


def me(ctx):
    """Your username from the logged-in session, or None."""
    try:
        st, d = get_json(ctx, "https://www.reddit.com/api/me.json")
        return ((d or {}).get("data") or {}).get("name") if st == 200 else None
    except Exception:
        return None


def listing(ctx, user: str, kind: str, after=None) -> dict:
    url = f"https://www.reddit.com/user/{user}/{kind}.json?limit=100&raw_json=1" + (f"&after={after}" if after else "")
    for attempt in range(6):
        st, d = get_json(ctx, url)
        if st == 429:
            time.sleep(10 * (attempt + 1))
            continue
        if st != 200 or not d:
            raise RuntimeError(f"Reddit {kind} → HTTP {st}")
        return d["data"]
    raise RuntimeError("Reddit kept answering 429")


def normalize(thing: dict):
    k, d = thing.get("kind"), thing.get("data") or {}
    if k not in ("t3", "t1"):
        return None
    created = datetime.fromtimestamp(d["created_utc"], timezone.utc).isoformat() if d.get("created_utc") else None
    sub = d.get("subreddit") or ""
    media, links, quoted = [], [], None
    url = "https://www.reddit.com" + (d.get("permalink") or "")
    if k == "t3":
        body = d.get("selftext") or ""
        text = (d.get("title") or "") + ("\n\n" + body if body else "")
        imgs = (d.get("preview") or {}).get("images") or []
        thumb = imgs[0].get("source", {}).get("url") if imgs else None
        if not thumb and str(d.get("thumbnail", "")).startswith("http"):
            thumb = d["thumbnail"]
        if thumb:
            vid = ((d.get("secure_media") or d.get("media") or {}).get("reddit_video") or {}).get("fallback_url")
            if d.get("is_video") or vid:
                media.append({"type": "video", "url": thumb, "thumb": thumb, "alt": "", "video_url": vid})
            else:
                media.append({"type": "photo", "url": thumb, "thumb": thumb, "alt": ""})
        if not d.get("is_self") and d.get("url") and "reddit.com" not in d["url"] and "redd.it" not in d["url"]:
            links.append(d["url"])
    else:
        text = d.get("body") or ""
        quoted = {"author": f"r/{sub}", "text": d.get("link_title") or ""}
    return {
        "id": f"reddit_{d.get('name')}", "site": "reddit", "url": url, "created_at": created,
        "author": {"handle": d.get("author") or "[deleted]", "name": f"r/{sub}", "bio": ""},
        "text": text, "media": media, "links": links, "quoted": quoted,
        "metrics": {"likes": d.get("score"), "replies": d.get("num_comments")},
        "subreddit": sub, "reddit_kind": "post" if k == "t3" else "comment",
    }


def cmd_login(args):
    pw, ctx = open_ctx(headless=False)
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.reddit.com/login/")
        X.log(f"→ Log in to Reddit in the window (this script never sees your password). Waiting up to {args.login_timeout} min…")
        deadline = time.time() + args.login_timeout * 60
        while time.time() < deadline:
            name = me(ctx)
            if name:
                X.log(f"✓ Logged in to Reddit as u/{name}.")
                return
            page.wait_for_timeout(3000)
        sys.exit("Timed out waiting for the Reddit login.")
    finally:
        ctx.close()
        pw.stop()


def cmd_scrape(args):
    known = set(X.load_likes())
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    pw, ctx = open_cdp(args.cdp) if args.cdp else open_ctx(headless=True)
    counts, idx = {}, 0
    try:
        user = me(ctx)
        if not user:
            if args.cdp:
                X.log("REDDIT: not logged in to Reddit in your Brave (log in at reddit.com in Brave), then run xlikes-update again.")
                sys.exit(LAPSED_EXIT)
            X.log("REDDIT LOGIN LAPSED: run ~/Work/x-likes/xlikes reddit-login (a window opens to log in), then xlikes-update again.")
            sys.exit(LAPSED_EXIT)
        X.log(f"✓ Logged in to Reddit as u/{user} · run {run_id}")
        for kind in ("upvoted", "saved"):
            after, seen, known_pages, n_new = None, 0, 0, 0
            while True:
                data = listing(ctx, user, kind, after)
                new = []
                for th in data.get("children") or []:
                    t = normalize(th)
                    if not t:
                        continue
                    seen += 1
                    if t["id"] in known:
                        continue
                    known.add(t["id"])
                    t.update(source=f"reddit-{kind}", run=run_id, liked_index=idx, captured_at=X.now_iso())
                    idx += 1
                    new.append(t)
                X.append_jsonl(X.LIKES_FILE, new)
                n_new += len(new)
                known_pages = known_pages + 1 if (data.get("children") and not new) else 0
                after = data.get("after")
                X.log(f"  {kind}: {len(new):>3} new · {seen} listed")
                if not after or (args.incremental and known_pages >= 2):
                    break
                time.sleep(args.delay)
            counts[kind] = (seen, n_new)
        total = sum(1 for t in X.load_likes().values() if t.get("site") == "reddit")
        X.log("Done: " + " · ".join(f"{k} {s} listed ({n} new)" for k, (s, n) in counts.items())
              + f" · {total:,} Reddit items on disk (upvoted + saved, deduped)")
    finally:
        if not args.cdp:
            ctx.close()
        pw.stop()


def add_commands(sub):
    p = sub.add_parser("reddit-login", help="open a window to log in to Reddit once (own profile)")
    p.add_argument("--login-timeout", type=int, default=15, metavar="MIN")
    p.set_defaults(func=cmd_login)
    p = sub.add_parser("reddit", help="save your upvoted + saved Reddit posts/comments as likes")
    p.add_argument("--incremental", action="store_true", help="stop once we reach already-saved items")
    p.add_argument("--delay", type=float, default=2.0, help="seconds between listing pages (2)")
    p.add_argument("--cdp", type=int, default=0, metavar="PORT",
                   help="use the Reddit login of a Brave on this DevTools port (xlikes-update passes brave-copy's)")
    p.set_defaults(func=cmd_scrape)
