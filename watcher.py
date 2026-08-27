#!/usr/bin/env python3
"""Poll Tweakers V&A RSS and push 5060 Ti / 9060 XT 16GB hits to ntfy."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import feedparser
import requests

FEED_URL = os.environ.get("FEED_URL", "https://tweakers.net/feeds/va.xml")
SEEN_FILE = Path(os.environ.get("SEEN_FILE", "/data/seen_ids.json"))
HEARTBEAT_FILE = Path(os.environ.get("HEARTBEAT_FILE", "/data/heartbeat"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))

NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "").strip()
NTFY_PRIORITY = os.environ.get("NTFY_PRIORITY", "default")

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "gpu-va-watcher/1.0 (personal; rss; not affiliated with Tweakers)",
)
SEND_TEST_ON_START = os.environ.get("SEND_TEST_ON_START", "").lower() in {
    "1",
    "true",
    "yes",
}

_SEP = re.compile(r"[\u00a0\u202f\u2007_\-–—/\\|+.,;:()\[\]{}*]+")
_LETTER_DIGIT = re.compile(r"([a-z])(\d)")
_DIGIT_LETTER = re.compile(r"(\d)([a-z])")
_WS = re.compile(r"\s+")
_HTML = re.compile(r"<[^>]+>")

VRAM_UNITS = {
    "g",
    "gb",
    "gib",
    "gig",
    "gigs",
    "giga",
    "gigabyte",
    "gigabytes",
    "gddr",
    "vram",
}
MB_UNITS = {"m", "mb", "mib"}
CHIP_LABEL = {
    "rtx_5060_ti": "RTX 5060 Ti 16GB",
    "rx_9060_xt": "RX 9060 XT 16GB",
}

log = logging.getLogger("watcher")


def normalize(text: str) -> str:
    s = (text or "").replace("\u2122", " ").replace("\u00ae", " ").replace("\u00a9", " ")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower()
    s = _SEP.sub(" ", s)
    s = _LETTER_DIGIT.sub(r"\1 \2", s)
    s = _DIGIT_LETTER.sub(r"\1 \2", s)
    return _WS.sub(" ", s).strip()


def _has_seq(toks: list[str], seq: list[str]) -> bool:
    n = len(seq)
    return any(toks[i : i + n] == seq for i in range(len(toks) - n + 1))


def _vram_gb(toks: list[str]) -> set[int]:
    sizes: set[int] = set()
    for i, tok in enumerate(toks[:-1]):
        nxt = toks[i + 1]
        if tok == "16" and nxt in VRAM_UNITS:
            sizes.add(16)
        elif tok == "8" and nxt in VRAM_UNITS:
            sizes.add(8)
        elif tok == "16384" and nxt in MB_UNITS:
            sizes.add(16)
        elif tok == "8192" and nxt in MB_UNITS:
            sizes.add(8)
    return sizes


def match_gpu(title: str, summary: str = "", *, allow_unknown_vram: bool = False):
    toks = normalize(f"{title}\n{summary}").split()
    vram = _vram_gb(toks)

    if _has_seq(toks, ["5060", "ti"]):
        chip = "rtx_5060_ti"
    elif "5060" in toks and 16 in vram:
        chip = "rtx_5060_ti"
    elif _has_seq(toks, ["9060", "xt"]):
        chip = "rx_9060_xt"
    else:
        return None

    if 16 in vram:
        return chip
    if 8 in vram:
        return None
    return chip if allow_unknown_vram else None


def strip_html(text: str) -> str:
    return _WS.sub(" ", _HTML.sub(" ", text or "")).strip()


def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(seen)))
    tmp.replace(SEEN_FILE)


def heartbeat() -> None:
    HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_FILE.write_text(str(time.time()))


def ntfy_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    if extra:
        headers.update(extra)
    return headers


def notify(title: str, body: str, click: str | None = None, tags: str = "computer") -> bool:
    if not NTFY_TOPIC:
        log.error("NTFY_TOPIC is empty")
        return False
    headers = ntfy_headers(
        {
            "Title": title[:200],
            "Priority": NTFY_PRIORITY,
            "Tags": tags,
            "Markdown": "yes",
        }
    )
    if click:
        headers["Click"] = click
    url = f"{NTFY_URL}/{NTFY_TOPIC}"
    try:
        r = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=20)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("ntfy failed: %s", e)
        return False


def fetch_feed() -> list:
    r = requests.get(
        FEED_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
        timeout=20,
    )
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    if not parsed.entries and "html" in r.headers.get("content-type", "").lower():
        raise RuntimeError("RSS URL returned HTML (consent/block page)")
    return list(parsed.entries)


def is_sale_gpu_ad(item) -> bool:
    title = item.get("title") or ""
    if not title.startswith("A:"):
        return False
    cat = ""
    if item.get("tags"):
        cat = (item.tags[0].get("term") or "").strip()
    if cat and cat != "Videokaarten":
        return False
    return True


def poll(seen: set[str], bootstrap: bool) -> None:
    entries = fetch_feed()
    new_ids = 0
    hits = 0
    pending_fail = 0

    for item in entries:
        guid = item.get("id") or item.get("link")
        if not guid or guid in seen:
            continue

        title = item.get("title") or ""
        summary = strip_html(item.get("summary") or "")
        link = item.get("link") or ""
        published = item.get("published") or ""

        if bootstrap or not is_sale_gpu_ad(item):
            seen.add(guid)
            new_ids += 1
            continue

        chip = match_gpu(title, summary)
        if not chip:
            seen.add(guid)
            new_ids += 1
            continue

        label = CHIP_LABEL[chip]
        body = f"**{title}**\n\n{link}\n\n{published}\n\n{summary[:500]}"
        ok = notify(
            title=f"Tweakers: {label}",
            body=body,
            click=link or None,
            tags="computer,money",
        )
        if ok:
            log.info("HIT %s | %s | %s", chip, title, link)
            seen.add(guid)
            hits += 1
            new_ids += 1
        else:
            pending_fail += 1

    save_seen(seen)
    if pending_fail == 0:
        heartbeat()
    log.info(
        "poll done entries=%d new=%d hits=%d notify_fail=%d seen=%d bootstrap=%s",
        len(entries),
        new_ids,
        hits,
        pending_fail,
        len(seen),
        bootstrap,
    )


def run_self_test() -> int:
    cases = [
        ("A: RTX 5060 Ti 16GB", True),
        ("A: rtx5060ti16g", True),
        ("A: PRIME-RTX5060Ti-O16G", True),
        ("A: RTX 5060 16GB", True),
        ("A: RX 9060 XT 16GB", True),
        ("A: rx9060xt16g", True),
        ("A: RX 9060 16GB", False),
        ("A: RTX 5060 8GB", False),
        ("A: RTX 5060 Ti 8GB", False),
        ("A: RX 9070 XT 16GB", False),
        ("A: RTX 4060 Ti 16GB", False),
        ("A: RTX 5060", False),
    ]
    failed = 0
    for text, expect in cases:
        got = match_gpu(text) is not None
        mark = "OK" if got == expect else "FAIL"
        if got != expect:
            failed += 1
        print(f"{mark:4} expect={expect!s:5} got={got!s:5} | {text}")
    return 1 if failed else 0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not NTFY_TOPIC:
        log.error("Set NTFY_TOPIC in .env")
        sys.exit(1)

    log.info(
        "watching %s every %ss -> %s/<topic hidden>",
        FEED_URL,
        POLL_SECONDS,
        NTFY_URL,
    )
    seen = load_seen()
    first = not bool(seen)

    if SEND_TEST_ON_START:
        notify(
            "GPU watcher started",
            f"Polling Tweakers every {POLL_SECONDS}s.",
            tags="white_check_mark",
        )

    if first:
        log.info("first run: recording current ads, no notifications")

    while True:
        try:
            poll(seen, bootstrap=first)
            first = False
        except Exception:
            log.exception("poll failed")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(run_self_test())
    main()
