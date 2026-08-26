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
RETENTION_SECONDS = 86400  # Auto-purge entries older than 24 hours

# --- TARGET SEARCH QUERIES (FREELANCER PUBLIC API) ---
TARGET_QUERIES = ["python", "web scraping", "data entry", "automation script"]

# --- ACTIVE GEMINI MODELS ---
MODEL_CANDIDATES = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro"
]

# --- FILTER 1: REJECT EMPLOYMENT / SALARIED / LARGE PROJECTS ---
EMPLOYMENT_TERMS = [
    r"\bfull[\s-]?time\b", r"\bpart[\s-]?time\b", r"\bsalary\b", r"\bannual\b",
    r"\bper annum\b", r"\bbenefits\b", r"\b401k\b", r"\bw2\b", r"\bpermanent\b",
    r"\bmonthly retainer\b", r"\bequity\b", r"\bstaff engineer\b", r"\bsenior role\b",
    r"\binterview process\b", r"\bsend resume\b", r"\bsend cv\b", r"\bmvp development\b"
]

# --- FILTER 2: REJECT PHYSICAL / ON-SITE / LOCAL REQUIREMENTS ---
PHYSICAL_TERMS = [
    r"\bon[\s-]?site\b", r"\bin[\s-]?person\b", r"\blocal only\b",
    r"\brelocation\b", r"\bphysical presence\b", r"\boffice visit\b",
    r"\bhardware delivery\b", r"\bshipping\b", r"\bcourier\b",
    r"\bpick[\s-]?up\b", r"\bhand delivery\b", r"\bpostal\b"
]

# --- FILTER 3: REJECT UPFRONT PAYMENT / DEPOSIT SCAMS ---
SCAM_PAYMENT_TERMS = [
    r"\bregistration fee\b", r"\bdeposit required\b", r"\bpay upfront\b",
    r"\binitial investment\b", r"\bbuy equipment\b", r"\bsecurity deposit\b",
    r"\bpay to apply\b", r"\bprocessing fee\b", r"\bpurchase software\b",
    r"\btraining fee\b", r"\bmembership fee\b"
]

# --- INCLUSION FILTER: TECHNICAL MICRO-KEYWORDS ---
TECH_KEYWORDS = [
    r"\bpython\b", r"\bscript\b", r"\bscripts\b", r"\bscrape\b", r"\bscraper\b",
    r"\bscraping\b", r"\bbot\b", r"\bbots\b", r"\bdiscord\b", r"\btelegram\b",
    r"\bautomation\b", r"\bautomate\b", r"\bapi\b", r"\bwebhook\b", r"\bfix\b",
    r"\bcrawler\b", r"\bextract\b", r"\bcsv\b", r"\bparser\b", r"\btool\b",
    r"\bselenium\b", r"\bplaywright\b", r"\bsql\b", r"\bdata\b", r"\bexcel\b"
]

def is_unwanted_task(text: str) -> bool:
    lower = text.lower()
    return (
        any(re.search(term, lower) for term in EMPLOYMENT_TERMS) or
        any(re.search(term, lower) for term in PHYSICAL_TERMS) or
        any(re.search(term, lower) for term in SCAM_PAYMENT_TERMS)
    )

def is_tech_task(text: str) -> bool:
    lower = text.lower()
    return any(re.search(term, lower) for term in TECH_KEYWORDS)

def get_stable_id(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]

# --- SAFE JSON & STRING SANITIZER ---
def parse_json_safely(raw_str: str):
    if not raw_str:
        return None
    clean_str = re.sub(r"^\x60\x60\x60[a-zA-Z]*\n?", "", raw_str.strip())
    clean_str = re.sub(r"\n?\x60\x60\x60$", "", clean_str.strip())
    
    match = re.search(r"\{.*\}", clean_str, re.DOTALL)
    if match:
        clean_str = match.group(0)

    try:
        return json.loads(clean_str, strict=False)
    except Exception:
        pass

    try:
        sanitized = re.sub(r'[\r\n\t]', lambda m: '\\n' if m.group(0) in '\r\n' else '\\t', clean_str)
        return json.loads(sanitized, strict=False)
    except Exception:
        return None

def sanitize_payload(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"^\x60\x60\x60[a-zA-Z]*\n?", "", text.strip())
    cleaned = re.sub(r"\n?\x60\x60\x60$", "", cleaned.strip())
    cleaned = re.sub(r"(?i)(author|developer|user|created by):\s*.*", "", cleaned)
    cleaned = re.sub(r"(/home/|/Users/|[A-Za-z]:\\Users\\)[a-zA-Z0-9_-]+", "/app", cleaned)
    cleaned = re.sub(r"192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|127\.0\.0\.1", "0.0.0.0", cleaned)
    return cleaned.strip()

# --- STATE STORAGE & AUTO-PURGE ---
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
    now = time.time()
    cleaned_state = {
        k: v for k, v in state.items()
        if now - v.get("created_at", now) < RETENTION_SECONDS
    }
    with open(STATE_FILE, "w") as f:
        json.dump(cleaned_state, f, indent=2)

# --- TELEGRAM ACTIONS ---
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
                    message = cb.get("message", {})
                    msg_id = message.get("message_id")
                    chat_id = message.get("chat", {}).get("id")

                    if data.startswith("sub_"):
                        job_key = data.replace("sub_", "")
                        if job_key in state:
                            state[job_key]["status"] = "SUBMITTED"
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                                json={"callback_query_id": cb_id, "text": "✅ Marked as SUBMITTED!"},
                                timeout=5
                            )
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
                                json={
                                    "chat_id": chat_id,
                                    "message_id": msg_id,
                                    "reply_markup": {"inline_keyboard": [[{"text": "✅ SUBMITTED", "callback_data": "done"}]]}
                                },
                                timeout=5
                            )

                    elif data.startswith("skip_"):
                        job_key = data.replace("skip_", "")
                        if job_key in state:
                            state[job_key]["status"] = "SKIPPED"
                            state[job_key].pop("package", None)

                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                                json={"callback_query_id": cb_id, "text": "🗑️ Task discarded and deleted!"},
                                timeout=5
                            )
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage",
                                json={"chat_id": chat_id, "message_id": msg_id},
                                timeout=5
                            )
    except Exception as e:
        print(f"[-] Telegram Sync Notice: {e}")
    return state

# --- GEMINI INFERENCE ENGINE ---
def query_gemini_api(prompt: str):
    if not GEMINI_API_KEY:
        print("[-] GEMINI_API_KEY missing.")
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2}
    }

    for model in MODEL_CANDIDATES:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        for attempt in range(2):
            try:
                res = requests.post(url, json=payload, timeout=50)
                data = res.json()
                if res.status_code == 200:
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        for part in parts:
                            if "text" in part and not part.get("thought", False):
                                return part["text"].strip()
                        if parts:
                            return parts[0].get("text", "").strip()
                elif res.status_code in [429, 503]:
                    time.sleep(2)
                    continue
                else:
                    break
            except Exception:
                break
    return None

# --- PROCESS MICRO-TASK (<$100) ---
def process_freelance_project(title: str, description: str):
    title = html.unescape(title)
    description = html.unescape(description)
    full_text = f"{title} {description}"

    if is_unwanted_task(full_text) or not is_tech_task(full_text):
        return None

    clean_desc = re.sub(r"<[^>]+>", " ", description)
    clean_desc = re.sub(r"\s+", " ", clean_desc)[:2500]

    prompt = f"""
    You are a direct, senior freelance Python engineer applying to a small technical task.
    Screen exclusively for SMALL, 100% REMOTE TECHNICAL TASKS under $100.

    STRICT REJECTION CRITERIA (Score = 0 and is_micro_task = false):
    1. Requires physical presence, on-site hardware, local visits, or mail shipping.
    2. Freelancer must pay a registration fee, equipment deposit, software purchase, or money upfront.
    3. Full-time employment, salaried positions, or long-term multi-week projects.

    CRITICAL TONE RULES FOR PITCH (ANTI-AI DETECTION):
    - NO greetings like "Dear Hiring Manager", "Hello client", or "I hope this finds you well".
    - NEVER use AI buzzwords: "delve", "testament", "seamless", "thrilled", "excited", "spearhead", "cutting-edge", "look no further".
    - Write exactly 2 natural, punchy sentences:
      * Sentence 1: Name the exact library/method you will use and state that you can deliver it immediately.
      * Sentence 2: State a flat rate between $25 and $85 and delivery within 4-12 hours.
    
    CRITICAL CODE RULES:
    - Write clean, runnable Python code with error handling.

    Project Title: {title}
    Project Details: {clean_desc}

    Return ONLY a single valid raw JSON object (no markdown formatting, no code fences):
    {{
        "fit_score": 85,
        "is_micro_task": true,
        "task_name": "<5-word deliverable name>",
        "suggested_bid": "<e.g. $35 Flat Rate / $50 Flat Rate>",
        "turnaround": "4-12 Hours",
        "pitch": "<2-sentence natural human pitch>",
        "code": "<Complete runnable Python prototype>"
    }}
    """

    raw_text = query_gemini_api(prompt)
    if not raw_text:
        return None

    result = parse_json_safely(raw_text)
    if not result:
        return None

    if result.get("fit_score", 0) < MIN_TASK_SCORE or not result.get("is_micro_task", False):
        return None

    return {
        "score": result.get("fit_score", 0),
        "task": result.get("task_name", title[:30]),
        "bid": result.get("suggested_bid", "$45 Flat Rate"),
        "turnaround": result.get("turnaround", "4-12h"),
        "pitch": result.get("pitch", ""),
        "code": sanitize_payload(result.get("code", ""))
    }

# --- TELEGRAM DISPATCH ---
def dispatch_task_alert(source, title, link, package, short_id):
    card = (
        f"⚡ <b>MICRO-TASK BOUNTY [&lt;$100] [{package['score']}/100]</b>\n"
        f"🌐 <b>Source:</b> {html.escape(source)}\n"
        f"📌 <b>Project:</b> {html.escape(title)}\n"
        f"🎯 <b>Deliverable:</b> <code>{html.escape(str(package['task']))}</code>\n\n"
        f"💰 <b>Suggested Bid:</b> {html.escape(str(package['bid']))}\n"
        f"⏱ <b>Turnaround:</b> {html.escape(str(package['turnaround']))}\n\n"
        f"💬 <b>Human Pitch:</b>\n"
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
        code_card = f"🛠 <b>Ready Python Script:</b>\n<pre><code class='language-python'>{html.escape(package['code'][:3800])}</code></pre>"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": code_card,
            "parse_mode": "HTML"
        }, timeout=10)

# --- FETCH FROM FREELANCER PUBLIC API ---
def fetch_freelancer_api_projects():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MicroEngine/20.0"}
    collected_projects = []

    for query in TARGET_QUERIES:
        api_url = f"https://www.freelancer.com/api/projects/0.1/projects/active/?query={query}&sort_field=time_updated&reverse_sort=true&limit=15&compact=true&job_details=true"
        try:
            res = requests.get(api_url, headers=headers, timeout=10).json()
            projects = res.get("result", {}).get("projects", [])
            print(f"[+] API Query '{query}': {len(projects)} live projects retrieved.")
            for p in projects:
                seo_url = p.get("seo_url", str(p.get("id")))
                link = f"https://www.freelancer.com/projects/{seo_url}"
                collected_projects.append({
                    "id": str(p.get("id")),
                    "title": p.get("title", ""),
                    "description": p.get("preview_description", p.get("description", "")),
                    "link": link,
                    "source": f"Freelancer ({query})"
                })
        except Exception as e:
            print(f"[-] API Error for '{query}': {e}")
    return collected_projects

# --- MAIN EXECUTION ---
def main():
    state = load_state()
    state = sync_telegram_actions(state)
    dispatched = 0

    projects = fetch_freelancer_api_projects()

    for item in projects:
        short_id = get_stable_id(item["id"])
        if short_id not in state:
            package = process_freelance_project(item["title"], item["description"])
            if package:
                state[short_id] = {
                    "title": item["title"],
                    "link": item["link"],
                    "status": "PENDING",
                    "created_at": time.time(),
                    "package": package
                }
                dispatch_task_alert(item["source"], item["title"], item["link"], package, short_id)
                dispatched += 1
                print(f" [✓] DISPATCHED: {item['title'][:35]}")
                time.sleep(0.5)

    pending_count = sum(1 for v in state.values() if v.get("status") == "PENDING")
    submitted_count = sum(1 for v in state.values() if v.get("status") == "SUBMITTED")

    save_state(state)
    print(f"\n==========================================")
    print(f"Dispatched: {dispatched} new micro-tasks.")
    print(f"Ledger Status: {pending_count} PENDING | {submitted_count} SUBMITTED")
    print(f"==========================================")

if __name__ == "__main__":
    main()
