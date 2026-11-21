import json
import os
import re
import time
from html.parser import HTMLParser

import requests

FEED_URL = "https://launchercontent.mojang.com/v2/javaPatchNotes.json"
BASE_URL = "https://launchercontent.mojang.com"
STATE_FILE = "posted.json"
MAX_POSTS_PER_RUN = 5  # 한 번 실행에 최대 몇 개까지 올릴지

TOKEN = os.environ["DISCORD_TOKEN"]
FORUM_ID = os.environ["FORUM_CHANNEL_ID"]
API = "https://discord.com/api/v10"
HEADERS = {
    "Authorization": f"Bot {TOKEN}",
    "User-Agent": "MCPatchNotesBot (personal use, 1.0)",
    "Content-Type": "application/json",
}


class HtmlToText(HTMLParser):
    """패치노트 HTML을 디스코드용 마크다운 글자로 바꿔주는 도우미"""

    def __init__(self):
        super().__init__()
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2"):
            self.out.append("\n\n## ")
        elif tag in ("h3", "h4", "h5"):
            self.out.append("\n\n### ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in ("p", "br", "ul", "ol"):
            self.out.append("\n")
        elif tag in ("strong", "b"):
            self.out.append("**")

    def handle_endtag(self, tag):
        if tag in ("strong", "b"):
            self.out.append("**")
        elif tag == "p":
            self.out.append("\n")

    def handle_data(self, data):
        self.out.append(re.sub(r"\s+", " ", data))


def html_to_text(html):
    parser = HtmlToText()
    parser.feed(html or "")
    text = "".join(parser.out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def truncate(text, limit=3800):
    if len(text) <= limit:
        return text
    cut = text[:limit]
    cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
    return cut + "\n\n… (내용이 길어 일부만 표시됩니다)"


def entry_id(entry):
    return str(entry.get("id") or entry.get("version") or entry.get("title"))


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None  # 파일이 없으면 '첫 실행'


def save_state(ids):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=2)


def post_thread(entry):
    title = entry.get("title") or entry.get("version") or "Minecraft 패치노트"
    description = truncate(html_to_text(entry.get("body", "")))

    embed = {
        "title": title[:256],
        "description": description or "내용이 없습니다.",
        "color": 0x5B8731,
    }
    image = entry.get("image") or {}
    url = image.get("url")
    if url:
        embed["image"] = {"url": BASE_URL + url if url.startswith("/") else url}

    payload = {"name": title[:100], "message": {"embeds": [embed]}}

    for _ in range(2):  # 속도 제한에 걸리면 한 번 재시도
        r = requests.post(
            f"{API}/channels/{FORUM_ID}/threads",
            headers=HEADERS,
            json=payload,
            timeout=30,
        )
        if r.status_code == 429:
            time.sleep(r.json().get("retry_after", 5))
            continue
        break

    if not r.ok:
        print("디스코드 오류:", r.status_code, r.text)
        r.raise_for_status()
    print("게시 완료:", title)


def main():
    feed = requests.get(FEED_URL, timeout=30, headers={"User-Agent": "MCPatchNotesBot"})
    feed.raise_for_status()
    entries = feed.json().get("entries", [])

    # 최신순 정렬 (날짜가 없으면 피드 순서 그대로)
    entries = sorted(entries, key=lambda e: e.get("date", ""), reverse=True)

    posted = load_state()
    first_run = posted is None
    posted = set(posted or [])

    new_entries = [e for e in entries if entry_id(e) not in posted]

    if first_run:
        # 첫 실행: 과거 글 수백 개가 올라가지 않도록 최신 1개만 올리고 나머지는 '읽음' 처리
        print("첫 실행입니다. 최신 1개만 게시하고 나머지는 기록만 합니다.")
        to_post = new_entries[:1]
        posted.update(entry_id(e) for e in new_entries)
    else:
        to_post = new_entries[:MAX_POSTS_PER_RUN]

    # 오래된 것부터 올려서 포럼에서도 시간순이 되게
    for entry in reversed(to_post):
        post_thread(entry)
        posted.add(entry_id(entry))
        time.sleep(2)

    save_state(posted)
    print(f"끝. 새 글 {len(to_post)}개 게시.")


if __name__ == "__main__":
    main()
