# VPS Watchtower

一个用于**私有部署**的网站内容变化监控 MVP：Flask 应用、SQLite 数据库、Docker Compose，可通过 Telegram、Email、Discord 和通用 Webhook 发送通知。

> 这是 MVP：使用 Flask 内置服务器，SSRF 防护并不完整。请仅在可信、受控环境部署，不要把它当作公网上的通用 URL 监控服务。

## 快速开始

```bash
cp .env.example .env
# 编辑 .env：至少设置 APP_SECRET、ADMIN_PASSWORD；按需配置通知变量
chmod 600 .env
mkdir -p data
# 默认部署（不要与 prod 文件合并）
sudo docker compose -f docker-compose.yml up -d --build

sudo docker compose -f docker-compose.yml port watchtower 8080
```

默认 Compose 将容器 `8080/tcp` 发布到宿主机 `10000–65535/tcp` 中由 Docker 自动分配的一个空闲端口（不保证密码学随机）。该发布默认绑定主机所有接口。查询实际端口必须使用上面的命令；浏览器访问 `http://服务器IP:实际端口`（不要使用输出中的 `0.0.0.0` 或 `[::]` 作为访问地址）；重建后端口可能变化。**只放行查询到的实际端口**，并同步核对云安全组及 Docker `DOCKER-USER` 规则；端口分配本身不等于安全措施，也不要宣称它能替代认证、防火墙或 HTTPS。

生产 HTTPS 部署必须单独使用生产文件，不能把两个 Compose 文件合并：

```bash
sudo docker compose -f docker-compose.prod.yml up -d --build
```

生产模式由 Caddy 监听 `80/443`，通过 `DOMAIN` 自动申请/续期 HTTPS，并将内部 `watchtower:8080` 反向代理；请先确认 DNS 指向服务器并放行 80、443。详见 [DEPLOY_CN.md](DEPLOY_CN.md)。

## 配置与数据

- `.env.example` 只包含空值或占位符；复制后编辑 `.env`，不要提交 `.env`。
- `APP_SECRET`、`ADMIN_PASSWORD` 必须替换为高强度随机值；不要把监控 URL、秘密或数据库文件写入仓库。
- 数据库默认持久化到 `./data/watchtower.db`；备份时保留 `data/` 与服务器上的 `.env`。
- 源码当前读取的环境变量见 `.env.example`；数据库中的站点 URL、选择器和通知渠道仍由登录后的管理界面配置。

## Telegram 汇总行为

每次非空的到期检查周期、手动检查单站或“检查全部”都会发送**一条逻辑汇总**，汇总本轮结果与全部启用的 Telegram 站点：首次建立基线、内容未变、内容变化、错误；并标出本轮未检查站点的最新状态。超长消息会自动分段。Telegram 发送失败不会改变检查状态；其他已配置渠道仍按变化事件发送通知。

## 离线单元测试

测试会 mock HTTP 与通知发送，不会实际请求监控网站，也不会实际发送 Telegram/Email/Discord/Webhook：

```bash
# Debian 13 首次准备测试环境：sudo apt install -y python3 python3-venv
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest -v
```

测试依赖安装需要网络；安装依赖后，测试本身完全离线。
