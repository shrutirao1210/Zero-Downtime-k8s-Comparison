import os
from flask import Flask, jsonify

app = Flask(__name__)
VERSION = os.environ.get("APP_VERSION", "v1")
ENVIRONMENT = os.environ.get("DEPLOY_ENV", "blue")
PRICES = {"1": 999.99, "2": 599.49, "3": 299.00, "4": 149.50, "5": 219.75}

@app.after_request
def add_headers(resp):
    resp.headers["X-App-Version"] = VERSION
    resp.headers["X-Deploy-Env"] = ENVIRONMENT
    return resp

@app.route("/price/<product_id>")
def price(product_id):
    amount = PRICES.get(product_id, 99.99)
    return jsonify({"product_id": product_id, "price": amount, "version": VERSION, "env": ENVIRONMENT})

@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
