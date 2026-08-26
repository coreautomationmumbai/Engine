import os
import json
import html
import re
import time
import hashlib
import requests
import feedparser

# --- CREDENTIALS & SECRETS ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
STATE_FILE = "jobs_state.json"
MIN_TASK_SCORE = 45

# --- TARGET FEEDS ---
FEEDS = {
    "Freelancer.com Projects": "https://www.freelancer.com/rss.xml",
    "CryptoJobs Bounties": "https://cryptojobslist.com/rss/freelance",
    "WWR Contracts": "https://weworkremotely.com/categories/remote-contract-jobs.rss",
    "Remotive Tech Tasks": "https://remotive.com/remote-jobs/feed?category=software-development"
}

# --- PRE-FILTER: REJECT EMPLOYMENT ---
EMPLOYMENT_TERMS = [
    r"\bfull[\s-]?time\b", r"\bpart[\s-]?time\b", r"\bsalary\b", r"\bannual\b",
    r"\bper annum\b", r"\bbenefits\b", r"\b401k\b", r"\bw2\b", r"\bpermanent\b",
    r"\bmonthly retainer\b", r"\bequity\b", r"\bstaff engineer\b", r"\bsenior role\b",
    r"\binterview process\b", r"\bsend resume\b", r"\bsend cv\b"
]

# --- PRE-FILTER: ACCEPT ONLY PROGRAMMING / SCRIPTING TASKS ---
TECH_KEYWORDS = [
    r"\bpython\b", r"\bscript\b", r"\bscripts\b", r"\bscrape\b", r"\bscraper\b",
    r"\bscraping\b", r"\bbot\b", r"\bbots\b", r"\bdiscord\b", r"\btelegram\b",
    r"\bautomation\b", r"\bautomate\b", r"\bapi\b", r"\bapis\b", r"\bwebhook\b",
    r"\bwebhooks\b", r"\bfix\b", r"\bcrawler\b", r"\bcrawling\b", r"\bextract\b",
    r"\bextraction\b", r"\bcsv\b", r"\bparser\b", r"\bparsing\b", r"\btool\b",
    r"\bselenium\b", r"\bplaywright\b", r"\bbackend\b", r"\bsql\b", r"\bdatabase\b"
]

def is_employment(text: str) -> bool:
    lower = text.lower()
    return any(re.search(term, lower) for term in EMPLOYMENT_TERMS)

def is_tech_task(text: str) -> bool:
    lower = text.lower()
    return any(re.search(term, lower) for term in TECH_KEYWORDS)

def get_stable_id(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]

def sanitize_payload(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip())
    cleaned = re.sub(r"\n?```$", "", cleaned.strip())
    cleaned = re.sub(r"(?i)(author|developer|user|created by):\s*.*", "", cleaned)
    cleaned = re.sub(r"(/home/|/Users/|[A-Za-z]:\\Users\\)[a-zA-Z0-9_-]+", "/app", cleaned)
    cleaned = re.sub(r"192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|127\.0\.0\.1", "0.0.0.0", cleaned)
    return cleaned.strip()

# --- STATE MANAGEMENT ---
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
    trimmed = dict(list(state.items())[-500:])
    with open(STATE_FILE, "w") as f:
        json.dump(trimmed, f, indent=2)

# --- TELEGRAM BUTTON SYNC ---
def sync_telegram_actions(state):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        res = requests.get(url, timeout=5).json()
        if res.get("ok"):
            for update in res.get("result", []):
                if "callback_query" in update:
                    cb = update["callback_query"]
                    data = cb.get("data", "")
                    cb_id = cb.get("id")

                    if data.startswith("sub_"):
                        job_key = data.replace("sub_", "")
                        if job_key in state:
                            state[job_key]["status"] = "SUBMITTED"
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                                json={"callback_query_id": cb_id, "text": "✅ Marked as SUBMITTED!"},
                                timeout=5
                            )
                    elif data.startswith("skip_"):
                        job_key = data.replace("skip_", "")
                        if job_key in state:
                            state[job_key]["status"] = "SKIPPED"
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                                json={"callback_query_id": cb_id, "text": "❌ Marked as SKIPPED."},
                                timeout=5
                            )
    except Exception as e:
        print(f"[-] Telegram Sync: {e}")
    return state

# --- GEMINI INFERENCE ENGINE ---
def process_freelance_project(title: str, description: str):
    full_text = f"{title} {description}"

    # 1. Instant drop for salaried/employment postings
    if is_employment(full_text):
        print(f"[*] Pre-Filter: Dropped Employment Role -> '{title[:40]}'")
        return None

    # 2. Instant drop for non-technical tasks (accounting, writing, voiceover)
    if not is_tech_task(full_text):
        print(f"[*] Pre-Filter: Dropped Non-Tech Task -> '{title[:40]}'")
        return None

    clean_desc = re.sub(r"<[^>]+>", " ", description)
    clean_desc = re.sub(r"\s+", " ", clean_desc)[:2500]

    prompt = f"""
    You are an elite Python Engineer & Technical Freelance Consultant.
    Evaluate this project and construct a client deliverable.

    Title: {title}
    Details: {clean_desc}

    Tasks:
    1. Verify if this is a discrete single-task/scripting/automation project (fit_score: 70-100).
    2. Write a 2-3 sentence confident, human pitch (no generic AI words like 'thrilled', 'delve', 'testament') with fixed flat-rate pricing and 24-48h delivery.
    3. Write a production-ready, complete Python script prototype with clean error handling.

    Return ONLY a single raw JSON object (no markdown backticks, no ```json):
    {{
        "fit_score": 85,
        "is_one_off": true,
        "task_name": "<Short 5-word deliverable summary>",
        "suggested_bid": "<e.g. $150 - $300 Flat Rate>",
        "turnaround": "24-48 Hours",
        "pitch": "<2-3 sentence proposal>",
        "code": "<Complete Python executable prototype script>"
    }}
    """

    url = f"[https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=](https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=){GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2}
    }

    try:
        # Generous 50-second timeout to guarantee completion
        res = requests.post(url, json=payload, timeout=50)
        data = res.json()

        candidates = data.get("candidates", [])
        if not candidates:
            print(f"[-] Gemini No Candidates: {data}")
            return None

        raw_text = candidates[0]["content"]["parts"][0]["text"].strip()
        clean_json = re.sub(r"^```[a-zA-Z]*\n?", "", raw_text)
        clean_json = re.sub(r"\n?```$", "", clean_json.strip())
        match = re.search(r"\{.*\}", clean_json, re.DOTALL)
        result = json.loads(match.group(0)) if match else json.loads(clean_json)

        if result.get("fit_score", 0) < MIN_TASK_SCORE or not result.get("is_one_off", False):
            print(f"[*] AI Rejected: '{title[:35]}...' (Score: {result.get('fit_score')})")
            return None

        return {
            "score": result.get("fit_score", 0),
            "task": result.get("task_name", title[:30]),
            "bid": result.get("suggested_bid", "Flat Quote"),
            "turnaround": result.get("turnaround", "24-48 Hours"),
            "pitch": result.get("pitch", ""),
            "code": sanitize_payload(result.get("code", ""))
        }
    except Exception as e:
        print(f"[-] Processing Error for '{title[:30]}': {e}")
        return None

# --- TELEGRAM DISPATCH ---
def dispatch_task_alert(source, title, link, package, short_id):
    card = (
        f"⚡ <b>ONE-OFF TASK [{package['score']}/100]</b>\n"
        f"🌐 <b>Platform:</b> {html.escape(source)}\n"
        f"📌 <b>Project:</b> {html.escape(title)}\n"
        f"🎯 <b>Deliverable:</b> <code>{html.escape(str(package['task']))}</code>\n\n"
        f"💰 <b>Flat Fee Quote:</b> {html.escape(str(package['bid']))}\n"
        f"⏱ <b>Turnaround:</b> {html.escape(str(package['turnaround']))}\n\n"
        f"📝 <b>Proposal Pitch:</b>\n"
        f"<code>{html.escape(str(package['pitch']))}</code>"
    )

    url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TELEGRAM_BOT_TOKEN}/sendMessage"

    # Send Pitch Card
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

    # Send Working Code Prototype
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
    state = sync_telegram_actions(state)

    dispatched = 0
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TaskEngine/11.0"}

    for source_name, feed_url in FEEDS.items():
        try:
            req = requests.get(feed_url, headers=headers, timeout=10)
            feed = feedparser.parse(req.content)
            print(f"\n[+] Scanning {source_name}: {len(feed.entries)} listings found.")

            for entry in feed.entries[:8]:
                task_id = getattr(entry, "id", entry.link)
                short_id = get_stable_id(task_id)

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
                        print(f" [✓] DISPATCHED: {title[:35]}")
                        time.sleep(1)

        except Exception as e:
            print(f"[-] Error on {source_name}: {e}")

    pending_count = sum(1 for v in state.
