"""The 30 affiliate / open-market / print-on-demand programs, ordered by the commission they publish.

Commission numbers are what the program or a directory states (source in `src`), CHECKED Nov 2026. Programs change rates often and most pay per merchant or
category, so the real number is always the one in your own dashboard. Where nobody states a single number we say "merchant ke hisaab se" and do not guess.
mode: feed (you paste the product-feed URL your network gives) | amazon | ebay (live search by API) | printful | printify (POD supplier, prints and ships)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .sdk import Field

DIR = "https://uppromote.com/affiliate-directory/"
D24 = "https://www.digistore24.com/blog/best-affiliate-programs/"
CUE = "https://www.cuelinks.com/blog/best-affiliate-programs-in-india/"


@dataclass
class Program:
    key: str
    label: str
    group: str  # network | marketplace | digital | pod_api | pod_market | universal
    mode: str
    commission: str
    top_pct: Optional[float]
    src: str
    signup: str
    howto: str
    defaults: dict = field(default_factory=dict)
    verified: str = "Feed padhne ka code fake feed se test hua; asli account par live nahi chala."


GROUP_LABEL = {"network": "Affiliate network (product feed)", "marketplace": "Open market / retailer", "digital": "Digital products (high commission)",
               "pod_api": "Print-on-Demand (API, print + ship)", "pod_market": "Print-on-Demand marketplace (affiliate)", "universal": "Koi bhi feed"}
FEED_HOW = "Network ke dashboard mein 'Product feeds / Catalog / Create-a-Feed' section se us advertiser ka feed URL copy karo (CSV, XML ya JSON, gzip bhi chalta hai) aur neeche paste karo. Feed mein tumhara tracking link pehle se hota hai."

PROGRAMS: list[Program] = [
    Program("aff_digistore24", "Digistore24", "digital", "feed", "15-90% (vendor ke hisaab se, avg 50-85% unka claim)", 90, D24, "https://www.digistore24.com/",
            "Digistore24 marketplace se products chuno, HopLink banao. Agar tumhare account mein product export/feed mile to URL daalo; warna 'Universal feed' ke sath apni CSV host karo. Account ke hisaab se feed option alag hota hai, docs se verify nahi hua."),
    Program("aff_clickbank", "ClickBank", "digital", "feed", "50-75% (kuch digital products 90% tak), cookie 60 din", 75, DIR + "clickbank/", "https://www.clickbank.com/",
            "ClickBank Marketplace se HopLink banate hain. ClickBank ka koi verified public product-feed API nahi mila, to feed/CSV khud banao (Universal feed jaisa) aur URL yahan daalo."),
    Program("aff_zazzle", "Zazzle Associates (POD marketplace)", "pod_market", "feed", "15% dusron ke products par; apne products par 35-50%; cookie 45 din", 50, DIR + "zazzle/", "https://www.zazzle.com/associates",
            "Zazzle Associate program center mein product feed milta hai. Apne design bana kar apne link se bechoge to 35-50% tak. " + FEED_HOW),
    Program("aff_cuelinks", "Cuelinks (India network)", "network", "feed", "Merchant ke hisaab se: Hostinger <=28.5%, Mamaearth <=22.5%, Flipkart <=11%, Amazon India <=9%, Myntra <=7.5%", 28.5, CUE, "https://www.cuelinks.com/",
            "Cuelinks India ke merchants ke links ko apne tracking link mein badalta hai. Kisi Indian merchant ka plain product feed daalo aur neeche 'Tracking link template' mein apna Cuelinks link format (https://linksredirect.com/?cid=<tumhara id>&source=linkkit&url={url_enc}) rakho; ye format yaad se hai, Cuelinks dashboard se confirm karo.",
            defaults={"deeplink_prefix": "https://linksredirect.com/?cid=APNA_CID&source=linkkit&url={url_enc}", "currency": "INR"}),
    Program("aff_cj_affiliate", "CJ Affiliate (Commission Junction; CJ Dropshipping se alag)", "network", "feed", "5-30% (merchant ke hisaab se)", 30, D24, "https://www.cj.com/", FEED_HOW),
    Program("aff_banggood", "Banggood", "marketplace", "feed", "6-13% (promo par 25% tak), cookie 30 din", 25, DIR + "banggood/", "https://www.banggood.com/affiliate/",
            "Banggood apna program chalata hai aur CJ Affiliate / Admitad par bhi hai. " + FEED_HOW),
    Program("aff_flipkart", "Flipkart Affiliate (India)", "marketplace", "feed", "0-11% (category ke hisaab se)", 11, CUE, "https://affiliate.flipkart.com/",
            "Flipkart affiliate dashboard mein category-wise product feed links milte hain. " + FEED_HOW, defaults={"currency": "INR"}),
    Program("aff_ali", "AliExpress", "marketplace", "feed", "3-9% category ke hisaab se, cookie 3 din", 9, DIR + "aliexpress/", "https://portals.aliexpress.com/",
            "AliExpress Portals ya Awin/CJ/Admitad/FlexOffers ke through. " + FEED_HOW),
    Program("aff_amazon", "Amazon Associates (Creators API)", "marketplace", "amazon", "1-10% category ke hisaab se", 10, D24, "https://affiliate-program.amazon.com/",
            "Associates portal mein Creators API ke liye application banao: Client ID, Client Secret aur credential version milta hai. Purani PA-API 5 2026 mein band ho chuki. Live search; links mein tumhara tag hota hai.",
            verified="Login/token ke steps aur search ka format Amazon ke docs se; search ka path getItems ke pattern par hai. Asli account par live nahi chala."),
    Program("aff_amazon_in", "Amazon India Associates (Creators API)", "marketplace", "amazon", "Up to 9% (Cuelinks ke hisaab se)", 9, CUE, "https://affiliate-program.amazon.in/",
            "Upar wali Amazon API, bas marketplace www.amazon.in aur apna .in Associate tag. Credential version apni region ke hisaab se (portal mein likha hota hai).",
            defaults={"marketplace": "www.amazon.in"}, verified="Amazon docs se; asli account par live nahi chala."),
    Program("pod_spreadshirt", "Spreadshirt (POD marketplace)", "pod_market", "feed", "15% (Webgains par; Admitad par kuch 25% tak), cookie 30 din", 15, DIR + "spreadshirt/", "https://www.spreadshirt.com/affiliate",
            FEED_HOW),
    Program("aff_wayfair", "Wayfair", "marketplace", "feed", "5-7%", 7, D24, "https://www.wayfair.com/", FEED_HOW),
    Program("aff_target", "Target", "marketplace", "feed", "Up to 8%", 8, D24, "https://www.target.com/", "Target ka program impact.com par hai. " + FEED_HOW),
    Program("aff_awin", "Awin", "network", "feed", "Advertiser ke hisaab se (Etsy, AliExpress aur hazaaron merchant)", None, "https://help.awin.com/developers/docs/faqs-for-publishers", "https://www.awin.com/",
            "Awin > Toolbox > Create-a-Feed se feed banao, download link copy karo (Awin docs). Columns aw_deep_link, search_price, product_name wagairah apne aap pehchane jate hain."),
    Program("aff_rakuten", "Rakuten Advertising", "network", "feed", "Merchant ke hisaab se (avg ~5%)", None, D24, "https://rakutenadvertising.com/", FEED_HOW),
    Program("aff_impact", "impact.com", "network", "feed", "Merchant ke hisaab se (Walmart, Target, Redbubble jaise)", None, D24, "https://impact.com/", FEED_HOW),
    Program("aff_flexoffers", "FlexOffers", "network", "feed", "Merchant ke hisaab se", None, DIR + "aliexpress/", "https://www.flexoffers.com/", FEED_HOW),
    Program("aff_admitad", "Admitad", "network", "feed", "Merchant ke hisaab se (AliExpress, Banggood, Spreadshirt yahan bhi)", None, DIR + "aliexpress/", "https://www.admitad.com/", FEED_HOW),
    Program("aff_partnerize", "Partnerize", "network", "feed", "Merchant ke hisaab se", None, "", "https://partnerize.com/", FEED_HOW),
    Program("aff_webgains", "Webgains", "network", "feed", "Merchant ke hisaab se (Spreadshirt 15%)", None, DIR + "spreadshirt/", "https://www.webgains.com/", FEED_HOW),
    Program("aff_tradedoubler", "TradeDoubler", "network", "feed", "Merchant ke hisaab se", None, "", "https://www.tradedoubler.com/", FEED_HOW),
    Program("aff_tradetracker", "TradeTracker", "network", "feed", "Merchant ke hisaab se", None, "", "https://tradetracker.com/", FEED_HOW),
    Program("aff_optimise", "Optimise", "network", "feed", "Merchant ke hisaab se", None, "", "https://optimisemedia.com/", FEED_HOW),
    Program("aff_redbubble", "Redbubble (POD marketplace)", "pod_market", "feed", "Up to 10% (kahin 2% base), cookie 30 din", 10, DIR + "redbubble", "https://www.redbubble.com/", "Redbubble ka program impact.com par chalta hai. " + FEED_HOW),
    Program("aff_ebay", "eBay Partner Network", "marketplace", "ebay", "1-4% (kuch jagah 6% tak): eBay ki fees ka hissa, cookie 24 ghante", 6, DIR + "ebay/", "https://partnernetwork.ebay.com/",
            "EPN account mein Campaign ID banao; eBay Developer account se App ID (Client ID) aur Cert ID (Client Secret) lo. Live search; links mein campaign id hota hai.",
            verified="eBay ka token + Browse API + affiliate header ka format yaad/docs se; asli account par live nahi chala."),
    Program("aff_etsy", "Etsy (Awin ke through)", "marketplace", "feed", "4%", 4, D24, "https://www.awin.com/", "Etsy ka program Awin par hai. " + FEED_HOW),
    Program("aff_walmart", "Walmart", "marketplace", "feed", "1-4%", 4, D24, "https://affiliates.walmart.com/", "Walmart ka program impact.com par hai. " + FEED_HOW),
    Program("pod_printful", "Printful (POD: print + ship)", "pod_api", "printful", "Tumhara profit = bechne ka daam - Printful ka base cost. Referral: 10% base cost ka, 12 mahine", 10, DIR + "printful/", "https://www.printful.com/",
            "Printful dashboard mein apne designs ke products banao, Settings > API se token lo. Hum unhe import karte hain, bechte hain, order aane par Printful ko bhejte hain.",
            verified="Endpoints (store/products, orders, @external_id) Printful docs se; base-cost endpoint yaad se. Asli account par live nahi chala."),
    Program("pod_printify", "Printify (POD: print + ship)", "pod_api", "printify", "Tumhara profit = bechne ka daam - Printify ka cost. Referral: 5% 12 mahine", 5, DIR + "printify/", "https://printify.com/",
            "Printify mein apne products banao, Account > Connections se Personal Access Token lo. Hum import karke bechte hain aur order Printify ko bhejte hain.",
            verified="Paths aur order body Printify ke OpenAPI se; asli account par live nahi chala."),
    Program("aff_feed", "Universal product feed (koi bhi company)", "universal", "feed", "Jo bhi program tumhare feed ke link mein hai", None, "", "",
            "Kisi bhi company/network ka product feed URL (CSV, TSV, XML, JSON ya .gz). Zaroori: har product mein naam, http link aur price."),
]


def by_key(key: str) -> Program | None:
    return next((p for p in PROGRAMS if p.key == key), None)


def ranked() -> list[Program]:
    """Highest published commission first; programs with no single stated number go after, in list order."""
    return sorted(PROGRAMS, key=lambda p: (p.top_pct is None, -(p.top_pct or 0)))


def fields_for(p: Program) -> list[Field]:
    d = p.defaults
    if p.mode == "amazon":
        return [Field("client_id", "Client ID", secret=True, help="Associates portal > Creators API > application banao."), Field("client_secret", "Client Secret", secret=True),
                Field("partner_tag", "Partner tag (Associate tag)", placeholder="mytag-20", help="Tumhara Associates tracking id; links mein yahi lagta hai."),
                Field("marketplace", "Marketplace", required=False, default=d.get("marketplace", "www.amazon.com"), help="www.amazon.com, www.amazon.in, www.amazon.co.uk ..."),
                Field("credential_version", "Credential version", required=False, default="3.1", help="Portal mein credential ke saath likha hota hai: 3.1 (NA), 3.2 (EU), 3.3 (FE) ya 2.1/2.2/2.3.")]
    if p.mode == "ebay":
        return [Field("client_id", "App ID (Client ID)", secret=True), Field("client_secret", "Cert ID (Client Secret)", secret=True),
                Field("campaign_id", "EPN Campaign ID", help="eBay Partner Network > Campaigns. Iske bina links par commission nahi milta."),
                Field("marketplace", "Marketplace", required=False, default="EBAY_US", help="EBAY_US, EBAY_GB, EBAY_DE, EBAY_AU, EBAY_CA ..."),
                Field("reference_id", "Reference id (optional)", required=False, help="Apne reports ke liye ek label.")]
    if p.mode == "printful":
        return [Field("api_token", "Printful API token", secret=True, help="Printful dashboard > Settings > API."), Field("store_id", "Store ID (sirf account-level token par)", required=False)]
    if p.mode == "printify":
        return [Field("api_token", "Printify Personal Access Token", secret=True, help="Printify > Account > Connections."), Field("shop_id", "Shop ID (optional)", required=False, help="Ek hi shop ho to khud mil jata hai."),
                Field("print_provider_id", "Print provider ID (optional)", required=False, help="Khali chhodo to har product ke liye pehla provider chunta hai. Printify > Catalog mein provider ki ID dekh sakte ho.")]
    earn = {
        "aff_awin": [Field("awin_publisher_id", "Awin Publisher ID (earnings ke liye)", required=False, help="Awin > Account > Overview mein Publisher ID."),
                     Field("awin_api_token", "Awin API token (earnings ke liye)", secret=True, required=False, help="Awin > Account > API Credentials > OAuth2 token. Isse commission/earnings padhe jaate hain.")],
        "aff_impact": [Field("impact_account_sid", "impact Account SID (earnings ke liye)", required=False, help="impact.com > Settings > API: Account SID."),
                       Field("impact_auth_token", "impact Auth Token (earnings ke liye)", secret=True, required=False, help="Wahi API page par Auth Token.")],
        "aff_cj_affiliate": [Field("cj_publisher_id", "CJ Publisher (company) ID (earnings ke liye)", required=False, help="CJ Account > Account Settings mein Company/Publisher ID (number)."),
                             Field("cj_token", "CJ Personal Access Token (earnings ke liye)", secret=True, required=False, help="developers.cj.com > Authentication > Personal Access Token.")],
    }.get(p.key, [])
    return [Field("feed_url", "Product feed URL" + (" (earnings-only ho to khali chhodo)" if earn else ""), secret=True, required=not earn,
                  help="Isme aksar tumhari API key hoti hai, isliye chhupa kar (encrypted) rakha jata hai."),
            Field("deeplink_prefix", "Tracking link template (optional)", required=False, default=d.get("deeplink_prefix", ""), help="Sirf tab jab feed mein plain merchant link ho. {url_enc} ya {url} ki jagah product link lagta hai."),
            Field("merchant", "Dukaan ka naam (optional)", required=False, placeholder=p.label.split(" (")[0], help="Storefront par 'Buy at ...' mein dikhta hai."),
            Field("commission_pct", "Commission % (optional)", required=False, help="Feed mein na ho to apni dashboard ka number likho; sirf tumhare reports ke liye."),
            Field("currency", "Feed ki currency", required=False, default=d.get("currency", "USD"), help="Feed mein currency na ho to ye maani jati hai (USD, INR, GBP, EUR ...).")] + earn
