# What is verified and what is not

Checked against the official docs during the build (CJ: developers.cjdropshipping.com, WooCommerce: woocommerce.github.io/woocommerce-rest-api-docs). Anything under "Not verified" must be confirmed with a real test order before you rely on it.

## Verified

| Area | Fact | Used in |
|---|---|---|
| CJ auth | `POST /api2.0/v1/authentication/getAccessToken` with `{"apiKey": ...}` returns `data.accessToken`, `refreshToken`; tokens last 180 days by default | `adapters/cj.py` `_access_token` |
| CJ auth | Send the token unchanged in the `CJ-Access-Token` header. `refreshAccessToken` takes `{"refreshToken": ...}` | same |
| CJ order | `POST /api2.0/v1/shopping/order/createOrderV2`. Required: `orderNumber`, `shippingCountryCode` (2 letters), `shippingCountry`, `shippingProvince`, `shippingCity`, `shippingCustomerName`, `shippingAddress`, `logisticName`, `fromCountryCode` (2 letters), `products[]` (each `quantity` plus `vid` or `sku`). Response `data.orderId` | `create_order` |
| CJ limits | 10 req/s per IP; free accounts 1 req/s. (Older daily quota is replaced by a points system) | adapter throttles to ~1 call per 1.1 s |
| WooCommerce | REST base `/wp-json/wc/v3`; HTTP Basic with consumer key and secret over HTTPS | `adapters/woocommerce.py` |
| WooCommerce | `PUT /orders/{id}` sets `status`. `POST /orders/{id}/notes` with `customer_note: true` notifies the customer by email | tracking notifications |

## Not verified (VERIFY before going live)

| # | Open point | Why it matters | How to settle it |
|---|---|---|---|
| 1 | CJ `getOrderDetail`: query parameter name, response fields (`orderStatus`, `trackNumber`, `logisticName`), and the real order status values | Tracking sync and status mapping (`normalize_cj_status`) | Place one real order, call the endpoint, compare with the docs page "Query Order", adjust `adapters/cj.py` |
| 2 | CJ stock and variant-price endpoints: `GET /product/stock/queryByVid` and `/product/variant/queryByVid` response shapes | Stock/price sync. The parser fails loudly with an alert if the shape is different | Call both for one `vid` and compare with `_parse_stock` and `_parse_price` |
| 3 | Does `createOrderV2` pay the order? Orders may wait unpaid in CJ until you pay from your balance (`payType` is optional and undocumented here) | A "placed" order may not ship | Check the first test order in the CJ dashboard. Keep balance topped up |
| 4 | Valid `logisticName` values and which `fromCountryCode` fits your products | Required by CJ; set in Settings | Use CJ's freight calculation to find the exact name for your route |
| 5 | CJ `shippingProvince` expects a province name; WooCommerce sends a state **code** (e.g. `MH`) | A code may be rejected or mis-routed | Test with a real address; add a state-code to name map in `adapters/woocommerce.py` if needed. Only ~25 country names are mapped (`COUNTRY_NAMES`) |
| 6 | CJ has no confirmed "find order by orderNumber" endpoint, so `supports_lookup = False`. After an ambiguous timeout the order stops for a human | Prevents duplicate parcels; costs one manual check | Implement `find_order` if you find an endpoint |
| 7 | CJ error code for an invalid token (code `1600001` and HTTP 401 are treated as "re-login") | Token expiry handling | Watch the first token refresh |
| 8 | CJ `isSandbox` (optional field in createOrderV2) is not used | Could allow test orders without cost | Read its docs, then wire it to DRY-RUN |
| 9 | WooCommerce webhook signature: header `X-WC-Webhook-Signature` = base64(HMAC-SHA256(raw body, webhook secret)) | Rejecting forged orders | From WooCommerce docs knowledge, not fetched. Confirm with the first delivery (WooCommerce > Status > Logs shows failures) |
| 10 | WooCommerce `PUT /products/{id}` fields `status` (publish/draft) and `regular_price`; `GET /orders?after=&status=processing` | Auto-hide, reprice, missed-order sweep | Same: confirm on a test product. Variable products use variation IDs |
| 11 | Hidden products use `draft` status, which also unpublishes the product page (SEO and links break while hidden) | Customer-facing | Acceptable for dropshipping; change to `catalog_visibility` if you prefer |

## Behaviour you should know

- Only orders with store status `processing` or `completed` are ingested (paid). Use webhook topic **Order updated**.
- Dry-run orders are rehearsals: they are marked, never tracked, and can be placed for real later with "Place for real".
- Customer email is sent by WooCommerce itself (customer notes), so no SMTP setup is needed.
- n8n workflows were generated and structurally validated (valid JSON, consistent connections) but were **not executed in a live n8n** in the build environment. Import them and run each once manually.
- The Docker images and the full compose stack were **not run** in the build environment (no Docker daemon). The compose file passes `docker compose config`, the code ran against real PostgreSQL 16, and the API ran under uvicorn with the same entrypoint as the Dockerfile.


## Platform connectors (AliExpress, Alibaba.com, 1688, Taobao, Printful, Shopify): BETA, never run against a live account

These presets were built from the public API documentation and tested only with mocked HTTP; the request signatures are recomputed independently in `tests/test_platforms.py`. Treat every row as VERIFY until one real test call succeeds (Connect page > Test).

| # | Platform | Open point | How to settle it |
|---|---|---|---|
| P1 | AliExpress DS | Method names (`aliexpress.ds.*`), `access_token` flow, and response paths are from memory of the public docs. Access needs an approved AliExpress Open Platform app | Create the connection, press Test, compare the error with the docs, and edit the spec JSON on the Connect form (no code change needed) |
| P2 | Alibaba.com | The preset contains placeholder API names that start with `REPLACE.` because they differ per approved app | Replace them with the names shown in your Alibaba.com app console |
| P3 | 1688 | Signature is `_aop_signature` = HMAC-SHA1 over the URL path plus sorted pairs. Overseas order APIs vary by agreement | Test with a product query first |
| P4 | Taobao | Catalog only. There is no overseas ordering API, so orders must be placed through an agent | Use it for importing products |
| P5 | Printful | Product and order field names; fulfilment starts only after you confirm the order | Test with a draft order |
| P6 | Shopify | Products, price, visibility and orders are covered. Stock levels are **not** pushed | Add inventory levels if you need them |
| P7 | Failover | A backup supplier is used only when the main one reports no stock or errors. The backup `vid` format for presets is `PRODUCT|VARIANT` | Test with the mock supplier first |

## Strategy agent (v3)
| # | Point | Note |
|---|---|---|
| S1 | Seed exchange rates, shipping, duty and VAT per country are rough guesses | Edit them. The agent refuses to change live prices from an unconfirmed (seed) rate |
| S2 | The free rates feed `https://open.er-api.com/v6/latest/USD` was not reachable from the build environment | Refresh is best-effort; on failure the saved rates stay |
| S3 | The market price is trusted to be the same or a comparable product | You (or your scraper) must make sure of it |
| S4 | A supplier price in another currency is converted only when *Supplier currency* is set on the product | Backup-supplier prices are assumed to be in the same currency as the main supplier |

## Sourcing and extras (v3.2)
| # | Point | Note |
|---|---|---|
| X1 | No IndiaMART/Alibaba scraping or API calls: offers come from you, CSV, a pasted link or your own scraper | Their terms and anti-bot rules are not ours to break |
| X2 | Link reader depends on schema.org / Open Graph markup; tested with sample pages only | Falls back to manual entry |
| X3 | Email purchase orders were tested with a fake sender and the SMTP error paths with simulated errors, never against a real mail server | Press Test on the seller connection, then send yourself a test order in rehearsal off with a seller email that is yours |
| X4 | systemd unit and Windows autostart task were not executed on real machines | Follow the steps and check the logs |
| X5 | Backups: SQLite only (consistent online copy). PostgreSQL needs pg_dump | |
| X6 | The free exchange-rate feed could not be reached from the build environment | Manual rates always work |

## v3.3 additions: what is and is not verified
- eBay price feed: tested only against a mocked eBay response. It returns ASKING prices (median of 5+ listings), not sold prices. Check with real keys before turning Autopilot on.
- Multi-country listing, risk check, returns/finance, customer emails, heartbeat, parallel stock sync: covered by automated tests with mock stores/suppliers. Real stores are verified by you.
- 2FA (TOTP) protects the admin account only. Extra users (operator/viewer) log in with a password.
- Not built: WhatsApp API sending (only a wa.me chat link), a Hindi UI toggle, AI-written descriptions, tax/GST filing.

## v4: Start/Stop, live view, Playbook, AI assistant, Hinglish UI
- **Start/Stop**: one button in the top bar. A fresh real install begins stopped (`paused=true`). Start switches on the engine and the autopilot settings (strategy, auto-list, multi-market, sourcing auto-import, Playbook). It does NOT turn off Rehearsal (dry-run): going live is a separate, deliberate step.
- **Live view**: pages poll themselves every 5 s (skipped while the tab is hidden, while you type, or when a form is dirty). Settings/connect/guide/assistant pages are not auto-refreshed.
- **Playbook** (`/playbook`): tiered markup, demand pricing, clearance, dated sale calendar, cheapest-supplier switch, auto-retire. All keep the margin floor and skip products with a manual price. Verified by tests against the mock store/supplier only; real store price pushes are as reliable as your store connector.
- **AI assistant** (`/assistant`): any OpenAI-compatible endpoint (Base URL + key + model) or Anthropic style. The key is stored encrypted with SECRET_KEY. It is read-only (cannot change anything) and never receives customer names, emails or addresses. Without a key it answers from a built-in knowledge base. NOT tested against a live provider.
- **Hinglish UI**: words are translated when each page is sent (`app/i18n.py`). Default Hinglish; the sidebar buttons switch to English. Anything not in the dictionary (dynamic sentences, your own data) stays English. Add words to `HI` in `i18n.py` to extend.
- Still unverified live: real AliExpress/CJ/eBay accounts, real store webhooks, and a real AI provider.

## v4.1: AI models (Base URL + API key), Fetch models, Auto mode
- AI Models + Assistant page (also linked from Connect): Base URL, API key, API style, Model(s). **Models fetch karo** calls `GET {base}/models` (OpenAI-compatible) or `GET {base}/v1/models` (Anthropic) and lists everything the key can use; tick the ones you want (embedding/image/audio models are greyed out).
- Several models can be chosen. The first is the main one; if it fails the next is used and the failed one rests for 10 minutes.
- **Auto mode** (turned on by Start): chosen models are used in rotation for background work, and the **AI Advisor** agent reviews the whole system every hour and writes short Hinglish advice (feed + "AI ki taza salah"). It skips the paid call when nothing changed. It is read-only: it never changes settings, prices or orders.
- Tested with a local fake provider (list, chat, rotation, fallback). NOT tested against real OpenAI/OpenRouter/Anthropic/Ollama servers: some providers return model lists in other shapes, and then "Models fetch karo" shows an error; you can still type the model name by hand.

## v4.2: AI replies were silently falling back to the built-in answer (fixed)
Causes found and fixed: newer models reject `temperature` / `max_tokens` (now retried automatically with `max_completion_tokens` and without temperature, and remembered); some providers (Cloudflare-fronted) block Python's default User-Agent (now a CheapeDeal agent); chat history could start with an assistant turn or repeat a role, which Anthropic/OpenAI refuse (now cleaned); Base URL without `/v1` gives 404 (now retried with `/v1`); reply `content` as a list of parts; a 'thinking' model used its small token budget on reasoning (budget raised). When the AI still fails, the chat now shows a red box with the real reason (401 key, 404 URL, 429 limit...) and tags every answer "AI jawab (model)" or "Built-in jawab (AI se nahi)". An unreadable saved key (SECRET_KEY changed) and a half-filled setup are reported too. Verified against a local provider that mimics these refusals, not against live OpenAI/Anthropic servers.

## v5: Demo duniya (simulator) and what is real vs invented [REMOVED in v12]
- **Real (same code path as live)**: order intake, idempotency, risk check, margin guard, approvals, retry/backoff, supplier placement, ambiguous-timeout recovery, tracking sync, customer notices, stock/price sync, finance, returns, strategies, Playbook, AI assistant.
- **Invented (only in Demo duniya)**: customers, the supplier (in-memory demo supplier), the store, supplier price/stock moves, returns, market prices. It only runs when the built-in demo supplier and store are the backends, no real supplier/store connection exists and no real orders exist. Connecting a real supplier/store turns it off, restores the daily order cap and puts Rehearsal back ON. It only creates/deletes records it made (SIM- orders, sim- products). Demo customers use example.com (never emailed) and demo-order alerts are not sent to Telegram.
- Found and fixed while building it: the demo supplier forgot its orders/products on restart (now rebuilt from the database); live mode silently held every order until a shipping-method name was set (demo seeds "MockExpress"); the per-day safety cap blocks compressed demo days (lifted during demo, restored after).
- **Still unverified with real accounts**: AliExpress, CJ, Shopify, WooCommerce, IndiaMART flows, eBay price feed, email/SMTP, Telegram, real AI providers. These need your real keys; use the Rehearsal mode and one small test order first.


## v6: AliExpress is the only supplier
- CJ, Alibaba.com, 1688, Taobao, Printful, generic REST suppliers, email sellers and the Sourcing (IndiaMART) page were removed. Old rows of those types stay in the database but are inert.
- Customer-email SMTP now comes only from the SMTP_* environment variables.
- All AliExpress calls (OAuth, feed/search, product, freight, order create, order status) are unverified against a real account. Method names follow the public DS docs; use the Connect checklist to test each step.
- New AliExpress features (all unverified live): keyword search (`aliexpress.ds.text.search`), freight lookup (`aliexpress.ds.freight.query`), shipping method `AUTO` (cheapest offered method), one retry on rate limits, Hinglish error hints, and a step-by-step checklist on the connection page (`/connect/<id>/edit?check=1`). If a method name or field is wrong, edit the JSON spec on the connection form.

## v6.1: Email page
- Mailbox = SMTP (host, port 587 STARTTLS or 465 SSL, user, password sealed with SECRET_KEY). Not verified against a real mailbox here: tests use a fake sender.
- Gmail needs an App Password (2-step verification on). Some hosts block outgoing SMTP ports.
- The AI only drafts unless "AI bina review ke bheje" is on; the chat assistant itself stays read-only (it cannot send mail), because text from customers could otherwise steer it. Daily cap applies to manual and AI mails.

## v6.2: AliExpress fixes from the ae_sdk source
- Business API calls now send the token as `session` (before: `access_token`, which AliExpress rejects as an invalid session).
- Order create answer is read from `aliexpress_trade_buy_placeorder_response`; `is_success=false` with a code (address, shipping method, stock, blacklist...) is now a clear final error with a Hinglish hint, not an "unknown, hold it" error.
- Shipping lookup now uses `aliexpress.logistics.buyer.freight.calculate` (the older freight methods are marked removed in ae_sdk). Product feed, order status and tracking paths were corrected the same way. Connections saved by older versions are repaired on load.
- Still unverified: keyword search (`aliexpress.ds.text.search` is not in ae_sdk). If it fails, the checklist shows Fail and the feed import still works.
- DSers: see DSERS.md. Not integrated.

## v6.3: AliExpress easy page
- /connect/aliexpress/easy: keys + one button, then the code is picked up from the clipboard (browser asks once) or by paste. The AliExpress login/consent itself cannot be automated by any program: it needs you, about once a month at most.
- Keep-alive (hourly, inside "maintenance"): renews the token before it ends and raises an alert (and Telegram) when you must press "Dobara jodo". Clipboard auto-read works in Chrome/Edge on localhost or https; other browsers fall back to the paste box.

## v6.4: CJ Dropshipping back as a second supplier (API key only)
- Reason: AliExpress needs a human login/consent; CJ needs only an API key (token 180 days, renewed from the key by the app, no human).
- Built from the public CJ API 2.0 docs (auth, product listV2/query, freightCalculate, createOrderV2, getOrderDetail). NOT run against a live CJ account. Unverified details: the exact date format of token expiry (falls back to defaults), stock field per warehouse, and CJ error wording (hints match on words like "balance").
- Orders use payType=2: paid from your CJ wallet balance, so keep balance in CJ. Shipping method AUTO = cheapest from freightCalculate.
- Earlier "AliExpress only" request is not undone: AliExpress stays. Switch one off in Connect and set the other as default.

## v7: AliExpress removed for good; CJ is the supplier
- All AliExpress code, pages, routes and tests are gone. A leftover AliExpress connection row in an old database stays but cannot be built ("type not installed"), so its orders are held, never sent to the demo supplier. Delete the row on the Connect page.
- CJ is auto-connected at start-up from CJ_API_KEY (see services/bootstrap.py).
- The CJ MCP token is for AI clients (CJ's MCP server), not for this app's REST calls, so it is not used.

## v8: website (Vercel) + backend (Render)
- Live-unverified: CJ with the supplied key (sandbox cannot reach CJ), Render, Vercel, Neon, Razorpay (order API, checkout popup, webhook). Code paths are tested with mocks only.
- The MCP token the owner supplied is NOT used: it is for CJ's own MCP server, the app needs only the API key. The key was pasted into chat: regenerate it in CJ after the first successful test.
- Prices are computed on the server; an order enters the engine only when paid (Razorpay signature / webhook amount check, or manual "Mark paid").
- Keep-alive needs an outside pinger (GitHub Actions or UptimeRobot): a sleeping server cannot wake itself.
- The design's fake discounts, countdown timers and "verified" claims were left out on purpose (no data to back them).

## v9: payments, currency, refunds
- (REMOVED in v12) Payment methods: UPI (manual confirm), crypto to own wallet (BTC/LTC/USDT-TRC20, auto-detected), BTCPay Greenfield API (checked against the BTCPay source: POST /api/v1/stores/{id}/invoices, header `Authorization: token`, webhook header `BTCPay-Sig: sha256=`, event InvoiceSettled), Razorpay, manual. Explorer and CoinGecko response shapes come from their public docs; not run live here (sandbox has no access).
- (REMOVED in v12) Crypto: matches exact amount incl. a unique tag; confirmed transactions only (setting `crypto_confirmed`). Fee-deducted exchange withdrawals do not match (use Mark paid). Price is locked when the order is made; payments are accepted until the order closes at 48 h.
- (REMOVED in v12) BTCPay itself is not bundled: it needs its own server (.NET + database). The connector talks to one you run.
- CJ balance endpoint (`/shopping/pay/getBalance`) is from CJ's public API list and read tolerantly; not run live.
- Not done yet: product variants, per-product pages/SEO, coupons, customer accounts, real CJ freight at checkout, shipped-email for website orders.

- Support: tickets and reviews are plain text; a ticket is readable only through its secret token. Reply emails go only if a mailbox is connected and customer emails are on. No attachments/photos in tickets yet.

## v10: Website jodo (Vercel + Render API)
- Vercel (`/v2/user`, `/v9/projects`, `/v13/deployments` inline files, `/v13/deployments/{id}`) aur Render (`/v1/owners`, `/v1/services`, per-key `PUT env-vars`, `POST deploys`) ke request shapes docs se bane hain; sandbox se live run NAHI hua. Tests nakli transport se.
- Tokens SECRET_KEY se sealed; Render par bheje env values (ADMIN_PASSWORD, DATABASE_URL, CJ_API_KEY) local save nahi hote.
- v11: Start = hosting check + auto-host. GitHub REST (user/repos, git/trees inline content, git/commits, refs PATCH) bhi live test nahi hui. Render create body (rootDir, envSpecificDetails) docs se; first real run confirm karega.
- v12: crypto/BTCPay payments aur Demo duniya (simulator) poori tarah hata diye. CJ key API Keys page se save karne par `cj_key_source=ui` banta hai: env (CJ_API_KEY) use kabhi overwrite nahi karti. Ping: `services/keepalive.ping` + API Keys page ka test button; soye server ko andar se nahi jagaya ja sakta, bahar ka pinger zaroori.
- v13: CLI mode (services/hostcli.py): `vercel whoami/link --yes --project/deploy --prod --yes`, `render whoami/services -o json/services create/deploys create` docs se; Render rootDir flag verify nahi hua isliye `cd api &&` use. Login output (device link) parse karta hai, live test nahi.
- v13: Flipkart Affiliate search (affiliate-api.flipkart.net/affiliate/search/json, headers Fk-Affiliate-Id/Token) docs se; docs ke mutabik deprecate hogi. Amazon/Flipkart seller APIs aur PA-API built nahi (seller account / sales chahiye). Platforms page par saaf likha hai.
- v14: API-key mode hata diya; sirf CLI. GitHub: `gh auth login --web -h github.com -p https -s repo` (Enter stdin se) aur `gh auth token` se REST upload; Neon: `neon login`, `neon projects list/create --output json`, `neon connection-string --project-id` (docs se, flags verify nahi). Admin password auto-generate hota hai (sealed), page par dikhane ka button hai.
- v14.1: neonctl@latest (9.x) hang `neon` -> `@neon/config@1.14.0` par npm ETARGET deta tha; npx ab neonctl@7.0.8 (phir 2.29.3, phir latest) try karta hai. 7.0.8 ka sirf `--version`/`--help` sandbox mein chala, login/projects asli Neon se nahi chale. CLI buttons ab scroll position yaad rakhte hain.
- v14.2: Render CLI 'no workspace set' (Start par aaya): app ab `render workspaces -o json` se ek workspace ho to ~/.render/cli.yaml mein workspace.id/name likh deti hai (docs ke hisaab se; `render workspace set` interactive hai, ID argument documented nahi). Config path/format sandbox mein asli Render se verify nahi; fail ho to terminal mein `render workspace set` ek baar.
- v15: Logging: DB (diag_events, 14 din/20k lines) + file logs/cheaperdeals.log (2MB x3). Secrets regex + stored values se hatate hain. /diagnostics page (grouped problems + Hinglish hints + health + report download). Hints sirf jaani hui problems ke liye hain (HINTS list in services/diag.py); naye errors ke liye hint nahi aata, tab Report bhejo.
- v15.1: v14.2 ne Render cli.yaml mein workspace ko nested map likha tha -> Render CLI har command par 'yaml: unmarshal errors ... !!map into string' deta tha (login bhi fail). Asli format (render-oss/cli pkg/config/config.go se padha): flat `workspace: <id>` + `workspace_name: <name>`, aur env RENDER_WORKSPACE file ko override karta hai. App ab env var set karti hai, flat likhti hai, aur purani kharab file status/login/Start se pehle khud repair karti hai.
- v16: deep audit. Bugs fixed: connector Edit page duplicate route (blank form), ui.py CONFIRMED NameError, SECURITY: Render par login limiter ko asli client IP chahiye (--proxy-headers), non-ASCII CSRF/internal key 500, AI URL sirf http(s). 3 orphan modules + dead adapters registry hata diye. Naya: Start se pehle jaanch, version check + 'Render par naya code bhejo' (GitHub push, Render auto-deploy ya `render deploys create`; ye dono asli Render/GitHub par live test nahi hue), postgres:// aur Neon -pooler URL normalise, login watchdog.
- v16.1: Start crash fix: `render services -o json` khali account par `null` chhapta hai, code use dict maan kar `AttributeError: 'NoneType' object has no attribute 'get'` deta tha; ab null/[]/{}/'No services' sab khali list (`hostcli.json_items`, Vercel/Neon par bhi). Naya: Start login na hone par browser login khud chalata aur wait karta hai; detection (`launch.detect`) Render/GitHub/Neon/Vercel par kya hai dekhta hai, plan log karta hai, `host_detect` mein save, Website jodo par panel. Render service naam alag ho to GitHub repo se bhi match. Ye sab fake CLI se test hua; asli Render/GitHub/Neon/Vercel par live nahi chala.
- v16.2: Neon "CLI ka jawab padh nahi paya" fix: neonctl (npx) JSON ke aage/peeche banner, warning ya update notice chhapta hai to pura output JSON nahi rehta tha. Ab output mein se JSON nikala jata hai (stdout+stderr), `{"projects":[..],"shared":[..]}` bhi chalta hai, aur agar CLI text table de to wo bhi padha jata hai. Parse na ho paye to error mein CLI ka asli output dikhta hai. connection-string/create output bhi isi tarah. Fake CLI se test hua, asli Neon par live nahi.
- v16.3: Neon "What organization would you like to use?" fix: neonctl poochta hai kaun si organization (arrow-key menu), jiska jawab Start nahi de sakta tha. Ab prompt dikhte hi org id (prompt text se ya `neon orgs list --output json` se) leke `--org-id` ke saath command dobara chalta hai; id yaad rakhta hai (projects list/create). Org na mile to saaf message: `neonctl set-context --org-id <id>`. Ye fake CLI se test hua; `--org-id` flag asli neonctl 7.0.8 par live nahi chala.
- v16.4: GitHub "files upload ne mana kiya (404) Not Found" fix: `gh auth login -s repo` ka token `workflow` scope nahi rakhta, aur code mein .github/workflows/keepalive.yml hai: GitHub us case mein poora tree 404 se mana karta hai. Ab login scopes `repo,workflow`; purane token par pehle poora upload try hota hai, 403/404/422 par workflow file hata kar baaki sab upload hota hai aur log mein warning aati hai (keep-alive file optional hai, server khud ko ping karta hai; chahiye to `gh auth refresh -h github.com -s workflow`). Review: ruff (unused imports hataye), naya tests/test_smoke_all_pages.py har GET page (login ke saath/bina, missing id ke saath) par 500 pakadta hai. Ye sab nakli GitHub/CLI se test hua, asli GitHub par live nahi.
