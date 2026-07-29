import os
import re
import sys
import time
import json
import logging
import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("telegram-groupme")

CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "moriahdoron")
PREVIEW_URL = f"https://t.me/s/{CHANNEL}"
GROUPME_BOT_ID = os.environ["GROUPME_BOT_ID"]
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
STATE_FILE = os.environ.get("STATE_FILE", "/data/seen.json")
MAX_BACKFILL = int(os.environ.get("MAX_BACKFILL", "5"))
GROUPME_ACCESS_TOKEN = os.environ["GROUPME_ACCESS_TOKEN"]  # from dev.groupme.com, needed to upload images

GROUPME_POST_URL = "https://api.groupme.com/v3/bots/post"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def load_seen():
    try:
        with open(STATE_FILE, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    trimmed = list(seen)[-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(trimmed, f)


def post_to_groupme(text, picture_url=None):
    if len(text) > 950:
        text = text[:947] + "..."
    payload = {"bot_id": GROUPME_BOT_ID, "text": text}
    if picture_url:
        payload["picture_url"] = picture_url
    resp = requests.post(GROUPME_POST_URL, json=payload, timeout=15)
    if resp.status_code >= 300:
        log.error("GroupMe post failed (%s): %s", resp.status_code, resp.text)
    else:
        log.info("Posted: %s", text[:80])


def upload_image_to_groupme(image_url):
    try:
        img_resp = requests.get(image_url, headers=HEADERS, timeout=15)
        img_resp.raise_for_status()
        upload_resp = requests.post(
            "https://image.groupme.com/pictures",
            headers={
                "X-Access-Token": GROUPME_ACCESS_TOKEN,
                "Content-Type": img_resp.headers.get("Content-Type", "image/jpeg"),
            },
            data=img_resp.content,
            timeout=20,
        )
        upload_resp.raise_for_status()
        return upload_resp.json()["payload"]["url"]
    except Exception:
        log.exception("Failed to upload image to GroupMe")
        return None


def fetch_messages():
    """
    Scrape t.me/s/<channel> — Telegram's public web preview.
    Each message lives in a div with class 'tgme_widget_message' and has a
    data-post attribute like 'channelname/1234' which we use as a stable ID.
    NOTE: Telegram's markup has changed before and may change again — if this
    stops finding messages, open t.me/s/moriahdoron in a browser, view source,
    and check these class names still match.
    """
    resp = requests.get(PREVIEW_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    messages = []
    for block in soup.select("div.tgme_widget_message"):
        post_id = block.get("data-post")  # e.g. "moriahdoron/4821"
        if not post_id:
            continue

        # Strip any quoted/replied-to message first — it has its own
        # tgme_widget_message_text div that would otherwise get matched
        # instead of the actual new message text.
        reply_block = block.select_one("div.tgme_widget_message_reply")
        if reply_block:
            reply_block.decompose()

        # A message quoting/replying to another has TWO tgme_widget_message_text
        # divs — the quoted one first, the actual new message last. Taking the
        # last one skips the quote regardless of what wrapper class Telegram uses.
        text_divs = block.select("div.tgme_widget_message_text")
        text = text_divs[-1].get_text("\n", strip=True) if text_divs else ""

        link_tag = block.select_one("a.tgme_widget_message_date")
        link = link_tag["href"] if link_tag and link_tag.get("href") else f"https://t.me/{post_id}"

        # Photo posts render as an <a> with a background-image inline style
        # rather than an <img> tag. Extract the URL from that style attribute.
        photo_el = block.select_one("a.tgme_widget_message_photo_wrap")
        image_url = None
        if photo_el and photo_el.get("style"):
            match = re.search(r"background-image:\s*url\('(.+?)'\)", photo_el["style"])
            if match:
                image_url = match.group(1)

        if text:  # skip pure media posts with no caption for now
            messages.append({"id": post_id, "text": text, "link": link, "image_url": image_url})

    return messages


def format_message(msg):
    return msg["text"]


def poll_once(seen, first_run):
    try:
        messages = fetch_messages()
    except Exception:
        log.exception("Failed to fetch/parse Telegram preview page")
        return seen

    if first_run:
        # Mark every currently-existing post as seen WITHOUT posting any of
        # them — the bot should only announce posts that appear after this
        # first run, not backfill old channel history.
        for msg in messages:
            seen.add(msg["id"])
        log.info("First run: marked %d existing posts as seen, posting nothing", len(messages))
        save_seen(seen)
        return seen

    new_messages = [m for m in messages if m["id"] not in seen]

    if len(new_messages) > MAX_BACKFILL:
        new_messages = new_messages[-MAX_BACKFILL:]

    for msg in new_messages:
        groupme_image = upload_image_to_groupme(msg["image_url"]) if msg.get("image_url") else None
        post_to_groupme(format_message(msg), picture_url=groupme_image)
        seen.add(msg["id"])
        time.sleep(1)

    if new_messages:
        save_seen(seen)

    return seen


def main():
    log.info("Starting Telegram -> GroupMe bot. Channel: %s Poll interval: %ss", CHANNEL, POLL_SECONDS)
    seen = load_seen()
    first_run = len(seen) == 0
    while True:
        try:
            seen = poll_once(seen, first_run)
            first_run = False
        except Exception:
            log.exception("Error during poll cycle")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
