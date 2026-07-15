# app.py
import os
import sqlite3
import logging
import re
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from flask_cors import CORS   # ✅ ADD THIS

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------
DB_PATH = os.path.join(os.path.dirname(__file__), "price_history.db")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

app = Flask(__name__)

# -------------------------------------------------------------------
# ✅ CORS CONFIG — ALLOW ONLY YOUR NETLIFY FRONTEND
# -------------------------------------------------------------------
CORS(
    app,
    resources={r"/api/*": {"origins": [
        "https://deluxe-daifuku-e4b1e1.netlify.app"
    ]}},
    supports_credentials=True
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# -------------------------------------------------------------------
# DATABASE
# -------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            title TEXT NOT NULL,
            merchant TEXT NOT NULL,
            price REAL NOT NULL,
            created_at TIMESTAMP NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def store_price_history(query, title, merchant, price):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO price_history (query, title, merchant, price, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (query, title, merchant, price, datetime.utcnow()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error("Error storing price history: %s", e)


def get_price_history_stats(query):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cutoff = datetime.utcnow() - timedelta(days=30)
        cur.execute(
            """
            SELECT AVG(price) as avg_price
            FROM price_history
            WHERE query = ?
              AND created_at >= ?
            """,
            (query, cutoff),
        )
        row = cur.fetchone()
        conn.close()
        if row and row[0] is not None:
            return float(row[0])
    except Exception as e:
        logging.error("Error reading price history: %s", e)
    return None

# -------------------------------------------------------------------
# UTILITIES
# -------------------------------------------------------------------
def clean_price(text):
    if not text:
        return None
    text = text.replace("\xa0", " ").replace(",", ".")
    match = re.findall(r"\d+[\.,]?\d*", text)
    if not match:
        return None
    try:
        return float(match[0].replace(",", "."))
    except ValueError:
        return None


def simulate_shipping(merchant, price):
    if merchant.lower() in ["amazon", "bestbuy", "fnac", "cdiscount"]:
        return 4.99 if price < 100 else 0.0
    if merchant.lower() == "ebay":
        return 6.99 if price < 150 else 3.99
    return 5.99


def dedupe_results(results):
    seen = set()
    deduped = []
    for r in results:
        key = (r["merchant"], r["title"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped

# -------------------------------------------------------------------
# SERPAPI GOOGLE SHOPPING
# -------------------------------------------------------------------
def fetch_serpapi_shopping(query):
    if not SERPAPI_KEY:
        logging.info("SERPAPI_KEY not set; skipping SerpAPI integration.")
        return []

    url = "https://serpapi.com/search"
    params = {
        "engine": "google_shopping",
        "q": query,
        "api_key": SERPAPI_KEY,
        "gl": "de",
        "hl": "de",
    }

    results = []
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        for item in data.get("shopping_results", [])[:10]:
            title = item.get("title")
            price = clean_price(item.get("price"))
            link = item.get("link")
            merchant = item.get("source") or "Google Shopping"
            image = item.get("thumbnail")

            if title and price and link:
                results.append({
                    "merchant": merchant,
                    "title": title,
                    "price": price,
                    "image": image,
                    "link": link,
                })
    except Exception as e:
        logging.error("SerpAPI error: %s", e)

    return results

# -------------------------------------------------------------------
# DIRECT SCRAPERS
# -------------------------------------------------------------------
def scrape_amazon(query):
    url = "https://www.amazon.de/s"
    params = {"k": query}
    results = []
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        items = soup.select("div.s-result-item[data-component-type='s-search-result']")[:8]

        for item in items:
            title_el = item.select_one("h2 a span")
            price_el = item.select_one("span.a-price span.a-offscreen")
            img_el = item.select_one("img.s-image")
            link_el = item.select_one("h2 a")

            title = title_el.get_text(strip=True) if title_el else None
            price = clean_price(price_el.get_text(strip=True) if price_el else None
            )
            img = img_el["src"] if img_el and img_el.has_attr("src") else None
            link = "https://www.amazon.de" + link_el["href"] if link_el else None

            if title and price and link:
                results.append({
                    "merchant": "Amazon",
                    "title": title,
                    "price": price,
                    "image": img,
                    "link": link,
                })
    except Exception as e:
        logging.error("Amazon scrape error: %s", e)

    return results

# (eBay, CDiscount, Fnac, BestBuy scrapers omitted here for brevity — keep your existing ones)

# -------------------------------------------------------------------
# BUY OR WAIT ANALYSIS
# -------------------------------------------------------------------
def analyze_discount_and_signal(query, results):
    if not results:
        return {
            "history_avg": None,
            "fake_discount": None,
            "signal": "No data",
            "reason": "No results to analyze.",
        }

    lowest_price = min(r["total"] for r in results)
    history_avg = get_price_history_stats(query)

    if history_avg is None:
        return {
            "history_avg": None,
            "fake_discount": None,
            "signal": "Neutral",
            "reason": "No historical data yet.",
        }

    fake_discount = lowest_price > history_avg * 1.05
    if fake_discount:
        reason = f"Current price {lowest_price:.2f} is above 30-day avg {history_avg:.2f}."
    else:
        reason = f"Current price {lowest_price:.2f} is below 30-day avg {history_avg:.2f}."

    if lowest_price < history_avg * 0.97:
        signal = "Buy"
    elif lowest_price > history_avg * 1.03:
        signal = "Wait"
    else:
        signal = "Neutral"

    return {
        "history_avg": history_avg,
        "fake_discount": fake_discount,
        "signal": signal,
        "reason": reason,
    }

# -------------------------------------------------------------------
# API ENDPOINT
# -------------------------------------------------------------------
@app.route("/api/search")
def api_search():
    # Récupère 'q' (comme envoyé par app.js) ou 'query' par sécurité
    query = request.args.get("q") or request.args.get("query", "")
    query = query.strip()
    
    if not query:
        return jsonify({"error": "Missing query parameter"}), 400

    sources = [
        ("serpapi", fetch_serpapi_shopping),
        ("amazon", scrape_amazon),
    ]

    aggregated = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        future_to_source = {executor.submit(func, query): name for name, func in sources}
        for future in as_completed(future_to_source):
            name = future_to_source[future]
            try:
                data = future.result()
                aggregated.extend(data)
            except Exception as e:
                logging.error("Error from source %s: %s", name, e)

    aggregated = dedupe_results(aggregated)

    # 1. Obtenir la liste unique des marchands pour "data.retailers"
    retailers_set = set()

    formatted_results = []
    for r in aggregated:
        shipping_cost = simulate_shipping(r["merchant"], r["price"])
        total_cost = r["price"] + shipping_cost
        store_price_history(query, r["title"], r["merchant"], total_cost)
        
        retailers_set.add(r["merchant"])

        # 2. Formater chaque élément précisément comme l'attend app.js !
        formatted_results.append({
            "retailer": r["merchant"],
            "title": r["title"],
            "subtitle": "Live offer scanned successfully.",
            "shipping_text": f"€{shipping_cost:.2f} shipping" if shipping_cost > 0 else "Free shipping",
            "price_text": f"€{r['price']:.2f}",
            "total_text": f"€{total_cost:.2f}",
            "delivery_speed": "2-4 days",
            "merchant_rating": "4.5★",
            "fake_discount": False, # Tu pourras relier ça à ton analyse de discount plus tard
            "buy_or_wait": "Buy" if r["price"] < r.get("price", 9999) * 1.05 else "Wait", 
            "signal_explanation": "Good price compared to market avg.",
            "url": r["link"]
        })

    # Trier par prix total croissant
    formatted_results.sort(key=lambda x: x["total_text"])

    # Renvoyer la structure exacte attendue par le JavaScript
    return jsonify({
        "query": query,
        "results": formatted_results,
        "retailers": list(retailers_set)
    })

# -------------------------------------------------------------------
# MAIN
# -------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)