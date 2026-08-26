import os
import requests

# Load your secure keys automatically from GitHub's server vault
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# The open remote tech/writing job boards to scan automatically
JOB_FEEDS = {
    "WeWorkRemotely-Tech": "https://weworkremotely.com",
    "WeWorkRemotely-Writing": "https://weworkremotely.com",
    "RemoteOK": "https://remoteok.com"
}

def call_gemini(prompt):
    url = f"https://googleapis.com{GEMINI_API_KEY}"
    data = {
        "systemInstruction": {"parts": [{"text": "You are a world-class freelancer. Read the job text, filter for tech/writing, execute the task at an elite human professional tier, and remove all AI buzzwords like 'delve' or 'testament'."}]},
        "contents": [{"parts": [{"text": prompt}]}]
    }
    try:
        res = requests.post(url, json=data, timeout=30)
        return res.json()['candidates']['content']['parts']['text']
    except:
        return None

def send_telegram(text):
    url = f"https://telegram.org{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)

# Main background loop execution step
for platform, url in JOB_FEEDS.items():
    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if response.status_code == 200:
            final_delivery = call_gemini(f"Process this latest job listing from {platform}:\n{response.text[:4000]}")
            if final_delivery:
                send_telegram(f"✨ **NEW DELIVERABLE FROM {platform.upper()}** ✨\n\n{final_delivery}")
    except Exception as e:
        print(f"Skipping {platform}: {e}")
