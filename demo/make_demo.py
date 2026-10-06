#!/usr/bin/env python3
"""Make the demo dataset: ~160 made-up likes in the same format as data/likes.jsonl and
data/categories.jsonl, with simple generated SVG images. Every author, handle, text and image is
invented (handles start with demo_ and links go to example.com); nothing comes from real X data.

  python3 demo/make_demo.py      # rewrites demo/data/ and demo/media/ (deterministic: same output)
"""
import hashlib, json, random
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
rng = random.Random(42)

AUTHORS = [
    ("demo_artist", "Ada Pixel", "Paints with code. Prints on request."),
    ("demo_shaderfox", "Shader Fox", "Real-time graphics, one shader a day."),
    ("demo_modelwatch", "Model Watch", "Notes on new AI models and papers."),
    ("demo_datavis", "Dana Charts", "Maps, charts and explorable explanations."),
    ("demo_terminal", "Tilde Tom", "Terminals, editors and tiny tools."),
    ("demo_siliconsam", "Silicon Sam", "Chips, boards and soldering irons."),
    ("demo_stargazer", "Vera Orbit", "Astronomy and physics, explained simply."),
    ("demo_typeface", "Kern & Co", "Type, layout and interfaces."),
    ("demo_marketbits", "Market Bits", "Tech business news, short."),
    ("demo_punchline", "Pun Intended", "Jokes about computers, mostly."),
    ("demo_slowliving", "Morning Walks", "Coffee, dogs and long walks."),
    ("demo_misc", "Odds & Ends", "Things that fit nowhere else."),
]
BY_CAT = {
    "Art": ("demo_artist", [
        "Generative ink study #{n}: {k} overlapping circles, one random seed.",
        "Finished the {k}-colour risograph series. Each print slightly different.",
        "Plotter drawing of flow fields, {k} hours of pen time.",
        "Glitch portrait made by sorting pixels by brightness. Prints soon.",
    ]),
    "Graphics": ("demo_shaderfox", [
        "Raymarched clouds in {k} lines of GLSL, runs at 60 fps on a laptop.",
        "Today's shader: a domain-warped noise ocean. Source below.",
        "Tried {k} ways to anti-alias signed distance fields; the cheapest won.",
        "Toon outline pass in WebGPU, no geometry shader needed.",
    ]),
    "AI": ("demo_modelwatch", [
        "New open model claims {k}% on a reasoning benchmark; weights are out.",
        "Paper summary: agents that write their own tools get {k}% fewer errors.",
        "Small model, big context: {k}k tokens on a single consumer GPU.",
        "Prompting tip: ask the model to plan first, then act. Fewer retries.",
    ]),
    "Vis": ("demo_datavis", [
        "Made a map of {k} city parks by tree count. Darker green = more trees.",
        "Small multiples beat one giant chart, again. {k} panels, one story.",
        "Explorable explanation of how compound interest works, with sliders.",
        "Colour-blind safe palette with {k} steps, free to use.",
    ]),
    "Dev": ("demo_terminal", [
        "My terminal now starts in {k} ms. The trick: lazy-load everything.",
        "Wrote a {k}-line script that renames photos by date. Sharing it.",
        "Editor tip: multiple cursors + regex search = refactor in seconds.",
        "Switched my dotfiles to one repo with {k} symlinks. Finally tidy.",
    ]),
    "Hardware": ("demo_siliconsam", [
        "Built a {k}-key macro pad from a kit. First keyboard I've soldered.",
        "New dev board: tiny, cheap and {k} GPIO pins.",
        "Took apart an old GPU to see the VRAM chips up close.",
    ]),
    "Science": ("demo_stargazer", [
        "A comet passes tonight, best seen {k} minutes after sunset.",
        "Why is the sky blue? A {k}-tweet thread with diagrams.",
        "New photo of a spiral galaxy {k} million light years away.",
    ]),
    "Design": ("demo_typeface", [
        "Redesigned a settings screen: {k} options became 3 sensible defaults.",
        "Variable font with {k} weights, all in one file.",
        "Good empty states tell you what to do next. Examples inside.",
    ]),
    "Industry": ("demo_marketbits", [
        "A startup making developer tools raised a {k}M round today.",
        "Big company reorg: {k} teams merged into one platform group.",
    ]),
    "Humor": ("demo_punchline", [
        "My code works and I have no idea why. Day {k}.",
        "Spent {k} hours automating a task that takes 5 minutes. Worth it.",
        "There are {k} kinds of people: those who understand binary and those who don't.",
    ]),
    "Life": ("demo_slowliving", [
        "Walked {k} km this morning. The dog insisted on every lamp post.",
        "Sunday pancakes, attempt {k}. Getting better.",
        "Reading by the window, rain outside, tea going cold.",
    ]),
    "Other": ("demo_misc", [
        "Found a {k}-year-old manual in the attic. The diagrams are lovely.",
        "Fun fact: there are {k} words for snow in my notes app.",
    ]),
}
TAGS = {"Art": ["generative", "print", "plotter"], "Graphics": ["shader", "webgpu", "rendering"],
        "AI": ["llm", "agents", "paper"], "Vis": ["maps", "charts", "color"], "Dev": ["terminal", "dotfiles", "editor"],
        "Hardware": ["keyboard", "electronics"], "Science": ["astronomy", "physics"], "Design": ["ui", "typography"],
        "Industry": ["startups", "funding"], "Humor": ["jokes", "programming"], "Life": ["walks", "food"],
        "Other": ["misc"]}
WEIGHTS = {"Art": 22, "AI": 22, "Life": 15, "Science": 11, "Vis": 10, "Graphics": 10, "Industry": 4,
           "Humor": 4, "Dev": 4, "Other": 3, "Design": 3, "Hardware": 2}
HUES = {"Art": 330, "Graphics": 260, "AI": 210, "Vis": 160, "Dev": 120, "Hardware": 30, "Science": 230,
        "Design": 45, "Industry": 0, "Humor": 55, "Life": 90, "Other": 190}


def svg(cat, seed):
    r = random.Random(seed)
    h = HUES[cat]
    shapes = []
    for _ in range(r.randint(5, 11)):
        x, y, s = r.randint(0, 640), r.randint(0, 400), r.randint(30, 160)
        hh = (h + r.randint(-40, 40)) % 360
        if r.random() < 0.5:
            shapes.append(f'<circle cx="{x}" cy="{y}" r="{s // 2}" fill="hsl({hh},70%,{r.randint(45, 70)}%)" fill-opacity="0.75"/>')
        else:
            shapes.append(f'<rect x="{x}" y="{y}" width="{s}" height="{s // 2}" rx="8" fill="hsl({hh},65%,{r.randint(40, 70)}%)" fill-opacity="0.7" transform="rotate({r.randint(-30, 30)} {x} {y})"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400" viewBox="0 0 640 400">'
            f'<rect width="640" height="400" fill="hsl({h},40%,94%)"/>{"".join(shapes)}'
            f'<text x="20" y="380" font-family="sans-serif" font-size="18" fill="#555">demo · {cat}</text></svg>')


def main():
    data, media = HERE / "data", HERE / "media"
    data.mkdir(parents=True, exist_ok=True)
    if media.exists():
        for f in sorted(media.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
    media.mkdir(exist_ok=True)
    tax = json.loads((ROOT / "categories.json").read_text())
    tax_hash = hashlib.sha1(json.dumps(tax, sort_keys=True).encode()).hexdigest()[:10]
    authors = {h: (n, b) for h, n, b in AUTHORS}
    cats = [c for c, w in WEIGHTS.items() for _ in range(w)]
    likes, classes = [], []
    n = 160
    for i in range(n):
        cat = rng.choice(cats)
        handle, templates = BY_CAT[cat]
        name, bio = authors[handle]
        tid = str(1000000000000000000 + i * 7919)
        y = rng.choice(range(2015, 2027))
        mo, d, hh, mm = rng.randint(1, 12 if y < 2026 else 9), rng.randint(1, 28), rng.randint(0, 23), rng.randint(0, 59)
        text = rng.choice(templates).format(n=i + 1, k=rng.randint(2, 99))
        has_img = cat in ("Art", "Graphics", "Vis", "Science", "Design", "Hardware") or rng.random() < 0.25
        m = []
        if has_img:
            (media / tid).mkdir()
            (media / tid / "1.svg").write_text(svg(cat, i))
            m = [{"type": "photo", "url": f"https://example.com/demo-media/{tid}.svg", "thumb": f"https://example.com/demo-media/{tid}.svg", "alt": f"Made-up {cat.lower()} picture"}]
        quoted = None
        if rng.random() < 0.08:
            qh, _ = BY_CAT[rng.choice(list(BY_CAT))]
            quoted = {"id": str(int(tid) + 1), "url": f"https://example.com/{qh}/status/{int(tid) + 1}", "author": qh,
                      "name": authors[qh][0], "text": "An earlier made-up post this one quotes."}
        likes.append({
            "id": tid, "url": f"https://example.com/{handle}/status/{tid}", "created_at": f"{y}-{mo:02d}-{d:02d}T{hh:02d}:{mm:02d}:00+00:00",
            "author": {"id": "demo" + hashlib.sha1(handle.encode()).hexdigest()[:8], "handle": handle, "name": name, "bio": bio, "verified": False, "followers": None},
            "text": text, "lang": "en", "in_reply_to": None, "media": m, "links": [], "quoted": quoted,
            "metrics": {"likes": rng.randint(5, 5000), "reposts": rng.randint(0, 500), "replies": rng.randint(0, 200), "quotes": 0, "bookmarks": rng.randint(0, 300), "views": rng.randint(500, 200000)},
            "source": "demo", "run": "20260101-000000", "liked_index": i, "captured_at": "2026-01-01T00:00:00+00:00",
        })
        classes.append({"id": tid, "category": cat, "tags": rng.sample(TAGS[cat], min(2, len(TAGS[cat]))),
                        "confidence": round(rng.uniform(0.6, 0.98), 2), "taxonomy": tax_hash, "model": "demo", "at": "2026-01-01T00:00:00+00:00"})
    (data / "likes.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in likes))
    (data / "categories.jsonl").write_text("".join(json.dumps(c) + "\n" for c in classes))
    print(f"demo: {n} likes, {sum(1 for t in likes if t['media'])} with images → {data}, {media}")


if __name__ == "__main__":
    main()
