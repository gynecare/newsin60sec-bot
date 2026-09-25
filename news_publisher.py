import os
import re
import json
import sys
import requests
import feedparser
from datetime import datetime, date
from difflib import SequenceMatcher
from dotenv import load_dotenv

# Force UTF-8 output on Windows console
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# ============================================================
# CONFIGURATION
# ============================================================
load_dotenv()

FB_PAGE_ID    = os.getenv("FB_PAGE_ID")
FB_PAGE_TOKEN = os.getenv("FB_PAGE_TOKEN")
GROQ_API_KEY  = os.getenv("GROQ_API_KEY")

GROQ_MODEL = "openai/gpt-oss-120b"

NEWS_SOURCES = [
    {"name": "BBC Asia",         "url": "https://feeds.bbci.co.uk/news/world/asia/rss.xml"},
    {"name": "Express Tribune",  "url": "https://tribune.com.pk/feed/home"},
    {"name": "GNN HD",           "url": "https://gnnhd.tv/rss/latest"},
    {"name": "Dawn News",        "url": "https://www.dawn.com/feeds/home"},
]

TEST_MODE = "--test" in sys.argv

POSTED_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "posted_titles.json")
SIMILARITY_THRESHOLD = 0.75   # 0.0 = anything matches, 1.0 = exact match only

# ============================================================
# STEP 0 — VERIFY TOKENS
# ============================================================
def verify_config():
    print("=" * 60)
    print("STEP 0 — Verifying configuration")
    print("=" * 60)

    missing = []
    if not FB_PAGE_ID:    missing.append("FB_PAGE_ID")
    if not FB_PAGE_TOKEN: missing.append("FB_PAGE_TOKEN")
    if not GROQ_API_KEY:  missing.append("GROQ_API_KEY")

    if missing:
        print(f"✗ Missing in .env: {', '.join(missing)}")
        sys.exit(1)

    print(f"✓ FB_PAGE_ID present (ends in ...{FB_PAGE_ID[-4:]})")
    print(f"✓ FB_PAGE_TOKEN present (length {len(FB_PAGE_TOKEN)})")
    print(f"✓ GROQ_API_KEY present (starts with {GROQ_API_KEY[:6]}...)")

    try:
        r = requests.get(
            f"https://graph.facebook.com/v25.0/{FB_PAGE_ID}",
            params={"fields": "name", "access_token": FB_PAGE_TOKEN},
            timeout=15,
        )
        data = r.json()
        if "error" in data:
            print(f"✗ Facebook token error: {data['error'].get('message')}")
            sys.exit(1)
        print(f"✓ Facebook page confirmed: {data.get('name')}")
    except Exception as e:
        print(f"✗ Facebook check failed: {e}")
        sys.exit(1)

    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": GROQ_MODEL,
                  "messages": [{"role": "user", "content": "Reply with one word: OK"}],
                  "max_tokens": 10},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"✗ Groq error ({r.status_code}): {r.text[:300]}")
            sys.exit(1)
        print(f"✓ Groq API confirmed (model: {GROQ_MODEL})")
    except Exception as e:
        print(f"✗ Groq check failed: {e}")
        sys.exit(1)

    print()

# ============================================================
# DUPLICATE PREVENTION
# ============================================================
def load_posted_titles():
    """Return list of headlines already posted today."""
    today = date.today().isoformat()
    if not os.path.exists(POSTED_LOG):
        return []
    try:
        with open(POSTED_LOG, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") != today:
            return []   # Reset each day
        return data.get("titles", [])
    except Exception:
        return []

def save_posted_titles(titles):
    """Overwrite the posted titles file with today's date and current list."""
    os.makedirs(os.path.dirname(POSTED_LOG), exist_ok=True)
    with open(POSTED_LOG, "w", encoding="utf-8") as f:
        json.dump({"date": date.today().isoformat(), "titles": titles}, f, ensure_ascii=False, indent=2)

def is_similar(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= SIMILARITY_THRESHOLD

def filter_duplicates(articles, already_posted):
    """Remove any article whose title is similar to something already posted today."""
    fresh = []
    for a in articles:
        if any(is_similar(a["title"], prev) for prev in already_posted):
            print(f"  ↷ Skipping (already posted today): {a['title'][:70]}")
            continue
        fresh.append(a)
    return fresh

# ============================================================
# STEP 1 — FETCH NEWS
# ============================================================
def clean_url(url):
    if not url:
        return url
    return re.sub(r"[?&](at_medium|at_campaign|utm_[a-z]+|fbclid|gclid)=[^&]+", "", url).rstrip("?&")

def short_url(url, max_len=45):
    if not url:
        return ""
    m = re.match(r"https?://(?:www\.)?([^/]+)(/.*)?", url)
    if not m:
        return url[:max_len]
    combined = m.group(1) + (m.group(2) or "")
    if len(combined) <= max_len:
        return combined
    return combined[:max_len - 3] + "..."

def fetch_rss(url, source_name, max_items=15):
    try:
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries[:max_items]:
            articles.append({
                "title":   entry.get("title", "").strip(),
                "link":    clean_url(entry.get("link", "").strip()),
                "source":  source_name,
                "summary": entry.get("summary", "")[:300],
            })
        print(f"  ✓ {source_name}: {len(articles)} articles")
        return articles
    except Exception as e:
        print(f"  ✗ {source_name} failed: {e}")
        return []

def fetch_all_news():
    print("=" * 60)
    print("STEP 1 — Fetching news")
    print("=" * 60)
    all_articles = []
    for src in NEWS_SOURCES:
        all_articles.extend(fetch_rss(src["url"], src["name"]))
    print(f"Total articles fetched: {len(all_articles)}\n")
    return all_articles

# ============================================================
# STEP 2 — AI HELPER
# ============================================================
def call_groq(prompt, system_prompt="You are a professional news editor.", json_mode=False):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 3000,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    r = requests.post(url, headers=headers, json=payload, timeout=90)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

# ============================================================
# STEP 3 — FILTER PAKISTAN NEWS
# ============================================================
def filter_pakistan_news(articles):
    print("=" * 60)
    print("STEP 2 — Filtering for Pakistan-related stories")
    print("=" * 60)

    if not articles:
        return []

    capped = articles[:40]
    headlines_text = "\n".join(
        f"{i+1}. [{a['source']}] {a['title']}" for i, a in enumerate(capped)
    )

    prompt = f"""From the news headlines below, select ONLY stories primarily about Pakistan.

Include: politics, economy, security, sports, culture, and anything involving Pakistani people, places, or interests.
Exclude: global news where Pakistan is not central.
Aim for 6 to 8 of the most important stories.

Headlines:
{headlines_text}

Return ONLY a JSON array of the index numbers (1-based), like: [1, 3, 7, 12]
No other text."""

    try:
        result = call_groq(prompt)
        start = result.find("[")
        end = result.rfind("]") + 1
        indices = json.loads(result[start:end])
        selected = [capped[i - 1] for i in indices if 0 < i <= len(capped)]
        selected = selected[:8]
        print(f"Selected {len(selected)} Pakistan-related stories\n")
        return selected
    except Exception as e:
        print(f"  Filtering failed: {e}")
        print("  Fallback: using first 8 articles\n")
        return capped[:8]

# ============================================================
# STEP 4 — GENERATE STRUCTURED BILINGUAL CARDS
# ============================================================
def generate_cards(articles):
    print("=" * 60)
    print("STEP 3 — Generating bilingual card content via AI")
    print("=" * 60)

    article_list = "\n".join(
        f"{i+1}. [{a['source']}] {a['title']}"
        for i, a in enumerate(articles)
    )

    prompt = f"""You are a bilingual news editor. For each story below, produce:
- "emoji": a single topic emoji that best matches the story. Choose ONLY from this list:
  🏛 (politics/government), 🚨 (breaking/urgent), ⚡ (security/military), 💰 (economy/trade),
  🌧 (weather/floods), ⚽ (sports), 🏥 (health), ☀️ (energy/solar), 🌍 (international/UN), 📌 (miscellaneous)
- "en_headline": English headline, max 12 words, punchy
- "en_summary": English summary, one short sentence (max 15 words), or empty string if not needed
- "ur_headline": Urdu headline, natural Urdu, equivalent meaning
- "ur_summary": Urdu summary in one short sentence, or empty string

Output ONLY valid JSON in this exact shape:
{{
  "stories": [
    {{"emoji":"🚨","en_headline":"...","en_summary":"...","ur_headline":"...","ur_summary":"..."}},
    ...
  ]
}}

There must be exactly {len(articles)} stories, in the same order as the input.

Stories:
{article_list}"""

    result = call_groq(
        prompt,
        system_prompt="You are a bilingual news editor. Output only valid JSON.",
        json_mode=True,
    )
    print("  ✓ Card content generated\n")

    try:
        data = json.loads(result)
        return data.get("stories", [])
    except Exception as e:
        print(f"  ✗ Failed to parse AI output: {e}")
        print(f"  Raw output preview: {result[:500]}")
        sys.exit(1)

# ============================================================
# STEP 5 — BUILD THE COLOURFUL POST
# ============================================================
DIVIDERS = ["━━━━━━━━━━━━━━━━━━", "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬", "═══╣ ◆ ╠═══", "◈━━━━━━━━━━━━━◈"]

def build_post_text(stories, articles):
    divider = DIVIDERS[datetime.now().day % len(DIVIDERS)]
    date_str = datetime.now().strftime("%d %b %Y")

    lines = [
        "🟦🟦🟦 NEWS IN 60 SEC 🟦🟦🟦",
        f"🗓 {date_str}",
        divider,
        "",
    ]

    for i, s in enumerate(stories):
        link = short_url(articles[i]["link"]) if i < len(articles) else ""
        lines.append(f"{s.get('emoji','📌')} {i+1}. {s.get('en_headline','').upper()}")
        if s.get("en_summary"):
            lines.append(s["en_summary"])
        if link:
            lines.append(f"🔗 {link}")
        lines.append(divider)
        lines.append("")

    lines.append("🟩🟩🟩 اردو بلیٹن 🟩🟩🟩")
    lines.append("")
    for i, s in enumerate(stories):
        lines.append(f"{s.get('emoji','📌')} {i+1}. {s.get('ur_headline','')}")
        if s.get("ur_summary"):
            lines.append(s["ur_summary"])
        lines.append("")

    lines.append(divider)
    lines.append("#NewsIn60Sec #Pakistan #BreakingNews #PakistanNews")

    return "\n".join(lines)

def post_to_facebook(message):
    print("=" * 60)
    print("STEP 4 — Publishing to Facebook")
    print("=" * 60)
    print("Preview of post:\n")
    print("-" * 60)
    print(message)
    print("-" * 60)

    if TEST_MODE:
        print("\n[TEST MODE] Skipping actual Facebook post.\n")
        return True

    r = requests.post(
        f"https://graph.facebook.com/v25.0/{FB_PAGE_ID}/feed",
        data={"message": message, "access_token": FB_PAGE_TOKEN},
        timeout=30,
    )
    data = r.json()
    if "error" in data:
        print(f"\n✗ Facebook error: {data['error'].get('message')}")
        return False

    print(f"\n✓ Posted to Facebook. Post ID: {data.get('id')}\n")
    return True

# ============================================================
# MAIN
# ============================================================
def main():
    print(f"\n=== News in 60 Sec — Publisher — {datetime.now()} ===\n")

    verify_config()

    # Load today's already-posted titles
    already_posted = load_posted_titles()
    if already_posted:
        print(f"ℹ Already posted today: {len(already_posted)} stories — will skip duplicates\n")

    articles = fetch_all_news()
    if not articles:
        print("No articles fetched. Exiting.")
        return

    # Remove stories already posted today
    fresh_articles = filter_duplicates(articles, already_posted)
    print(f"Fresh articles after duplicate filter: {len(fresh_articles)}\n")

    if len(fresh_articles) < 3:
        print("Not enough fresh stories to build a bulletin. Skipping this run.")
        return

    pakistan = filter_pakistan_news(fresh_articles)
    if not pakistan:
        print("No Pakistan news found. Exiting.")
        return

    stories = generate_cards(pakistan)
    post_text = build_post_text(stories, pakistan)

    success = post_to_facebook(post_text)

    # If posted (or in test mode), record the titles so future runs skip them
    if success and not TEST_MODE:
        new_titles = already_posted + [a["title"] for a in pakistan]
        save_posted_titles(new_titles)
        print(f"ℹ Saved {len(new_titles)} posted titles to logs/posted_titles.json")

    print("=== Done ===\n")

if __name__ == "__main__":
    main()