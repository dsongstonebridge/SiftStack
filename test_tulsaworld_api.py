import requests
from datetime import datetime, timedelta

API = "https://us-central1-enotice-production.cloudfunctions.net/api/search/public-notices"
HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://tulsaworld.column.us",
    "Referer": "https://tulsaworld.column.us/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
now = datetime.now()

# Get all pages to understand the type distribution
payload = {
    "search": "",
    "allFilters": [
        {"publishedtimestamp": {"from": int((now - timedelta(days=60)).timestamp() * 1000), "to": int(now.timestamp() * 1000)}},
        {"newspapername": ["Tulsa World"]},
        {"noticetype": ["Foreclosure Sale", "Notice of Sale"]},
    ],
    "noneFilters": [],
    "sort": [{"publishedtimestamp": "desc"}],
    "pageSize": 100,
    "isDemo": False,
}
r = requests.post(API, json=payload, headers=HEADERS, timeout=15)
data = r.json()
results = data["results"]
print(f"Total: {len(results)} | pages={data['page']}")
from collections import Counter
types = Counter(x["noticetype"] for x in results)
print(f"Type distribution: {dict(types)}")

# Look for keywords in text to distinguish real-estate from equipment/storage
real_estate_keywords = ["commonly known as", "real property", "located at", "property address",
                        "IN THE DISTRICT COURT", "Case No.", "FORECLOSURE", "MORTGAGE",
                        "Power of Sale"]
for rec in results[:20]:
    text = rec["text"].upper()
    has_re = any(kw.upper() in text for kw in real_estate_keywords)
    has_storage = any(w in text for w in ["STORAGE", "EQUIPMENT", "VEHICLE", "LIEN FOR REPAIR"])
    print(f"  {rec['noticetype']:20} | RE={has_re} | STORAGE/EQUIP={has_storage} | id={rec['id'][:8]}")
