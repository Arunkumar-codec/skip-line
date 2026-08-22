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

# 3. Generate SSL certificate (Let's Encrypt)
apt install certbot
certbot certonly --standalone -d yourdomain.com
cp /etc/letsencrypt/live/yourdomain.com/fullchain.pem nginx/ssl/cert.pem
cp /etc/letsencrypt/live/yourdomain.com/privkey.pem   nginx/ssl/key.pem

# 4. Uncomment HTTPS redirect in nginx/nginx.conf

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

```nginx
# Add to the server block in nginx/nginx.conf
listen 443 ssl http2;
ssl_certificate     /etc/nginx/ssl/cert.pem;
ssl_certificate_key /etc/nginx/ssl/key.pem;
ssl_protocols       TLSv1.2 TLSv1.3;
ssl_ciphers         ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256;
ssl_session_cache   shared:SSL:10m;
```

---

## Database Backup

```bash
# Daily backup script
cat > /etc/cron.daily/skipline-backup << 'EOF'
#!/bin/bash
DATE=$(date +%Y%m%d)
mysqldump -u skipline_user -p"$DB_PASSWORD" skipline \
  | gzip > /backups/skipline_$DATE.sql.gz
# Retain 30 days
find /backups -name "skipline_*.sql.gz" -mtime +30 -delete
EOF
chmod +x /etc/cron.daily/skipline-backup
```

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
