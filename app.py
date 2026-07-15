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
from flask_cors import CORS

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------
DB_PATH = os.path.join(os.path.dirname(__file__), "price_history.db")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")  # optional, for Google Shopping via SerpAPI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

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
  """Return 30-day average price for this query (across all merchants)."""
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
  """Simple shipping estimation logic."""
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
# UNIVERSAL SEARCH VIA SERPAPI (GOOGLE SHOPPING)
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
    resp.raise_for_status()
    data = resp.json()
    for item in data.get("shopping_results", [])[:10]:
      title = item.get("title")
      price_str = item.get("price")
      price = clean_price(price_str)
      link = item.get("link")
      merchant = item.get("source") or "Google Shopping"
      image = item.get("thumbnail")

      if title and price and link:
        results.append(
            {
                "merchant": merchant,
                "title": title,
                "price": price,
                "image": image,
                "link": link,
            }
        )
  except Exception as e:
    logging.error("SerpAPI error: %s", e)

  return results


# -------------------------------------------------------------------
# DIRECT SCRAPERS (TOP TECH STORES)
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
      price = clean_price(price_el.get_text(strip=True) if price_el else None)
      img = img_el["src"] if img_el and img_el.has_attr("src") else None
      link = "https://www.amazon.de" + link_el["href"] if link_el and link_el.has_attr("href") else None

      if title and price and link:
        results.append(
            {
                "merchant": "Amazon",
                "title": title,
                "price": price,
                "image": img,
                "link": link,
            }
        )
  except Exception as e:
    logging.error("Amazon scrape error: %s", e)

  return results


def scrape_ebay(query):
  url = "https://www.ebay.de/sch/i.html"
  params = {"_nkw": query}
  results = []

  try:
    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    soup = BeautifulSoup(resp.text, "html.parser")
    items = soup.select("li.s-item")[:8]

    for item in items:
      title_el = item.select_one("h3.s-item__title")
      price_el = item.select_one(".s-item__price")
      img_el = item.select_one("img.s-item__image-img")
      link_el = item.select_one("a.s-item__link")

      title = title_el.get_text(strip=True) if title_el else None
      price = clean_price(price_el.get_text(strip=True) if price_el else None)
      img = img_el["src"] if img_el and img_el.has_attr("src") else None
      link = link_el["href"] if link_el and link_el.has_attr("href") else None

      if title and price and link:
        results.append(
            {
                "merchant": "eBay",
                "title": title,
                "price": price,
                "image": img,
                "link": link,
            }
        )
  except Exception as e:
    logging.error("eBay scrape error: %s", e)

  return results


def scrape_cdiscount(query):
  url = "https://www.cdiscount.com/search/10/" + requests.utils.quote(query) + ".html"
  results = []

  try:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    soup = BeautifulSoup(resp.text, "html.parser")
    items = soup.select("div.jsProductList div.prdtBloc")[:8]

    for item in items:
      title_el = item.select_one(".prdtBTit")
      price_el = item.select_one(".price")
      img_el = item.select_one("img")
      link_el = item.select_one("a")

      title = title_el.get_text(strip=True) if title_el else None
      price = clean_price(price_el.get_text(strip=True) if price_el else None)
      img = img_el["src"] if img_el and img_el.has_attr("src") else None
      link = "https://www.cdiscount.com" + link_el["href"] if link_el and link_el.has_attr("href") else None

      if title and price and link:
        results.append(
            {
                "merchant": "CDiscount",
                "title": title,
                "price": price,
                "image": img,
                "link": link,
            }
        )
  except Exception as e:
    logging.error("CDiscount scrape error: %s", e)

  return results


def scrape_fnac(query):
  url = "https://www.fnac.com/SearchResult/ResultList.aspx"
  params = {"Search": query}
  results = []

  try:
    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    soup = BeautifulSoup(resp.text, "html.parser")
    items = soup.select("article.Article-item")[:8]

    for item in items:
      title_el = item.select_one(".Article-title")
      price_el = item.select_one(".userPrice")
      img_el = item.select_one("img")
      link_el = item.select_one("a")

      title = title_el.get_text(strip=True) if title_el else None
      price = clean_price(price_el.get_text(strip=True) if price_el else None)
      img = img_el["src"] if img_el and img_el.has_attr("src") else None
      link = "https://www.fnac.com" + link_el["href"] if link_el and link_el.has_attr("href") else None

      if title and price and link:
        results.append(
            {
                "merchant": "Fnac",
                "title": title,
                "price": price,
                "image": img,
                "link": link,
            }
        )
  except Exception as e:
    logging.error("Fnac scrape error: %s", e)

  return results


def scrape_bestbuy(query):
  url = "https://www.bestbuy.com/site/searchpage.jsp"
  params = {"st": query}
  results = []

  try:
    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    soup = BeautifulSoup(resp.text, "html.parser")
    items = soup.select("li.sku-item")[:8]

    for item in items:
      title_el = item.select_one("h4.sku-header a")
      price_el = item.select_one("div.priceView-hero-price span[aria-hidden='true']")
      img_el = item.select_one("img.product-image")
      link_el = item.select_one("h4.sku-header a")

      title = title_el.get_text(strip=True) if title_el else None
      price = clean_price(price_el.get_text(strip=True) if price_el else None)
      img = img_el["src"] if img_el and img_el.has_attr("src") else None
      link = "https://www.bestbuy.com" + link_el["href"] if link_el and link_el.has_attr("href") else None

      if title and price and link:
        results.append(
            {
                "merchant": "BestBuy",
                "title": title,
                "price": price,
                "image": img,
                "link": link,
            }
        )
  except Exception as e:
    logging.error("BestBuy scrape error: %s", e)

  return results


# -------------------------------------------------------------------
# BUY OR WAIT + FAKE DISCOUNT
# -------------------------------------------------------------------
def analyze_discount_and_signal(query, results):
  if not results:
    return {
        "history_avg": None,
        "fake_discount": None,
        "signal": "No data",
        "reason": "No results to analyze.",
    }

  lowest_price = min(r["price"] + r["shipping"] for r in results)
  history_avg = get_price_history_stats(query)

  if history_avg is None:
    return {
        "history_avg": None,
        "fake_discount": None,
        "signal": "Neutral",
        "reason": "No historical data yet. We are still learning this product.",
    }

  # Fake discount: if current price is higher than 30-day average by >5%, flag as inflated
  if lowest_price > history_avg * 1.05:
    fake_discount = True
    reason = (
        f"Current lowest total price ({lowest_price:.2f}) is above 30-day average "
        f"({history_avg:.2f}). Discount may be inflated."
    )
  else:
    fake_discount = False
    reason = (
        f"Current lowest total price ({lowest_price:.2f}) is at or below 30-day average "
        f"({history_avg:.2f}). Deal looks genuine."
    )

  # Buy or Wait logic
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
  query = request.args.get("query", "").strip()
  if not query:
    return jsonify({"error": "Missing query parameter"}), 400

  logging.info("Search request: %s", query)

  sources = [
      ("serpapi", fetch_serpapi_shopping),
      ("amazon", scrape_amazon),
      ("ebay", scrape_ebay),
      ("cdiscount", scrape_cdiscount),
      ("fnac", scrape_fnac),
      ("bestbuy", scrape_bestbuy),
  ]

  aggregated = []

  with ThreadPoolExecutor(max_workers=6) as executor:
    future_to_source = {executor.submit(func, query): name for name, func in sources}
    for future in as_completed(future_to_source):
      name = future_to_source[future]
      try:
        data = future.result()
        logging.info("Source %s returned %d results", name, len(data))
        aggregated.extend(data)
      except Exception as e:
        logging.error("Error from source %s: %s", name, e)

  aggregated = dedupe_results(aggregated)

  # Add shipping and total price, store history
  for r in aggregated:
    shipping = simulate_shipping(r["merchant"], r["price"])
    r["shipping"] = shipping
    r["total"] = r["price"] + shipping
    store_price_history(query, r["title"], r["merchant"], r["total"])

  aggregated.sort(key=lambda x: x["total"])

  analysis = analyze_discount_and_signal(query, aggregated)

  return jsonify(
      {
          "query": query,
          "count": len(aggregated),
          "results": aggregated,
          "analysis": analysis,
      }
  )


# -------------------------------------------------------------------
# MAIN
# -------------------------------------------------------------------
if __name__ == "__main__":
  init_db()
  app.run(host="0.0.0.0", port=5000, debug=True)
