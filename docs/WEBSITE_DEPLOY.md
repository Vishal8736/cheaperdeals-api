# Website: dukaan Vercel par, backend Render par (sab free)

```
Customer -> Vercel (static dukaan, edge cache) --/api/*--> Render (ye project: API + engine) --> Postgres (Neon, free)
                                                                  |--> CJ Dropshipping (supplier)
```

## Ye kyun sleep nahi hoga aur load kam rahega
| Kaam | Kaise |
|---|---|
| Browsing par Render ko kuch nahi karna padta | `/shop/products` aur `/shop/config` ke answers Vercel ke edge par 5 minute cache hote hain (`s-maxage=300`). Hazaron visitor = Render par ~1 request / 5 min |
| Server soye nahi | GitHub Actions `keepalive.yml` har 10 minute `/ping` kholta hai (`/ping` mein database nahi, 0 kaam). Server khud ko bhi ping karta hai (`RENDER_EXTERNAL_URL` se) |
| Pehle se jaga hua | Customer cart kholta hai to browser `/ping` bhejta hai, checkout tak server ready |
| Database free me bacha rahe | Render ki free disk restart par mit jaati hai, isliye DB Neon/Supabase ka free Postgres hai |
| Order spam | Har visitor par rate limit; prices server par dobara nikalte hain |

Seema: Render free = 750 ghante/mahina. Ek service 24x7 = ~744 ghante, bas ek service rakho. Agar fir bhi kabhi so jaye, pehla customer request 30-60 sec leta hai; dukaan us case me "wait" dikhati hai, fail nahi hoti. Extra pinger: UptimeRobot (free) par HTTP monitor `https://<render>/ping`, 5 min.

## Start dabao, baaki project karta hai (v14: sab CLI login se, koi API key nahi)
Menu > **API Keys + Ping**:
1. **CJ API key** daalo (hamesha ke liye save).
2. **Vercel, Render, GitHub, Neon** ke "login (CLI)" buttons dabao. Har ek browser kholta hai, wahan allow karo. Phir "Status check".
   CLI chahiye (page par na ho to install ka tareeka likha aata hai): Vercel aur Neon ke liye Node.js (`npm i -g vercel`, `npm i -g neonctl`), Render ke liye `render` (render.com/docs/cli), GitHub ke liye `gh` (cli.github.com).
3. Upar **Start** dabao (ya Website jodo page par "Abhi check karo aur host karo"). Koi extra host-button nahi: Start khud sab karta hai. Project:
   - Pehle **login**: jis CLI mein login nahi, uska browser login khud khol deta hai aur 10 minute tak tumhare allow karne ka intezaar karta hai (link/code Start ke log mein bhi aata hai).
   - Phir **detection**: Render, GitHub, Neon, Vercel par dekhta hai kya pehle se bana hai (page par "mila / nahi mila / pata nahi"), aur jo nahi mila wo banata hai.
   - Render par `cheaperdeals-api` dhundta hai aur `/ping` se dekhta hai jawab deti hai. Hai to wahi use karta hai.
   - Nahi hai to: Neon par free database `cheaperdeals` banata hai (address encrypted save), GitHub par public repo `cheaperdeals-api` mein code daalta hai (`local.env`/`.env`/database files kabhi nahi jaati), Render par free web service banata hai (CJ key, database, naya admin password env mein), build ka intezaar karta hai.
   - Vercel par `cheaperdeals-shop` dekhta hai; backend se juda aur `/api/ping` jawab deta hai to wahi use karta hai; nahi to dukaan deploy karta hai.
   - Render server ko Start ka signal bhejta hai, phir is copy ka engine bhi Start.
   Kuch fail ho to engine Start nahi hota aur page par saaf wajah likhi aati hai. "Sirf is computer par Start" se hosting chhod kar local chala sakte ho.
4. Render server ka admin login: user `admin`, password Website jodo page par "Render admin password dikhao".
**Live checkbox** (Website jodo): band = Rehearsal (CJ ko asli order nahi jata). ON = `DRY_RUN=false`, shipping method khali ho to AUTO, CJ wallet se paisa katega. Pehle ek test order dekho.
**Sach (padh lo):**
- Vercel/Render/GitHub/Neon CLI ke commands docs se liye hain, build sandbox se live nahi chalaye (tests mein CLI nakli hai). Pehla asli Start hi asli test hai; galti aaye to page par CLI ka apna message aata hai.
- `gh auth login --web` ko non-terminal mein Enter bhejna padta hai (app bhejta hai); na chale to terminal mein `gh auth login` khud chalao, phir Status check.
- Render CLI se env variable baad mein badalna nahi hota (sirf service banate waqt jaate hain); badalna ho to Render dashboard. `services create` fail ho to Blueprint link (render.yaml, one-click) page par aata hai.
- Render par bana server **alag copy** hai: apna database (Neon), apne orders. Uske admin login se Import products karo, warna dukaan khali dikhegi.
- Render free: 15 min idle par so jata hai (keep-alive laga hai par pehla request 30-60 sec le sakta hai), 750 ghante/mahina.
- GitHub repo public banta hai (taaki Render bina extra permission ke padh sake): koi secret nahi jata par code sabko dikhta hai.
- Vercel naam kisi aur ne le rakha ho to address `naam-xxxx.vercel.app` ho sakta hai; "Deployment Protection" ON ho to `/api` block ho sakta hai: error mein yahi likha aata hai.

## Haath se, step by step (agar CLI use na karni ho)
Admin mein **Website > Website jodo: Vercel + Render** (`/website/connect`). Yahan sirf token paste karte ho, baaki project API se karta hai.
1. **Vercel token** (vercel.com/account/tokens) paste karo -> "Jodo aur check karo". Tumhare Vercel projects list mein aa jate hain.
2. **Render API key** (Render Account Settings > API Keys) paste karo -> "Jodo aur check karo". Apni web services list mein aati hain. Chuno **ya** nayi free service banao (GitHub repo URL do; Render ko us repo ka access chahiye).
3. "Settings Render par bhejo": PUBLIC_URL, KEEPALIVE_URL, SHOP_ORIGINS apne aap jate hain; ADMIN_PASSWORD, DATABASE_URL (Neon), CJ_API_KEY yahin type karo, seedha Render ko jate hain, is app mein save nahi hote.
4. Vercel project chuno ya naya naam do -> "Dukaan Vercel par deploy karo". Dukaan `naam.vercel.app` par khulti hai aur uska address project mein "chuni hui website" ban jata hai.
Tokens encrypted save hote hain aur dobara screen par nahi dikhte. Dusri dukaan chunni ho to dusra Vercel project chuno aur dobara deploy karo.
**Sach:** ye Vercel/Render API calls sandbox se live test nahi hui (tests nakli server se hain). Pehla asli run hi asli test hai; galti aaye to page par Vercel/Render ka message dikhta hai. Free Render par DB disk nahi bachti, isliye Neon/Supabase Postgres zaroori hai.
Dhyan: Render par chalne wala server is code ka alag instance hai (apna database). Asli dukaan wahi hai; PC wala sirf rehearsal ke liye.

## Haath se (agar page use na karna ho)
### Step 1: Database (Neon, 3 min)
neon.tech par free account -> project -> connection string copy karo (`postgresql://...?sslmode=require`).

### Step 2: Code GitHub par
Ye poora folder ek GitHub repo me push karo (private chalega). `local.env` ko push MAT karo (usme key hai, `.gitignore` me hai).

### Step 3: Render (backend)
1. render.com -> New -> Blueprint -> repo chuno (`render.yaml` apne aap padh leta hai).
2. Env vars bharo: `ADMIN_PASSWORD` (lamba), `DATABASE_URL` (Neon), `CJ_API_KEY`, `PUBLIC_URL` (= `https://<service>.onrender.com`).
3. Deploy. `https://<service>.onrender.com/ping` par `ok` dikhe to ready. `/` par admin login.
4. `DRY_RUN=true` rehearsal mode hai: pehle sab dekh lo, phir Playbook page se off karo.

### Step 4: Vercel (dukaan)
Dukaan ki files `api/app/storefront/` mein hain (alag se `cheapedeal-storefront.zip` bhi milta hai).
1. `api/app/storefront/vercel.json` mein `cheapedeal.onrender.com` ko apne Render URL se badlo.
2. vercel.com -> Add New Project -> repo -> **Root Directory = `api/app/storefront`**, Framework = Other, build command khali. (Ya zip ka folder Vercel par drag-and-drop.)
3. Dukaan ka naam, shipping, payment: admin ke **Website** page par. Products: Products page par selling price daalo, phir "website par lagao".
Design badalni ho to: `cd frontend && npx tailwindcss -c ../api/app/storefront/tailwind.config.js -i ../api/app/storefront/input.css -o ../api/app/storefront/tailwind.css --minify`.

## Step 5: Keep-alive
GitHub repo -> Settings -> Secrets and variables -> Actions -> **Variables** -> `BACKEND_URL` = Render URL. Actions tab me `keepalive` ko ek baar "Run workflow" karo.

## Payment (zaroori)
Engine supplier ko tab hi paisa deta hai jab order **paid** ho. Website page par jitne tarike chaho on karo; customer checkout par chunta hai.

| Tarika | Fee | Currency | Paid kaise hota hai |
|---|---|---|---|
| **UPI** | 0 | sirf INR | Customer QR/link se pay karta hai, tum bank app dekh ke **Mark paid** dabate ho (customer UTR bhejta hai to Alert aata hai) |
| **Manual** | 0 | koi bhi | Tum paisa lo (bank/PayPal/kuch bhi), **Mark paid** |
| **Razorpay** | Razorpay ki fee | unke account ke hisaab se | Signature + webhook (amount match) |

Crypto aur BTCPay is version (v12) mein **hata diye** gaye hain. Sach: card payment bina card processor ke nahi ho sakta aur har processor fee leta hai; "bina commission" sirf UPI aur bank/manual se possible hai.

**Razorpay:** Key ID + Secret (encrypted). Webhook URL `https://<render>/shop/razorpay/webhook`, event `payment.captured`.

## Currency
System ka base USD hai (CJ ka cost bhi USD). Customer apni currency chunta hai (Website page par `USD,INR,EUR...`), price **server** par exchange rate se badalti hai aur order me wahi rakam lock hoti hai. Rate: Strategy page ya Website page ka "Exchange rates refresh" (free feed, har 12 ghante apne aap). Kisi currency ka rate haath se daalo to wo kabhi overwrite nahi hota.
Limit: 0-decimal currencies (jaise JPY) ki rounding alag hoti hai, abhi 2 decimal maani jati hai.

## Refund
Website page par paid order ke saamne **Refund**: Razorpay ho to paisa API se wapas, baaki me tum khud UPI/bank se bhejte ho (app sirf record karta hai). Engine ka order bhi cancel hota hai; agar CJ par order lag chuka ho to Alert batata hai ki CJ dashboard se bhi cancel karo.

## Legal pages
`privacy.html`, `terms.html`, `refund.html`, `shipping.html` storefront mein hain. Naam, email, address aur delivery ke din Website page se apne aap bharte hain. Ye **template** hai: apne business ke hisaab se text badlo aur kisi jaankar se check karwa lo.

## Customer support, feedback, reviews
- Dukaan par **Help / report a problem** (order problem, return, sawal, feedback + rating). Order number + wahi email dene par ticket order se jud jata hai.
- Customer ko ticket ka secret link milta hai (`/#ticket=...`), wahan jawab padhta aur reply karta hai.
- Admin: **Support** page. Jawab do, band/dobara kholo. Email page connected + customer emails ON ho to customer ko email bhi jata hai (link ke liye Website page par "Dukaan ka address" bharo). Naya ticket Alert banata hai; 24 ghante se unanswered ho to warning aati hai.
- **Reviews:** sirf paid order wale (order number + email check, ek product ka ek hi review per order). Tumhare approve karne ke baad hi dikhta hai, aur product card par ★ rating aati hai.
- Spam: hidden honeypot field, rate limit, text length limit, sab plain text.

## CJ wallet
CJ order apne wallet se pay karta hai. Server har 6 ghante balance dekhta hai aur limit (default $20) se kam hone par Alert deta hai. (Balance wala CJ endpoint live check nahi hua.)

## Project ka poora control (autonomous)
Website sirf engine ki Products table dikhati hai. Isliye engine jo karta hai wahi dukaan par dikhta hai: Playbook ki pricing rules, stock sync (stock 0 = apne aap hide), auto-hide, naye products import. Paid order -> Orders pipeline -> CJ order -> tracking -> customer ka `/#track=...` page. Alerts (naya order, amount mismatch) Alerts page par aate hain.

## Jo maine live test NAHI kiya
Render, Vercel, Neon, CJ, Razorpay live. Code tests (signature, pricing, chain matching, order flow) mock par pass hain. Pehle `DRY_RUN=true` rakho aur ek chhota crypto/UPI order khud karke dekho (testnet ya bahut chhota amount).
