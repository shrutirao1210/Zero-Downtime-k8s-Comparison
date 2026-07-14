import os
from flask import Flask, jsonify

app = Flask(__name__)
VERSION = os.environ.get("APP_VERSION", "v1")
ENVIRONMENT = os.environ.get("DEPLOY_ENV", "blue")
PRODUCTS = ["Laptop", "Phone", "Tablet", "Headphones", "Monitor"]

@app.after_request
def add_headers(resp):
    resp.headers["X-App-Version"] = VERSION
    resp.headers["X-Deploy-Env"] = ENVIRONMENT
    return resp

@app.route("/catalog")
def catalog():
    return jsonify({"products": PRODUCTS, "version": VERSION, "env": ENVIRONMENT})

@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
