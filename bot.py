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
MIN_TASK_SCORE = 50

# --- TARGET FEEDS ---
FEEDS = {
    "Freelancer.com Projects": "https://www.freelancer.com/rss.xml",
    "CryptoJobs Bounties": "https://cryptojobslist.com/rss/freelance",
    "WWR Contracts": "https://weworkremotely.com/categories/remote-contract-jobs.rss",
    "Remotive Tech Tasks": "https://remotive.com/remote-jobs/feed?category=software-development"
}

# --- PRE-FILTER REGEX (INSTANT ZERO-LATENCY SKIPS) ---
EMPLOYMENT_TERMS = [
    r"\bfull[\s-]?time\b", r"\bpart[\s-]?time\b", r"\bsalary\b", r"\bannual\b",
    r"\bper annum\b", r"\bbenefits\b", r"\b401k\b", r"\bw2\b", r"\bpermanent\b",
    r"\bmonthly retainer\b", r"\bequity\b", r"\bstaff engineer\b", r"\bsenior role\b",
    r"\binterview process\b", r"\bsend resume\b", r"\bsend cv\b"
]

def is_strict_employment(text: str) -> bool:
    lower = text.lower()
    return any(re.search(term, lower) for term in EMPLOYMENT_TERMS)

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

# --- UNIFIED SINGLE-PASS GEMINI 3.7 THINKING INFERENCE ---
def process_freelance_project(title: str, description: str):
    full_text = f"{title} {description}"
    if is_strict_employment(full_text):
        print(f"[*] Pre-Filter Dropped (Employment terms): '{title[:35]}'")
        return None

    clean_desc = re.sub(r"<[^>]+>", " ", description)
    clean_desc = re.sub(r"\s+", " ", clean_desc)[:2500]

    prompt = f"""
    Analyze this freelance project listing as an Elite Technical Consultant & Python Engineer.
    
    Listing Title: {title}
    Listing Details: {clean_desc}

    Perform the following internal steps:
    1. Filter: Check if this is a discrete 1-off technical task (scraper, bot, script, API bridge, data cleaning, automation). Reject full-time jobs or non-technical requests.
    2. Proposal: Write a concise, confident 2-3 sentence human pitch with a fixed-rate flat quote and a 24-48h turnaround commitment (no AI cliches like 'delve', 'thrilled', 'testament').
    3. Code: Write a production-ready, complete Python solution prototype with type annotations and clean exception handling.

    Return ONLY a single valid JSON object (no markdown wrappers, no backticks):
    {{
        "fit_score": <int 0-100>,
        "is_one_off": <true/false>,
        "task_name": "<Short 5-word deliverable name>",
        "suggested_bid": "<e.g. $150 - $350 Flat Rate>",
        "turnaround": "24-48 Hours",
        "pitch": "<2-3 sentence proposal>",
        "code": "<Complete executable Python code>"
    }}
    """

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.7-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "thinkingConfig": {"thinkingBudget": 1024}
        }
    }

    try:
        res = requests.post(url, json=payload, timeout=25)
        data = res.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return None

        parts = candidates[0].get("content", {}).get("parts", [])
        raw_text = ""
        for part in parts:
            if "text" in part and not part.get("thought", False):
                raw_text = part["text"].strip()
                break

        if not raw_text and parts:
            raw_text = parts[0].get("text", "").strip()

        clean_json = re.sub(r"^```[a-zA-Z]*\n?", "", raw_text.strip())
        clean_json = re.sub(r"\n?```$", "", clean_json.strip())
        match = re.search(r"\{.*\}", clean_json, re.DOTALL)
        result = json.loads(match.group(0)) if match else json.loads(clean_json)

        if result.get("fit_score", 0) < MIN_TASK_SCORE or not result.get("is_one_off", False):
            print(f"[*] Rejected: '{title[:35]}...' (Score: {result.get('fit_score')}, OneOff: {result.get('is_one_off')})")
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

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

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

    if package.get("code"):
        code_card = f"🛠 <b>Solution Prototype:</b>\n<pre><code class='language-python'>{html.escape(package['code'][:3800])}</code></pre>"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": code_card,
            "parse_mode": "HTML"
        }, timeout=10)

# --- MAIN LOOP ---
def main():
    state = load_state()
    state = sync_telegram_actions(state)

    dispatched = 0
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TaskEngine/10.0"}

    for source_name, feed_url in FEEDS.items():
        try:
            req = requests.get(feed_url, headers=headers, timeout=10)
            feed = feedparser.parse(req.content)
            print(f"\n[+] Scanning {source_name}: {len(feed.entries)} listings found.")

            # Scan top 4 most recent listings to ensure fast execution
            for entry in feed.entries[:4]:
                task_id = getattr(entry, "id", entry.link)
                short_id = get_stable_id(task_id)

                if short_id not in state:
                    title = entry.title
                    link = entry.link
                    desc = getattr(entry, "summary", title)

                    print(f" -> Evaluating: {title[:40]}...")
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
                        print(f" [✓] DISPATCHED: {title[:30]}")
                        time.sleep(0.5)

        except Exception as e:
            print(f"[-] Error on {source_name}: {e}")

    pending_count = sum(1 for v in state.values() if v.get("status") == "PENDING")
    submitted_count = sum(1 for v in state.values() if v.get("status") == "SUBMITTED")

    save_state(state)
    print(f"\n==========================================")
    print(f"Dispatched: {dispatched} one-off tasks.")
    print(f"Ledger Status: {pending_count} PENDING | {submitted_count} SUBMITTED")
    print(f"==========================================")

if __name__ == "__main__":
    main()
