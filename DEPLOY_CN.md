# VPS Watchtower 完整部署配置（中文）

## 1. 配置文件

- `.env.example`：环境变量模板。复制为 `.env` 后填写密钥与通知渠道。
- `docker-compose.yml`：直接通过 `http://服务器IP:8080` 测试用。
- `docker-compose.prod.yml` + `Caddyfile.example`：使用真实域名自动申请 HTTPS 证书的部署方式。
- `examples/sites-template.csv`：批量导入网站的 CSV 空模板。
- `examples/sites.csv`：CSV 示例（示例域名仅用于说明格式，请替换为真实网址）。
- `.github/workflows/build.yml`：推送到 GitHub `main` 分支后构建并发布 GHCR 镜像。

## 2. 首次部署

```bash
cp .env.example .env
```

生成随机 APP_SECRET（将输出复制到 `.env` 中替换 APP_SECRET）：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

编辑 `.env`，至少更改 `APP_SECRET` 和 `ADMIN_PASSWORD`。不要把 `.env` 提交到 GitHub。

### 本机 / 临时测试

```bash
docker compose up -d --build
docker compose logs -f
```

访问 `http://服务器IP:8080`。如果启用了服务器防火墙，只有在你确认要直接暴露测试端口时才开放 TCP 8080。

### 正式 HTTPS 部署

1. 准备一个域名，例如 `monitor.example.com`，将 A 记录指向 VPS 公网 IPv4；如配置了 AAAA 记录，必须确保 IPv6 也可访问。
2. `.env` 中设置 `DOMAIN=monitor.example.com`，并设置强密码和随机 APP_SECRET。
3. 确保 TCP 80/443 可从互联网访问，且没有其他服务占用这两个端口。
4. 执行：

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f
```

5. 访问 `https://monitor.example.com`。Caddy 会尝试自动申请和续期 TLS 证书。

## 3. 多渠道通知配置

### Telegram

在 `.env` 设置 `TELEGRAM_BOT_TOKEN` 与 `TELEGRAM_CHAT_ID`。先创建 Telegram Bot，并让 Bot 能向目标聊天发送消息。不要把 Token 发给他人或提交到仓库。

### SMTP 邮件

填写 `SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASSWORD`、`SMTP_FROM`、`SMTP_TO`。常见提交端口为 587（STARTTLS）或服务商指定的端口；具体值以邮件服务商为准。

### Discord

将 `DISCORD_WEBHOOK_URL` 设置为目标频道的 Incoming Webhook URL。该 URL 相当于秘密凭据。

### 通用 Webhook

将 `GENERIC_WEBHOOK_URL` 设置为接收 JSON POST 的 HTTPS 端点。当前消息体包含 `text` 和 `event` 字段。

各网站的 `channels` 字段用逗号分隔，例如：`telegram,email,discord,webhook`。没有配置凭据的渠道会记录发送失败。

## 4. 导入 50 个网站

1. 用电子表格打开 `examples/sites-template.csv`，每行填写一个网站。
2. 必填列：`name`、`url`。
3. 可选列：
   - `interval_seconds`：检查间隔，最少 60 秒。
   - `selector`：CSS 选择器；留空表示提取整页可见文本。
   - `keywords`：逗号分隔的关键词。
   - `price_threshold`：美元价格的粗略阈值，仅为启发式识别。
   - `stock_keywords`：库存词，例如 `in stock,available,有货`。
   - `channels`：`telegram,email,discord,webhook` 中的一项或多项。
4. 在后台选择“批量导入 CSV”。
5. 首次成功检查只建立基线，不发送网页变化提醒；后续内容发生变化才生成事件。

## 5. GitHub 发布

```bash
git init
git add .
git commit -m "Initial VPS Watchtower"
git branch -M main
git remote add origin https://github.com/YOUR_ACCOUNT/vps-watchtower.git
git push -u origin main
```

先在 GitHub 创建空仓库，再将 `YOUR_ACCOUNT` 替换为你的账号或组织名。Actions 使用 `GITHUB_TOKEN` 推送 GHCR 镜像。公开仓库不代表容器包一定公开；请在 GitHub Packages 设置中核实包可见性。

## 6. 重要限制与安全要求

- 当前 MVP 使用共享管理员密码，不支持多用户和角色权限。
- 当前版本没有完整 CSRF 防护；上线前应补上 CSRF 防护、登录限速和更严格的 SSRF 防护。
- 只监控允许访问的公开页面，遵守网站服务条款、robots.txt 和合理访问频率。不要尝试绕过验证码、登录或反爬机制。
- 对价格和库存的识别是启发式的，页面包含多个价格时可能误判；建议每个商家使用专属 CSS 选择器并人工验证。
- 动态 JavaScript 页面可能需要独立的浏览器采集器。
- BGP、ASN、IP 归属、Looking Glass 与线路质量必须通过独立数据源/适配器采集；网页文本变化不等于线路质量变化。
- 备份 `./data` 数据目录；更新前先停止服务并做数据库备份。
