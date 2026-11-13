"""Ways a website customer can pay. Which ones are on is chosen on the Website page (shop_methods).

  manual    you receive money any way you like and press "Mark paid"
  upi       direct UPI (INR). QR + link carry the exact amount and order number. No fee. You confirm (Mark paid), customer may send the UTR
  razorpay  hosted gateway (it charges its own fee)

Honest note: card payments cannot be accepted without a card processor, and every processor charges. "No commission" is possible only with
UPI and bank transfer (manual).
"""
from __future__ import annotations

import hashlib
import hmac
from urllib.parse import quote as urlq

from . import shopcfg as C

ALL = ["manual", "upi", "razorpay"]
LABELS = {"manual": "Pay later (we send details)", "upi": "UPI (India)", "razorpay": "Card / UPI / NetBanking (Razorpay)"}


def ready(db, method: str) -> bool:
    g = lambda k: C.get(db, k)  # noqa: E731
    return {"manual": True, "upi": bool(g("upi_vpa")), "razorpay": bool(g("razorpay_key_id") and C.read_secret(db, "razorpay_key_secret_enc"))}.get(method, False)


def enabled(db) -> list[str]:
    want = [m.strip() for m in C.get(db, "shop_methods").split(",") if m.strip() in ALL]
    out = [m for m in want if ready(db, m)]
    return out or ["manual"]


def options(db) -> list[dict]:
    return [{"id": m, "label": LABELS[m]} for m in enabled(db)]


def prepare(db, so, method: str, *, redirect_url: str = "") -> dict:
    """Create what the customer needs to pay `so` with `method`. Raises ValueError (customer-safe) if the method does not fit the order."""
    if method not in enabled(db):
        raise ValueError("Ye payment tarika abhi available nahi hai.")
    so.pay_mode = method
    num = f"W{1000 + so.id}"
    if method == "manual":
        return {"note": C.get(db, "shop_pay_note")}
    if method == "upi":
        if so.currency != "INR":
            raise ValueError("UPI sirf INR (rupees) mein hota hai. Currency INR chuno.")
        vpa, name = C.get(db, "upi_vpa"), C.get(db, "upi_name") or C.get(db, "shop_name")
        link = f"upi://pay?pa={urlq(vpa)}&pn={urlq(name)}&am={so.total}&cu=INR&tn={urlq(num)}"
        return {"link": link, "vpa": vpa, "amount": str(so.total), "note": f"Order number {num} note mein likho. Payment ke baad UTR niche daalo."}
    if method == "razorpay":
        key, secret = C.get(db, "razorpay_key_id"), C.read_secret(db, "razorpay_key_secret_enc")
        r = C.client().post("https://api.razorpay.com/v1/orders", auth=(key, secret), json={"amount": int(so.total * 100), "currency": so.currency, "receipt": num})
        if r.status_code >= 300:
            raise RuntimeError(f"Razorpay ne order nahi banaya ({r.status_code}): {r.text[:160]}")
        so.pay_ref = str(r.json().get("id", ""))
        return {"order_id": so.pay_ref, "amount": int(so.total * 100), "currency": so.currency, "key": key}
    raise ValueError("Unknown payment method")


# ---- signatures ------------------------------------------------------------------------------------
def hmac_hex(secret: str, data: bytes) -> str:
    return hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()


def razorpay_signature_ok(secret: str, order_id: str, payment_id: str, signature: str) -> bool:
    return bool(secret and order_id and payment_id and signature) and hmac.compare_digest(hmac_hex(secret, f"{order_id}|{payment_id}".encode()), signature)


def webhook_hex_ok(secret: str, raw: bytes, signature: str) -> bool:
    signature = (signature or "").removeprefix("sha256=")  # Razorpay sends plain hex
    return bool(secret and signature) and hmac.compare_digest(hmac_hex(secret, raw), signature)


# ---- refunds ---------------------------------------------------------------------------------------
def refund(db, so) -> tuple[bool, str]:
    """Razorpay: money goes back through its API. Everything else: you send it back yourself; we only record it."""
    if so.pay_mode == "razorpay" and so.payment_id and not so.payment_id.startswith(("manual", "payment")):
        r = C.client().post(f"https://api.razorpay.com/v1/payments/{so.payment_id}/refund",
                            auth=(C.get(db, "razorpay_key_id"), C.read_secret(db, "razorpay_key_secret_enc")), json={"amount": int(so.total * 100)})
        if r.status_code >= 300:
            return False, f"Razorpay refund fail ({r.status_code}): {r.text[:160]}"
        return True, "Razorpay se refund shuru ho gaya."
    return True, "Refund mark ho gaya. Paisa tumhe khud wapas bhejna hai (UPI/bank)."
