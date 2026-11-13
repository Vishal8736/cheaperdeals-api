"""Which marketplaces/suppliers can be connected for free, what each one really does, and the Flipkart price check."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..security import require_login, verify_csrf
from ..services import research
from ..services import settings_service as S
from ..services import shopcfg as C
from .deps import get_db, get_rt
from .ui import back, flash, page

router = APIRouter(dependencies=[Depends(require_login)])

# (name, what it is, free key?, status, note). status: ready | research | later | no
CATALOG = [
    ("Supplier: maal tumhe bhejne wale", [
        ("CJ Dropshipping", "Products + order fulfilment", "Haan, free API key", "ready", "Built-in. API Keys page par key daalo."),
        ("Printify / Printful", "Print-on-demand (custom print waale products)", "Haan, free token (Printify: My Profile > Connections)", "later", "Abhi built nahi: alag flow hai (design + print provider). Connector file se add ho sakta hai."),
        ("AliExpress", "Marketplace", "Approval wali API", "no", "Tumhare kehne par hata diya gaya."),
        ("Amazon / Flipkart ko supplier banana", "-", "-", "no", "Inke paas dropshipper ke liye order-fulfilment API hi nahi hai. Ye sirf bechne waalon (sellers) ya affiliates ke liye hain."),
    ]),
    ("Dukaan: jahan bechna hai", [
        ("Tumhari apni dukaan (Vercel + Render)", "Cheape Deal ki website", "Free (API key bhi nahi, CLI se)", "ready", "Website jodo page."),
        ("Shopify", "Store", "Haan, free custom-app token (store chahiye)", "ready", "Connect page par jodo."),
        ("WooCommerce", "Store", "Haan, free REST keys (WordPress site chahiye)", "ready", "Connect page par jodo."),
        ("Apni koi bhi site", "Store", "Tumhari apni API", "ready", "Connector file: docs/CONNECTORS.md."),
        ("Amazon Seller (SP-API)", "Marketplace seller", "Developer registration + approved seller account chahiye", "later", "Bina seller account ke nahi chalta. Account ho jaye tab connector file se add kar sakte hain (abhi built nahi)."),
        ("Flipkart Seller (Marketplace API)", "Marketplace seller", "Approved seller account chahiye", "later", "Bina seller account ke nahi chalta (abhi built nahi)."),
        ("Etsy / eBay", "Marketplace seller", "Free developer key, par approval + OAuth login", "later", "Abhi built nahi."),
    ]),
    ("Research: sirf price dekhna", [
        ("Flipkart Affiliate API", "Flipkart par price / stock padhna (order nahi)", "Affiliate banke free token (affiliate.flipkart.com/api/api-token)", "research", "Neeche price check chalu hai."),
        ("Amazon PA-API", "Amazon price padhna", "Affiliate account par qualifying sales chahiye (reports ke mutabik)", "later", "Abhi built nahi."),
    ]),
]


@router.get("/platforms", response_class=HTMLResponse)
def platforms_page(request: Request, q: str = "", db: Session = Depends(get_db), rt=Depends(get_rt)):
    res, err = None, ""
    if q.strip():
        try:
            res = research.compare(db, rt, q)
        except research.ResearchError as e:
            err = str(e)
        except Exception as e:
            err = f"Flipkart tak pahunch nahi paya: {str(e)[:150]}"
    return page(request, db, "platforms.html", catalog=CATALOG, fk_on=research.flipkart_ready(db), fk_id=S.get(db, "flipkart_affid", ""), q=q, res=res, err=err)


@router.post("/platforms/flipkart", dependencies=[Depends(verify_csrf)])
async def save_flipkart(request: Request, db: Session = Depends(get_db)):
    f = {k: str(v).strip() for k, v in (await request.form()).items()}
    if f.get("affid"):
        S.set_value(db, "flipkart_affid", f["affid"][:80])
    if f.get("token"):
        C.save_secret(db, "flipkart_token_enc", f["token"])
    db.commit()
    flash(request, "ok" if research.flipkart_ready(db) else "error", "Flipkart ID + token save ho gaye (token encrypted)." if research.flipkart_ready(db) else "Affiliate ID aur token dono chahiye.")
    return back("/platforms")
