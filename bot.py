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
MIN_TASK_SCORE = 45  # Balanced threshold to capture all viable tasks

# --- LIVE FREELANCE & TASK FEEDS ---
FEEDS = {
    "Freelancer.com Projects": "https://www.freelancer.com/rss.xml",
    "CryptoJobs Bounties": "https://cryptojobslist.com/rss/freelance",
    "WWR Contracts": "https://weworkremotely.com/categories/remote-contract-jobs.rss",
    "Remotive Tech": "https://remotive.com/remote-jobs/feed?category=software-development"
}

# --- STABLE HASH FUNCTION ---
def get_stable_id(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]

# --- METADATA SANITIZER ---
def sanitize_payload(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip())
    cleaned = re.sub(r"\n?```$", "", cleaned.strip())
    cleaned = re.sub(r"(?i)(author|developer|user|created by):\s*.*", "", cleaned)
    cleaned = re.sub(r"(/home/|/Users/|[A-Za-z]:\\Users\\)[a-zA-Z0-9_-]+", "/app", cleaned)
    cleaned = re.sub(r"192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|127\.0\.0\.1", "0.0.0.0", cleaned)
    return cleaned.strip()

# --- STATE STORAGE ---
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
        print(f"[-] Telegram Sync: {e}")
    return state

# --- GEMINI INFERENCE CALL ---
def call_gemini(prompt: str, temperature: float = 0.2):
    if not GEMINI_API_KEY:
        print("[-] Missing GEMINI_API_KEY.")
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
        else:
            print(f"[-] Gemini No Candidates: {data}")
    except Exception as e:
        print(f"[-] Gemini Error: {e}")
    return None

# --- EVALUATOR & SOLUTION GENERATOR ---
def process_freelance_project(title: str, description: str):
    clean_desc = re.sub(r"<[^>]+>", " ", description)
    clean_desc = re.sub(r"\s+", " ", clean_desc)[:3000]

    prompt = f"""
    You are an automated freelance contract screener and senior Python developer.
    Evaluate this freelance listing:

    Title: {title}
    Details: {clean_desc}

    Tasks:
    1. Score from 0 to 100 on whether this is a technical software task, script, scraper, bot, automation, API, or bug fix (reject full-time non-technical roles).
    2. Write a 2-3 sentence confident, professional human pitch (no generic fluff like 'thrilled' or 'delve').
    3. Generate a complete, working Python script prototype for this task with error handling.

    Return ONLY raw JSON (no markdown formatting, no code fences):
    {{
        "fit_score": <int 0-100>,
        "is_technical_task": <true/false>,
        "task_name": "<Short 5-word summary>",
        "suggested_bid": "<e.g. $100 - $300 Fixed>",
        "turnaround": "<e.g. 24 Hours>",
        "pitch": "<2-3 sentence pitch>",
        "code": "<Complete Python prototype>"
    }}
    """
    raw_response = call_gemini(prompt, temperature=0.2)
    if not raw_response:
        return None

    try:
        clean_json = re.sub(r"^```[a-zA-Z]*\n?", "", raw_response.strip())
        clean_json = re.sub(r"\n?```$", "", clean_json.strip())
        match = re.search(r"\{.*\}", clean_json, re.DOTALL)
        if match:
            clean_json = match.group(0)
        result = json.loads(clean_json)
    except Exception as err:
        print(f"[-] JSON Parse Error for '{title[:30]}': {err}")
        return None

    score = result.get("fit_score", 0)
    is_tech = result.get("is_technical_task", False)

    if score < MIN_TASK_SCORE or not is_tech:
        print(f"[*] Rejected: '{title[:35]}...' (Score: {score}, Tech: {is_tech})")
        return None

    return {
        "score": score,
        "task": result.get("task_name", title[:30]),
        "bid": result.get("suggested_bid", "Fixed Rate"),
        "turnaround": result.get("turnaround", "24h"),
        "pitch": result.get("pitch", ""),
        "code": sanitize_payload(result.get("code", ""))
    }

# --- TELEGRAM DISPATCH ---
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

# --- MAIN EXECUTION ---
def main():
    state = load_state()
    state = sync_telegram_actions(state)

    dispatched = 0
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TaskEngine/7.0"}

    for source_name, feed_url in FEEDS.items():
        try:
            req = requests.get(feed_url, headers=headers, timeout=15)
            feed = feedparser.parse(req.content)
            print(f"\n[+] Scanning {source_name}: {len(feed.entries)} listings found.")

            for entry in feed.entries[:10]:
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
                        print(f" [✓] DISPATCHED TO TELEGRAM: {title[:35]}")
                        time.sleep(1)

        except Exception as e:
            print(f"[-] Error on {source_name}: {e}")

    pending_count = sum(1 for v in state.values() if v.get("status") == "PENDING")
    submitted_count = sum(1 for v in state.values() if v.get("status") == "SUBMITTED")
    
    save_state(state)
    print(f"\n==========================================")
    print(f"Dispatched: {dispatched} new tasks.")
    print(f"Ledger Status: {pending_count} PENDING | {submitted_count} SUBMITTED")
    print(f"==========================================")

if __name__ == "__main__":
    main()
