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

# --- SNIPER THRESHOLDS (PRESERVE YOUR LAST 5 BIDS) ---
MAX_EXISTING_BIDS = 5          # Discard if more than 5 freelancers already bid
MAX_PROJECT_AGE_MINUTES = 15   # Discard if posted more than 15 minutes ago

# --- HIGH-YIELD NICHE QUERIES (LOW BOT DENSITY) ---
TARGET_QUERIES = [
    "python", "web scraping", "data extraction", "automation script",
    "csv", "excel script", "telegram bot", "discord bot",
    "fix script", "api integration", "selenium", "playwright"
]

# --- EXTERNAL COMMUNITY FEEDS ---
EXTERNAL_FEEDS = {
    "RemoteOK Python & Scripts": "https://remoteok.com/remote-python-jobs.rss"
}

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

# --- FILTER 3: REJECT UPFRONT PAYMENT / BALANCE / MEMBERSHIP GATES ---
SCAM_BALANCE_TERMS = [
    r"\bregistration fee\b", r"\bdeposit required\b", r"\bpay upfront\b",
    r"\binitial investment\b", r"\bbuy equipment\b", r"\bsecurity deposit\b",
    r"\bpay to apply\b", r"\bprocessing fee\b", r"\bpurchase software\b",
    r"\btraining fee\b", r"\bmembership fee\b", r"\bminimum balance\b",
    r"\baccount balance\b", r"\bverification fee\b", r"\bpaid test\b"
]

# --- FILTER 4: REJECT PROOF-OF-WORK & PORTFOLIO GATEKEEPERS ---
PORTFOLIO_PROOF_TERMS = [
    r"\bproven track record\b", r"\bshow past work\b", r"\bportfolio required\b",
    r"\bsend portfolio\b", r"\battach past projects\b", r"\bprevious client reviews\b",
    r"\bminimum \d+\+? reviews\b", r"\btop rated only\b", r"\b5[\s-]star rating\b",
    r"\bcase studies required\b", r"\bsend previous code samples\b", r"\bverified experience\b",
    r"\bproof of previous\b", r"\bexperience proof\b", r"\bproven experience\b"
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
        any(re.search(term, lower) for term in SCAM_BALANCE_TERMS) or
        any(re.search(term, lower) for term in PORTFOLIO_PROOF_TERMS)
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

# --- PROCESS TASK (<$100, ZERO GATEKEEPING) ---
def process_freelance_project(title: str, description: str):
    title = html.unescape(title)
    description = html.unescape(description)
    full_text = f"{title} {description}"

    if is_unwanted_task(full_text) or not is_tech_task(full_text):
        return None

    clean_desc = re.sub(r"<[^>]+>", " ", description)
    clean_desc = re.sub(r"\s+", " ", clean_desc)[:2500]

    prompt = f"""
    You are an automated technical screener looking ONLY for pure direct delivery micro-tasks (<$100).

    STRICT REJECTION CRITERIA (Set fit_score = 0 and is_micro_task = false if matched):
    1. Demands past portfolios, past reviews, 5-star profile ratings, or proofs of prior client work.
    2. Requires paying any fee, subscription, account deposit, software purchase, or balance requirement.
    3. Requires physical on-site presence, phone calls, or shipping hardware.
    4. Full-time, salaried, or multi-week commitments.

    CRITICAL TONE RULES FOR PITCH (ANTI-AI DETECTION):
    - NO greetings ("Dear client", "Hello", "Hope you are doing well").
    - NEVER use AI buzzwords ("delve", "testament", "seamless", "thrilled", "cutting-edge").
    - Exactly 2 sentences:
      * Sentence 1: Name the exact Python library/logic you will use and state you can deliver it immediately.
      * Sentence 2: State a flat rate between $25 and $75 with delivery within 4-12 hours.

    Project Title: {title}
    Project Details: {clean_desc}

    Return ONLY raw JSON (no markdown formatting, no code fences):
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

# --- TELEGRAM DISPATCH WITH COMPETITION STATS ---
def dispatch_task_alert(source, title, link, package, short_id, bid_count, age_mins):
    card = (
        f"⚡ <b>SNIPER BOUNTY [&lt;$100] [{package['score']}/100]</b>\n"
        f"🌐 <b>Source:</b> {html.escape(source)}\n"
        f"📌 <b>Project:</b> {html.escape(title)}\n"
        f"🎯 <b>Deliverable:</b> <code>{html.escape(str(package['task']))}</code>\n\n"
        f"👥 <b>Competition:</b> {bid_count} existing bids | ⏱ <b>Posted:</b> {age_mins}m ago\n"
        f"💰 <b>Suggested Bid:</b> {html.escape(str(package['bid']))}\n"
        f"⏱ <b>Turnaround:</b> {html.escape(str(package['turnaround']))}\n\n"
        f"💬 <b>Human Pitch (Copy & Paste):</b>\n"
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

# --- SCRAPER 1: FREELANCER API WITH SNIPER FILTERS ---
def fetch_freelancer_api_projects():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SniperEngine/23.0"}
    collected = []
    now = time.time()

    for query in TARGET_QUERIES:
        api_url = f"https://www.freelancer.com/api/projects/0.1/projects/active/?query={query}&sort_field=time_updated&reverse_sort=true&limit=15&compact=true&job_details=true"
        try:
            res = requests.get(api_url, headers=headers, timeout=10).json()
            projects = res.get("result", {}).get("projects", [])
            for p in projects:
                # 1. Proposal count check
                bid_count = p.get("bid_stats", {}).get("bid_count")
                if bid_count is None:
                    bid_count = p.get("bid_count", 0)

                if bid_count > MAX_EXISTING_BIDS:
                    continue  # Discard high-competition listings

                # 2. Freshness check
                submit_time = p.get("submitdate") or p.get("time_submitted") or now
                age_minutes = int((now - submit_time) / 60)

                if age_minutes > MAX_PROJECT_AGE_MINUTES:
                    continue  # Discard older listings

                seo_url = p.get("seo_url", str(p.get("id")))
                collected.append({
                    "id": str(p.get("id")),
                    "title": p.get("title", ""),
                    "description": p.get("preview_description", p.get("description", "")),
                    "link": f"https://www.freelancer.com/projects/{seo_url}",
                    "source": f"Freelancer ({query})",
                    "bid_count": bid_count,
                    "age_mins": max(1, age_minutes)
                })
        except Exception as e:
            print(f"[-] API Error on '{query}': {e}")
    return collected

# --- SCRAPER 2: EXTERNAL PUBLIC RSS FEEDS ---
def fetch_external_rss_feeds():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SniperFeedReader/23.0"}
    collected = []

    for source_name, feed_url in EXTERNAL_FEEDS.items():
        try:
            req = requests.get(feed_url, headers=headers, timeout=10)
            feed = feedparser.parse(req.content)
            for entry in feed.entries[:10]:
                task_id = getattr(entry, "id", entry.link)
                collected.append({
                    "id": task_id,
                    "title": entry.title,
                    "description": getattr(entry, "summary", entry.title),
                    "link": entry.link,
                    "source": source_name,
                    "bid_count": 0,
                    "age_mins": 5
                })
        except Exception as e:
            print(f"[-] RSS Error on '{source_name}': {e}")
    return collected

# --- MAIN EXECUTION ---
def main():
    state = load_state()
    state = sync_telegram_actions(state)
    dispatched = 0

    print("[+] Running Sniper Scan (Max 5 Bids, Max 15 mins old)...")
    fl_projects = fetch_freelancer_api_projects()
    print(f"[+] Found {len(fl_projects)} fresh low-competition Freelancer tasks.")

    ext_projects = fetch_external_rss_feeds()
    all_listings = fl_projects + ext_projects

    for item in all_listings:
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
                dispatch_task_alert(
                    item["source"],
                    item["title"],
                    item["link"],
                    package,
                    short_id,
                    item.get("bid_count", 0),
                    item.get("age_mins", 1)
                )
                dispatched += 1
                print(f" [✓] DISPATCHED: {item['title'][:35]}")
                time.sleep(0.5)

    pending_count = sum(1 for v in state.values() if v.get("status") == "PENDING")
    submitted_count = sum(1 for v in state.values() if v.get("status") == "SUBMITTED")

    save_state(state)
    print(f"\n==========================================")
    print(f"Dispatched: {dispatched} sniper-tier micro-tasks.")
    print(f"Ledger Status: {pending_count} PENDING | {submitted_count} SUBMITTED")
    print(f"==========================================")

if __name__ == "__main__":
    main()
