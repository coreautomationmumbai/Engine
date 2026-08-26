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

# --- TARGET MICRO-TASK FEEDS ---
FEEDS = {
    "Freelancer.com Micro Projects": "https://www.freelancer.com/rss.xml",
    "CryptoJobs Bounties": "https://cryptojobslist.com/rss/freelance",
    "Remotive Tech Tasks": "https://remotive.com/remote-jobs/feed?category=software-development"
}

# --- ACTIVE GEMINI MODELS (3.7 FLASH FIRST WITH FAILOVERS) ---
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
    r"\bselenium\b", r"\bplaywright\b", r"\bsql\b"
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

# --- CONTROL CHARACTER & PAYLOAD SANITIZER ---
def parse_json_safely(raw_str: str):
    if not raw_str:
        return None
    clean_str = re.sub(r"^```[a-zA-Z]*\n?", "", raw_str.strip())
    clean_str = re.sub(r"\n?
