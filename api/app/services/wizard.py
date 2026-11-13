"""Setup wizard: every step is checked against the real state of the system (nothing is ticked by hand)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Connection, FxRate, MarketPrice, Product
from . import assistant as AI
from . import finance as FIN
from . import launch, mailer
from . import settings_service as S


def steps(db: Session, cfg) -> list[dict]:
    sup = db.query(Connection).filter(Connection.kind == "supplier", Connection.type == "cj_dropshipping", Connection.enabled.is_(True)).all()
    sto = db.query(Connection).filter(Connection.kind == "store", Connection.type != "mock_store", Connection.enabled.is_(True)).all()
    sup_ok = any(c.last_status == "ok" for c in sup)
    sto_ok = any(c.last_status == "ok" for c in sto)
    both = S.get_bool(db, "host_use_cli")
    site_url = S.get(db, "host_vercel_url", "") or S.get(db, "shop_site_url", "")
    host_ok = both and launch.state(db) == "ok"
    rates = db.query(FxRate).filter(FxRate.source != "seed").count() > 1
    S_ = [
        dict(key="login", title="Login aur password", required=True, done=True, href="/users", link="Users aur 2FA",
             what="Tumhara admin username/password pehle se bana hua hai (jo start karte time terminal mein dikha tha).",
             where="Password badalna ho to Settings ya terminal mein: python scripts/local_run.py --reset-password. Internet par chalate ho to 2FA zaroor lagao (Users page)."),
        dict(key="business", title="Business ki details", required=False, done=bool(FIN.business(db)["business_name"]), href="/finance", link="Finance > Business details",
             what="Business ka naam, address, (GSTIN), invoice prefix, payment gateway fee %.",
             where="Ye invoice par chhapta hai aur Finance ke profit mein payment fee ginti hai. Fee % apne payment provider (Stripe/PayPal/Razorpay) ke pricing page par milta hai."),
        dict(key="supplier", title="Supplier jodo (CJ Dropshipping)", required=True, done=sup_ok, href="/keys", link="API Keys",
             what="Tumhari CJ Dropshipping API key (ek key, koi Authorize nahi).",
             where="API Keys page par CJ wala box: key paste karke save karo. Ek baar set karne par hamesha ke liye save rehti hai aur neeche CJ ke options (shipping, markup, margin) khul jate hain."
                   + (" Abhi tumhara supplier jura hai par test pass nahi hua: Connect page par error dekho." if sup and not sup_ok else "")),
        dict(key="hosting", title="Dukaan online karo (Vercel + Render)", required=True, done=host_ok, href="/keys", link="API Keys (CLI login)",
             what="Vercel, Render, GitHub aur Neon (free database) mein browser se ek baar login. Koi API key ya password bharna nahi.",
             where="API Keys page par chaaron 'login (CLI)' dabao, browser mein allow karo. Phir upar Start dabao: project check karta hai ki Render/Vercel par dukaan pehle se hai ya nahi; nahi hai to bana deta hai."
                   + (" Abhi: login shuru hua par launch poora nahi hua (Website jodo page par log dekho)." if both and not host_ok else "")
                   + (f" Dukaan: {site_url}" if site_url else "")),
        dict(key="store", title="Dusra store (optional: Shopify/Woo)", required=False, done=sto_ok, href="/connect", link="Connect",
             what="Apni dukaan (upar wala step) ke alawa Shopify ya WooCommerce par bhi bechna ho to.",
             where="Zaroori nahi. Cheape Deal ki apni dukaan alag se kaam karti hai."
                   + (" Store jura hai par test pass nahi hua." if sto and not sto_ok else "")),
        dict(key="shipping", title="Shipping method", required=True, done=bool(S.get(db, "default_logistic_name", "")), href="/settings", link="Settings",
             what="Supplier ko batana ki maal kaise bhejna hai (jaise CJPacket Ordinary).", where="Sabse aasaan: Settings mein AUTO likho, to har order ke liye CJ ka sabse sasta method khud chuna jata hai. Bina iske live nahi ja sakte."),
        dict(key="products", title="Products", required=True, done=db.query(Product).count() > 0, href="/catalog", link="Import products",
             what="Kam se kam ek product jo tum bechna chahte ho.", where="Import products page par supplier ke catalog se chuno. Pehle CJ jodo, phir uska catalog yahan dikhega."),
        dict(key="fx", title="Exchange rates confirm", required=False, done=rates, href="/strategy", link="Strategy > Rates",
             what="INR, EUR, GBP... ke rates. Bina confirm kiye Strategy autopilot paisa-sambandhit faisle nahi leta.", where="Strategy page par rates ke saamne 'Confirm' dabao (rate Google/XE par dekh kar)."),
        dict(key="market", title="Dusre desh ka price (arbitrage)", required=False, done=db.query(MarketPrice).count() > 0, href="/strategy", link="Strategy",
             what="Jis desh mein product mehenga bikta hai wahan ka price, ya eBay Price watch ki keys.", where="Strategy page par haath se daalo, ya Price watch panel mein free eBay developer keys (developer.ebay.com) daalo."),
        dict(key="notify", title="Alerts aur email", required=False, done=bool(cfg.telegram_bot_token and cfg.telegram_chat_id) or mailer.configured(db), href="/system", link="System",
             what="Telegram bot token + chat id (alerts phone par) ya SMTP (customer/seller ko email).", where="Telegram: @BotFather se bot banao, token lo; chat id @userinfobot se. SMTP: Gmail ke liye App Password banao. .env file mein daalo (docs/DEPLOY.md)."),
        dict(key="ai", title="AI Assistant (optional)", required=False, done=AI.configured(db), href="/assistant", link="AI Assistant",
             what="Base URL + API key + model naam, taaki AI tumhare har sawal ka jawab de. Bina iske built-in madad chalti hai.", where="Kisi bhi AI provider ka API key. Free local model chahiye to Ollama (base URL http://localhost:11434/v1)."),
        dict(key="heartbeat", title="Uptime alert (optional)", required=False, done=bool(S.get(db, "heartbeat_url", "")), href="/system", link="System",
             what="healthchecks.io ya UptimeRobot ka free ping URL.", where="Wahan check banao, ping URL copy karke System page par daalo. Server band hone par wo tumhe batayenge."),
        dict(key="start", title="Start dabao (autonomous)", required=True, done=S.get_bool(db, "engine_started_once") and not S.get_bool(db, "paused"), href="/", link="Upar ka Start button",
             what="Upar strip mein Start dabao. Phir sab kuch apne aap chalta hai.", where="Pehle Rehearsal mode mein dekho ki sab sahi chal raha hai."),
        dict(key="live", title="Live jao (rehearsal band)", required=True, done=not S.get_bool(db, "dry_run"), href="/settings#go-live", link="Settings > Go live",
             what="Rehearsal band karne par asli orders supplier ko jaate hain.", where="Pehle ek chhota test order rehearsal mein chalao, phir approval threshold chhota rakh kar live jao. Guide page ka 'Live jaane ka checklist' dekho."),
    ]
    return S_
