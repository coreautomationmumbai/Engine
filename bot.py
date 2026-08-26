import os
import json
import html
import re
import requests
import feedparser

# --- CREDENTIALS & CONFIG ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_FILE = "seen_jobs.json"
MIN_VERIFIED_SCORE = 70

FEEDS = {
    "WWR Python/Backend": "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    "WWR Automation/DevOps": "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    "RemoteOK Tech": "https://remoteok.com/remote-jobs.rss"
}

# --- METADATA SANITIZER ---
def sanitize_payload(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip())
    cleaned = re.sub(r"\n?```$", "", cleaned.strip())
    cleaned = re.sub(r'(?i)(author|developer|user|created by):\s*.*', '', cleaned)
    cleaned = re.sub(r'(/home/|/Users/|[A-Za-z]:\\Users\\)[a-zA-Z0-9_-]+', '/app', cleaned)
    cleaned = re.sub(r'\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[0-1]))\.\d{1,3}\.\d{1,3}\b', '0.0.0.0', cleaned)
    return cleaned.strip()

# --- CACHE STATE ---
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
        json.dump(list(seen_set)[-500:], f, indent=2)

# --- GEMINI INFERENCE WRAPPER ---
def call_gemini(system_role: str, user_prompt: str, temperature: float = 0.2):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": f"System Role: {system_role}\n\nTask: {user_prompt}"}]}
        ],
        "generationConfig": {"temperature": temperature}
    }
    try:
        res = requests.post(url, json=payload, timeout=45)
        return res.json()['candidates'][0]['content']['parts'][0]['text'].strip()
    except Exception as e:
        print(f"Agent Inference Error: {e}")
        return None

# --- MULTI-AGENT COUNCIL FUSION PIPELINE ---
def run_agent_council(title: str, description: str):
    clean_desc = re.sub(r'<[^>]+>', ' ', description)[:3500]

    # --- AGENT 1: TRIAGE EVALUATOR ---
    evaluator_role = "Senior Technical Recruiter & Deal Screener"
    evaluator_prompt = f"""
    Evaluate this job listing for a 1-off fixed-price deliverable (e.g., scraper, API script, bot, data pipeline).
    Reject full-time W-2 roles, agencies, or unclear tasks.
    Return ONLY a raw JSON object (no markdown formatting, no ```json):
    {{
        "fit_score": <int 0-100>,
        "is_one_off": <true/false>,
        "key_problem": "<1-sentence summary of the core technical task>"
    }}
    Job: {title}
    Details: {clean_desc}
    """
    eval_raw = call_gemini(evaluator_role, evaluator_prompt, temperature=0.1)
    if not eval_raw:
        return None

    try:
        clean_json = re.sub(r"^```[a-zA-Z]*\n?", "", eval_raw.strip())
        clean_json = re.sub(r"\n?```$", "", clean_json.strip())
        triage = json.loads(clean_json)
    except Exception:
        return None

    if triage.get("fit_score", 0) < MIN_VERIFIED_SCORE or not triage.get("is_one_off", False):
        return None

    # --- AGENT 2: PROPOSER ARCHITECT (Draft 1) ---
    architect_role = "Principal Software Architect"
    architect_prompt = f"""
    Task: {triage['key_problem']}
    Draft an initial Python script prototype and a direct, human 3-sentence proposal.
    Format your response with two clear tags:
    [PITCH_DRAFT]
    (Your 3-sentence proposal with flat pricing)
    [/PITCH_DRAFT]
    [CODE_DRAFT]
    (Your python script)
    [/CODE_DRAFT]
    """
    draft_1 = call_gemini(architect_role, architect_prompt, temperature=0.3)
    if not draft_1 or "[CODE_DRAFT]" not in draft_1:
        return None

    # --- AGENT 3: ADVERSARIAL VERIFIER & CODE AUDITOR ---
    critic_role = "Principal QA Auditor & Security Reviewer"
    critic_prompt = f"""
    Audit this initial proposal and code prototype for the task: '{triage['key_problem']}'.
    Draft to Audit:
    {draft_1}

    Identify:
    1. Edge cases, potential rate limits, or missing error-handling.
    2. Overly generic or clichéd pitch phrasing.
    Provide concise critique points for the final judge to fix.
    """
    critique = call_gemini(critic_role, critic_prompt, temperature=0.1)

    # --- AGENT 4: EXECUTIVE JUDGE & SYNTHESIZER ---
    judge_role = "Chief Technology Officer (Final Authority)"
    judge_prompt = f"""
    You hold final sign-off. Synthesize Draft 1 and the Auditor's Critique into a flawless final deliverable.
    
    Task: {triage['key_problem']}
    Draft 1: {draft_1}
    Auditor Critique: {critique}

    Rules:
    - Eliminate all cliches ('delve', 'thrilled', 'excited', 'testament').
    - Fix all code edge cases, missing imports, and exception blocks.
    - Anonymize all identifiers.
    
    Return ONLY valid JSON in this exact structure (no markdown wrapper, no ```json):
    {{
        "final_score": {triage['fit_score']},
        "task_summary": "{triage['key_problem']}",
        "polished_pitch": "<Final human pitch with flat-rate 24-48h quote>",
        "verified_code": "<Final production-ready python script>"
    }}
    """
    final_output_raw = call_gemini(judge_role, judge_prompt, temperature=0.1)
    if not final_output_raw:
        return None

    try:
        clean_final = re.sub(r"^```[a-zA-Z]*\n?", "", final_output_raw.strip())
        clean_final = re.sub(r"\n?```$", "", clean_final.strip())
        final_package = json.loads(clean_final)
        final_package["verified_code"] = sanitize_payload(final_package.get("verified_code", ""))
        return final_package
    except Exception:
        return None

# --- TELEGRAM DISPATCH ---
def dispatch_to_telegram(source, title, link, package):
    score = package.get("final_score", 0)
    
    card = (
        f"🏛 <b>COUNCIL-VERIFIED LEAD [{score}/100]</b>\n"
        f"🌐 <b>Source:</b> {html.escape(source)}\n"
        f"📌 <b>Target:</b> {html.escape(title)}\n"
        f"🎯 <b>Objective:</b> <code>{html.escape(str(package.get('task_summary', 'N/A')))}</code>\n\n"
        f"📝 <b>Polished Executive Pitch:</b>\n"
        f"<code>{html.escape(str(package.get('polished_pitch', '')))}</code>"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    # Message 1: Proposal Card
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": card[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[{"text": "⚡ Apply Directly", "url": link}]]
        }
    }, timeout=10)

    # Message 2: Verified Code Block
    code_body = package.get("verified_code", "")[:3900]
    if code_body:
        code_card = f"🛠 <b>Council-Audited Code:</b>\n<pre><code class='language-python'>{html.escape(code_body)}</code></pre>"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": code_card,
            "parse_mode": "HTML"
        }, timeout=10)

# --- MAIN LOOP ---
def main():
    seen = load_cache()
    dispatched = 0

    for source_name, feed_url in FEEDS.items():
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:6]:
                job_id = getattr(entry, "id", entry.link)
                if job_id not in seen:
                    seen.add(job_id)
                    title = entry.title
                    link = entry.link
                    desc = getattr(entry, "summary", title)

                    package = run_agent_council(title, desc)
                    if package:
                        dispatch_to_telegram(source_name, title, link, package)
                        dispatched += 1
        except Exception as e:
            print(f"Error checking {source_name}: {e}")

    save_cache(seen)
    print(f"Council complete. Dispatched {dispatched} audited leads.")

if __name__ == "__main__":
    main()
