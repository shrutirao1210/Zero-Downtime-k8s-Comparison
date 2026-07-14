import os
import requests
from flask import Flask, jsonify

app = Flask(__name__)
VERSION = os.environ.get("APP_VERSION", "v1")
ENVIRONMENT = os.environ.get("DEPLOY_ENV", "blue")

CATALOG_URL = os.environ.get("CATALOG_URL", "http://catalog-svc:8080")
PRICE_URL = os.environ.get("PRICE_URL", "http://price-svc:8080")
INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://inventory-svc:8080")
SHIPPING_URL = os.environ.get("SHIPPING_URL", "http://shipping-svc:8080")

@app.after_request
def add_headers(resp):
    resp.headers["X-App-Version"] = VERSION
    resp.headers["X-Deploy-Env"] = ENVIRONMENT
    return resp

@app.route("/health")
def health():
    return jsonify({"status": "ok", "version": VERSION, "env": ENVIRONMENT}), 200

@app.route("/")
def root():
    return jsonify({"service": "api-gateway", "version": VERSION, "env": ENVIRONMENT}), 200

@app.route("/catalog")
def catalog():
    r = requests.get(f"{CATALOG_URL}/catalog", timeout=2)
    return jsonify(r.json()), r.status_code

@app.route("/price/<product_id>")
def price(product_id):
    r = requests.get(f"{PRICE_URL}/price/{product_id}", timeout=2)
    return jsonify(r.json()), r.status_code

@app.route("/inventory/<product_id>")
def inventory(product_id):
    r = requests.get(f"{INVENTORY_URL}/inventory/{product_id}", timeout=2)
    return jsonify(r.json()), r.status_code

@app.route("/shipping")
def shipping():
    r = requests.get(f"{SHIPPING_URL}/shipping", timeout=2)
    return jsonify(r.json()), r.status_code

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
