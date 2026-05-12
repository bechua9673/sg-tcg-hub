import asyncio
import requests
from datetime import datetime, timedelta, timezone
from dateutil import parser as date_parser
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from supabase import create_client, Client

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

def load_dynamic_keywords():
    global ROUTING_RULES, IGNORE_RULES
    print("Loading dynamic keywords from Hub Engine...")
    
    # 1. Fetch Ignore Rules
    res_ig = supabase.table("IGNORE_KEYWORDS").select("keyword").execute()
    if res_ig.data:
        IGNORE_RULES = [r['keyword'].lower() for r in res_ig.data]
        
    # 2. Fetch Routing Rules
    res_rt = supabase.table("ROUTING_KEYWORDS").select("*").execute()
    ROUTING_RULES = {}
    if res_rt.data:
        for r in res_rt.data:
            f = r['franchise'].lower()
            k = r['keyword'].lower()
            if f not in ROUTING_RULES: ROUTING_RULES[f] = []
            ROUTING_RULES[f].append(k)

def get_base_domain(url):
    parts = url.split('/')
    return f"{parts[0]}//{parts[2]}"

def is_recent(date_str):
    if not date_str: return True 
    try:
        article_date = date_parser.parse(date_str, fuzzy=True)
        return article_date >= datetime.now() - timedelta(days=90)
    except Exception: return True 

def is_excluded(text):
    text_lower = text.lower()
    return any(kw in text_lower for kw in IGNORE_RULES)

def determine_franchise(text, default_franchise):
    text_lower = text.lower()
    for franchise, keywords in ROUTING_RULES.items():
        if any(kw in text_lower for kw in keywords):
            return franchise
    return default_franchise

# --- 3. SHOPIFY FEED (Local Pre-Orders) ---
def scrape_shopify_feed(url, source, default_franchise):
    print(f"--- [FAST FEED] {source} ---")
    base_domain = get_base_domain(url)
    try:
        r = requests.get(f"{url}/collections/all.atom", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.content, "xml")
        for entry in soup.find_all("entry")[:15]:
            title = entry.find("title").text.strip()
            
            if is_excluded(title): continue

            link = entry.find("link")["href"]
            pub_date = entry.find("published").text if entry.find("published") else None
            
            image_url = None
            summary_html = entry.find("summary").text if entry.find("summary") else ""
            if summary_html:
                summary_soup = BeautifulSoup(summary_html, "html.parser")
                img_tag = summary_soup.find("img")
                if img_tag: 
                    image_url = img_tag.get("src")
                    if image_url and image_url.startswith("/"):
                        image_url = base_domain + image_url if not image_url.startswith("//") else "https:" + image_url

            if is_recent(pub_date):
                actual_franchise = determine_franchise(title, default_franchise)
                data = {
                    "title": title[:180], "url": link, "source": source, 
                    "category": "Local Drops", "franchise": actual_franchise,
                    "image_url": image_url, "published_date": pub_date
                }
                supabase.table("TCG_NEWS").upsert(data, on_conflict="url").execute()
                print(f"Sync: {title[:40]}... [{actual_franchise.upper()}]")
    except Exception as e: print(f"Feed Error ({source}): {e}")

# --- 4. DEEP DYNAMIC SCAN (News & Articles) ---
async def scrape_news_dynamic(browser, url, source, selector, default_franchise):
    print(f"--- [DYNAMIC SCAN] {source} ---")
    base_domain = get_base_domain(url)
    page = await browser.new_page()
    await Stealth().apply_stealth_async(page)
    
    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
        for btn_text in ["Accept", "Allow", "Close", "Agree", "United States"]:
            try:
                btn = page.get_by_role("button", name=btn_text).first
                if await btn.is_visible(): await btn.click()
            except: pass

        await page.evaluate("window.scrollTo(0, 800)")
        await page.wait_for_timeout(10000) 
        
        items = await page.query_selector_all(selector)
        for item in items[:15]:
            headline_el = await item.query_selector("h2, h3, h4, [class*='title']")
            link_el = await item.query_selector("a")
            img_el = await item.query_selector("img")
            date_el = await item.query_selector("p, span, time")
            
            if link_el:
                title = (await headline_el.inner_text()).strip() if headline_el else ""
                
                # BULLETPROOF TITLE GRABBER
                if len(title) < 10 or "202" in title: 
                    lines = (await item.inner_text()).split('\n')
                    for line in lines:
                        clean_line = line.strip()
                        if len(clean_line) > 10 and "202" not in clean_line and "Learn More" not in clean_line:
                            title = clean_line
                            break
                
                if not title or is_excluded(title): continue

                link = await link_el.get_attribute("href")
                date_text = await date_el.inner_text() if date_el else None
                image_url = await img_el.get_attribute("src") or await img_el.get_attribute("data-src") if img_el else None
                
                if image_url and image_url.startswith("/"):
                    image_url = "https:" + image_url if image_url.startswith("//") else base_domain + image_url

                if len(title) > 10 and not any(b in title.lower() for b in ["learn more", "read more", "cookies"]):
                    if is_recent(date_text):
                        if link and link.startswith("/"): link = base_domain + link
                        
                        try: iso_date = date_parser.parse(date_text, fuzzy=True).isoformat() if date_text else None
                        except Exception: iso_date = None 
                        
                        actual_franchise = determine_franchise(title, default_franchise)
                        
                        data = {
                            "title": title[:180], "url": link, "source": source, 
                            "category": "Local Drops" if source == "Real Troves" else "Global News",
                            "franchise": actual_franchise, "image_url": image_url, "published_date": iso_date
                        }
                        supabase.table("TCG_NEWS").upsert(data, on_conflict="url").execute()
                        print(f"Sync: {title[:40]}... [{actual_franchise.upper()}]")
                    
    except Exception as e: print(f"Error ({source}): {e}")
    finally: await page.close()

# --- 5. DATABASE GARBAGE COLLECTION ---
def clean_old_database_records():
    print("\n--- RUNNING DATABASE GARBAGE COLLECTION ---")
    try:
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        supabase.table("TCG_NEWS").delete().lt("published_date", cutoff_date).execute()
        print("Old records purged successfully.")
    except Exception as e: print(f"Garbage Collection Error: {e}")

async def main():
    load_dynamic_keywords() # Load database rules before scanning

    # PART A: FEED
    feeds = [
        ("Sawadee Kard", "https://sawadeekard.com", "pokemon"), 
        ("1Collectibles", "https://1collectibles.com", "pokemon"), 
        ("Cardboard Collectible", "https://cardboardcollectible.com", "others")
    ]
    for s, u, f in feeds: scrape_shopify_feed(u, s, f)

    # PART B: DYNAMIC
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        tasks = [
            ("Official Pokemon", "https://play.pokemon.com/en-us/news/", "ul[class*='Cards'] li", "pokemon"),
            ("Official One Piece", "https://en.onepiece-cardgame.com/news/", "li.recommendColBox", "one piece"),
            ("Real Troves", "https://realtroves.com/pre-order/", ".porto-tb-item.product", "one piece"),
            ("PokeBeach", "https://www.pokebeach.com/", "article.post", "pokemon"),
            ("One Piece Player", "https://onepieceplayer.com/articles/", "article", "one piece")
        ]
        for src, url, sel, default_f in tasks:
            await scrape_news_dynamic(browser, url, src, sel, default_f)
        await browser.close()
        
    # PART C: PURGE
    clean_old_database_records()
    print("\n--- HUB UPDATE COMPLETE ---")

if __name__ == "__main__":
    asyncio.run(main())