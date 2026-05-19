import asyncio
import requests
import uuid
import re
from urllib.parse import urlparse, urlunparse
from datetime import datetime, timedelta, timezone
from dateutil import parser as date_parser
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from supabase import create_client, Client

# ==========================================
#          BLOCK: SETUP & MEMORY
# ==========================================
# --- 1. SETUP ---
import os

# Use environment variables for GitHub Actions
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# --- 2. DYNAMIC UTILITIES, ROUTERS & EXCLUDERS ---
ROUTING_RULES = {}
IGNORE_RULES = []

PROCESSED_URLS = set()
PROCESSED_TITLES = set()


# ==========================================
#          BLOCK: CORE UTILITIES
# ==========================================
def normalize_url(url):
    try:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', parsed.fragment)).rstrip('/')
    except Exception: return url

def get_base_domain(url):
    """Extracts just the https://website.com part of a URL"""
    try:
        parts = url.split('/')
        return f"{parts[0]}//{parts[2]}"
    except: return url

def make_absolute_url(image_url, source_url):
    """Glues the domain name to broken relative image paths"""
    if not image_url: return None
    if image_url.startswith('http'): return image_url
    if image_url.startswith('//'): return 'https:' + image_url
    
    base_domain = get_base_domain(source_url)
    if image_url.startswith('/'): return base_domain + image_url
    return base_domain + '/' + image_url

def rehost_image(image_url, folder="scraped"):
    if not image_url or image_url.startswith('data:'): return None
    try:
        response = requests.get(image_url, headers=HEADERS, stream=True, timeout=10)
        if response.status_code == 200:
            ext = image_url.split('.')[-1].split('?')[0]
            if len(ext) > 4 or not ext.isalnum(): ext = 'jpg' 
            unique_filename = f"{folder}/{uuid.uuid4().hex[:12]}.{ext}"
            file_bytes = response.content
            content_type = response.headers.get('content-type', 'image/jpeg')
            supabase.storage.from_("images").upload(unique_filename, file_bytes, {"content-type": content_type})
            return supabase.storage.from_("images").get_public_url(unique_filename)
    except Exception as e: print(f"  -> Image rehost failed: {e}")
    return image_url

ROUTING_RULES = {}
IGNORE_RULES = []

def load_dynamic_keywords():
    global ROUTING_RULES, IGNORE_RULES
    res_ig = supabase.table("IGNORE_KEYWORDS").select("keyword").execute()
    if res_ig.data: IGNORE_RULES = [r['keyword'].lower() for r in res_ig.data]
        
    res_rt = supabase.table("ROUTING_KEYWORDS").select("*").execute()
    ROUTING_RULES = {}
    if res_rt.data:
        for r in res_rt.data:
            f = r['franchise'].lower()
            k = r['keyword'].lower()
            if f not in ROUTING_RULES: ROUTING_RULES[f] = []
            ROUTING_RULES[f].append(k)

def is_excluded(text):
    return any(kw in text.lower() for kw in IGNORE_RULES)

def determine_franchises(text, default_franchise):
    matches = set()
    for franchise, keywords in ROUTING_RULES.items():
        if any(kw in text.lower() for kw in keywords): matches.add(franchise)
    if not matches: return default_franchise
    return ", ".join(sorted(list(matches)))

def is_duplicate(title, url):
    norm_url = normalize_url(url)
    if norm_url in PROCESSED_URLS or title.lower() in PROCESSED_TITLES: return True
    PROCESSED_URLS.add(norm_url)
    PROCESSED_TITLES.add(title.lower())
    return False


# ==========================================
#          BLOCK: PIPELINE - REDDIT
# ==========================================
def scrape_reddit(subreddit, keyword, min_upvotes, default_franchise, default_category):
    print(f"--- [REDDIT SCAN] r/{subreddit} (Keyword: {keyword or 'ALL'}) ---")
    
    # UPGRADE: Fetch 50 items instead of 10 to grab historical data
    if keyword: url = f"https://www.reddit.com/r/{subreddit}/search.json?q={keyword}&restrict_sr=on&sort=new&limit=50"
    else: url = f"https://www.reddit.com/r/{subreddit}/new.json?limit=50"
    
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            posts = r.json().get('data', {}).get('children', [])[:50]
            
            for post in posts:
                post_data = post['data']
                title = post_data['title']
                upvotes = post_data['ups']
                
                if upvotes < min_upvotes or is_excluded(title): continue

                link = f"https://www.reddit.com{post_data['permalink']}"
                if is_duplicate(title, link): continue
                
                # UPGRADE: Hold data for 90 days instead of 14 days
                post_date = datetime.fromtimestamp(post_data['created_utc'], tz=timezone.utc)
                if post_date < datetime.now(timezone.utc) - timedelta(days=90): continue

                raw_image_url = post_data.get('url_overridden_by_dest')
                if not raw_image_url or not any(ext in raw_image_url.lower() for ext in ['.jpg', '.png', '.jpeg', '.webp']):
                    if 'preview' in post_data and 'images' in post_data['preview']:
                        raw_image_url = post_data['preview']['images'][0]['source']['url'].replace('&amp;', '&')
                    elif post_data.get('thumbnail') and post_data['thumbnail'].startswith('http'):
                        raw_image_url = post_data['thumbnail']
                    else: raw_image_url = None

                secure_image_url = rehost_image(raw_image_url, folder="reddit") if raw_image_url else None
                actual_franchises = determine_franchises(title, default_franchise)
                
                assigned_category = default_category
                if assigned_category == "Global News" and re.search(r'\b(sg|singapore)\b', title.lower()): assigned_category = "Local News"
                    
                db_data = {
                    "title": title[:180], "url": link, "source": f"Reddit: r/{subreddit}", 
                    "category": assigned_category, "franchise": actual_franchises,
                    "image_url": secure_image_url, "published_date": post_date.isoformat()
                }
                supabase.table("TCG_NEWS").upsert(db_data, on_conflict="url").execute()
                print(f"Sync: {title[:30]}... [⬆️ {upvotes}] [{actual_franchises.upper()}] -> {assigned_category}")
    except Exception as e: print(f"Reddit Error: {e}")


# ==========================================
#          BLOCK: PIPELINE - RSS / GOOGLE
# ==========================================
def scrape_rss_feed(url, source, default_franchise, default_category):
    print(f"--- [RSS FEED] {source} ---")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.content, "xml")
        entries = soup.find_all("entry") or soup.find_all("item")
        
        # UPGRADE: Process up to 50 RSS items
        for entry in entries[:50]:
            title_node = entry.find("title")
            title = BeautifulSoup(title_node.text, "html.parser").text if title_node else ""
            if not title or is_excluded(title): continue

            link_node = entry.find("link")
            link = link_node["href"] if link_node else ""
            if is_duplicate(title, link): continue

            pub_date = entry.find("published") or entry.find("pubDate")
            pub_date_text = pub_date.text if pub_date else None

            raw_image_url = None
            content_node = entry.find("content") or entry.find("description")
            if content_node:
                img_tag = BeautifulSoup(content_node.text, "html.parser").find("img")
                if img_tag: raw_image_url = img_tag.get("src")

            try:
                if pub_date_text and date_parser.parse(pub_date_text, fuzzy=True) < datetime.now(timezone.utc) - timedelta(days=90): continue
            except: pass

            # UPGRADE: Fix absolute URLs before downloading
            raw_image_url = make_absolute_url(raw_image_url, url)
            actual_franchises = determine_franchises(title, default_franchise)
            secure_image_url = rehost_image(raw_image_url, folder="news") if raw_image_url else None
            
            assigned_category = default_category
            if assigned_category == "Global News" and re.search(r'\b(sg|singapore)\b', title.lower()): assigned_category = "Local News"
                
            data = {
                "title": title[:180], "url": link, "source": source, 
                "category": assigned_category, "franchise": actual_franchises,
                "image_url": secure_image_url, "published_date": pub_date_text
            }
            supabase.table("TCG_NEWS").upsert(data, on_conflict="url").execute()
            print(f"Sync: {title[:40]}... [{actual_franchises.upper()}] -> {assigned_category}")
    except Exception as e: print(f"RSS Error ({source}): {e}")


# ==========================================
#          BLOCK: PIPELINE - SHOPIFY
# ==========================================
def scrape_shopify_feed(url, source, default_franchise, default_category):
    print(f"--- [FAST FEED] {source} ---")
    try:
        r = requests.get(f"{url}/collections/all.atom", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.content, "xml")
        for entry in soup.find_all("entry")[:30]:
            title = entry.find("title").text.strip()
            if is_excluded(title): continue

            link = entry.find("link")["href"]
            if is_duplicate(title, link): continue
            
            pub_date = entry.find("published").text if entry.find("published") else None
            
            raw_image_url = None
            summary_html = entry.find("summary").text if entry.find("summary") else ""
            if summary_html:
                img_tag = BeautifulSoup(summary_html, "html.parser").find("img")
                if img_tag: raw_image_url = img_tag.get("src")

            # UPGRADE: Fix absolute URLs before downloading
            raw_image_url = make_absolute_url(raw_image_url, url)
            actual_franchises = determine_franchises(title, default_franchise)
            secure_image_url = rehost_image(raw_image_url, folder="shopify") if raw_image_url else None
            
            data = {
                "title": title[:180], "url": link, "source": source, 
                "category": default_category, "franchise": actual_franchises,
                "image_url": secure_image_url, "published_date": pub_date
            }
            supabase.table("TCG_NEWS").upsert(data, on_conflict="url").execute()
            print(f"Sync: {title[:40]}... [{actual_franchises.upper()}]")
    except Exception as e: print(f"Feed Error ({source}): {e}")


# ==========================================
#          BLOCK: PIPELINE - PLAYWRIGHT
# ==========================================
async def scrape_news_dynamic(browser, url, source, selector, default_franchise, default_category):
    print(f"--- [DYNAMIC SCAN] {source} ---")
    page = await browser.new_page()
    await Stealth().apply_stealth_async(page)
    
    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
        for btn_text in ["Accept", "Allow", "Close", "Agree"]:
            try:
                btn = page.get_by_role("button", name=btn_text).first
                if await btn.is_visible(): await btn.click()
            except: pass

        await page.evaluate("window.scrollTo(0, 800)")
        await page.wait_for_timeout(5000) 
        
        items = await page.query_selector_all(selector)
        for item in items[:20]:
            headline_el = await item.query_selector("h2, h3, h4, [class*='title']")
            link_el = await item.query_selector("a")
            img_el = await item.query_selector("img")
            
            if link_el:
                title = (await headline_el.inner_text()).strip() if headline_el else ""
                if len(title) < 10: 
                    lines = (await item.inner_text()).split('\n')
                    for line in lines:
                        if len(line.strip()) > 10 and "Learn More" not in line: title = line.strip(); break
                
                if not title or is_excluded(title): continue

                link = await link_el.get_attribute("href")
                link = make_absolute_url(link, url)
                if is_duplicate(title, link): continue

                raw_image_url = await img_el.get_attribute("src") if img_el else None
                
                # UPGRADE: Fix absolute URLs before downloading
                raw_image_url = make_absolute_url(raw_image_url, url)

                actual_franchises = determine_franchises(title, default_franchise)
                
                assigned_category = default_category
                if assigned_category == "Global News" and re.search(r'\b(sg|singapore)\b', title.lower()): assigned_category = "Local News"
                
                secure_image_url = rehost_image(raw_image_url, folder="news") if raw_image_url else None
                
                data = {
                    "title": title[:180], "url": link, "source": source, 
                    "category": assigned_category, "franchise": actual_franchises, 
                    "image_url": secure_image_url, "published_date": datetime.now().isoformat()
                }
                supabase.table("TCG_NEWS").upsert(data, on_conflict="url").execute()
                print(f"Sync: {title[:30]}... [{actual_franchises.upper()}] -> {assigned_category}")
    except Exception as e: print(f"Error ({source}): {e}")
    finally: await page.close()


def clean_old_database_records():
    print("\n--- RUNNING DATABASE GARBAGE COLLECTION ---")
    try:
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        supabase.table("TCG_NEWS").delete().lt("published_date", cutoff_date).execute()
    except Exception: pass


# ==========================================
#          BLOCK: SYSTEM ORCHESTRATOR
# ==========================================
async def main():
    load_dynamic_keywords()
    print("Fetching Target URLs from Control Plane...")
    
    res_web = supabase.table("SCRAPE_SOURCES_WEB").select("*").eq("is_active", True).execute()
    web_sources = res_web.data if res_web.data else []
    
    dynamic_tasks = []
    for s in web_sources:
        cat = s.get('default_category', 'Global News')
        if s['scrape_type'] == 'Shopify': scrape_shopify_feed(s['url'], s['source_name'], s['default_franchise'], cat)
        elif s['scrape_type'] == 'RSS': scrape_rss_feed(s['url'], s['source_name'], s['default_franchise'], cat)
        elif s['scrape_type'] == 'Dynamic': dynamic_tasks.append((s['source_name'], s['url'], s['css_selector'], s['default_franchise'], cat))

    res_reddit = supabase.table("SCRAPE_SOURCES_REDDIT").select("*").eq("is_active", True).execute()
    reddit_sources = res_reddit.data if res_reddit.data else []
    for r in reddit_sources:
        cat = r.get('default_category', 'Global News')
        scrape_reddit(r['subreddit'], r.get('keyword'), r['min_upvotes'], r['default_franchise'], cat)

    if dynamic_tasks:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            for src, url, sel, default_f, cat in dynamic_tasks:
                await scrape_news_dynamic(browser, url, src, sel, default_f, cat)
            await browser.close()
            
    clean_old_database_records()
    print("\n--- HUB UPDATE COMPLETE ---")

if __name__ == "__main__":
    asyncio.run(main())