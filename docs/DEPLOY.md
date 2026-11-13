# Deploy on a VPS (Ubuntu 22.04 or 24.04)

Needs about 2 vCPU and 4 GB RAM. Oracle Cloud "Always Free" ARM works (all images used have arm64 builds), but free-tier terms and capacity change, so check them before you sign up.

## 1. Server basics

```bash
# as root, once
adduser deploy && usermod -aG sudo deploy
# copy your SSH public key to /home/deploy/.ssh/authorized_keys, then:
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/; s/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart ssh      # test a second SSH session BEFORE closing the first
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw enable
apt update && apt install -y docker.io docker-compose-v2 git unattended-upgrades
usermod -aG docker deploy
```

Oracle Cloud also has its own network rules: open ports 80 and 443 in the VCN security list.

## 2. Domains

Two names pointing to the server's public IP, for example `deal.yourdomain.com` (dashboard) and `n8n.yourdomain.com`. Free option: DuckDNS subdomains. Caddy gets HTTPS certificates automatically once DNS resolves.

## 3. Install

```bash
git clone <your repo> cheapedeal && cd cheapedeal
cp .env.example .env
python3 scripts/gen_secrets.py     # paste into .env
nano .env
```

Set in `.env`: `APP_ENV=production`, `ENABLE_DEV_TOOLS=false`, `APP_DOMAIN`, `N8N_DOMAIN`, `PUBLIC_URL=https://<APP_DOMAIN>`, `N8N_PROTOCOL=https`, `N8N_HOST=<N8N_DOMAIN>`, `N8N_WEBHOOK_URL=https://<N8N_DOMAIN>/`, `N8N_SECURE_COOKIE=true`, and keep `DRY_RUN=true`.

```bash
docker compose --profile prod up -d --build
docker compose ps
curl -s https://<APP_DOMAIN>/health        # {"ok":true}
```

The app refuses to start with weak or missing secrets and will tell you which ones.

## 4. Connect the real services (one at a time, in DRY-RUN)

1. **n8n**: open `https://<N8N_DOMAIN>`, create the owner account, import and activate the workflows (see `SETUP_WINDOWS.md`, section B).
2. **WooCommerce**: Settings > Advanced > REST API > Add key (Read/Write). Put key and secret in `.env`. Settings > Advanced > Webhooks > Add: topic **Order updated**, delivery URL `https://<APP_DOMAIN>/webhooks/woocommerce`, a long random **Secret** (same value as `WOO_WEBHOOK_SECRET`), API version "WP REST API Integration v3". Set `STORE_BACKEND=woocommerce`.
3. **CJ Dropshipping**: put `CJ_API_KEY=` in `.env` (or paste the key in Connect). The app connects itself at start-up.
4. `docker compose up -d` to apply `.env`. Settings page should show "WooCommerce".
5. Add your real products (Products page): store product ID, CJ variant ID (`vid`), cost, price.
6. Place a **test order in your store** (cash-on-delivery or a 100% coupon, set to `processing`). In DRY-RUN it must appear in Orders with a Rehearsal badge.
7. Go through `docs/ASSUMPTIONS.md` "Not verified". Then and only then: Settings > shipping method name > Go live, with a **low daily cap (5)** and **approval threshold low** for the first week.

## 5. Backups

```bash
crontab -e     # add:
15 3 * * * cd /home/deploy/cheapedeal && ./scripts/backup.sh >> backups/backup.log 2>&1
```

Keep a copy of `.env` and especially `N8N_ENCRYPTION_KEY` outside the server. Test a restore once: `./scripts/restore.sh cheapedeal backups/<file>.sql.gz`.

## 6. Updating

```bash
git pull && docker compose --profile prod up -d --build
```

Database tables are created on start. A schema change in a later version needs a migration, which this version does not include (see "Known gaps").

## Known gaps

- No Alembic migrations yet: tables are created with `create_all`. Fine for a fresh install; plan migrations before you change columns in production.
- Single admin user, in-memory login rate limit (resets on restart).
- n8n image is `latest` in compose. After your first successful boot, pin the exact version.

## Without Docker (VPS, small server, or any Linux box)
```
git clone / unzip the project, then:
./run_linux.sh --setup-only          # one time: creates the environment and the settings file
sudo cp deploy/cheapedeal.service /etc/systemd/system/   # edit User and paths inside
sudo systemctl daemon-reload && sudo systemctl enable --now cheapedeal
sudo journalctl -u cheapedeal -f     # the first start prints the admin password
```
The agents run inside the app (no n8n needed). It listens on 127.0.0.1 by default: for the internet set `CHEAPEDEAL_HOST=0.0.0.0` and `CHEAPEDEAL_PUBLIC_URL=https://yourdomain` and put HTTPS in front (Caddy, nginx or a Cloudflare Tunnel). Never expose the login over plain HTTP. `deploy/cheapedeal.service` was written from the systemd documentation and was not run on a real server in the build environment.

## Windows autostart
`scripts\windows_autostart.bat` creates a scheduled task that starts Cheape Deal when you log in (not tested on a real Windows PC).
