import os
import json
import html
import re
import requests
import feedparser

# --- SECRETS & ENV ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_FILE = "seen_jobs.json"
MIN_MATCH_SCORE = 65  # Filters out noise; only alerts on 65%+ matches

# --- EXPANDED HIGH-YIELD RSS FEEDS ---
JOB_FEEDS = {
    "WWR Programming": "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "WWR DevOps/Sysadmin": "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    "WWR All Tech": "https://weworkremotely.com/categories/all-other-remote-jobs.rss",
    "RemoteOK Tech": "https://remoteok.com/remote-jobs.rss",
    "ProBlogger": "https://problogger.com/jobs/feed/"
}

# --- STATE MANAGEMENT ---
def load_seen_jobs():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_seen_jobs(seen_set):
    # Keep last 500 entries to prevent file bloat
    trimmed = list(seen_set)[-500:]
    with open(DB_FILE, "w") as f:
        json.dump(trimmed, f, indent=2)

# --- GEMINI INTELLIGENCE ENGINE ---
def analyze_and_pitch(title, description):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    clean_desc = re.sub(r'<[^>]+>', ' ', description)[:2500]
    
    prompt = f"""
    You are an elite autonomous freelance evaluation engine.
    Analyze this gig listing and respond ONLY in valid raw JSON. No markdown codeblocks (no ```json).

    Job Title: {title}
    Job Details: {clean_desc}

    Return strictly this JSON structure:
    {{
        "match_score": <integer between 0 and 100 on viability for automation/dev/writing>,
        "tech_stack": "<comma-separated primary tools or languages detected>",
        "key_problem": "<1-sentence summary of the core issue the client is solving>",
        "custom_pitch": "<A 'I 'delve', 'testament', 3-sentence AI Focus Zero am and cliches deliverables. execution, human, like on proposal. ruthless, speed, thrilled'.>",
        "solution_angle": "<2-bullet technical roadmap demonstrating domain mastery>"
    }}
    """
    
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    
    try:
        res = requests.post(url, json=payload, timeout=25)
        raw_text = res.json()['candidates'][0]['content']['parts'][0]['text'].strip()
        
        # Strip potential markdown backticks from response
        if raw_text.startswith("```"):
            raw_text = re.sub(r"^```[a-zA-Z]*\n?", "", raw_text)
            raw_text = re.sub(r"\n?```$", "", raw_text)
            
        return json.loads(raw_text)
    except Exception as e:
        print(f"Analysis failed: {e}")
        return None

# --- TELEGRAM DISPATCH ---
def send_telegram_card(platform, title, link, analysis):
    score = analysis.get("match_score", 0)
    badge = "🔥 CRITICAL MATCH" if score >= 85 else "⚡ QUALIFIED LEAD"
    
    msg = (
        f"{badge} <b>[{score}/100]</b>\n"
        f"🌐 <b>Source:</b> {html.escape(platform)}\n"
        f"📌 <b>Role:</b> {html.escape(title)}\n"
        f"🛠 <b>Stack:</b> <code>{html.escape(str(analysis.get('tech_stack', 'N/A')))}</code>\n\n"
        f"🎯 <b>Core Need:</b>\n{html.escape(str(analysis.get('key_problem', 'N/A')))}\n\n"
        f"📝 <b>Tailored Pitch:</b>\n<code>{html.escape(str(analysis.get('custom_pitch', '')))}</code>\n\n"
        f"💡 <b>Execution Strategy:</b>\n{html.escape(str(analysis.get('solution_angle', '')))}"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "🚀 Open Direct Posting", "url": link}]
            ]
        }
    }
    
    requests.post(url, json=payload, timeout=10)

# --- RUNNER ---
def main():
    seen_jobs = load_seen_jobs()
    new_jobs_processed = 0

    for platform, feed_url in JOB_FEEDS.items():
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:5]: # Scan 5 most recent per feed
                job_id = getattr(entry, "id", entry.link)
                
                if job_id not in seen_jobs:
                    seen_jobs.add(job_id)
                    title = entry.title
                    link = entry.link
                    desc = getattr(entry, "summary", title)
                    
                    analysis = analyze_and_pitch(title, desc)
                    if analysis and analysis.get("match_score", 0) >= MIN_MATCH_SCORE:
                        send_telegram_card(platform, title, link, analysis)
                        new_jobs_processed += 1
                        
        except Exception as e:
            print(f"Error processing {platform}: {e}")

    save_seen_jobs(seen_jobs)
    print(f"Loop finished. Processed {new_jobs_processed} qualifying leads.")

if __name__ == "__main__":
    main()
