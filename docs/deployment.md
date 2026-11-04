# Skip Line — Deployment Guide

## Option 1: Docker Compose (Recommended)

### Prerequisites
- Docker 24+
- Docker Compose 2.20+
- A domain name with DNS pointing to your server

### Steps

```bash
# 1. Clone the repository
git clone <repo-url> /opt/skip-line
cd /opt/skip-line

# 2. Configure environment
cp .env.example .env
nano .env
# Set all required variables (see README)
# Set APP_ENV=production, APP_DEBUG=false

# 3. Obtain and stage TLS certificates (stay out of Git — nginx/ssl is
#    committed but .gitignored)
sudo certbot certonly --standalone -d yourdomain.com
sudo cp /etc/letsencrypt/live/yourdomain.com/fullchain.pem nginx/ssl/
sudo cp /etc/letsencrypt/live/yourdomain.com/privkey.pem  nginx/ssl/

# 4. Switch compose nginx to the production config (in .env):
#      NGINX_CONF=nginx.production.conf

# 5. Build and start
docker compose up --build -d

# 6. Check health
curl http://localhost/health
docker compose ps
docker compose logs -f app
```

### Updating

```bash
git pull
docker compose build app
docker compose up -d app
docker compose exec app alembic upgrade head
```

---

## Option 2: Manual (Ubuntu 22.04)

```bash
# Install system dependencies
apt update && apt install -y python3.12 python3.12-venv mysql-server nginx certbot

# Create app user
useradd -r -s /sbin/nologin skipline
mkdir -p /opt/skip-line
chown skipline:skipline /opt/skip-line

# Setup MySQL
mysql_secure_installation
mysql -u root -p << 'SQL'
CREATE DATABASE skipline CHARACTER SET utf8mb4;
CREATE USER 'skipline_user'@'localhost' IDENTIFIED BY 'StrongPassword!';
GRANT ALL ON skipline.* TO 'skipline_user'@'localhost';
SQL

# Install application
su - skipline
cd /opt/skip-line
git clone <repo> .
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
.venv/bin/alembic upgrade head

# Systemd service
cat > /etc/systemd/system/skipline.service << 'EOF'
[Unit]
Description=Skip Line Application
After=network.target mysql.service

[Service]
User=skipline
WorkingDirectory=/opt/skip-line
ExecStart=/opt/skip-line/.venv/bin/uvicorn app.main:app \
    --host 127.0.0.1 --port 8000 --workers 4
Restart=always
RestartSec=5
EnvironmentFile=/opt/skip-line/.env

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now skipline
```

---

## Nginx Configuration (SSL)

Phase 8 ships a production nginx config; the dev default stays HTTP:

```text
nginx/nginx.conf                # dev: HTTP on :80 (used by default)
nginx/nginx.production.conf     # prod: 443 TLS, HTTP→HTTPS redirect, HSTS
```

Select the production config with the `NGINX_CONF` variable (compose
mounts `./nginx/${NGINX_CONF:-nginx.conf}`):

```bash
# .env
NGINX_CONF=nginx.production.conf
```

**Certificates stay out of Git.** Obtain them first (certbot example),
then mount them into `./nginx/ssl/` — the directory is committed but its
`nginx/ssl/.gitignore` ignores all contents except itself:

```bash
# stop the nginx container first if renaming live files
# (with the running compose stack, webroot renewal works because the
#  nginx service mounts ./nginx/webroot at /var/www/certbot)
sudo certbot certonly --webroot -w /opt/skip-line/nginx/webroot -d queue.example.com
cp /etc/letsencrypt/live/queue.example.com/fullchain.pem nginx/ssl/
cp /etc/letsencrypt/live/queue.example.com/privkey.pem  nginx/ssl/
chmod 600 nginx/ssl/privkey.pem
docker compose --profile backup run --rm backup   # snapshot before switching
docker compose up -d nginx
```

Production behavior of `nginx.production.conf` (validated with
`nginx -t` against nginx 1.26):

- HTTP (:80) serves only ACME renewal challenges, everything else 301s
  to HTTPS;
- TLS 1.2/1.3 only, modern cipher list, session cache, no session
  tickets, `http2 on`;
- the auth path regex and `/api/` keep the same `limit_req` zones as dev;
- security headers (CSP, `X-Frame-Options`, `nosniff`,
  `Referrer-Policy`, `Permissions-Policy`) are set by the FASTAPI
  middleware (see `app/middleware/headers.py`) and pass through nginx
  untouched — nginx adds only HSTS, kept on the TLS listener alone;
- `X-Forwarded-Proto https` is forwarded to the app.

Both configs are checked by `nginx -t` in CI/manual verification; the
production file additionally needs the cert/key pair present or nginx
refuses to start (intentional fail-closed behavior).

---

## Database Backup

Phase 8 ships real backup/restore tooling instead of a copy-paste cron
snippet:

```bash
# logical dump → ./backups/skipline_YYYYMMDDTHHMMSSZ.sql.gz (gitignored)
# runs mysqldump inside a mysql:8.3 one-shot container; keeps newest 7
make backup

# validate an artifact into a scratch database first (non-destructive)
make restore FILE=backups/skipline_20261104T120000Z.sql.gz

# real restore (DESTRUCTIVE — drops & rebuilds; 5s Ctrl+C window)
make restore FILE=backups/skipline_20261104T120000Z.sql.gz MODE=force
```

Container equivalent: `docker compose run --rm backup` (the `backup`
service carries the `backup` compose profile, so plain `up` never starts
it). `scripts/backup.sh` refuses to call a <1 KB dump a successful
backup and prints its sha256; retention (`BACKUP_KEEP`, default 7) and
the minimum-size gate (`BACKUP_MIN_BYTES`, default 1000) are env-tunable.

Schedule it from the host cron (the compose service is one-shot by
design):

```cron
0 2 * * *  cd /srv/skipline && docker compose run --rm backup >> /var/log/skipline-backup.log 2>&1
```

Restore semantics, honest-claims notes and the scratch-database
verification path are documented in `scripts/restore.sh` itself.

---

## Monitoring

```bash
# Application health
curl http://localhost/health

# Docker container status
docker compose ps

# Real-time logs
docker compose logs -f --tail=100 app

# MySQL connection count
mysql -e "SHOW STATUS LIKE 'Threads_connected';"
```

---

## Rollback

```bash
# Rollback database migration one step
docker compose exec app alembic downgrade -1

# Rollback to specific revision
docker compose exec app alembic downgrade <revision_id>

# List migration history
docker compose exec app alembic history
```
