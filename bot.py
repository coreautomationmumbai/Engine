import os
import json
import html
import re
import requests
import feedparser

# --- ENVIRONMENT CREDENTIALS ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_FILE = "seen_jobs.json"
MIN_TASK_SCORE = 65

# --- FREELANCE TASK & BOUNTY FEEDS ---
FEEDS = {
    "Freelancer.com Live Projects": "https://www.freelancer.com/rss.xml",
    "CryptoJobs Freelance Bounties": "https://cryptojobslist.com/rss/freelance",
    "WWR Contract Only": "https://weworkremotely.com/categories/remote-contract-jobs.rss",
    "Nodes Contract Stream": "https://nodes.com/jobs/rss",
    "Remotive Dev Tasks": "https://remotive.com/remote-jobs/feed?category=software-development"
}

# --- METADATA & ARTIFACT SANITIZER ---
def sanitize_payload(text: str) -> str:
    if not text:
        return ""
    # Strip markdown code blocks
    cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip())
    cleaned = re.sub(r"\n?```$", "", cleaned.strip())
    
    # Strip author identifiers and user system paths safely
    cleaned = re.sub(r"(?i)(author|developer|user|created by):\s*.*", "", cleaned)
    cleaned = re.sub(r"(/home/|/Users/|[A-Za-z]:\\Users\\)[a-zA-Z0-9_-]+", "/app", cleaned)
    
    # Standardize local IPs to placeholder
    cleaned = re.sub(r"192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|127\.0\.0\.1", "0.0.0.0", cleaned)
    return cleaned.strip()

# --- CACHE MANAGEMENT ---
def load_cache():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_cache(seen_set):
    with open(DB_FILE, "w") as f:
        json.dump(list(seen_set)[-1000:], f, indent=2)

# --- GEMINI INFERENCE CALL ---
def call_gemini(system_prompt: str, user_prompt: str, temperature: float = 0.2):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=" + str(GEMINI_API_KEY)
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": f"SYSTEM: {system_prompt}\n\nINPUT: {user_prompt}"}]}
        ],
        "generationConfig": {"temperature": temperature}
    }
    try:
        res = requests.post(url, json=payload, timeout=40)
        return res.json()['candidates'][0]['content']['parts'][0]['text'].strip()
    except Exception as e:
        print(f"Gemini API Exception: {e}")
        return None

# --- MULTI-STAGE TASK EVALUATOR & SCRIPT BUILDER ---
def process_freelance_project(title: str, description: str):
    clean_desc = re.sub(r"<[^>]+>", " ", description)
    clean_desc = re.sub(r"\s+", " ", clean_desc)[:3500]

    evaluator_system = """You are an automated freelance contract screener.
Filter strictly for discrete, 1-off technical tasks (e.g. scrapers, scripts, bots, data cleaning, API glue, bug fixes).

STRICT RULES:
1. REJECT (fit_score: 0) any full-time salaried jobs, long-term employment, or posts requiring resumes/interviews.
2. ACCEPT (fit_score: 70-100) only standalone programming tasks with clear, deliverable requirements.

Return ONLY raw JSON (no markdown formatting, no backticks):
{
    "fit_score": 85,
    "is_single_task": true,
    "task_summary": "Summary of deliverable",
    "recommended_bid": "$200 Flat Rate",
    "turnaround": "24 Hours"
}"""

    raw_eval = call_gemini(evaluator_system, f"Title: {title}\nDetails: {clean_desc}", temperature=0.1)
    if not raw_eval:
        return None

    try:
        clean_json = re.sub(r"^```[a-zA-Z]*\n?", "", raw_eval.strip())
        clean_json = re.sub(r"\n?```$", "", clean_json.strip())
        eval_data = json.loads(clean_json)
    except Exception:
        return None

    if eval_data.get("fit_score", 0) < MIN_TASK_SCORE or not eval_data.get("is_single_task", False):
        return None

    builder_system = """You are a Principal Software Consultant.
Write:
1. A human, confident 2-3 sentence pitch tailored to this project (zero generic AI words like 'thrilled', 'delve', 'testament').
2. A production-ready Python solution script handling the task with clean error handling and type annotations.

Return ONLY raw JSON:
{
    "custom_proposal": "Direct proposal with flat turnaround commitment.",
    "python_solution": "Executable Python code prototype"
}"""

    raw_build = call_gemini(builder_system, f"Task: {eval_data.get('task_summary', '')}\nDetails: {clean_desc}", temperature=0.2)
    if not raw_build:
        return None

    try:
        clean_build = re.sub(r"^```[a-zA-Z]*\n?", "", raw_build.strip())
        clean_build = re.sub(r"\n?```$", "", clean_build.strip())
        build_data = json.loads(clean_build)
    except Exception:
        return None

    return {
        "score": eval_data.get("fit_score", 0),
        "task": eval_data.get("task_summary", "Custom Python Script"),
        "bid": eval_data.get("recommended_bid", "Fixed Quote"),
        "turnaround": eval_data.get("turnaround", "24h"),
        "proposal": build_data.get("custom_proposal", ""),
        "code": sanitize_payload(build_data.get("python_solution", ""))
    }

# --- TELEGRAM DISPATCH ---
def dispatch_task_alert(source, title, link, package):
    card = (
        f"⚡ <b>FREELANCE TASK [{package['score']}/100]</b>\n"
        f"🌐 <b>Platform:</b> {html.escape(source)}\n"
        f"📌 <b>Project:</b> {html.escape(title)}\n"
        f"🎯 <b>Deliverable:</b> <code>{html.escape(package['task'])}</code>\n\n"
        f"💰 <b>Recommended Quote:</b> {html.escape(package['bid'])}\n"
        f"⏱ <b>Turnaround:</b> {html.escape(package['turnaround'])}\n\n"
        f"📝 <b>Custom Proposal:</b>\n"
        f"<code>{html.escape(package['proposal'])}</code>"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": card[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[{"text": "🚀 Open Project Listing", "url": link}]]
        }
    }, timeout=10)

    if package["code"]:
        code_card = f"🛠 <b>Solution Prototype:</b>\n<pre><code class='language-python'>{html.escape(package['code'][:3800])}</code></pre>"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": code_card,
            "parse_mode": "HTML"
        }, timeout=10)

# --- MAIN EXECUTION LOOP ---
def main():
    seen = load_cache()
    count = 0
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FreelanceTaskEngine/4.0"}

    for source_name, feed_url in FEEDS.items():
        try:
            req = requests.get(feed_url, headers=headers, timeout=15)
            feed = feedparser.parse(req.content)
            for entry in feed.entries[:8]:
                task_id = getattr(entry, "id", entry.link)
                if task_id not in seen:
                    seen.add(task_id)
                    title = entry.title
                    link = entry.link
                    desc = getattr(entry, "summary", title)

                    package = process_freelance_project(title, desc)
                    if package:
                        dispatch_task_alert(source_name, title, link, package)
                        count += 1
        except Exception as e:
            print(f"Error checking {source_name}: {e}")

    save_cache(seen)
    print(f"Loop finished. Dispatched {count} verified freelance tasks.")

if __name__ == "__main__":
    main()
