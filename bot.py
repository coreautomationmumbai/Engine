import os
import json
import html
import re
import requests
import feedparser

# --- ENVIRONMENT VARIABLES ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_FILE = "seen_jobs.json"
MIN_SCORE = 65

JOB_FEEDS = {
    "WWR Python/Backend": "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    "WWR Automation": "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    "RemoteOK Tech": "https://remoteok.com/remote-jobs.rss"
}

# --- CACHE HELPERS ---
def load_cache():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_cache(cache_set):
    with open(DB_FILE, "w") as f:
        json.dump(list(cache_set)[-500:], f, indent=2)

# --- GEMINI CALL HELPER ---
def query_gemini(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2}
    }
    try:
        res = requests.post(url, json=payload, timeout=40)
        return res.json()['candidates'][0]['content']['parts'][0]['text'].strip()
    except Exception as e:
        print(f"Gemini API Error: {e}")
        return None

# --- MULTI-AGENT EXECUTION PIPELINE ---
def process_lead_end_to_end(title, description):
    clean_desc = re.sub(r'<[^>]+>', ' ', description)[:3000]

    # Agent 1: Evaluation & Architecture
    eval_prompt = f"""
    You are an elite autonomous freelance technical director.
    Analyze this posting. Filter for ONE-TIME, fixed-scope projects (scrapers, APIs, automations, scripts).
    Return ONLY a JSON object (no markdown formatting, no ```json).

    Job Title: {title}
    Details: {clean_desc}

    JSON Structure:
    {{
        "fit_score": <int 0-100>,
        "is_one_off": <true/false>,
        "deliverable_title": "<10-word summary of deliverable>",
        "turnaround": "<e.g. 24 Hours>",
        "pitch": "<3-sentence executive pitch with fixed pricing anchor>",
        "technical_spec": "<Clear logic required solve task technical the to>"
    }}
    """
    raw_eval = query_gemini(eval_prompt)
    if not raw_eval:
        return None

    try:
        clean_eval = re.sub(r"^```[a-zA-Z]*\n?", "", raw_eval)
        clean_eval = re.sub(r"\n?```$", "", clean_eval)
        analysis = json.loads(clean_eval)
    except Exception:
        return None

    if analysis.get("fit_score", 0) < MIN_SCORE or not analysis.get("is_one_off", False):
        return None

    # Agent 2: Autonomous Code Builder & Implementation
    build_prompt = f"""
    You are a Principal Software Engineer. Write a production-grade Python solution prototype for this client task.
    
    Task: {analysis['deliverable_title']}
    Technical Spec: {analysis['technical_spec']}

    Requirements:
    - Include full working logic, error handling, and clean code comments.
    - Anonymize all author tags, system paths, and personal references.
    - Provide concise implementation code only.
    """
    generated_code = query_gemini(build_prompt)
    analysis["generated_code"] = generated_code or "# Prototype generation pending manual scope verification."

    return analysis

# --- TELEGRAM DISPATCH ---
def send_telegram_package(platform, title, link, analysis):
    score = analysis.get("fit_score", 0)
    
    # 1. Send the Executive Pitch Card
    header_card = (
        f"💎 <b>AUTONOMOUS LEAD [{score}/100]</b>\n"
        f"🌐 <b>Source:</b> {html.escape(platform)}\n"
        f"📌 <b>Role:</b> {html.escape(title)}\n"
        f"🎯 <b>Asset:</b> <code>{html.escape(str(analysis.get('deliverable_title', 'N/A')))}</code>\n\n"
        f"⏱ <b>Target Turnaround:</b> {html.escape(str(analysis.get('turnaround', 'N/A')))}\n\n"
        f"📝 <b>Ready-to-Send Pitch:</b>\n"
        f"<code>{html.escape(str(analysis.get('pitch', '')))}</code>"
    )

    url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TELEGRAM_BOT_TOKEN}/sendMessage"
    
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": header_card[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[{"text": "⚡ Open Job Posting", "url": link}]]
        }
    }, timeout=10)

    # 2. Send the Generated Code Prototype as a Separate Message
    code_text = analysis.get("generated_code", "")[:3900]
    code_card = f"🛠 <b>Auto-Generated Prototype:</b>\n<pre><code class='language-python'>{html.escape(code_text)}</code></pre>"
    
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": code_card,
        "parse_mode": "HTML"
    }, timeout=10)

# --- RUNNER ---
def main():
    cache = load_cache()
    dispatched = 0

    for platform, feed_url in JOB_FEEDS.items():
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:6]:
                job_id = getattr(entry, "id", entry.link)
                if job_id not in cache:
                    cache.add(job_id)
                    title = entry.title
                    link = entry.link
                    desc = getattr(entry, "summary", title)

                    analysis = process_lead_end_to_end(title, desc)
                    if analysis:
                        send_telegram_package(platform, title, link, analysis)
                        dispatched += 1
        except Exception as e:
            print(f"Error on {platform}: {e}")

    save_cache(cache)
    print(f"Loop finished. Sent {dispatched} ready-to-deliver assets.")

if __name__ == "__main__":
    main()
