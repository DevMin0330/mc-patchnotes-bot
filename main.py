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
TRANSLATE = True       # 번역 끄고 싶으면 False 로 바꾸세요

# 위에서부터 순서대로 시도하고, 한국어 번역이 안 나오면 다음 모델로 넘어갑니다
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "llama-3.1-8b-instant",
]

TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = os.environ["FORUM_CHANNEL_ID"]  # 이름은 그대로, 값은 일반 채널 ID
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
API = "https://discord.com/api/v10"
HEADERS = {
    "Authorization": f"Bot {TOKEN}",
    "User-Agent": "MCPatchNotesBot (personal use, 1.0)",
    "Content-Type": "application/json",
}
WEB_HEADERS = {"User-Agent": "MCPatchNotesBot (personal use, 1.0)"}


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


def is_korean(text):
    """번역 결과에 한글이 충분히 들어 있는지 검사"""
    hangul = len(re.findall(r"[가-힣]", text))
    letters = len(re.findall(r"[A-Za-z가-힣]", text))
    return letters > 0 and hangul / letters >= 0.2


def translate_with_groq(text):
    """Groq(무료 AI)로 한국어 번역. 한국어 결과가 나오면 번역문, 아니면 None."""
    if not GROQ_KEY:
        print("GROQ_API_KEY 가 없어 번역을 건너뜁니다.")
        return None

    system_prompt = (
        "당신은 마인크래프트 자바 에디션 패치노트 전문 번역가입니다. "
        "항상 영어를 한국어로 번역해서 한국어로만 답합니다."
    )
    user_prompt = (
        "아래 영어 패치노트를 한국어로 번역하세요. 반드시 한국어로 출력해야 합니다.\n"
        "규칙:\n"
        "- 마크다운 형식(##, ###, -, **)을 그대로 유지\n"
        "- 버전 이름, 버그 번호(MC-123456), 코드/명령어는 번역하지 말고 그대로 둠\n"
        "- 블록, 몹, 아이템 이름은 한국어판 마인크래프트 공식 명칭 사용\n"
        "- 설명이나 인사말 없이 번역문만 출력\n\n"
        "[번역할 영어 원문]\n" + text
    )

    for model in GROQ_MODELS:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 4096,
        }
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=90,
            )
            if not r.ok:
                print(f"[{model}] 실패 {r.status_code}: {r.text[:200]}")
                continue
            result = r.json()["choices"][0]["message"]["content"].strip()
            print(f"[{model}] 응답 앞부분: {result[:80]!r}")
            if result and is_korean(result):
                print(f"[{model}] 번역 성공")
                return result
            print(f"[{model}] 한국어가 아니라서 다음 모델로 넘어갑니다.")
        except Exception as e:
            print(f"[{model}] 오류: {e}")
    return None


def entry_id(entry):
    return str(entry.get("id") or entry.get("version") or entry.get("title"))


def get_body_html(entry):
    """피드에 본문이 있으면 그걸 쓰고, 없으면 경로를 찾아 따로 가져온다."""
    if entry.get("body"):
        return entry["body"]

    path = (
        entry.get("contentPath")
        or entry.get("bodyPath")
        or entry.get("content")
        or entry.get("path")
    )
    if not path or not isinstance(path, str):
        return ""

    if path.startswith("http"):
        candidates = [path]
    elif path.startswith("/"):
        candidates = [BASE_URL + path, BASE_URL + "/v2" + path]
    else:
        candidates = [f"{BASE_URL}/v2/{path}", f"{BASE_URL}/{path}"]

    for url in candidates:
        try:
            r = requests.get(url, timeout=30, headers=WEB_HEADERS)
            if r.ok and r.text.strip():
                print("본문 가져옴:", url)
                try:
                    data = r.json()
                    if isinstance(data, dict):
                        return data.get("body", "")
                except ValueError:
                    pass
                return r.text
        except requests.RequestException:
            pass
    print("본문 경로를 찾지 못함:", path)
    return ""


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None  # 파일이 없으면 '첫 실행'


def save_state(ids):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=2)


def post_message(entry):
    title = entry.get("title") or entry.get("version") or "Minecraft 패치노트"
    original = truncate(html_to_text(get_body_html(entry)), limit=3500)

    description = original
    footer_text = None
    if TRANSLATE and original:
        translated = translate_with_groq(original)
        if translated:
            description = truncate(translated, limit=4000)
            footer_text = "AI(Groq)로 자동 번역됨"
        else:
            footer_text = "번역에 실패해 영어 원문으로 표시됩니다"

    embed = {
        "title": title[:256],
        "description": description or "본문을 불러오지 못했습니다.",
        "color": 0x5B8731,
    }
    if footer_text:
        embed["footer"] = {"text": footer_text}

    image = entry.get("image") or {}
    url = image.get("url")
    if url:
        embed["image"] = {"url": BASE_URL + url if url.startswith("/") else url}

    payload = {"embeds": [embed]}

    for _ in range(2):  # 속도 제한에 걸리면 한 번 재시도
        r = requests.post(
            f"{API}/channels/{CHANNEL_ID}/messages",
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
    feed = requests.get(FEED_URL, timeout=30, headers=WEB_HEADERS)
    feed.raise_for_status()
    entries = feed.json().get("entries", [])

    if entries:
        print("피드 항목에 들어있는 키:", list(entries[0].keys()))

    # 최신순 정렬 (날짜가 없으면 피드 순서 그대로)
    entries = sorted(entries, key=lambda e: e.get("date", ""), reverse=True)

    posted = load_state()
    first_run = posted is None
    posted = set(posted or [])

    new_entries = [e for e in entries if entry_id(e) not in posted]

    if first_run:
        print("첫 실행입니다. 최신 1개만 게시하고 나머지는 기록만 합니다.")
        to_post = new_entries[:1]
        posted.update(entry_id(e) for e in new_entries)
    else:
        to_post = new_entries[:MAX_POSTS_PER_RUN]

    # 오래된 것부터 올려서 채널에서도 시간순이 되게
    for entry in reversed(to_post):
        post_message(entry)
        posted.add(entry_id(entry))
        time.sleep(2)

    save_state(posted)
    print(f"끝. 새 글 {len(to_post)}개 게시.")


if __name__ == "__main__":
    main()
