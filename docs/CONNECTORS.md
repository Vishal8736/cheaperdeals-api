# Connecting companies and websites (Connect page)

Everything is on the **Connect** page. Nothing here needs code unless the company only gives you an SDK.

## 1. Supplier: CJ Dropshipping only
CJ Dropshipping is the one real supplier (API key only, no authorize step; token renewed by the app). Set `CJ_API_KEY` in `.env` or paste it on the Connect page.
The Demo supplier is for practice only. AliExpress, Alibaba.com, 1688, Taobao, Printful, generic REST suppliers, email sellers and the Sourcing page were removed.
Store connector files (section 2) still work. Nothing here has been run against a live CJ account yet: see `ASSUMPTIONS.md`.

## 2. Your own website (built in)
The public website (`api/app/storefront/`, on Vercel) is the store. Nothing to connect: products you list on the **Website** page are shown, and paid website orders enter the same pipeline as any store order. See `WEBSITE_DEPLOY.md`.
Other stores (WooCommerce, Shopify) are still added on the Connect page under Store.

## Extra features
- **Backup supplier** (Products > Backup supplier): switches automatically when the main supplier is out of stock or failing.
- **Price ending** (Settings): for example `99` gives 19.99. It never rounds a price down.
- **Outgoing webhook** (Settings): signed `X-CheapeDeal-Signature` (HMAC-SHA256) events such as `order.received` and `order.placed`, sent only after the data is saved.
- **Analytics** page: revenue, cost, profit, margin, top products, and per-supplier totals.
- **CSV export and import** (Products): rows match by SKU; new rows become drafts.


