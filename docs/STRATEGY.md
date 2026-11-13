# Strategy: sell where it sells for the most

Open **Strategy** in the menu. All money is in **USD**.

## The idea
A product that costs about $24 from an Indian supplier may sell for $2000 in the USA. Cheape Deal lists it there at the shelf price minus your undercut (25% = **$1500**), and your supplier ships it from India. It only does this if the profit stays above your minimum after shipping, import duty, VAT and payment fees.

## Setup in 5 steps
1. **Countries**: check shipping, duty, VAT and fee for each country you want (starting values are only estimates).
2. **Exchange rates**: press *Refresh from the internet* (or type the rate). Rates marked "estimate" are never used to change a live price.
3. If your supplier quotes in rupees (or any other currency), open the product's Strategy page and set *Supplier currency* to INR.
4. **Add a market price**: what the same product sells for in that country (form, CSV, or `POST /internal/strategy/prices` from a scraper or n8n, with the `X-Internal-Key` header).
5. Press **Analyse now**. Good opportunities show with an *Apply* button. Turn on **Autopilot** to let the agent apply them every 6 hours.

## The maths
- landed cost = (supplier cost + shipping) x (1 + duty)
- lowest safe price = landed cost / ((1 - min profit) / (1 + VAT) - fee)
- our price = the higher of (shelf price x (1 - undercut)) and the lowest safe price, rounded up to your price ending
- it must still be at least *minimum gap* % cheaper than the shelf price, otherwise the market is "Not profitable"

Pricing styles: undercut by your %, just below the market (2%), or aggressive (1.5x your undercut, max 60%).

## Safety rules (why the autopilot may wait for you)
- Exchange rate still an estimate: waits.
- Market price older than the set number of days: waits.
- A price sent by a scraper that is more than the *sanity ratio* times our cost: waits for your confirmation (prices you type or import are trusted but still show a warning).
- Products with *manual price* switched on, or inactive, are never changed.
- If the store refuses the price, nothing changes and an alert is raised; the next run retries.
- If the gap disappears, a Strategy-priced product goes back to the normal markup price by itself.
- Every change is written to the price history, the live agent feed, and the outgoing webhook (`product.strategy_applied`).

## Limits to know
- One selling price per product: the agent picks the single best country. The product is listed on that country's store if you set one for the country.
- The store must charge in USD (let the store's own currency feature show local prices).
- Market prices are entered by you or your scraper; Cheape Deal does not scrape competitor sites itself.
- Duty, VAT and shipping are flat per country, not per product weight.
- Never run against a live store yet: test with the built-in rehearsal store first.
