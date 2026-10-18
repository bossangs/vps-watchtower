# 1. 在 Debian 13 VPS 上确认 Docker Compose 已安装：
docker --version && docker compose version
如果提示命令不存在，先按 Docker 官方 Debian 安装步骤安装 Docker Engine 和 Compose 插件。
2. 克隆项目并设置后台密码：
apt update && apt install -y git openssl
git clone https://github.com/bossangs/vps-watchtower.git
cd vps-watchtower
umask 077
read -rsp '设置后台登录密码: ' ADMIN_PASSWORD; echo
printf 'APP_SECRET=%s\nADMIN_PASSWORD=%s\n' "$(openssl rand -hex 32)" "$ADMIN_PASSWORD" > .env
unset ADMIN_PASSWORD
chmod 600 .env

密码输入时不会显示，记好它，登录后台要用。
3. 先临时测试：
docker compose up -d --build
docker compose ps

浏览器打开 http://你的VPS公网IP:8080
。如果只是测试，防火墙最好只允许你自己的 IP 访问 8080。
4. 正式公网使用建议配域名 HTTPS。先把域名 A 记录指向 VPS，再执行下面一行，把域名换成自己的，并确保公网 TCP 80/443 已放行：
echo 'DOMAIN=monitor.example.com' >> .env
docker compose -f docker-compose.prod.yml up -d --build

然后访问 https://monitor.example.com
。这个版本还缺登录限速和完整 CSRF 防护，不建议裸露 8080 长期公网使用。完整中文说明在 DEPLOY_CN.md。VPS Watchtower

Self-hosted multi-site monitoring and notification MVP for VPS provider websites.

## Features
- Web UI for adding/editing/pausing/deleting monitored URLs
- Whole-page text diff or CSS selector extraction
- Keyword triggers, basic price/stock heuristics, and configurable polling interval
- SQLite persistence for sites, snapshots, events, and notification logs
- Multi-channel notifications: Telegram, SMTP email, Discord webhook, generic webhook
- Docker Compose deployment
- CSV bulk import/export

> This is a practical MVP, not a turnkey commercial-grade crawler. Respect each site's terms, robots.txt, rate limits, and access controls. Do not use it to bypass login, CAPTCHA, or anti-bot protections. Dynamic JavaScript pages may need a separately configured browser collector.

## Quick start (Docker)
1. Copy `.env.example` to `.env` and set `APP_SECRET` to a long random value.
2. Optionally configure one or more notification channels in `.env`.
3. Run:
   ```bash
   docker compose up -d --build
   ```
4. Open `http://YOUR_SERVER_IP:8080`.

The first run creates a local SQLite database in `./data/watchtower.db`.
For public exposure, put it behind HTTPS/reverse proxy and restrict access with a firewall. This MVP has a shared admin password; set `ADMIN_PASSWORD` before exposing it.

## Environment
- `APP_SECRET`: session signing secret
- `ADMIN_PASSWORD`: shared login password (required for login)
- `DATABASE_PATH`: SQLite file path (default `/data/watchtower.db`)
- `DEFAULT_INTERVAL`: default polling interval in seconds (300)
- `REQUEST_TIMEOUT`: HTTP timeout in seconds (20)
- `MAX_RESPONSE_BYTES`: max response body read (2 MB)
- `USER_AGENT`: crawler user agent
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TO`, `SMTP_STARTTLS`
- `DISCORD_WEBHOOK_URL`
- `GENERIC_WEBHOOK_URL`

## Monitoring configuration
A site supports:
- URL, name, interval seconds
- CSS selector (optional; blank means whole page text)
- comma-separated keywords (any keyword matching the new content triggers)
- optional price threshold (alert if parsed price is below it)
- optional stock keywords (e.g. `in stock,available,有货`)
- notification channels selected per site

The first successful check creates a baseline snapshot and does not send a change alert. Later checks create events when content changes or a configured trigger fires.

## GitHub deployment
Push this directory to a GitHub repository. For a VPS, clone the repository and run Docker Compose. The included GitHub Actions workflow builds and publishes a Docker image to GHCR on pushes to `main`; configure the package visibility if you want it public.

## Notes / limitations
- Price parsing is heuristic and can be wrong when a page contains multiple prices or currency conversions.
- Stock detection relies on configured keywords and is site-specific.
- CSS selectors use BeautifulSoup's SoupSieve support.
- Line quality, BGP route, IP ownership, and Looking Glass checks should be added as separate data-source adapters; a normal webpage diff cannot establish network performance.
- Add a reverse proxy, HTTPS, backups, and stronger user management before internet-facing production use.

## Full deployment configuration (Chinese)
See `DEPLOY_CN.md`, `docker-compose.prod.yml`, and `Caddyfile.example` for the full configuration and HTTPS deployment steps. A blank 50-site CSV template is provided at `examples/sites-template.csv`.
