#!/usr/bin/env python3
"""
VOX Cinemas Egypt watcher -> Telegram alerts.

Alerts you when:
  1. A new movie is listed (Coming Soon or Now Showing)
  2. Booking opens for a movie (showtimes appear on its page)
  3. A movie you were alerted about gets showtimes at a new cinema

Usage:
  python vox.py --get-chat-id   # find your Telegram chat id (after messaging your bot)
  python vox.py --test          # check the site + send a test Telegram message
  python vox.py                 # normal run (cron runs this every 5 minutes)
"""
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://egy.voxcinemas.com"
COMING_SOON_URL = BASE + "/movies/comingsoon"
NOW_SHOWING_URL = BASE + "/movies/whatson"

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
ENV_FILE = HERE / ".env"
LOCK_FILE = HERE / ".lock"

TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm)\b", re.I)
SKIP_SLUGS = {"whatson", "comingsoon", "coming-soon", "now-showing"}
GENERIC_LINK_TEXT = {"showtimes", "view showtimes", "info", "book", "book now",
                     "trailer", "watch trailer", "more info"}
FAILS_BEFORE_WARNING = 6          # 6 failed runs x 5 min = 30 min
FORGET_AFTER_DAYS = 30            # forget movies unlisted for this long


# ---------- config ----------
def load_env():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# Optional, comma separated, e.g. "Almaza,Mall of Egypt". Empty = any cinema.
WATCH_CINEMAS = [c.strip().lower() for c in os.environ.get("WATCH_CINEMAS", "").split(",") if c.strip()]

session = requests.Session()
session.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
})


# ---------- helpers ----------
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(f"[{now_iso()}] {msg}", flush=True)


def send(text):
    if not TOKEN or not CHAT_ID:
        log("Telegram not configured; message was: " + text)
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": text}, timeout=20)
        if not r.ok:
            log(f"Telegram error {r.status_code}: {r.text}")
    except Exception as e:  # never crash because of Telegram
        log(f"Telegram send failed: {e}")


def fetch(url):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(STATE_FILE)


# ---------- parsing ----------
def list_movies(html):
    """Return {slug: title} for every /movies/<slug> link on a listing page."""
    soup = BeautifulSoup(html, "html.parser")
    movies = {}
    for a in soup.find_all("a", href=True):
        path = urlparse(urljoin(BASE, a["href"])).path.rstrip("/")
        m = re.fullmatch(r"/movies/([A-Za-z0-9][A-Za-z0-9\-]*)", path)
        if not m:
            continue
        slug = m.group(1).lower()
        if slug in SKIP_SLUGS:
            continue
        title = a.get_text(" ", strip=True)
        if not title:
            img = a.find("img")
            title = (img.get("alt", "") if img else "")
            title = re.sub(r"^movie poster for\s+", "", title, flags=re.I).strip()
        if title.lower() in GENERIC_LINK_TEXT:
            title = ""
        if title or slug not in movies:
            if not movies.get(slug):
                movies[slug] = title
    return {s: (t or s.replace("-", " ").title()) for s, t in movies.items()}


def booking_status(html):
    """Return (is_open, [cinema names]) based on showtimes on a movie page."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    found = False
    cinemas = set()
    for node in soup.find_all(string=TIME_RE):
        found = True
        h = node.find_previous(["h2", "h3", "h4"])
        name = h.get_text(" ", strip=True) if h else ""
        if name and "showtime" not in name.lower() and len(name) < 60:
            cinemas.add(name)
    return found, sorted(cinemas)


def relevant(cinemas):
    if not WATCH_CINEMAS:
        return cinemas or ["(any)"]
    return [c for c in cinemas if any(w in c.lower() for w in WATCH_CINEMAS)]


# ---------- commands ----------
def get_chat_id():
    if not TOKEN:
        print("Put TELEGRAM_TOKEN in .env first.")
        return
    r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=20).json()
    ids = {u["message"]["chat"]["id"] for u in r.get("result", []) if "message" in u}
    if ids:
        print("Your chat id:", ", ".join(map(str, ids)))
    else:
        print("No messages found. Send any message to your bot in Telegram, then run this again.")


def test():
    print("Fetching Coming Soon...")
    soon = list_movies(fetch(COMING_SOON_URL))
    print(f"  {len(soon)} movies: {', '.join(list(soon.values())[:8])}{' ...' if len(soon) > 8 else ''}")
    print("Fetching Now Showing...")
    showing = list_movies(fetch(NOW_SHOWING_URL))
    print(f"  {len(showing)} movies")
    print("Checking booking status of a few movies:")
    for slug, title in list(soon.items())[:3] + list(showing.items())[:2]:
        is_open, cinemas = booking_status(fetch(f"{BASE}/movies/{slug}"))
        print(f"  {title}: {'OPEN ' + str(cinemas) if is_open else 'not open'}")
        time.sleep(1)
    send(f"✅ VOX watcher connected!\nComing Soon: {len(soon)} movies\nNow Showing: {len(showing)} movies")
    print("Test message sent to Telegram (check your phone).")


def run():
    state = load_state()
    baseline = not state.get("initialized")
    movies = state.setdefault("movies", {})
    try:
        soon = list_movies(fetch(COMING_SOON_URL))
        showing = list_movies(fetch(NOW_SHOWING_URL))
        listed = {**showing, **soon}
        if not listed:
            raise RuntimeError("No movies found. VOX may have changed its site layout.")

        # 1) New listings
        for slug, title in listed.items():
            if slug not in movies:
                movies[slug] = {"title": title, "alerted": False, "cinemas": []}
                if not baseline:
                    where = "Coming Soon" if slug in soon else "Now Showing"
                    send(f"🎬 New on VOX Egypt ({where}): {title}\n{BASE}/movies/{slug}")
                    log(f"New listing: {title}")
            movies[slug]["last_seen"] = now_iso()[:10]  # date only, so state changes rarely

        # 2) Booking status
        for slug, info in movies.items():
            if slug not in listed:
                continue
            if info.get("alerted") and slug in showing:
                continue  # already open and now showing: nothing new to learn
            is_open, cinemas = booking_status(fetch(f"{BASE}/movies/{slug}"))
            time.sleep(1)
            if not is_open:
                continue
            known = set(info.get("cinemas", []))
            new = [c for c in cinemas if c not in known]
            info["cinemas"] = sorted(known | set(cinemas))
            rel = relevant(cinemas)
            if baseline:
                info["alerted"] = bool(rel)
            elif not info.get("alerted") and rel:
                info["alerted"] = True
                where = ", ".join(cinemas) if cinemas else "see page"
                send(f"🎟️ BOOKING OPEN: {info['title']}\nCinemas: {where}\n{BASE}/movies/{slug}")
                log(f"Booking open: {info['title']}")
            elif info.get("alerted"):
                new_rel = relevant(new) if new else []
                if new and new_rel and new_rel != ["(any)"]:
                    send(f"➕ More showtimes for {info['title']}: {', '.join(new_rel)}\n{BASE}/movies/{slug}")

        # Forget movies unlisted for a long time
        cutoff = time.time() - FORGET_AFTER_DAYS * 86400
        for slug in [s for s, i in movies.items()
                     if datetime.fromisoformat(i.get("last_seen", now_iso())[:10]).replace(tzinfo=timezone.utc).timestamp() < cutoff]:
            del movies[slug]

        if baseline:
            state["initialized"] = True
            send(f"👀 VOX watcher is running. Tracking {len(listed)} movies.\n"
                 f"You'll get alerts for new listings and when booking opens.")
        if state.get("fail_alerted"):
            send("✅ VOX watcher is working again.")
        state.pop("fail_count", None)
        state.pop("fail_alerted", None)
        log(f"OK: {len(soon)} coming soon, {len(showing)} now showing")
    except Exception as e:
        state["fail_count"] = state.get("fail_count", 0) + 1
        log(f"ERROR ({state['fail_count']}): {e}")
        if state["fail_count"] >= FAILS_BEFORE_WARNING and not state.get("fail_alerted"):
            send(f"⚠️ VOX watcher has failed for ~30 minutes.\nLast error: {e}")
            state["fail_alerted"] = True
    save_state(state)


if __name__ == "__main__":
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)  # previous run still going
    if "--get-chat-id" in sys.argv:
        get_chat_id()
    elif "--test" in sys.argv:
        test()
    else:
        run()
