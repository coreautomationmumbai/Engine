import os
import json
import html
import re
import time
import requests
import feedparser

# --- CREDENTIALS & SECRETS ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
STATE_FILE = "jobs_state.json"
MIN_TASK_SCORE = 50

# --- ACTIVE PROJECT & BOUNTY FEEDS ---
FEEDS = {
    "Freelancer.com Live Feed": "https://www.freelancer.com/rss.xml",
    "CryptoJobs Freelance Gigs": "https://cryptojobslist.com/rss/freelance",
    "WWR Contract Roles": "https://weworkremotely.com/categories/remote-contract-jobs.rss",
    "Remotive Tech Tasks": "https://remotive.com/remote-jobs/feed?category=software-development"
}

TASK_KEYWORDS = [
    "python", "script", "scrape", "scraper", "scraping", "bot", "discord",
    "telegram", "automation", "api", "selenium", "playwright", "crawler",
    "extract", "csv", "fix", "webhook", "integration", "tool"
]

def contains_task_intent(text: str) -> bool:
    lower = text.lower()
    return any(re.search(r'\b' + re.escape(kw) + r'\b', lower) for kw in TASK_KEYWORDS)

def sanitize_payload(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip())
    cleaned = re.sub(r"\n?```$", "", cleaned.strip())
    cleaned = re.sub(r"(?i)(author|developer|user|created by):\s*.*", "", cleaned)
    cleaned = re.sub(r"(/home/|/Users/|[A-Za-z]:\\Users\\)[a-zA-Z0-9_-]+", "/app", cleaned)
    cleaned = re.sub(r"192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|127\.0\.0\.1", "0.0.0.0", cleaned)
    return cleaned.strip()

# --- STRUCTURED LEDGER STATE ---
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            return {}
    return {}

def save_state(state):
    # Keep last 500 records to prevent memory bloat
    trimmed = dict(list(state.items())[-500:])
    with open(STATE_FILE, "w") as f:
        json.dump(trimmed, f, indent=2)

# --- PROCESS TELEGRAM BUTTON CLICKS (CALLBACKS) ---
def sync_telegram_actions(state):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        res = requests.get(url, timeout=10).json()
        if not res.get("ok"):
            return state

        for update in res.get("result", []):
            if "callback_query" in update:
                cb = update["callback_query"]
                data = cb.get("data", "")
                cb_id = cb.get("id")

                if data.startswith("sub_"):
                    job_key = data.replace("sub_", "")
                    if job_key in state:
                        state[job_key]["status"] = "SUBMITTED"
                        state[job_key]["submitted_at"] = time.time()
                        # Acknowledge Telegram notification
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", 
                                      json={"callback_query_id": cb_id, "text": "✅ Marked as SUBMITTED! Removed from retry pool."})
                
                elif data.startswith("skip_"):
                    job_key = data.replace("skip_", "")
                    if job_key in state:
                        state[job_key]["status"] = "SKIPPED"
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", 
                                      json={"callback_query_id": cb_id, "text": "❌ Marked as SKIPPED."})
    except Exception as e:
        print(f"[-] Telegram Sync Notice: {e}")
    return state

# --- GEMINI INFERENCE ---
def call_gemini(prompt: str, temperature: float = 0.2):
    if not GEMINI_API_KEY:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature}
    }
    try:
        res = requests.post(url, json=payload, timeout=30)
        data = res.json()
        candidates = data.get("candidates", [])
        if candidates:
            return candidates[0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"[-] Gemini Exception: {e}")
    return None

# --- EVALUATION & PROPOSAL BUILDER ---
def process_freelance_project(title: str, description: str):
    clean_desc = re.sub(r"<[^>]+>", " ", description)
    clean_desc = re.sub(r"\s+", " ", clean_desc)[:3000]
    is_keyword_match = contains_task_intent(f"{title} {clean_desc}")

    prompt = f"""
    Evaluate this freelance project and construct a client-ready response.
    Title: {title}
    Details: {clean_desc}

    Tasks:
    1. Score from 0 to 100 on whether this is a discrete single-task / script / scraper / bot gig (reject full-time jobs).
    2. Write a 2-3 sentence confident human pitch.
    3. Generate a complete, working Python script prototype for this task with error handling.

    Return ONLY raw JSON:
    {{
        "fit_score": <int 0-100>,
        "is_single_task": <true/false>,
        "task_name": "<Short 5-word summary>",
        "suggested_bid": "<e.g. $100 - $300 Fixed>",
        "turnaround": "<e.g. 24 Hours>",
        "pitch": "<2-3 sentence proposal>",
        "code": "<Complete Python script prototype>"
    }}
    """
    raw_response = call_gemini(prompt, temperature=0.2)
    if not raw_response:
        return None

    try:
        clean_json = re.sub(r"^```[a-zA-Z]*\n?", "", raw_response.strip())
        clean_json = re.sub(r"\n?```$", "", clean_json.strip())
        match = re.search(r'\{.*\}', clean_json, re.DOTALL)
        if match:
            clean_json = match.group(0)
        result = json.loads(clean_json)
    except Exception:
        return None

    score = result.get("fit_score", 0)
    if is_keyword_match and score < MIN_TASK_SCORE:
        score = 70

    if score < MIN_TASK_SCORE or not result.get("is_single_task", False):
        return None

    return {
        "score": score,
        "task": result.get("task_name", title[:30]),
        "bid": result.get("suggested_bid", "Fixed Rate"),
        "turnaround": result.get("turnaround", "24h"),
        "pitch": result.get("pitch", ""),
        "code": sanitize_payload(result.get("code", ""))
    }

# --- DISPATCH WITH INTERACTIVE BUTTONS ---
def dispatch_task_alert(source, title, link, package, short_id):
    card = (
        f"⚡ <b>FREELANCE TASK [{package['score']}/100]</b>\n"
        f"🌐 <b>Platform:</b> {html.escape(source)}\n"
        f"📌 <b>Project:</b> {html.escape(title)}\n"
        f"🎯 <b>Deliverable:</b> <code>{html.escape(str(package['task']))}</code>\n\n"
        f"💰 <b>Recommended Quote:</b> {html.escape(str(package['bid']))}\n"
        f"⏱ <b>Turnaround:</b> {html.escape(str(package['turnaround']))}\n\n"
        f"📝 <b>Custom Proposal:</b>\n"
        f"<code>{html.escape(str(package['pitch']))}</code>"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    # Message 1: Pitch Card with Submission Status Buttons
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": card[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "🚀 Open Project Listing", "url": link}],
                [
                    {"text": "✅ Mark Submitted", "callback_data": f"sub_{short_id}"},
                    {"text": "❌ Skip / Discard", "callback_data": f"skip_{short_id}"}
                ]
            ]
        }
    }, timeout=10)

    # Message 2: Ready Code Prototype
    if package.get("code"):
        code_card = f"🛠 <b>Solution Prototype:</b>\n<pre><code class='language-python'>{html.escape(package['code'][:3800])}</code></pre>"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": code_card,
            "parse_mode": "HTML"
        }, timeout=10)

# --- MAIN EXECUTION ---
def main():
    state = load_state()
    # 1. Sync button presses from Telegram
    state = sync_telegram_actions(state)

    dispatched = 0
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TaskEngine/6.0"}

    # 2. Check for new jobs across feeds
    for source_name, feed_url in FEEDS.items():
        try:
            req = requests.get(feed_url, headers=headers, timeout=15)
            feed = feedparser.parse(req.content)
            
            for entry in feed.entries[:8]:
                task_id = getattr(entry, "id", entry.link)
                short_id = str(abs(hash(task_id)))[:10]  # Short hash for Telegram callback limits

                # If completely new: evaluate and add to state ledger
                if short_id not in state:
                    title = entry.title
                    link = entry.link
                    desc = getattr(entry, "summary", title)

                    package = process_freelance_project(title, desc)
                    if package:
                        state[short_id] = {
                            "title": title,
                            "link": link,
                            "status": "PENDING",
                            "created_at": time.time(),
                            "package": package
                        }
                        dispatch_task_alert(source_name, title, link, package, short_id)
                        dispatched += 1
                        time.sleep(0.5)

        except Exception as e:
            print(f"[-] Error on {source_name}: {e}")

    # 3. Log current status summary
    pending_count = sum(1 for v in state.values() if v.get("status") == "PENDING")
    submitted_count = sum(1 for v in state.values() if v.get("status") == "SUBMITTED")
    
    save_state(state)
    print(f"\n==========================================")
    print(f"Dispatched: {dispatched} new tasks.")
    print(f"Ledger Status: {pending_count} PENDING | {submitted_count} SUBMITTED")
    print(f"==========================================")

if __name__ == "__main__":
    main()
