"""Pull feed: a custom website can read the listed products from here instead of (or as well as) us pushing them."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..models import Product
from ..services import settings_service as S
from .deps import get_db

router = APIRouter()


@router.get("/feed/products.json")
def products_feed(token: str = "", db: Session = Depends(get_db)):
    want = S.get(db, "feed_token", "")
    if not want or not hmac.compare_digest(want, token or ""):
        raise HTTPException(status_code=401, detail="Bad or missing token")
    rows = db.query(Product).filter(Product.listing_status == "listed", Product.active.is_(True), Product.auto_hidden.is_(False),
                                    Product.stock > 0).order_by(Product.id).all()
    return {"count": len(rows), "products": [
        {"id": str(p.id), "title": p.title, "price": f"{p.selling_price:.2f}", "sku": p.sku, "description": p.description,
         "image_url": p.image_url, "stock": p.stock, "category": p.category} for p in rows]}
