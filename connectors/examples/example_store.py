"""TEMPLATE: connect your own website (or any shop platform). Copy to ../my_site.py, change key/label/fields.

Implement publish_product to list products. To also receive orders, implement verify_webhook + normalize_order and point
your site's "order paid" webhook at  <your Cheape Deal address>/webhooks/store/<connection id>.
"""
from decimal import Decimal

from app.connectors.sdk import Field, NormalizedItem, NormalizedOrder, ProductPayload, StoreConnector, StoreError


class Connector(StoreConnector):
    key = "my_site"
    label = "My Website"
    description = "Lists products on my website."
    fields = [
        Field("base_url", "Website address", placeholder="https://mysite.com"),
        Field("api_key", "Site API key", secret=True),
        Field("webhook_secret", "Webhook secret", secret=True, required=False),
    ]

    def test_connection(self) -> str:
        self.http("GET", self.config["base_url"].rstrip("/") + "/api/ping", headers={"X-Api-Key": self.secrets["api_key"]}, error=StoreError)
        return "My website answered."

    def publish_product(self, p: ProductPayload) -> tuple[str, str]:
        r = self.http("POST", self.config["base_url"].rstrip("/") + "/api/products", headers={"X-Api-Key": self.secrets["api_key"]}, error=StoreError,
                      json={"title": p.title, "price": float(p.price), "sku": p.sku, "description": p.description,
                            "image": p.image_url, "stock": p.stock})
        return str(r["id"]), r.get("url", "")  # (id on your site, public link)

    # --- optional: receive orders ---
    def verify_webhook(self, raw_body: bytes, signature: str) -> bool:
        import hashlib, hmac
        secret = self.secrets.get("webhook_secret", "")
        return bool(secret) and hmac.compare_digest(hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest(), signature.removeprefix("sha256="))

    def normalize_order(self, p: dict) -> NormalizedOrder:
        return NormalizedOrder(
            store_order_id=str(p["id"]), number=str(p["id"]), status="paid", paid=True, customer_name=p["name"], email=p["email"],
            phone=p.get("phone", ""), address1=p["address1"], address2=p.get("address2", ""), city=p["city"], state=p.get("state", ""),
            zip_code=p["zip"], country_code=p["country_code"], country=p.get("country", p["country_code"]), total=Decimal(str(p["total"])),
            currency=p.get("currency", "USD"),
            items=[NormalizedItem(str(i["product_id"]), i.get("sku", ""), i["title"], int(i["qty"]), Decimal(str(i["price"]))) for i in p["items"]])
