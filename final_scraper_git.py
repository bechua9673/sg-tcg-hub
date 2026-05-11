import asyncio
import requests
import hashlib
from datetime import datetime, timedelta
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
RELEVANT_KEYWORDS = ["pokemon", "one piece", "op-", "sv-", "mega", "evolution", "chaos rising", "booster"]

# --- 2. UTILITIES ---
def get_base_domain(url):
    """Constructs the base domain (e.g., https://play.pokemon.com) from a full URL."""
    parts = url.split('/')
    return f"{parts[0]}//{parts[2]}"

def is_recent(date_str):
    if not date_str: return True 
    try:
        article_date = date_parser.parse(date_str, fuzzy=True)
        return article_date >= datetime.now() - timedelta(days=90)
    except: return True 

def is_relevant(text, dedicated=False):
    if dedicated: return True
    return any(kw in text.lower() for kw in RELEVANT_KEYWORDS)

# --- 3. SHOPIFY FEED (Absolute URL Fix) ---
def scrape_shopify_feed(url, source):
    print(f"--- [FAST FEED] {source} ---")
    base_domain = get_base_domain(url)
    try:
        r = requests.get(f"{url}/collections/all.atom", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.content, "xml")
        for entry in soup.find_all("entry")[:15]:
            title = entry.find("title").text.strip()
            link = entry.find("link")["href"]
            pub_date = entry.find("published").text if entry.find("published") else None
            
            # --- IMAGE FIX ---
            image_url = None
            summary_html = entry.find("summary").text if entry.find("summary") else ""
            if summary_html:
                summary_soup = BeautifulSoup(summary_html, "html.parser")
                img_tag = summary_soup.find("img")
                if img_tag: 
                    image_url = img_tag.get("src")
                    # Fix root-relative Shopify images
                    if image_url and image_url.startswith("/"):
                        image_url = base_domain + image_url
                    elif image_url and image_url.startswith("//"):
                        image_url = "https:" + image_url

            if is_relevant(title) and is_recent(pub_date):
                data = {
                    "title": title, "url": link, "source": source, 
                    "category": "Local Drops", "image_url": image_url,
                    "published_date": pub_date
                }
                supabase.table("TCG_NEWS").upsert(data, on_conflict="url").execute()
                print(f"Sync: {title[:45]}...")
    except Exception as e: print(f"Feed Error ({source}): {e}")

# --- 4. DEEP DYNAMIC SCAN (Absolute URL Fix) ---
async def scrape_news_dynamic(browser, url, source, selector, dedicated=False):
    print(f"--- [DYNAMIC SCAN] {source} ---")
    base_domain = get_base_domain(url)
    page = await browser.new_page()
    await Stealth().apply_stealth_async(page)
    
    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
        
        # Overlay bypass
        for btn_text in ["Accept", "Allow", "Close", "Agree", "United States"]:
            try:
                btn = page.get_by_role("button", name=btn_text).first
                if await btn.is_visible(): await btn.click()
            except: pass

        await page.evaluate("window.scrollTo(0, 800)")
        await page.wait_for_timeout(10000) 
        
        items = await page.query_selector_all(selector)
        print(f"[{source}] Detected {len(items)} containers.")

        for item in items[:15]:
            headline_el = await item.query_selector("h2, h3, h4, [class*='title']")
            link_el = await item.query_selector("a")
            img_el = await item.query_selector("img")
            date_el = await item.query_selector("p, span, time")
            
            if link_el:
                title = ""
                if headline_el:
                    title = (await headline_el.inner_text()).strip()
                if not title or len(title) < 5:
                    full_text = await item.inner_text()
                    title = full_text.split('\n')[0].strip()

                link = await link_el.get_attribute("href")
                date_text = await date_el.inner_text() if date_el else None

                # --- FUZZY IMAGE + ABSOLUTE FIX ---
                image_url = None
                if img_el:
                    image_url = await img_el.get_attribute("src") or \
                                await img_el.get_attribute("data-src") or \
                                await img_el.get_attribute("srcset")
                
                if image_url:
                    # Fix root-relative URLs (e.g. /_images/...)
                    if image_url.startswith("/"):
                        if image_url.startswith("//"):
                            image_url = "https:" + image_url
                        else:
                            image_url = base_domain + image_url

                if len(title) > 10 and not any(b in title.lower() for b in ["learn more", "read more", "cookies"]):
                    if is_relevant(title, dedicated) and is_recent(date_text):
                        # Fix root-relative links
                        if link and link.startswith("/"):
                            link = base_domain + link
                        
                        iso_date = None
                        if date_text:
                            try: iso_date = date_parser.parse(date_text, fuzzy=True).isoformat()
                            except: pass

                        data = {
                            "title": title[:180], "url": link, "source": source, 
                            "category": "Local Drops" if source == "Real Troves" else "Global News",
                            "image_url": image_url, "published_date": iso_date
                        }
                        supabase.table("TCG_NEWS").upsert(data, on_conflict="url").execute()
                        print(f"Sync: {title[:45]}...")
                    
    except Exception as e: print(f"Error ({source}): {e}")
    finally: await page.close()

async def main():
    # PART A: FEED
    for s, u in [("Sawadee Kard", "https://sawadeekard.com"), 
                 ("1Collectibles", "https://1collectibles.com"), 
                 ("Cardboard Collectible", "https://cardboardcollectible.com")]:
        scrape_shopify_feed(u, s)

    # PART B: DYNAMIC
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        tasks = [
            ("Official Pokemon", "https://play.pokemon.com/en-us/news/", "ul[class*='Cards'] li", True),
            ("Official One Piece", "https://en.onepiece-cardgame.com/news/", "li.recommendColBox", True),
            ("Real Troves", "https://realtroves.com/pre-order/", ".porto-tb-item.product", False),
            ("PokeBeach", "https://www.pokebeach.com/", "article.post", False),
            ("One Piece Player", "https://onepieceplayer.com/articles/", "article", True)
        ]
        for src, url, sel, ded in tasks:
            await scrape_news_dynamic(browser, url, src, sel, ded)
        await browser.close()
    print("\n--- HUB UPDATE COMPLETE ---")

if __name__ == "__main__":
    asyncio.run(main())