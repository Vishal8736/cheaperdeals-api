"""Hinglish interface. The pages are written in English; this turns the words you SEE (headings, buttons, table heads, statuses,
placeholders) into Hinglish when the page is sent. Switch language at the bottom of the left menu. Technical names (API, SKU,
URL, CSV, USD...) and anything you typed yourself stay as they are."""
from __future__ import annotations

import re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

HI = {
    # navigation and general
    "Dashboard": "Dashboard", "Agents": "Agents (robots)", "Orders": "Orders", "Analytics": "Stats aur analysis", "Stats": "Stats", "Products": "Products", "Import products": "Products import karo",
    "Import": "Import", "Connect": "Jodo (Connect)", "Alerts": "Alerts", "System": "System", "Users and 2FA": "Users aur 2FA", "Users": "Users", "Settings": "Settings", "Guide (Hinglish)": "Guide (Hinglish)",
    "Guide": "Guide", "Setup wizard": "Setup wizard", "Setup": "Setup", "AI Assistant": "AI Assistant", "AI": "AI", "Playbook (strategies)": "Playbook (strategies)", "Playbook": "Playbook",
    "Strategy": "Strategy", "Sourcing": "Sourcing (sasta maal)", "Approvals": "Approvals (haan/na)", "Approve": "Approve", "Risk check": "Risk check", "Risk": "Risk", "Finance": "Finance (paisa)", "Money": "Paisa",
    "Returns": "Returns", "Home": "Home", "Operate": "Chalana", "Catalog": "Catalog", "Sign out": "Sign out", "Search...": "Khojo...", "Search everything": "Sab kuch khojo", "Search": "Khojo",
    "Cheape Deal": "Cheape Deal", "AI se poochho": "AI se poochho",
    # buttons
    "Save": "Save karo", "Save and test": "Save karo aur test karo", "Save settings": "Settings save karo", "Save rules": "Rules save karo", "Save product": "Product save karo", "Save price": "Price save karo",
    "Save rate": "Rate save karo", "Save note": "Note save karo", "Save offer": "Offer save karo", "Save token": "Token save karo", "Save and tell the customer": "Save karo aur customer ko batao",
    "Cancel": "Cancel", "Delete": "Delete karo", "Edit": "Badlo", "Add": "Jodo", "Add user": "User jodo", "Add product": "Product jodo", "Remove": "Hatao", "Change": "Badlo", "Clear": "Saaf karo",
    "Apply": "Lagao", "Approve and send": "Approve karke bhejo", "Reject": "Reject karo", "Retry now": "Abhi dobara try karo", "Run now": "Abhi chalao", "Run every agent now": "Saare agents abhi chalao",
    "Run one agent by hand": "Ek agent haath se chalao", "Analyse now": "Abhi analyse karo", "Scan offers now": "Offers abhi scan karo", "Sync stock and prices now": "Stock aur price abhi sync karo",
    "Back up now": "Backup abhi lo", "Download CSV": "CSV download karo", "Export CSV": "CSV export karo", "Import CSV": "CSV import karo", "Upload": "Upload karo", "Test": "Test", "Test connection": "Connection test karo",
    "Send test": "Test bhejo", "Send": "Bhejo", "Bhejo": "Bhejo", "Filter": "Filter", "Open": "Kholo", "Open full view": "Poora kholo", "See all": "Sab dekho", "See list": "List dekho", "Print": "Print karo",
    "Previous": "Pichhla", "Next": "Agla", "Previous page": "Pichhla page", "Next page": "Agla page", "Ignore": "Ignore karo", "Done": "Ho gaya", "Mark all as done": "Sab ho gaya mark karo", "Pick": "Chuno",
    "Choose...": "Chuno...", "Tick all": "Sab tick karo", "Browse": "Dekho", "Make product": "Product banao", "List ticked": "Tick kiye hue list karo", "Import ticked products": "Tick kiye products import karo",
    "Create test order": "Test order banao", "Add demo products": "Demo products jodo", "Place for real": "Asli order do", "Keep my price": "Mera price rakho", "Put on sale": "Sale par lagao", "Take off sale": "Sale hatao",
    "Revert to normal markup price": "Normal markup price par wapas", "Set price = cost + markup": "Price = cost + markup lagao", "Move supplier forward": "Supplier aage badhao", "Cancel order": "Order cancel karo",
    "Reload connector files": "Connector files dobara load karo", "Refresh from the internet": "Internet se naya lao", "Sign in": "Sign in karo", "Sign in to your console": "Apne console mein sign in karo",
    "Set up 2FA": "2FA set up karo", "Turn 2FA on": "2FA ON karo", "Turn 2FA off": "2FA OFF karo", "Apply Online": "Apply Online", "Go live": "Live jao",
    "Pause everything": "Sab kuch roko", "Pause (kill switch)": "Roko (kill switch)", "Resume automation": "Automation wapas chalu karo", "Back to rehearsal": "Rehearsal par wapas", "Back to dashboard": "Dashboard par wapas",
    "Back to orders": "Orders par wapas", "Download supplier template": "Supplier template download karo", "Download website/store template": "Website/store template download karo", "Save and tell the customer ": "Save karo aur customer ko batao",
    "Open return": "Return kholo", "WhatsApp customer": "Customer ko WhatsApp", "Invoice": "Invoice", "Change ": "Badlo", "Upload a connector file (.py)": "Connector file (.py) upload karo",
    "Add a supplier": "Supplier jodo", "Add a store or website": "Store ya website jodo", "Add a cheap offer": "Sasta offer jodo", "Add a market price": "Market price jodo", "Add a product": "Product jodo",
    "Connect a supplier": "Supplier jodo", "Connect your website": "Apni website jodo", "Read the page": "Page padho", "Read a product link": "Product link padho", "Import some": "Kuch import karo",
    "Import from a supplier": "Supplier se import karo", "Import from supplier": "Supplier se import karo", "Many offers at once (CSV)": "Kai offers ek saath (CSV)", "List ticked products on:": "Tick kiye products yahan list karo:",
    "Ticked products:": "Tick kiye hue products:", "also apply good prices now": "achhe price abhi bhi lagao",
    # headings / table heads
    "Alerts and history": "Alerts aur history", "Agents, live": "Agents, live", "All orders": "Saare orders", "Audit log": "Audit log (kisne kya kiya)", "Automatic settings": "Automatic settings",
    "Autopilot and rules": "Autopilot aur rules", "Backups": "Backups", "Business rules": "Business ke rules", "Connections": "Connections", "Your connections": "Tumhari connections",
    "Connector files (for SDKs and special APIs)": "Connector files (SDK aur khaas API ke liye)", "Controls": "Controls", "Countries (markets)": "Desh (markets)", "Every country, best first": "Har desh, sabse achha pehle",
    "Exchange rates (per 1 USD)": "Exchange rates (1 USD ke liye)", "Fulfilment health": "Order poore hone ki sehat", "Health checks": "Health checks", "Latest orders": "Naye orders", "Market prices you entered": "Tumhare daale market prices",
    "Orders today": "Aaj ke orders", "Pending approvals": "Approval ke intezaar mein", "Price history": "Price ka itihaas", "Recent decisions": "Haal ke faisle", "Revenue and profit per day": "Roz ki kamai aur profit",
    "Setup checklist": "Setup checklist", "Supplier scorecard": "Supplier scorecard", "Supplier and tracking": "Supplier aur tracking", "Team": "Team", "Telegram alerts": "Telegram alerts", "Top products by profit": "Sabse zyada profit wale products",
    "Where orders are right now": "Orders abhi kahan hain", "Where it sells for more": "Kahan mehenga bikta hai", "Live checklist": "Live checklist", "Rules": "Rules", "Rehearsal": "Rehearsal", "Chat": "Chat",
    "AI model jodo": "AI model jodo", "Business details (invoice aur fees ke liye)": "Business details (invoice aur fees ke liye)", "Scheduled jobs (from n8n)": "Scheduled jobs (n8n se)",
    "Order risk check": "Order risk check", "Read-only API": "Sirf-padhne wali API", "Product feed for your website": "Tumhari website ke liye product feed", "Customer tracking page": "Customer tracking page",
    "Price watch (eBay)": "Price watch (eBay)", "Price watch": "Price watch", "Competitor price watch (eBay)": "Competitor price watch (eBay)", "Price arbitrage": "Price arbitrage (mehenga desh)",
    "Multi-country": "Multi-country", "Multi-country selling": "Multi-country selling", "Backup supplier failover": "Backup supplier failover", "Margin guard": "Margin guard (loss se bachao)",
    "Psychological price ending": "Price ending (.99)", "Fraud / risk check": "Fraud / risk check", "Admin 2FA": "Admin 2FA",
    "Action": "Kaam", "Actions": "Kaam", "Actor": "Kisne", "Address": "Pata (address)", "Amount": "Amount", "By": "Kisne", "Carrier": "Courier", "Category": "Category", "Checked": "Check hua", "Code": "Code",
    "Cost": "Cost", "Country": "Desh", "Currency": "Currency", "Customer": "Customer", "Decision": "Faisla", "Detail": "Detail", "Duty %": "Duty %", "Fee %": "Fee %", "Fees": "Fees", "Item": "Item", "Items": "Items",
    "Landed cost": "Landed cost (pahunchne tak ka kharcha)", "Last sync": "Aakhri sync", "Listed on": "Kahan listed hai", "Margin": "Margin", "Market price": "Market price", "Month": "Mahina", "Name": "Naam",
    "Net": "Net", "Net profit": "Net profit", "Offer": "Offer", "Offers": "Offers", "Order": "Order", "Order ID": "Order ID", "Our price": "Hamara price", "Our price / profit": "Hamara price / profit", "Price": "Price",
    "Product": "Product", "Product name": "Product ka naam", "Profit": "Profit", "Profit / unit": "Profit / unit", "Profit margin": "Profit margin", "Qty": "Qty", "Quantity": "Matra (quantity)", "Reason": "Wajah",
    "Refund": "Refund", "Refunds": "Refunds", "Revenue": "Revenue (kamai)", "Role": "Role", "Shelf price": "Dukaan wala price", "Shipping": "Shipping", "Shipping $": "Shipping $", "Source": "Source",
    "State": "Halat", "Status": "Status", "Stock": "Stock", "Store": "Store", "Supplier": "Supplier", "Supplier cost": "Supplier cost", "Tracking": "Tracking", "Tracking number": "Tracking number", "Units": "Units",
    "VAT %": "VAT %", "Verdict": "Nateeja", "Viewer": "Viewer", "What happened": "Kya hua", "When": "Kab", "When (UTC)": "Kab (UTC)", "Total": "Total", "Weight (kg)": "Wazan (kg)", "Undercut": "Undercut",
    "Selling price": "Selling price", "Seller email": "Seller ka email", "Seller name": "Seller ka naam", "Seller has (units)": "Seller ke paas (units)", "Product cost": "Product cost", "Paid each": "Har ek ka daam",
    "Our order number": "Hamara order number", "Our cost to deliver": "Pahunchane mein hamara kharcha", "Minimum order (units)": "Kam se kam order (units)", "Minimum order x price. Offers above it wait for you.": "Minimum order x price. Isse upar ke offers tumhari haan ka intezaar karte hain.",
    "Max capital": "Max paisa (capital)", "Money to start": "Shuru karne ka paisa", "Estimated profit": "Andaazi profit", "Estimated margin today": "Aaj ka andaazi margin", "Est. gross margin today": "Aaj ka andaazi margin",
    "Revenue today": "Aaj ki kamai", "Average order": "Average order", "Average time to ship": "Bhejne ka average time", "Hours to ship": "Bhejne ke ghante", "Products in total": "Kul products", "Products analysed": "Analyse hue products",
    "Products hidden automatically": "Apne aap chhupaye products", "Products by supplier": "Supplier ke hisaab se products", "Profit by supplier": "Supplier ke hisaab se profit",
    # statuses / chips
    "ON": "ON", "Off": "OFF", "On": "ON", "Always on": "Hamesha ON", "Not set up": "Set up nahi", "Not tested": "Test nahi hua", "Connected": "Jura hua", "Switched off": "Band", "disabled": "band",
    "Shipped": "Bhej diya", "Delivered": "Pahunch gaya", "Received": "Mil gaya", "Failed": "Fail", "Failed orders": "Fail hue orders", "Order confirmed": "Order confirm", "Late": "Late", "On time": "Time par",
    "Out of stock": "Stock khatam", "Needs attention": "Dhyan chahiye", "Need your check": "Tumhari check chahiye", "Needed you (failed)": "Tumhari zarurat padi (fail)", "Stuck on you": "Tumhare upar atka",
    "Works normally": "Theek chal raha hai", "Without problems": "Bina problem ke", "working now": "abhi kaam kar raha hai", "Profitable now": "Abhi profitable", "Imported, not listed yet": "Import hua, abhi list nahi",
    "Listed for sale": "Bikne ke liye listed", "Drafts (not listed)": "Draft (list nahi)", "Hidden or switched off": "Chhupa ya band", "Not selling (60 days)": "60 din se nahi bik raha", "On sale": "Sale par",
    "Priced by Strategy": "Strategy se price", "Manual price: the agent will not touch it": "Haath ka price: agent ise nahi chhoota", "In flight (confirmed / shipped)": "Raaste mein (confirm / shipped)",
    "Suggest only": "Sirf suggest karo", "Autopilot": "Autopilot", "Rehearsal only": "Sirf rehearsal", "Rehearsal (DRY-RUN) ON": "Rehearsal (DRY-RUN) ON", "Rehearsal (DRY-RUN) or live": "Rehearsal (DRY-RUN) ya live",
    "DRY-RUN": "DRY-RUN (nakli)", "LIVE": "LIVE", "Became products": "Products bane", "Offers waiting": "Offers intezaar mein", "Good opportunities waiting": "Achhe mauke intezaar mein", "Live on a better market": "Behtar market par live",
    "Best country": "Sabse achha desh", "Sells for more in": "Is desh mein mehenga bikta hai", "Sell in": "Yahan becho", "Ship to": "Kahan bhejna hai", "Seller update": "Seller ka update", "The seller says": "Seller ne kaha",
    "Internal note": "Andar ka note", "Your note": "Tumhara note", "Open alerts": "Khule alerts", "Open returns": "Khule returns", "Score wale orders": "Score wale orders", "Saare returns": "Saare returns",
    "Risk check ON": "Risk check ON", "Price watch ON": "Price watch ON", "Rules ": "Rules",
    # settings/form labels
    "Username": "Username", "Password": "Password", "Password (8+)": "Password (8+)", "New password": "Naya password", "New key": "Nayi key", "Current code": "Abhi ka code", "2FA code": "2FA code", "Base URL": "Base URL",
    "API key": "API key", "API style": "API style", "App Secret": "App Secret", "Callback URL": "Callback URL", "Business name": "Business ka naam", "Blocked countries": "Blocked desh", "Blocked emails": "Blocked emails",
    "Banned words": "Banned words (mana shabd)", "Approval threshold": "Approval limit", "Hold score (1-100)": "Roko score (1-100)", "Markup on cost (%)": "Cost par markup (%)", "Markup percent": "Markup percent",
    "Min margin % (optional)": "Min margin % (optional)", "Min margin % (blank = global)": "Min margin % (khali = global)", "Minimum profit (%)": "Minimum profit (%)", "Minimum gap (%)": "Minimum fark (%)",
    "Pricing style": "Price lagane ka tareeka", "Price valid for (days)": "Price kitne din valid (din)", "Sanity ratio": "Sanity ratio (shak ki seema)", "Undercut by (%)": "Kitna sasta (%)", "Exchange rate": "Exchange rate",
    "Units per 1 USD": "1 USD ke liye units", "Seller update / tracking manual": "Seller update / tracking haath se", "Heartbeat address (uptime monitor)": "Heartbeat address (uptime monitor)",
    "Daily report hour (0-23)": "Daily report ka ghanta (0-23)", "GSTIN / tax number (optional)": "GSTIN / tax number (optional)", "Invoice number prefix": "Invoice number prefix", "Payment gateway fee (%)": "Payment gateway fee (%)",
    "Internal note ": "Andar ka note", "Ships from country (blank = global)": "Kis desh se jayega (khali = global)", "CJ shipping method (blank = global)": "CJ shipping method (khali = global)",
    "Where (site)": "Kahan (site)", "Product link": "Product ka link", "Price per unit": "Ek unit ka price", "Shelf price there": "Wahan dukaan wala price", "Name (just for you)": "Naam (sirf tumhare liye)",
    "Filter by status": "Status se filter karo", "All statuses": "Saare status", "All suppliers": "Saare suppliers", "All": "Sab", "Simulate": "Nakli karo", "Search orders": "Orders khojo", "Search (optional)": "Khojo (optional)",
    "Seller agreed to ship single units to my customers": "Seller ne meri customers ko ek-ek unit bhejne ki haan ki hai", "Link to the listing (optional)": "Listing ka link (optional)",
    "Use it from your own website, a spreadsheet or n8n. It cannot change anything.": "Ise apni website, spreadsheet ya n8n se use karo. Ye kuch badal nahi sakti.",
    "Keep it private. Anyone with this address can read your product list.": "Ise private rakho. Is address wala koi bhi tumhari product list padh sakta hai.",
    # long helps (most visible)
    "No activity yet. When an order arrives, a product is imported or a job runs, it shows up here instantly.": "Abhi koi activity nahi. Order aate hi, product import hote hi ya job chalte hi yahan turant dikhega.",
    "No open alerts. Problems show up here and on Telegram.": "Koi khula alert nahi. Problem aane par yahan aur Telegram par dikhegi.",
    "No open alerts. When something fails, the reason and the order show up here and on Telegram.": "Koi khula alert nahi. Kuch fail hone par wajah aur order yahan aur Telegram par dikhenge.",
    "No orders in this period.": "Is dauraan koi order nahi.", "No orders yet. When a customer pays in your store, it appears here within seconds.": "Abhi koi order nahi. Customer ke store mein paise dete hi yahan kuch second mein dikhega.",
    "No products yet.": "Abhi koi product nahi.", "No sales in this period yet.": "Is dauraan abhi koi sale nahi.", "No offers yet. Add the first one above.": "Abhi koi offer nahi. Upar pehla jodo.",
    "Nothing is waiting for you.": "Tumhare liye kuch intezaar mein nahi.", "Nothing logged yet.": "Abhi kuch log nahi hua.", "Nothing yet.": "Abhi kuch nahi.", "Nothing found. Try another search.": "Kuch nahi mila. Doosra khoj try karo.",
    "None.": "Koi nahi.", "No changes yet.": "Abhi koi badlav nahi.", "Not sent to the supplier yet.": "Abhi supplier ko nahi gaya.", "This order was cancelled.": "Ye order cancel ho gaya.",
    "Nothing is listed on a store yet.": "Abhi kuch bhi kisi store par listed nahi.", "Nothing is stuck. Every listed product sold in the last 60 days, or is newer than that.": "Kuch atka nahi. Har listed product pichhle 60 din mein bika ya usse naya hai.",
    "This connection needs no settings.": "Is connection ko koi setting nahi chahiye.", "Thank you for your order.": "Order ke liye dhanyavaad.", "Swipe the table sideways to see every column.": "Saare columns dekhne ke liye table ko side mein khisakao.",
    "Orders over the threshold in Settings will appear here. Alerts also reach you on Telegram.": "Settings ki limit se bade orders yahan dikhenge. Alerts Telegram par bhi aate hain.",
    "Orders at or above your approval threshold wait here. Approve to send them to the supplier, or reject to cancel.": "Approval limit ya usse bade orders yahan ruke hain. Approve karo to supplier ko jayenge, reject karo to cancel.",
    "Stops new supplier orders, tracking, and stock syncs immediately. Orders keep arriving and wait for you.": "Naye supplier orders, tracking aur stock sync turant ruk jate hain. Orders aate rahenge aur intezaar karenge.",
    "In rehearsal, orders go through every check but are never sent to the supplier. Go live only after a successful rehearsal.": "Rehearsal mein order har check se guzarte hain par supplier ko nahi jaate. Rehearsal safal hone ke baad hi live jao.",
    "Each agent is one worker in your shop. This page shows what every one of them is doing right now, how much work it finished, and anything that went wrong.": "Har agent tumhari dukaan ka ek karmchari hai. Ye page dikhata hai ki har ek abhi kya kar raha hai, kitna kaam hua, aur kya galat gaya.",
    "Everything you sell or plan to sell: where it comes from, where it is listed, and its price and margin. Stock and cost are checked on a schedule, and anything that would sell at a loss is hidden automatically.": "Jo kuch tum bechte ho ya bechne wale ho: kahan se aata hai, kahan listed hai, price aur margin. Stock aur cost samay samay par check hote hain, aur loss mein bikne wala product apne aap chhup jata hai.",
    "Import some from a connected supplier, or add one by hand below. Orders for products that are not here are held for you, never guessed.": "Jude hue supplier se kuch import karo, ya neeche haath se jodo. Jo product yahan nahi hain unke orders tumhare liye ruke rehte hain, andaaza nahi lagaya jata.",
    "Browse a supplier's products, tick the ones you want, and import them as drafts. You review the price, then list them on your store or website from": "Supplier ke products dekho, jo chahiye unhe tick karo aur draft ke roop mein import karo. Price tum dekhoge, phir store ya website par list karoge:",
    "Shipping, duty and VAT are starting estimates. Change them to your real numbers: they decide the minimum safe price.": "Shipping, duty aur VAT shuruaati andaaze hain. Inhe apne asli numbers se badlo: yahi minimum safe price tay karte hain.",
    "\"Estimate\" rates are rough starting values. The agent will not change a live price from an estimate: press Refresh or type the rate yourself to confirm it.": "\"Estimate\" rates shuruaati andaaze hain. Agent estimate se live price nahi badlega: Refresh dabao ya rate khud likh kar confirm karo.",
    "Older market prices are not acted on.": "Purane market prices par koi kaam nahi hota.", "Must be at least this much cheaper than the market.": "Market se kam se kam itna sasta hona chahiye.",
    "After shipping, duty, VAT and fees. We never price below this.": "Shipping, duty, VAT aur fees ke baad. Isse neeche price kabhi nahi lagta.",
    "A scraper-sent market price this many times above our cost waits for you to confirm.": "Scraper se aaya market price hamari cost se itne guna upar ho to tumhari confirmation ka intezaar karta hai.",
    "A connector file is code that runs on this computer. Only upload files you wrote or trust.": "Connector file ek code hai jo is computer par chalta hai. Sirf wahi file upload karo jo tumne likhi ho ya jis par bharosa ho.",
    "If the main supplier runs out or fails, the backup takes over automatically.": "Main supplier ke paas stock khatam ho ya wo fail ho to backup apne aap sambhal leta hai.",
    "If the order is not there, Cheape Deal can safely send it again.": "Agar order wahan nahi hai to Cheape Deal use surakshit tareeke se dobara bhej sakta hai.",
    "Did the supplier get this order?": "Kya supplier ko ye order mila?", "Yes, it was placed": "Haan, order lag gaya", "No, send it again": "Nahi, dobara bhejo", "Ask him.": "Usse poocho.",
    "Listed products with no order for 60 days. Consider switching them off on the Products page.": "Jin listed products par 60 din se koi order nahi. Products page par inhe band karne ka sochho.",
    "Potential profit per unit across good and live items:": "Achhe aur live items par ek unit ka sambhavit profit:", "Track your order": "Apna order track karo", "Bill to": "Bill kisko", "newest first, updates every 3 seconds": "naya pehle, har 3 second mein update",
    "last run per job": "har job ka aakhri run", "items handled in 24h": "24 ghante mein sambhale items", "runs in 24h": "24 ghante mein runs", "errors": "errors", "handled": "sambhale", "runs": "runs", "profit": "profit",
    "Poora email, ya @domain.com.": "Poora email, ya @domain.com.", "Download": "Download",
    # agents (names and does)
    "Order Intake": "Order Intake (order lena)", "Order Router": "Order Router (order bhejna)", "Stock and Price": "Stock aur Price", "Listing": "Listing", "Sourcing Scout": "Sourcing Scout (sasta maal dhundhna)",
    "Report and Backup": "Report aur Backup", "Missed-Order Sweeper": "Chhute Order Sweeper", "Reconciler": "Reconciler (hisaab milana)", "Router": "Router", "Sweeper": "Sweeper", "Scout": "Scout", "Strategist": "Strategist", "Reporter": "Reporter",
    "Receives paid orders from your stores and saves each one exactly once.": "Tumhare stores se paid orders leta hai aur har order ek hi baar save karta hai.",
    "Checks each order, applies the margin and approval rules, and places it with the right supplier.": "Har order check karta hai, margin aur approval rules lagata hai aur sahi supplier ko order deta hai.",
    "Asks suppliers for tracking numbers and tells your customers.": "Suppliers se tracking number mangta hai aur customers ko batata hai.",
    "Keeps stock and prices in line with each supplier and hides products that would lose money.": "Har supplier ke hisaab se stock aur price theek rakhta hai aur loss wale products chhupa deta hai.",
    "Runs your dropshipping strategies: demand pricing, clearance, sales, cheapest supplier, retiring dead products.": "Tumhari dropshipping strategies chalata hai: demand pricing, clearance, sale, sasta supplier, bekaar products band.",
    "Browses connected suppliers and imports products you choose.": "Jude hue suppliers dekhta hai aur tumhare chune products import karta hai.",
    "Publishes products on your stores and websites.": "Products ko tumhare stores aur websites par list karta hai.",
    "Finds the country where each product sells for the most, undercuts the local price and keeps a safe profit.": "Har product ke liye wo desh dhundhta hai jahan sabse mehenga bikta hai, local price se thoda sasta lagata hai aur safe profit rakhta hai.",
    "Checks cheap offers (IndiaMART, wholesalers) against prices abroad and turns the profitable ones into products.": "Sasta offers (IndiaMART, wholesaler) ko videsh ke prices se milata hai aur profitable ko products bana deta hai.",
    "Sends the daily report, saves a database backup and tidies old activity.": "Daily report bhejta hai, database backup leta hai aur purani activity saaf karta hai.",
    "Re-checks the store for paid orders a webhook may have missed.": "Store mein dobara dekhta hai ki koi paid order webhook se chhut to nahi gaya.",
    "Every night compares our records with the suppliers and reports differences.": "Roz raat hamare records ko suppliers se milata hai aur fark batata hai.",
    # order status labels (from the label filter)
    "With supplier": "Supplier ke paas", "Supplier confirmed": "Supplier ne confirm kiya", "Needs approval": "Approval chahiye", "Needs you": "Tumhari zarurat", "Retrying": "Dobara try ho raha",
    "Received ": "Mil gaya", "Validated": "Check ho gaya", "Confirmed": "Confirm", "Cancelled": "Cancel", "Pending": "Intezaar mein", "Submitted to supplier": "Supplier ko bheja", "Failed manual": "Fail (haath se dekho)",
    # misc words
    "Example:": "Example:", "Format:": "Format:", "Columns:": "Columns:", "Placeholders:": "Placeholders:", "Safety:": "Suraksha:", "Windows:": "Windows:", "Login:": "Login:", "Status:": "Status:", "Backup:": "Backup:",
    "Heartbeat:": "Heartbeat:", "Health checks:": "Health checks:", "Finance:": "Finance:", "Folder:": "Folder:", "From:": "Kahan se:", "to:": "ko:", "Header:": "Header:", "Roles:": "Roles:", "Returns:": "Returns:",
    "Risk check:": "Risk check:", "Risk score:": "Risk score:", "Invoice:": "Invoice:", "Retry:": "Dobara try:", "Scheduling:": "Schedule:", "Order loop:": "Order ka chakkar:", "Reconciliation:": "Hisaab milana:",
    "Stock/price sync:": "Stock/price sync:", "Tracking sync:": "Tracking sync:", "Tracking number:": "Tracking number:", "Order webhook for this site:": "Is site ke liye order webhook:", "Store webhook URL": "Store webhook URL",
    "Listed on:": "Kahan listed:", "Minimum margin:": "Minimum margin:", "Approval threshold:": "Approval limit:", "Customer emails:": "Customer emails:", "Daily report:": "Daily report:", "Read-only API:": "Sirf-padhne wali API:",
    "Customer tracking page:": "Customer tracking page:", "Customer tracking page (share if you like):": "Customer tracking page (chaho to share karo):", "Strategy \"needs rate\":": "Strategy \"needs rate\":", "Backup:": "Backup:",
    "Shabdkosh": "Shabdkosh", "Problem aaye to (FAQ)": "Problem aaye to (FAQ)", "Open": "Kholo", "open": "kholo", "open order": "order kholo", "link": "link", "store": "store", "supplier": "supplier", "strategy": "strategy",
    "Total ": "Total", "Not in Products yet": "Abhi Products mein nahi", "Order dena:": "Order dena:", "Zaroori:": "Zaroori:", "Dhyan:": "Dhyan:",
    # dashboard extras
    'Everything is moving': 'Sab kuch chal raha hai',
    'Open them below, fix the cause, then retry or approve.': 'Neeche kholo, wajah theek karo, phir retry ya approve karo.',
    'Rehearsal mode is on, so nothing reaches the supplier yet.': 'Rehearsal mode ON hai, isliye abhi kuch supplier tak nahi pahunchta.',
    'No order is stuck. Agents refresh every 3 seconds, the rest every 30.': 'Koi order atka nahi. Agents har 3 second mein, baaki har 30 second mein refresh hote hain.',
    'What is happening': 'Abhi kya ho raha hai',
    'Open alerts ': 'Khule alerts',
    'Latest orders': 'Naye orders',
    'Demo duniya': 'Demo duniya',
    'Demo World': 'Demo duniya (nakli)',
    'Automation heartbeat': 'Automation heartbeat (dhadkan)',
    'AI Models + Assistant': 'AI Models + Assistant',
    'AI model (Base URL + API key)': 'AI model (Base URL + API key)',
    'AI model jodo': 'AI model jodo',
    'AI Advisor': 'AI Advisor (salah dene wala)',
    'AI ki taza salah': 'AI ki taza salah',
    'Models fetch karo': 'Models fetch karo',
    'Automation is paused': 'Automation ruka hua hai',
    'Resume it from the bar above when you are ready.': 'Taiyaar ho to upar wali patti se Start dabao.',
    'Connect a real supplier': 'Asli supplier jodo',
    'Connect your store or website': 'Apna store ya website jodo',
    'Confirm exchange rates': 'Exchange rates confirm karo',
    'Add your first product': 'Apna pehla product jodo',
    'Add a price from another country': 'Doosre desh ka price jodo',
    'Add a cheap offer (IndiaMART, wholesaler)': 'Sasta offer jodo (IndiaMART, wholesaler)',
    'Turn off rehearsal mode (go live)': 'Rehearsal band karo (live jao)',
    'Not run yet': 'Abhi chala nahi',
    'Nothing to report yet.': 'Abhi batane layak kuch nahi.',
    'Pehle Setup wizard': 'Pehle Setup wizard',
}

# sentences with numbers inside: (pattern, replacement)
PATTERNS = [
    (re.compile(r"^(\d+) orders? needs? you$"), r"\1 order ko tumhari zarurat hai"),
    (re.compile(r"^Setup: (\d+) of (\d+) done$"), r"Setup: \2 mein se \1 ho gaya"),
    (re.compile(r"^(\d+) of (\d+)$"), r"\1 / \2"),
    (re.compile(r"^(\d+) runs?$"), r"\1 baar chala"), (re.compile(r"^(\d+) handled$"), r"\1 sambhale"),
]

_SKIP = re.compile(r"(<script\b.*?</script>|<style\b.*?</style>|<textarea\b.*?</textarea>)", re.S | re.I)
_TEXT = re.compile(r">([^<>]+)<")
_ATTR = re.compile(r'\b(placeholder|title|aria-label)="([^"]*)"')


def translate_html(html: str) -> str:
    def text(m: re.Match) -> str:
        raw = m.group(1)
        key = " ".join(raw.split())
        hi = HI.get(key)
        if hi is None:
            for rx, rep in PATTERNS:
                if rx.match(key):
                    hi = rx.sub(rep, key)
                    break
        if hi is None:
            return m.group(0)
        lead = raw[: len(raw) - len(raw.lstrip())]
        trail = raw[len(raw.rstrip()):]
        return f">{lead}{hi}{trail}<"

    def attr(m: re.Match) -> str:
        hi = HI.get(" ".join(m.group(2).split()))
        return f'{m.group(1)}="{hi}"' if hi else m.group(0)
    parts = _SKIP.split(html)
    for i in range(0, len(parts), 2):  # even parts are outside script/style/textarea
        parts[i] = _ATTR.sub(attr, _TEXT.sub(text, parts[i]))
    return "".join(parts)


class HinglishMiddleware(BaseHTTPMiddleware):
    default = "hi"  # main.py sets "en" in the test environment so old tests keep reading English

    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        if request.scope.get("session", {}).get("lang", self.default) != "hi" or "text/html" not in resp.headers.get("content-type", ""):
            return resp
        body = b"".join([chunk async for chunk in resp.body_iterator])
        out = translate_html(body.decode("utf-8", "replace")).encode("utf-8")
        headers = {k: v for k, v in resp.headers.items() if k.lower() not in ("content-length", "content-encoding")}
        return Response(out, status_code=resp.status_code, headers=headers, media_type="text/html")
