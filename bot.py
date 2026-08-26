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
MIN_TASK_SCORE = 55

# --- TARGET FEEDS ---
FEEDS = {
    "Freelancer.com Projects": "https://www.freelancer.com/rss.xml",
    "CryptoJobs Bounties": "https://cryptojobslist.com/rss/freelance",
    "WWR Contracts": "https://weworkremotely.com/categories/remote-contract-jobs.rss",
    "Remotive Tech Tasks": "https://remotive.com/remote-jobs/feed?category=software-development"
}

# --- PRE-FILTER: STRICT EMPLOYMENT REJECTION LIST ---
EMPLOYMENT_TERMS = [
    r"\bfull[\s-]?time\b", r"\bpart[\s-]?time\b", r"\bsalary\b", r"\bannual\b",
    r"\bper annum\b", r"\bbenefits\b", r"\b401k\b", r"\bw2\b", r"\bhealth insurance\b",
    r"\bjoin our team\b", r"\bpermanent\b", r"\bhourly rate ongoing\b",
    r"\bmonthly retainer\b", r"\bequity\b", r"\bstaff engineer\b", r"\bsenior role\b",
    r"\binterview process\b", r"\bsend resume\b", r"\bsend cv\b"
]

TASK_ACCEPT_TERMS = [
    r"\bpython\b", r"\bscript\b", r"\bscrape\b", r"\bscraper\b", r"\bscraping\b",
    r"\bbot\b", r"\bautomation\b", r"\bapi\b", r"\bwebhook\b", r"\bfix\b",
    r"\bcrawler\b", r"\bextract\b", r"\bcsv\b", r"\bparser\b", r"\btool\b"
]

def is_strict_employment(text: str) -> bool:
    lower = text.lower()
    return any(re.search(term, lower) for term in EMPLOYMENT_TERMS)

def contains_task_keywords(text: str) -> bool:
    lower = text.lower()
    return any(re.search(term, lower) for term in TASK_ACCEPT_TERMS)

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
        res = requests.get(url, timeout=10).json()
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
                                json={"callback_query_id": cb_id, "text": "✅ Marked as SUBMITTED!"}
                            )
                    elif data.startswith("skip_"):
                        job_key = data.replace("skip_", "")
                        if job_key in state:
                            state[job_key]["status"] = "SKIPPED"
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                                json={"callback_query_id": cb_id, "text": "❌ Marked as SKIPPED."}
                            )
    except Exception as e:
        print(f"[-] Telegram Sync Notice: {e}")
    return state

# --- GEMINI 3.7 FLASH EXTENDED CALL ---
def call_gemini(system_prompt: str, user_prompt: str, thinking_budget: int = 2048):
    if not GEMINI_API_KEY:
        print("[-] Missing GEMINI_API_KEY.")
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.7-flash:generateContent?key={GEMINI_API_KEY}"
    
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": f"SYSTEM INSTRUCTION: {system_prompt}\n\nUSER PROMPT: {user_prompt}"}]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "thinkingConfig": {
                "thinkingBudget": thinking_budget
            }
        }
    }

    try:
        res = requests.post(url, json=payload, timeout=60)
        data = res.json()

        if res.status_code != 200:
            print(f"[-] API Error [{res.status_code}]: {data.get('error', {}).get('message', res.text)}")
            return None

        candidates = data.get("candidates", [])
        if not candidates:
            return None

        parts = candidates[0].get("content", {}).get("parts", [])
        for part in parts:
            if "text" in part and not part.get("thought", False):
                return part["text"].strip()

        return parts[0]["text"].strip() if parts else None
    except Exception as e:
        print(f"[-] Gemini Exception: {e}")
        return None

# --- 3-AGENT REASONING (ONE-OFF STRICT ENFORCEMENT) ---
def process_freelance_project(title: str, description: str):
    full_text = f"{title} {description}"
    
    # 1. Fast regex rejection of employment posts
    if is_strict_employment(full_text):
        print(f"[*] Pre-Filter Dropped (Employment terms detected): '{title[:40]}'")
        return None

    clean_desc = re.sub(r"<[^>]+>", " ", description)
    clean_desc = re.sub(r"\s+", " ", clean_desc)[:3500]

    # --- AGENT 1: STRICT ONE-OFF TRIAGE ---
    screener_sys = """You are a Strict Micro-Contract Screener.
Your ONLY goal is to accept standalone, 1-off discrete technical tasks (scripts, scrapers, bots, API bridges, bug fixes).

MANDATORY REJECTION CRITERIA:
- REJECT (fit_score: 0, is_one_off: false) if this is a salaried job, full-time/part-time employment, requires daily standups/meetings, or asks for resumes.
- ACCEPT (fit_score: 70-100, is_one_off: true) ONLY if it is a single deliverable with an immediate flat-rate turnaround.
"""
    screener_usr = f"""
    Title: {title}
    Details: {clean_desc}

    Return ONLY raw JSON (no markdown formatting, no code fences):
    {{
        "fit_score": <int 0-100>,
        "is_one_off": <true/false>,
        "task_summary": "<Short 5-word summary of the standalone deliverable>"
    }}
    """
    res1 = call_gemini(screener_sys, screener_usr, thinking_budget=1024)
    if not res1:
        return None

    try:
        clean_json = re.sub(r"^```[a-zA-Z]*\n?", "", res1.strip())
        clean_json = re.sub(r"\n?```$", "", clean_json.strip())
        match = re.search(r"\{.*\}", clean_json, re.DOTALL)
        data1 = json.loads(match.group(0)) if match else json.loads(clean_json)
    except Exception:
        return None

    if data1.get("fit_score", 0) < MIN_TASK_SCORE or not data1.get("is_one_off", False):
        print(f"[*] Agent 1 Rejected: '{title[:35]}...' (Score: {data1.get('fit_score')}, OneOff: {data1.get('is_one_off')})")
        return None

    # --- AGENT 2: PRINCIPAL DEVELOPER (SOLVER & PITCH) ---
    builder_sys = "You are a Senior Python Developer. Create a concise, human flat-rate pitch and an executable standalone Python prototype."
    builder_usr = f"""
    Task: {data1.get('task_summary', title)}
    Context: {clean_desc}

    Requirements:
    1. Write a 2-3 sentence confident, human pitch with a fixed flat-rate quote and 24-48h turnaround commitment (no AI cliches like 'delve', 'thrilled', 'testament').
    2. Write a production-ready, complete Python solution handling the task with clean error handling and type annotations.

    Return ONLY raw JSON:
    {{
        "pitch": "<2-3 sentence human proposal>",
        "code": "<Complete runnable Python code>",
        "suggested_bid": "<e.g. $150 - $350 Flat Rate>"
    }}
    """
    res2 = call_gemini(builder_sys, builder_usr, thinking_budget=2048)
    if not res2:
        return None

    try:
        clean_json2 = re.sub(r"^```[a-zA-Z]*\n?", "", res2.strip())
        clean_json2 = re.sub(r"\n?```$", "", clean_json2.strip())
        match2 = re.search(r"\{.*\}", clean_json2, re.DOTALL)
        data2 = json.loads(match2.group(0)) if match2 else json.loads(clean_json2)
    except Exception:
        return None

    # --- AGENT 3: QA AUDITOR & EXECUTIVE VERIFIER ---
    auditor_sys = "You are a Principal QA and Security Reviewer. Audit the proposal and Python solution for bugs, missing imports, or corporate/AI jargon."
    auditor_usr = f"""
    Task: {data1.get('task_summary')}
    Pitch Draft: {data2.get('pitch')}
    Code Draft: {data2.get('code')}

    Fix any missing imports, edge cases, and eliminate any synthetic AI words.
    Return ONLY raw JSON:
    {{
        "final_pitch": "<Polished proposal>",
        "verified_code": "<Audited Python code>"
    }}
    """
    res3 = call_gemini(auditor_sys, auditor_usr, thinking_budget=1024)
    if not res3:
        final_pitch = data2.get("pitch", "")
        verified_code = data2.get("code", "")
    else:
        try:
            clean_json3 = re.sub(r"^```[a-zA-Z]*\n?", "", res3.strip())
            clean_json3 = re.sub(r"\n?```$", "", clean_json3.strip())
            match3 = re.search(r"\{.*\}", clean_json3, re.DOTALL)
            data3 = json.loads(match3.group(0)) if match3 else json.loads(clean_json3)
            final_pitch = data3.get("final_pitch", data2.get("pitch", ""))
            verified_code = data3.get("verified_code", data2.get("code", ""))
        except Exception:
            final_pitch = data2.get("pitch", "")
            verified_code = data2.get("code", "")

    return {
        "score": data1.get("fit_score", 0),
        "task": data1.get("task_summary", title[:30]),
        "bid": data2.get("suggested_bid", "Flat Quote"),
        "turnaround": "24-48 Hours",
        "pitch": final_pitch,
        "code": sanitize_payload(verified_code)
    }

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
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TaskEngine/9.0"}

    for source_name, feed_url in FEEDS.items():
        try:
            req = requests.get(feed_url, headers=headers, timeout=15)
            feed = feedparser.parse(req.content)
            print(f"\n[+] Scanning {source_name}: {len(feed.entries)} listings found.")

            for entry in feed.entries[:8]:
                task_id = getattr(entry, "id", entry.link)
                short_id = get_stable_id(task_id)

                if short_id not in state:
                    title = entry.title
                    link = entry.link
                    desc = getattr(entry, "summary", title)

                    print(f" -> Evaluating: {title[:45]}...")
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
                        print(f" [✓] DISPATCHED ONE-OFF TASK: {title[:35]}")
                        time.sleep(1)

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
