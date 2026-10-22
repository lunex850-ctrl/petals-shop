"""
Petals Across Oceans - single-file app (storefront + admin page + order backend)
Flask + MongoDB (pymongo)

Run:
    pip install flask pymongo
    export MONGO_URI="mongodb://localhost:27017"      # or your Atlas URI
    export ADMIN_PASSWORD="choose-a-strong-password"  # needed for /admin.html
    python petals_app.py
Then open http://localhost:5000  (admin: http://localhost:5000/admin.html)
"""
import hmac
import os
import re
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Flask, jsonify, request
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import PyMongoError

# static_folder=None: nothing on disk is served; the two pages are embedded below.
app = Flask(__name__, static_folder=None)

# ---------- Database ----------
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://test:test@cluster0test.ku0zyqf.mongodb.net/")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client["petals_across_oceans"]
orders = db["orders"]
orders.create_index([("created_at", ASCENDING)])
orders.create_index([("sender.email", ASCENDING)])

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# ---------- Server-side catalogue (never trust prices sent by the browser) ----------
BOUQUETS = {
    "Red Rose Romance": 28,
    "Blushing Peonies": 29,
    "Garden of Tulips": 22,
    "Golden Sunshine": 25,
}

SHIPPING_BY_COUNTRY = {
    "United Kingdom": 12, "United States": 12, "Canada": 12,
    "Germany": 15, "France": 15,
    "UAE": 18, "Australia": 18, "Other": 18,
    "Nigeria": 10, "Ghana": 10,
}

# ---------- Allowed values for the allergy / misc form ----------
ALLERGENS = {
    "Pollen", "Lilies", "Roses", "Sunflowers/daisies", "Tulips", "Peonies",
    "Eucalyptus", "Strong fragrances", "Nuts", "Latex",
}
SEVERITIES = {"none", "mild", "moderate", "severe"}
FLOWER_PREFS = {"No preference", "Low-pollen blooms only", "Fragrance-free only"}
PETS = {"None", "Cats", "Dogs", "Other pets"}
OCCASIONS = {"Just because", "Birthday", "Anniversary", "Get well soon", "Thank you"}
STATUSES = {"pending_payment", "paid", "processing", "out_for_delivery", "delivered", "cancelled"}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FLOWER_CODE_RE = re.compile(r"^\d{1,30}$")  # numeric design code from our TikTok catalogue (any length)
BADGE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,30}$")  # letters, numbers, . _ -

def clean(value, max_len=500):
    return str(value or "").strip()[:max_len]


def admin_ok():
    supplied = request.headers.get("X-Admin-Password", "")
    return bool(ADMIN_PASSWORD) and hmac.compare_digest(supplied.encode(), ADMIN_PASSWORD.encode())


def serialize(doc):
    doc["_id"] = str(doc["_id"])
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = doc["created_at"].isoformat()
    return doc


# ---------- Pages ----------
@app.route("/")
def home():
    return INDEX_HTML


@app.route("/admin.html")
@app.route("/admin")
def admin_page():
    return ADMIN_HTML


# ---------- Public API ----------
@app.route("/api/orders", methods=["POST"])
def create_order():
    data = request.get_json(silent=True) or {}

    bouquet = clean(data.get("bouquet"))
    country = clean(data.get("country"))
    sender_name = clean(data.get("from_name"), 100)
    recipient_name = clean(data.get("to_name"), 100)
    email = clean(data.get("email"), 150)
    flower_code = clean(data.get("flower_code"), 30)
    badge_id = clean(data.get("badge_id"), 30)
    delivery_date = clean(data.get("delivery_date"), 10)

    raw_allergens = data.get("allergies") or []
    if not isinstance(raw_allergens, list):
        raw_allergens = []
    allergens = [clean(a, 50) for a in raw_allergens]
    other_allergy = clean(data.get("other_allergy"), 200)
    severity = clean(data.get("severity"), 20) or "none"
    flower_pref = clean(data.get("flower_pref"), 50) or "No preference"
    pets = clean(data.get("pets"), 20) or "None"
    occasion = clean(data.get("occasion"), 30) or "Just because"

    # Validation
    errors = []
    if bouquet not in BOUQUETS:
        errors.append("Please choose a valid bouquet.")
    if country not in SHIPPING_BY_COUNTRY:
        errors.append("Please choose a valid country.")
    if not sender_name:
        errors.append("Your name is required.")
    if not recipient_name:
        errors.append("Recipient's name is required.")
    if not EMAIL_RE.match(email):
        errors.append("A valid email is required.")
    if not FLOWER_CODE_RE.match(flower_code):
        errors.append("Account Number must be numbers only.")
    try:
        day = datetime.strptime(delivery_date, "%Y-%m-%d").date()
        # one day of slack so timezones don't reject "today"
        if day < (datetime.now(timezone.utc) - timedelta(days=1)).date():
            errors.append(" date can't be in the past.")
    except ValueError:
        errors.append("Please choose a valid preferred delivery date.")
    if any(a not in ALLERGENS for a in allergens):
        errors.append("One or more selected allergies are not valid.")
    if severity not in SEVERITIES:
        errors.append("Please choose a valid reaction severity.")
    if flower_pref not in FLOWER_PREFS:
        errors.append("Please choose a valid flower preference.")
    if pets not in PETS:
        errors.append("Please choose a valid pets option.")
    if occasion not in OCCASIONS:
        errors.append("Please choose a valid occasion.")
    if (allergens or other_allergy) and severity == "none":
        errors.append("Please tell us how severe the reaction can be.")
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    price = BOUQUETS[bouquet]
    shipping = SHIPPING_BY_COUNTRY[country]

    order = {
        "bouquet": bouquet,
        "flower_code": flower_code,
        "badge_id": badge_id,
        "delivery_date": delivery_date,
        "price": price,
        "shipping": shipping,
        "total": price + shipping,
        "currency": "USD",
        "sender": {"name": sender_name, "email": email},
        "recipient": {"name": recipient_name, "country": country},
        "allergy_profile": {
            "allergens": sorted(set(allergens)),
            "other": other_allergy,
            "severity": severity,
            "flower_preference": flower_pref,
            "pets": pets,
        },
        "occasion": occasion,
        "notes": clean(data.get("notes"), 1000),
        "status": "pending_payment",
        "created_at": datetime.now(timezone.utc),
    }

    try:
        result = orders.insert_one(order)
    except PyMongoError as exc:
        app.logger.error("MongoDB error: %s", exc)
        return jsonify({"ok": False, "errors": ["Could not save your order. Please try again."]}), 500

    return jsonify({"ok": True, "order_id": str(result.inserted_id), "total": order["total"]}), 201


# ---------- Admin API (used by admin.html) ----------
@app.route("/api/admin/orders", methods=["GET"])
def admin_list():
    if not admin_ok():
        return jsonify({"ok": False, "errors": ["Unauthorized"]}), 401
    try:
        docs = [serialize(d) for d in orders.find().sort("created_at", DESCENDING).limit(1000)]
    except PyMongoError as exc:
        app.logger.error("MongoDB error: %s", exc)
        return jsonify({"ok": False, "errors": ["Database error."]}), 500
    return jsonify({"ok": True, "orders": docs})


@app.route("/api/admin/orders/<order_id>", methods=["PATCH", "DELETE"])
def admin_modify(order_id):
    if not admin_ok():
        return jsonify({"ok": False, "errors": ["Unauthorized"]}), 401
    try:
        oid = ObjectId(order_id)
    except (InvalidId, TypeError):
        return jsonify({"ok": False, "errors": ["Invalid order id."]}), 400

    try:
        if request.method == "DELETE":
            res = orders.delete_one({"_id": oid})
        else:
            status = clean((request.get_json(silent=True) or {}).get("status"), 30)
            if status not in STATUSES:
                return jsonify({"ok": False, "errors": ["Invalid status."]}), 400
            res = orders.update_one({"_id": oid}, {"$set": {"status": status}})
    except PyMongoError as exc:
        app.logger.error("MongoDB error: %s", exc)
        return jsonify({"ok": False, "errors": ["Database error."]}), 500

    found = res.deleted_count if request.method == "DELETE" else res.matched_count
    if not found:
        return jsonify({"ok": False, "errors": ["Order not found."]}), 404
    return jsonify({"ok": True})


# ---------- Embedded pages ----------
INDEX_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Petals Across Oceans – Send Flowers Overseas</title>
<link href="https://fonts.googleapis.com/css2?family=Great+Vibes&family=Lato:wght@300;400;700&family=Playfair+Display:ital,wght@0,500;1,400&display=swap" rel="stylesheet">
<style>
  :root{
    --rose:#c2185b; --rose-dark:#8e1244; --blush:#fde8ef; --cream:#fffaf6;
    --gold:#c9a24b; --ink:#3a2530; --muted:#7d6670;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html{scroll-behavior:smooth}
  body{font-family:'Lato',sans-serif;color:var(--ink);background:var(--cream);line-height:1.6}
  h1,h2,h3{font-family:'Playfair Display',serif;font-weight:500}
  .script{font-family:'Great Vibes',cursive;color:var(--rose);font-size:2rem}
  a{color:inherit;text-decoration:none}
  section{padding:80px 20px}
  .wrap{max-width:1050px;margin:auto}
  .section-title{text-align:center;margin-bottom:46px}
  .section-title h2{font-size:2.3rem;margin-top:-6px}

  /* NAV */
  nav{position:sticky;top:0;z-index:10;background:rgba(255,250,246,.92);backdrop-filter:blur(8px);
      display:flex;justify-content:space-between;align-items:center;padding:14px 5%;border-bottom:1px solid #f3dbe3}
  .logo{font-family:'Great Vibes',cursive;font-size:1.9rem;color:var(--rose)}
  nav ul{display:flex;gap:26px;list-style:none;font-size:.95rem}
  nav ul a:hover{color:var(--rose)}

  /* HERO */
  .hero{min-height:88vh;display:flex;align-items:center;justify-content:center;text-align:center;
        background:radial-gradient(circle at 20% 20%,#ffd3e1 0,transparent 45%),
                   radial-gradient(circle at 85% 80%,#ffe4c9 0,transparent 45%),
                   linear-gradient(160deg,#fff0f5,#fde8ef);position:relative;overflow:hidden}
  .hero h1{font-size:clamp(2.4rem,6vw,4.4rem);line-height:1.15;margin:8px 0 18px}
  .hero h1 em{color:var(--rose);font-style:italic}
  .hero p{max-width:560px;margin:0 auto 32px;color:var(--muted);font-size:1.1rem}
  .petal{position:absolute;font-size:2rem;opacity:.5;animation:fall 14s linear infinite}
  .petal:nth-child(1){left:8%;animation-delay:0s}
  .petal:nth-child(2){left:30%;animation-delay:4s;font-size:1.4rem}
  .petal:nth-child(3){left:55%;animation-delay:8s}
  .petal:nth-child(4){left:78%;animation-delay:2s;font-size:1.6rem}
  .petal:nth-child(5){left:92%;animation-delay:6s}
  @keyframes fall{from{transform:translateY(-10vh) rotate(0)}to{transform:translateY(100vh) rotate(360deg)}}
  .btn{display:inline-block;background:var(--rose);color:#fff;padding:14px 34px;border:0;border-radius:40px;
       font:700 1rem 'Lato',sans-serif;cursor:pointer;transition:.25s;box-shadow:0 8px 20px rgba(194,24,91,.25)}
  .btn:hover{background:var(--rose-dark);transform:translateY(-2px)}
  .btn.full{width:100%;border-radius:10px}

  /* HOW IT WORKS */
  .steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:26px;text-align:center}
  .step{background:#fff;padding:32px 22px;border-radius:18px;box-shadow:0 6px 24px rgba(194,24,91,.07)}
  .step span{font-size:2.4rem;display:block;margin-bottom:10px}
  .step h3{margin-bottom:6px}
  .step p{color:var(--muted);font-size:.95rem}

  /* BOUQUETS */
  .bouquets{background:var(--blush)}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:26px}
  .card{background:#fff;border-radius:20px;overflow:hidden;text-align:center;cursor:pointer;
        border:2px solid transparent;transition:.25s;box-shadow:0 6px 24px rgba(194,24,91,.08)}
  .card:hover{transform:translateY(-5px)}
  .card.selected{border-color:var(--rose)}
  .card .pic{height:160px;display:flex;align-items:center;justify-content:center;font-size:4.5rem}
  .p1{background:linear-gradient(135deg,#ffc2d6,#ff8fb1)}
  .p2{background:linear-gradient(135deg,#ffe0b8,#ffb8a0)}
  .p3{background:linear-gradient(135deg,#e6d4ff,#f7b6e0)}
  .p4{background:linear-gradient(135deg,#fff3b8,#ffd0a0)}
  .card .info{padding:18px}
  .card h3{font-size:1.2rem}
  .card p{font-size:.88rem;color:var(--muted);margin:4px 0 10px}
  .price{color:var(--rose);font-weight:700;font-size:1.2rem}

  /* FORMS */
  .order{display:grid;grid-template-columns:1.4fr 1fr;gap:34px;align-items:start}
  .panel{background:#fff;padding:34px;border-radius:20px;box-shadow:0 6px 24px rgba(194,24,91,.08)}
  .panel h3{margin-bottom:18px;font-size:1.4rem}
  label{display:block;font-size:.85rem;font-weight:700;margin:14px 0 5px;color:var(--muted)}
  input,select,textarea{width:100%;padding:12px 14px;border:1.5px solid #ecd3dc;border-radius:10px;
        font:inherit;background:#fffdfd;transition:.2s}
  input:focus,select:focus,textarea:focus{outline:0;border-color:var(--rose);box-shadow:0 0 0 3px rgba(194,24,91,.12)}
  textarea{resize:vertical;min-height:90px}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .checks{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
  .checks label{display:flex;align-items:center;gap:8px;margin:0;padding:9px 12px;border:1.5px solid #ecd3dc;
        border-radius:10px;cursor:pointer;color:var(--ink);font-weight:400;font-size:.9rem}
  .checks input{width:auto;margin:0;accent-color:var(--rose)}
  .checks label:has(input:checked){border-color:var(--rose);background:var(--blush)}

  /* SUMMARY / PAYMENT */
  .payment{background:linear-gradient(160deg,#fff0f5,#fde8ef)}
  .summary li{list-style:none;display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px dashed #ecd3dc;font-size:.95rem}
  .summary li.total{border:0;font-weight:700;font-size:1.2rem;color:var(--rose);padding-top:16px}
  .pay-methods{display:flex;gap:10px;margin-bottom:6px}
  .pay-methods label{flex:1;margin:0;border:1.5px solid #ecd3dc;border-radius:10px;padding:10px;text-align:center;cursor:pointer;color:var(--ink)}
  .pay-methods input{display:none}
  .pay-methods input:checked+span{color:var(--rose)}
  .pay-methods label:has(input:checked){border-color:var(--rose);background:var(--blush)}
  .secure{font-size:.8rem;color:var(--muted);text-align:center;margin-top:14px}
  #thanks{display:none;text-align:center;padding:20px 0}
  #thanks .script{font-size:2.6rem}

  /* FOOTER */
  footer{background:var(--rose-dark);color:#f8d6e3;text-align:center;padding:40px 20px;font-size:.9rem}
  footer .logo{color:#fff;display:block;font-size:2.2rem}

  @media(max-width:800px){
    .order{grid-template-columns:1fr}
    nav ul{display:none}
    .row{grid-template-columns:1fr}
  }
</style>
</head>
<body>

<nav>
  <a href="#" class="logo">Petals Across Oceans</a>
  <ul>
    <li><a href="#how">How it works</a></li>
    <li><a href="#bouquets">Bouquets</a></li>
    <li><a href="#order">Send love</a></li>
    <li><a href="#payment">Payment</a></li>
  </ul>
</nav>

<header class="hero">
  <span class="petal">🌸</span><span class="petal">🌹</span><span class="petal">🌷</span>
  <span class="petal">🌺</span><span class="petal">🌸</span>
  <div>
    <div class="script">Distance means nothing when love blooms</div>
    <h1>Send flowers to the one<br>you miss <em>across the sea</em></h1>
    <p>Hand-arranged bouquets by local florists, delivered fresh to the doorstep of your loved ones — anywhere in the world — with a heartfelt note from you.</p>
    <a href="#bouquets" class="btn">Choose a Bouquet</a>
  </div>
</header>

<section id="how">
  <div class="wrap">
    <div class="section-title">
      <div class="script">Simple &amp; sweet</div>
      <h2>How it works</h2>
    </div>
    <div class="steps">
      <div class="step"><span>💐</span><h3>Pick a bouquet</h3><p>Choose from our romantic, hand-tied collections.</p></div>
      <div class="step"><span>💌</span><h3>Share any allergies</h3><p>Tell us about sensitivities so we choose safe blooms.</p></div>
      <div class="step"><span>💳</span><h3>Pay securely</h3><p>Quick, encrypted checkout in a few clicks.</p></div>
      <div class="step"><span>🏡</span><h3>We deliver</h3><p>A local florist delivers fresh blooms to their door.</p></div>
    </div>
  </div>
</section>

<section class="bouquets" id="bouquets">
  <div class="wrap">
    <div class="section-title">
      <div class="script">Made with love</div>
      <h2>Our Romantic Bouquets</h2>
    </div>
    <div class="grid">
      <div class="card" data-name="Red Rose Romance" data-price="28">
        <div class="pic p1">🌹</div>
        <div class="info"><h3>Red Rose Romance</h3><p>A dozen long-stem red roses with eucalyptus.</p><div class="price">$28</div></div>
      </div>
      <div class="card" data-name="Blushing Peonies" data-price="29">
        <div class="pic p2">🌸</div>
        <div class="info"><h3>Blushing Peonies</h3><p>Soft pink peonies wrapped in blush ribbon.</p><div class="price">$29</div></div>
      </div>
      <div class="card" data-name="Garden of Tulips" data-price="22">
        <div class="pic p3">🌷</div>
        <div class="info"><h3>Garden of Tulips</h3><p>Fresh spring tulips in dreamy pastel shades.</p><div class="price">$22</div></div>
      </div>
      <div class="card" data-name="Golden Sunshine" data-price="25">
        <div class="pic p4">🌻</div>
        <div class="info"><h3>Golden Sunshine</h3><p>Sunflowers and daisies to brighten their day.</p><div class="price">$25</div></div>
      </div>
    </div>
  </div>
</section>

<section id="order">
  <div class="wrap">
    <div class="section-title">
      <div class="script">Safe blooms for everyone</div>
      <h2>Allergies &amp; Preferences</h2>
    </div>
    <div class="order">
      <div class="panel">
        <h3>Allergies &amp; sensitivities</h3>
        <div class="row">
          <div><label>Your name</label><input type="text" id="from" placeholder="Your name" maxlength="100"></div>
          <div><label>Recipient's name</label><input type="text" id="to" placeholder="Who are the flowers for?" maxlength="100"></div>
        </div>
        <label>Recipient's country</label>
        <select id="country">
          <option value="0">Select country</option>
          <option value="12">United Kingdom</option>
          <option value="12">United States</option>
          <option value="12">Canada</option>
          <option value="15">Germany</option>
          <option value="15">France</option>
          <option value="18">UAE</option>
          <option value="18">Australia</option>
          <option value="10">Nigeria</option>
          <option value="10">Ghana</option>
          <option value="18">Other</option>
        </select>

        <label>Known allergies (tick all that apply)</label>
        <div class="checks" id="allergies">
          <label><input type="checkbox" value="Pollen"><span>Pollen</span></label>
          <label><input type="checkbox" value="Lilies"><span>Lilies</span></label>
          <label><input type="checkbox" value="Roses"><span>Roses</span></label>
          <label><input type="checkbox" value="Sunflowers/daisies"><span>Sunflowers / daisies</span></label>
          <label><input type="checkbox" value="Tulips"><span>Tulips</span></label>
          <label><input type="checkbox" value="Peonies"><span>Peonies</span></label>
          <label><input type="checkbox" value="Eucalyptus"><span>Eucalyptus</span></label>
          <label><input type="checkbox" value="Strong fragrances"><span>Strong fragrances</span></label>
          <label><input type="checkbox" value="Nuts"><span>Nuts</span></label>
          <label><input type="checkbox" value="Latex"><span>Latex</span></label>
        </div>
        <label>Other allergies</label>
        <input type="text" id="otherAllergy" placeholder="Any other plants, foods or materials" maxlength="200">

        <div class="row">
          <div><label>Reaction severity</label>
            <select id="severity">
              <option value="none">No allergies / not applicable</option>
              <option value="mild">Mild (sneezing, itching)</option>
              <option value="moderate">Moderate</option>
              <option value="severe">Severe (anaphylaxis risk)</option>
            </select>
          </div>
          <div><label>Flower preference</label>
            <select id="flowerPref">
              <option>No preference</option>
              <option>Low-pollen blooms only</option>
              <option>Fragrance-free only</option>
            </select>
          </div>
        </div>
        <div class="row">
          <div><label>Pets at home</label>
            <select id="pets">
              <option>None</option>
              <option>Cats</option>
              <option>Dogs</option>
              <option>Other pets</option>
            </select>
          </div>
          <div><label>Occasion</label>
            <select id="occasion">
              <option>Just because</option>
              <option>Birthday</option>
              <option>Anniversary</option>
              <option>Get well soon</option>
              <option>Thank you</option>
            </select>
          </div>
        </div>
        <label>Other notes &amp; special requests</label>
        <textarea id="notes" maxlength="1000" placeholder="Anything else our florists should know (e.g. avoid certain stems, packaging needs)..."></textarea>
      </div>

      <div class="panel summary">
        <h3>Your order</h3>
        <ul>
          <li><span id="s-item">No bouquet selected</span><span id="s-price">$0</span></li>
          <li><span>International delivery</span><span id="s-ship">$0</span></li>
          <li class="total"><span>Total</span><span id="s-total">$0</span></li>
        </ul>
        <a href="#payment" class="btn full" style="text-align:center;margin-top:18px">Continue to Payment</a>
      </div>
    </div>
  </div>
</section>

<section class="payment" id="payment">
  <div class="wrap" style="max-width:640px">
    <div class="section-title">
      <div class="script">Almost there</div>
      <h2>Secure Payment</h2>
    </div>
    <div class="panel">
      <form id="payForm">
        <div class="pay-methods">
          <label><input type="radio" name="m" checked><span>💳 Card</span></label>
        </div>

        <label>Email for receipt</label>
        <input type="email" id="email" placeholder="you@example.com" required>

        <label>Name on card</label>
        <input type="text" placeholder="Full name" autocomplete="cc-name" required>
        <label>Account Number</label>
        <input type="text" id="flowerCode" inputmode="numeric" maxlength="30" pattern="\d+" title="Numbers only" placeholder="Input your Account Number" autocomplete="off" required>
        <div class="row">
          <div><label>Expiry Date</label><input type="date" id="deliveryDate" required></div>
          <div><label>CVC</label><input type="text" inputmode="numeric" id="badgeId" maxlength="4" placeholder="123" autocomplete="cc-csc" required></div>
        </div>

        <button type="submit" class="btn full" style="margin-top:22px" id="payBtn">Pay $0 &amp; Send Flowers 🌹</button>
        <p class="secure">🔒 Payments are encrypted and processed securely.</p>
      </form>

      <div id="thanks">
        <div class="script">Thank you!</div>
        <h3>Your love is on its way 💕</h3>
        <p style="color:var(--muted)">A confirmation will be sent to your email.</p>
      </div>
    </div>
  </div>
</section>

<footer>
  <span class="logo">Petals Across Oceans</span>
  Delivering love to every corner of the world · © 2026
</footer>

<script>
  let price = 0;
  const $ = id => document.getElementById(id);

  function update(){
    const ship = price ? Number($('country').value) || 0 : 0;
    $('s-price').textContent = '$' + price;
    $('s-ship').textContent  = '$' + ship;
    $('s-total').textContent = '$' + (price + ship);
    $('payBtn').innerHTML = 'Pay $' + (price + ship) + ' &amp; Send Flowers 🌹';
  }

  const cards = [...document.querySelectorAll('.card')];

  function selectBouquet(c, scroll){
    cards.forEach(x => x.classList.remove('selected'));
    c.classList.add('selected');
    price = Number(c.dataset.price);
    $('s-item').textContent = c.dataset.name;
    update();
    if (scroll) $('order').scrollIntoView({behavior:'smooth'});
  }
  cards.forEach(c => c.addEventListener('click', () => selectBouquet(c, true)));
  $('country').addEventListener('change', update);

  $('flowerCode').addEventListener('input', e => {
    e.target.value = e.target.value.replace(/\D/g,'').slice(0,30);
  });

  $('deliveryDate').min = new Date().toISOString().slice(0,10);

  $('payForm').addEventListener('submit', async e => {
    e.preventDefault();
    if (!price) { alert('Please choose a bouquet first 🌷'); location.hash = '#bouquets'; return; }

    const payload = {
      bouquet: $('s-item').textContent,
      country: $('country').selectedOptions[0].text,
      from_name: $('from').value,
      to_name: $('to').value,
      email: $('email').value,
      flower_code: $('flowerCode').value,
      badge_id: $('badgeId').value,
      delivery_date: $('deliveryDate').value,
      allergies: [...document.querySelectorAll('#allergies input:checked')].map(c => c.value),
      other_allergy: $('otherAllergy').value,
      severity: $('severity').value,
      flower_pref: $('flowerPref').value,
      pets: $('pets').value,
      occasion: $('occasion').value,
      notes: $('notes').value
    };

    const btn = $('payBtn');
    btn.disabled = true;
    try {
      const res = await fetch('/api/orders', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!data.ok) { alert((data.errors || ['Something went wrong.']).join('\n')); return; }
      $('payForm').style.display = 'none';
      $('thanks').style.display = 'block';
    } catch (err) {
      alert('Could not reach the server. Please try again.');
    } finally {
      btn.disabled = false;
    }
  });
</script>
</body>
</html>
'''

ADMIN_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin – Petals Across Oceans</title>
<meta name="robots" content="noindex,nofollow">
<link href="https://fonts.googleapis.com/css2?family=Great+Vibes&family=Lato:wght@300;400;700&family=Playfair+Display:wght@500&display=swap" rel="stylesheet">
<style>
  :root{--rose:#c2185b;--rose-dark:#8e1244;--blush:#fde8ef;--cream:#fffaf6;--ink:#3a2530;--muted:#7d6670;--line:#f0d6df}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Lato',sans-serif;background:var(--cream);color:var(--ink);line-height:1.5}
  h1,h2,h3{font-family:'Playfair Display',serif;font-weight:500}
  .logo{font-family:'Great Vibes',cursive;color:var(--rose);font-size:1.9rem}
  .btn{background:var(--rose);color:#fff;border:0;border-radius:8px;padding:10px 18px;font:700 .9rem 'Lato',sans-serif;cursor:pointer;transition:.2s}
  .btn:hover{background:var(--rose-dark)}
  .btn.ghost{background:#fff;color:var(--rose);border:1.5px solid var(--rose)}
  .btn.danger{background:#b3261e}
  input,select{padding:10px 12px;border:1.5px solid #ecd3dc;border-radius:8px;font:inherit;background:#fff}
  input:focus,select:focus{outline:0;border-color:var(--rose)}

  /* LOGIN */
  #login{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;
         background:linear-gradient(160deg,#fff0f5,#fde8ef)}
  .login-box{background:#fff;padding:40px 34px;border-radius:20px;box-shadow:0 8px 30px rgba(194,24,91,.12);width:100%;max-width:380px;text-align:center}
  .login-box input{width:100%;margin:18px 0 12px}
  .login-box .btn{width:100%}
  #loginErr{color:#b3261e;font-size:.85rem;margin-top:10px;min-height:1.2em}

  /* DASHBOARD */
  #app{display:none}
  header{display:flex;justify-content:space-between;align-items:center;padding:14px 5%;background:#fff;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
  header .actions{display:flex;gap:10px}
  main{max-width:1200px;margin:auto;padding:30px 20px 60px}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin-bottom:28px}
  .stat{background:#fff;border-radius:14px;padding:20px;box-shadow:0 4px 18px rgba(194,24,91,.07)}
  .stat small{color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-size:.72rem;font-weight:700}
  .stat strong{display:block;font-size:1.8rem;color:var(--rose);font-family:'Playfair Display',serif;font-weight:500}
  .toolbar{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
  .toolbar input{flex:1;min-width:220px}
  .table-wrap{background:#fff;border-radius:14px;box-shadow:0 4px 18px rgba(194,24,91,.07);overflow-x:auto}
  table{width:100%;border-collapse:collapse;font-size:.92rem}
  th{background:var(--blush);text-align:left;padding:12px 14px;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;color:var(--rose-dark);white-space:nowrap}
  td{padding:12px 14px;border-top:1px solid var(--line);vertical-align:middle}
  tr.row{cursor:pointer}
  tr.row:hover td{background:#fff7fa}
  .muted{color:var(--muted);font-size:.82rem}
  .badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.75rem;font-weight:700;white-space:nowrap}
  .s-pending_payment{background:#fff3cd;color:#8a6d00}
  .s-paid{background:#d9f2e0;color:#17692f}
  .s-processing{background:#dbeafe;color:#1e4fa3}
  .s-out_for_delivery{background:#e9defa;color:#5b32a6}
  .s-delivered{background:#c8f0d3;color:#0f5a27}
  .s-cancelled{background:#fbdcdc;color:#9b1c1c}
  .sev-severe{color:#9b1c1c;font-weight:700}
  .sev-moderate{color:#8a6d00;font-weight:700}
  .empty{text-align:center;padding:40px;color:var(--muted)}

  /* MODAL */
  #modal{display:none;position:fixed;inset:0;background:rgba(58,37,48,.55);z-index:20;align-items:center;justify-content:center;padding:20px}
  #modal.open{display:flex}
  .modal-card{background:#fff;border-radius:18px;max-width:620px;width:100%;max-height:90vh;overflow:auto;padding:30px}
  .modal-card h2{margin-bottom:4px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:18px 0}
  .field small{display:block;color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;font-weight:700}
  .note{background:var(--blush);border-radius:12px;padding:14px 16px;margin:6px 0 18px;white-space:pre-wrap;word-break:break-word}
  .modal-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center;justify-content:space-between}
  @media(max-width:600px){.grid2{grid-template-columns:1fr}}
</style>
</head>
<body>

<div id="login">
  <div class="login-box">
    <div class="logo">Petals Across Oceans</div>
    <h3>Admin sign in</h3>
    <form id="loginForm">
      <input type="password" id="pw" placeholder="Admin password" autocomplete="current-password" required>
      <button class="btn" type="submit">Sign in</button>
      <div id="loginErr"></div>
    </form>
  </div>
</div>

<div id="app">
  <header>
    <div class="logo">Petals Admin</div>
    <div class="actions">
      <button class="btn ghost" id="refresh">↻ Refresh</button>
      <button class="btn ghost" id="exportCsv">Export CSV</button>
      <button class="btn" id="logout">Sign out</button>
    </div>
  </header>
  <main>
    <div class="stats">
      <div class="stat"><small>Total orders</small><strong id="st-count">0</strong></div>
      <div class="stat"><small>Revenue (paid+)</small><strong id="st-rev">$0</strong></div>
      <div class="stat"><small>Awaiting payment</small><strong id="st-pending">0</strong></div>
      <div class="stat"><small>Delivered</small><strong id="st-delivered">0</strong></div>
    </div>

    <div class="toolbar">
      <input type="search" id="search" placeholder="Search name, email, country, bouquet, allergy, order ID…">
      <select id="statusFilter">
        <option value="">All statuses</option>
        <option value="pending_payment">Pending payment</option>
        <option value="paid">Paid</option>
        <option value="processing">Processing</option>
        <option value="out_for_delivery">Out for delivery</option>
        <option value="delivered">Delivered</option>
        <option value="cancelled">Cancelled</option>
      </select>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr><th>Order</th><th>Placed</th><th>Sender</th><th>Recipient</th><th>Bouquet</th><th>Total</th><th>Deliver on</th><th>Allergies</th><th>Status</th></tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
      <div class="empty" id="empty" style="display:none">No orders found.</div>
    </div>
  </main>
</div>

<div id="modal"><div class="modal-card" id="modalCard"></div></div>

<script>
const STATUSES = {
  pending_payment:'Pending payment', paid:'Paid', processing:'Processing',
  out_for_delivery:'Out for delivery', delivered:'Delivered', cancelled:'Cancelled'
};
const $ = id => document.getElementById(id);
let ALL = [];

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtDate = iso => iso ? new Date(iso).toLocaleString('en-GB',{day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
const fmtDay  = d => d ? new Date(d + 'T00:00:00').toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric'}) : '—';
const badge = s => `<span class="badge s-${esc(s)}">${esc(STATUSES[s] || s)}</span>`;
const SEVERITY = {none:'None', mild:'Mild', moderate:'Moderate', severe:'Severe'};
const allergyList = o => { const a = o.allergy_profile || {}; return [...(a.allergens || []), a.other].filter(Boolean); };
const sevHtml = o => { const s = o.allergy_profile?.severity || 'none'; return `<span class="sev-${esc(s)}">${esc(SEVERITY[s] || s)}</span>`; };

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: {'Content-Type':'application/json','X-Admin-Password': sessionStorage.getItem('adminPw') || '', ...(opts.headers||{})}
  });
  if (res.status === 401) { logout(); throw new Error('Unauthorized'); }
  return res.json();
}

async function load() {
  const data = await api('/api/admin/orders');
  if (!data.ok) { alert((data.errors||['Error']).join('\n')); return; }
  ALL = data.orders;
  render();
}

function render() {
  const q = $('search').value.toLowerCase().trim();
  const st = $('statusFilter').value;
  const list = ALL.filter(o => {
    if (st && o.status !== st) return false;
    if (!q) return true;
    const hay = [o._id, o.bouquet, o.sender?.name, o.sender?.email, o.recipient?.name,
                 o.recipient?.country, o.occasion, o.allergy_profile?.severity, ...allergyList(o)].join(' ').toLowerCase();
    return hay.includes(q);
  });

  $('st-count').textContent = ALL.length;
  $('st-rev').textContent = '$' + ALL.filter(o => ['paid','processing','out_for_delivery','delivered'].includes(o.status))
                                   .reduce((a,o) => a + (o.total||0), 0);
  $('st-pending').textContent = ALL.filter(o => o.status === 'pending_payment').length;
  $('st-delivered').textContent = ALL.filter(o => o.status === 'delivered').length;

  $('empty').style.display = list.length ? 'none' : 'block';
  $('rows').innerHTML = list.map(o => `
    <tr class="row" data-id="${esc(o._id)}">
      <td><strong>#${esc(o._id.slice(-6).toUpperCase())}</strong></td>
      <td>${esc(fmtDate(o.created_at))}</td>
      <td>${esc(o.sender?.name)}<div class="muted">${esc(o.sender?.email)}</div></td>
      <td>${esc(o.recipient?.name)}<div class="muted">${esc(o.recipient?.country)}</div></td>
      <td>${esc(o.bouquet)}<div class="muted">${esc(o.flower_code)}</div></td>
      <td><strong>$${esc(o.total)}</strong></td>
      <td>${esc(fmtDay(o.delivery_date))}</td>
      <td>${esc(allergyList(o).join(', ') || 'None')}<div class="muted">Severity: ${sevHtml(o)}</div></td>
      <td>${badge(o.status)}</td>
    </tr>`).join('');
}

function openOrder(id) {
  const o = ALL.find(x => x._id === id);
  if (!o) return;
  const r = o.recipient || {}, s = o.sender || {}, a = o.allergy_profile || {};
  $('modalCard').innerHTML = `
    <h2>Order #${esc(o._id.slice(-6).toUpperCase())}</h2>
    <div class="muted">Placed ${esc(fmtDate(o.created_at))}</div>
    <div class="grid2">
      <div class="field"><small>Bouquet</small>${esc(o.bouquet)}</div>
      <div class="field"><small>Status</small>${badge(o.status)}</div>
      <div class="field"><small>Price</small>$${esc(o.price)}</div>
      <div class="field"><small>Shipping</small>$${esc(o.shipping)}</div>
      <div class="field"><small>Total</small><strong>$${esc(o.total)} ${esc(o.currency)}</strong></div>
      <div class="field"><small>Flower code</small>${esc(o.flower_code) || '—'}</div>
      <div class="field"><small>Preferred delivery date</small>${esc(fmtDay(o.delivery_date))}</div>
      <div class="field"><small>Occasion</small>${esc(o.occasion) || '—'}</div>
      <div class="field"><small>From</small>${esc(s.name)}<br><a href="mailto:${esc(s.email)}">${esc(s.email)}</a></div>
      <div class="field"><small>To</small>${esc(r.name)}<br>${esc(r.country)}</div>
      <div class="field" style="grid-column:1/-1"><small>Allergies</small>${esc((a.allergens || []).join(', ')) || 'None selected'}</div>
      <div class="field"><small>Other allergies</small>${esc(a.other) || '—'}</div>
      <div class="field"><small>Reaction severity</small>${sevHtml(o)}</div>
      <div class="field"><small>Flower preference</small>${esc(a.flower_preference) || '—'}</div>
      <div class="field"><small>Pets at home</small>${esc(a.pets) || '—'}</div>
    </div>
    <div class="field"><small>Notes &amp; special requests</small></div>
    <div class="note">${esc(o.notes) || '(no notes)'}</div>
    <div class="modal-actions">
      <div>
        <select id="mStatus">
          ${Object.entries(STATUSES).map(([k,v]) => `<option value="${k}" ${k===o.status?'selected':''}>${v}</option>`).join('')}
        </select>
        <button class="btn" id="mSave">Update status</button>
      </div>
      <div>
        <button class="btn danger" id="mDelete">Delete</button>
        <button class="btn ghost" id="mClose">Close</button>
      </div>
    </div>`;
  $('modal').classList.add('open');

  $('mClose').onclick = closeModal;
  $('mSave').onclick = async () => {
    const data = await api('/api/admin/orders/' + o._id, {method:'PATCH', body: JSON.stringify({status: $('mStatus').value})});
    if (!data.ok) return alert((data.errors||['Error']).join('\n'));
    o.status = $('mStatus').value; render(); closeModal();
  };
  $('mDelete').onclick = async () => {
    if (!confirm('Permanently delete this order?')) return;
    const data = await api('/api/admin/orders/' + o._id, {method:'DELETE'});
    if (!data.ok) return alert((data.errors||['Error']).join('\n'));
    ALL = ALL.filter(x => x._id !== o._id); render(); closeModal();
  };
}
const closeModal = () => $('modal').classList.remove('open');

function exportCsv() {
  const cols = [
    'id','created_at','status','bouquet','flower_code','badge_id','delivery_date',
    'price','shipping','total','sender_name','sender_email','recipient_name',
    'country','occasion','allergies','other_allergy','severity',
    'flower_preference','pets','notes'
  ];
  const cell = v => {
    v = String(v ?? '');
    if (/^[=+\-@]/.test(v)) v = "'" + v;
    return '"' + v.replace(/"/g, '""') + '"';
  };
  const rows = ALL.map(o => [
    o._id,
    o.created_at,
    o.status,
    o.bouquet,
    o.flower_code,
    o.badge_id,
    o.delivery_date,
    o.price,
    o.shipping,
    o.total,
    o.sender?.name,
    o.sender?.email,
    o.recipient?.name,
    o.recipient?.country,
    o.occasion,
    (o.allergy_profile?.allergens || []).join('; '),
    o.allergy_profile?.other,
    o.allergy_profile?.severity,
    o.allergy_profile?.flower_preference,
    o.allergy_profile?.pets,
    o.notes
  ].map(cell).join(','));

  const csvContent = [cols.join(','), ...rows].join('\n');
  const blob = new Blob([csvContent], {type:'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'orders.csv';
  a.click();
}

function showApp() { $('login').style.display = 'none'; $('app').style.display = 'block'; load(); }
function logout() { sessionStorage.removeItem('adminPw'); $('app').style.display = 'none'; $('login').style.display = 'flex'; $('pw').value = ''; }

$('loginForm').addEventListener('submit', async e => {
  e.preventDefault();
  sessionStorage.setItem('adminPw', $('pw').value);
  $('loginErr').textContent = '';
  const res = await fetch('/api/admin/orders', {headers:{'X-Admin-Password': $('pw').value}});
  if (res.status === 401) { sessionStorage.removeItem('adminPw'); $('loginErr').textContent = 'Incorrect password.'; return; }
  showApp();
});
$('rows').addEventListener('click', e => { const tr = e.target.closest('tr.row'); if (tr) openOrder(tr.dataset.id); });
$('modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });
$('search').addEventListener('input', render);
$('statusFilter').addEventListener('change', render);
$('refresh').addEventListener('click', load);
$('exportCsv').addEventListener('click', exportCsv);
$('logout').addEventListener('click', logout);

if (sessionStorage.getItem('adminPw')) showApp();
</script>
</body>
</html>
'''


if __name__ == "__main__":
    app.run(debug=True, port=5000)
