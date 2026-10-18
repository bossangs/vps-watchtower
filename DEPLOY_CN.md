# VPS Watchtower 多用户版安装与更新

本说明供你手动审阅后操作。本次交付没有登录你的服务器、部署应用或推送 GitHub。示例目录为 `/opt/vps-watchtower-package`，系统以 Debian 12／13 为例。

## 1. 准备 Docker 和服务器

需要 Docker Engine、Compose v2 插件、可用磁盘及备份位置。先运行：

```bash
sudo docker version
sudo docker compose version
```

未安装时，按 [Docker 官方 Debian 安装说明](https://docs.docker.com/engine/install/debian/) 设置官方 apt 仓库。仅在确认系统是 Debian、并检查旧容器运行情况后执行：

```bash
cat /etc/os-release
sudo apt update
sudo apt install -y ca-certificates curl python3 python3-venv rsync unzip
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run --rm hello-world
```

如果已有 `docker.io`、`podman-docker` 等冲突包，先按官方说明评估并处理，不要直接卸载正在承载其他业务的组件。Docker 发布端口可能绕过 ufw／firewalld，必须同时核对云安全组和 Docker 防火墙链。

## 2. 新装与环境配置

解压后把 `vps-watchtower-package` 整个目录（含隐藏文件）放到 `/opt`。以下命令仅用于**新安装**，已有实例请直接看“旧版更新”。

```bash
cd /opt/vps-watchtower-package
cp .env.example .env
chmod 600 .env
mkdir -p data
sudo chown -R 10001:10001 data
sudo chmod 700 data
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

将最后生成的随机值写入 `.env` 的 `APP_SECRET`。至少检查这些配置：

| 配置 | 含义 |
|---|---|
| `APP_SECRET` | 至少 32 字符的随机密钥；占位值会导致启动失败 |
| `DOMAIN` | HTTPS 使用的纯域名，如 `monitor.example.com`，不能带协议或路径 |
| `REGISTRATION_ENABLED` | 默认 false；完成验收后改 true 开放注册 |
| `MAX_USERS` | 用户总量上限，默认 1000 |
| `DATABASE_PATH` | 容器内默认 `/data/watchtower.db`，对应宿主 `./data/watchtower.db` |
| `TELEGRAM_BOT_TOKEN` | 服务器共享 Bot 的 Token，可暂留空 |
| `TELEGRAM_CHAT_ID` | 可选，仅供管理员原版全站汇总使用，不是所有用户的接收地址 |
| `DEFAULT_INTERVAL` | 新监控默认间隔，最少 60 秒 |
| `DISABLE_SCHEDULER` | 正常运行 false；执行 CLI 时通过 `-e` 临时设 true |

`ADMIN_PASSWORD` 仅为旧配置兼容保留，不再是网页登录密码；请用下面的管理员创建命令。不要把 `.env`、data、备份上传仓库。

### 推荐：HTTPS 生产模式

先让域名 DNS A／AAAA 指向服务器；只在实际配置 IPv6 时保留 AAAA。80/tcp 与 443/tcp 不能被其他网站进程占用；HTTP/3 的 443/udp 可选。已有宝塔 Nginx／其他反向代理时，不要直接启动抢占 80/443 的 Caddy，可按后文使用随机端口后自行配置受信任代理。

```bash
cd /opt/vps-watchtower-package
sudo docker compose -f docker-compose.prod.yml config --quiet
sudo docker compose -f docker-compose.prod.yml build
# 交互输入并再次确认管理员密码，长度 10–128 字符
sudo docker compose -f docker-compose.prod.yml run --rm -e DISABLE_SCHEDULER=true watchtower flask --app app create-admin --username admin
sudo docker compose -f docker-compose.prod.yml up -d
sudo docker compose -f docker-compose.prod.yml ps
sudo docker compose -f docker-compose.prod.yml logs --tail=80 watchtower caddy
```

访问 `https://你的域名/healthz` 应返回 `{"status":"ok"}`，随后到 `/login` 使用管理员用户名和刚才设置的密码登录。生产编排强制启用 Secure Cookie；不要在 HTTP 下测试登录。

Caddy 自动申请和保存证书到 Docker named volumes。**不要执行 `down -v`**，也不要随意更改 Compose project 名称，否则可能切换到不同证书卷。数据始终是宿主目录 `./data`。

### 保留：随机宿主端口模式

默认编排从 `10000–65535/tcp` 范围分配一个可用端口，容器仍监听 8080；非密码学随机，重建后可能变化。为了不让公网直接传输登录密码，现默认仅绑定 `127.0.0.1`。

```bash
sudo docker compose -f docker-compose.yml config --quiet
sudo docker compose -f docker-compose.yml build
sudo docker compose -f docker-compose.yml run --rm -e DISABLE_SCHEDULER=true watchtower flask --app app create-admin --username admin
sudo docker compose -f docker-compose.yml up -d
sudo docker compose -f docker-compose.yml port watchtower 8080
```

假设查到 `127.0.0.1:32567`，在自己电脑上建立隧道：

```bash
ssh -L 18080:127.0.0.1:32567 user@SERVER
```

浏览器访问 `http://127.0.0.1:18080`。该编排专为隧道验收设置 `COOKIE_SECURE=false`。端口输出不是密码，随机端口也不替代身份验证。

若改由现有 Nginx 代理该端口，必须在公网启用 HTTPS，并把编排里的 `COOKIE_SECURE` 改为 true 后重建；不要仅修改 `.env`，因为 Compose 的 environment 会覆盖它。应用默认不信任 `X-Forwarded-For`，反代后 IP 限流会共享代理 IP；不要为获取真实 IP 而无条件启用 ProxyFix。

切换两种模式前，使用当前模式的文件执行 `down`（不加 `-v`），再启动另一种。**不要将两份 Compose 文件叠加。**

## 3. 配置商家、套餐和两类网址

1. 管理员进入“商家管理”，填写名称、官网和原创介绍。商家独立公开页在 `/providers/ID`。
2. “通知点击跳转的商家／购买链接”是公开的 `push_url`，可与官网不同。个人 Telegram 通知和订阅汇总优先使用此地址；为空时使用对应套餐监控网址。不能把含密钥的 Webhook 填在这里。
3. 进入“监控管理”，添加名称、公开 HTTP(S) 网址、CSS 选择器、检查间隔和库存关键词。选择器必须由你根据可公开访问的页面设置，不要求商家提供专用接口。
4. 打开“套餐／发布设置”，关联商家，填写地区、CPU、RAM MB、磁盘 GB、流量 GB、价格、币种和周期。建议统一周期用 `月` 或 `年`；搜索按同币种、同周期比较，不自动换汇。
5. 设置正向库存词和售罄词。售罄词优先，英文按词边界匹配。整页存在多个套餐状态时必须缩小 CSS 选择器，否则库存结果可能失真。
6. 手动检查建立基线，再审核并勾选“发布到公开目录”。未发布监控只有管理员可见。公开后名称、套餐网址、目录字段、关键词生成的事件摘要将可见，不应含任何敏感信息。
7. 商家“Webhook 设置与记录”是另一独立管理入口，填写商家接收通知的 HTTPS 地址及事件类型；URL 不公开。先配置固定名称 `PROVIDER_WEBHOOK_SECRET_ID` 的至少 32 字符密钥，再启用。完整格式见 [WEBHOOK_CN.md](WEBHOOK_CN.md)。

`restock` 为已知售罄→有货；`sellout` 为已知有货→售罄；其他文本变化记为 `change`。首轮只建基线，不捏造补货事件。HTTP 错误保留上次库存状态，检查状态则标记 ERROR。遇到 403／登录／验证码时应换成商家允许读取的公开来源或暂停，不尝试绕过。

管理员 CSV 保留原版字段：`name,url,interval_seconds,selector,keywords,price_threshold,stock_keywords,channels,enabled`（导入主要使用原版监控字段，发布和商家元数据请在后台设置）。导出对可能被电子表格当成公式的文本加前缀防护；它不是数据库备份，也不包含用户和商家设置。

## 4. 用户注册、订阅和 Telegram

管理员验收后把 `.env` 的 `REGISTRATION_ENABLED=true`，使用**当前模式**执行 `up -d --force-recreate`。普通注册用户不会获得管理员权限。用户名是 3–64 位小写字母、数字、点、下划线或连字符；密码长度 10–128 位，不需要邮箱。

共享 Bot 由站点管理员使用 Telegram 官方 BotFather 创建，将 Token 填入服务器 `.env` 后重建。普通用户不填写 Token：

1. 在 Telegram 与此 Bot 私聊并点击 Start／发送 `/start`，否则 Bot 不能主动发送。
2. 获得自己的正整数私聊 chat_id。管理员可以在自己受控的服务器上通过官方 `getUpdates` 响应确认 `message.chat.id` 并告知对应用户；不要公开这些响应或 Token。用户也可用其信任的方法获知自己的 ID。应用不自动占用 Bot 的 polling/webhook 接收通道。
3. 登录网站，进入“个人设置→Telegram”，填 chat_id，发送验证码，在网站回填收到的六位码。验证码有效期 10 分钟、最多 5 次尝试，发送冷却 60 秒；只允许私聊，暂不支持群组。
4. 从商家或套餐详情页订阅，在“我的订阅”中设置开启／暂停、事件类型和是否汇总。个人 Telegram 偏好可进一步全局关闭补货、售罄、变化或汇总。
5. 使用测试消息确认绑定，再检查个人发送记录。解绑会停止该用户接收 Telegram。

个人通知只涉及自己订阅且公开、启用中的套餐。订阅套餐又订阅其商家时，同一事件不会重复发。个人汇总不会包含其他用户的监控范围；原版 `TELEGRAM_CHAT_ID` 仍接收管理员全站汇总，两者互不替代。

忘记密码或需要人工封禁时，管理员在服务器执行（生产示例）：

```bash
sudo docker compose -f docker-compose.prod.yml exec -e DISABLE_SCHEDULER=true watchtower flask --app app reset-password 用户名
sudo docker compose -f docker-compose.prod.yml exec -e DISABLE_SCHEDULER=true watchtower flask --app app disable-user 用户名
```

密码交互输入，不写命令行参数。改密码、重置密码、封禁都会撤销旧会话。创建管理员的 CLI 会显式提升同名账户，请核对用户名。`bootstrap` 是 `create-admin` 的交互别名，不会在启动时自动执行。

## 5. 从原版升级：先备份，再迁移

新版不沿用单一管理员密码登录，须手动创建管理员；旧监控记录不会自动变成任何普通用户的私有资源。它们保留为管理员维护的共享目录来源，并默认不公开。

以下例子假定旧实例使用 `docker-compose.yml`；生产实例应将命令中的文件名全换为 `docker-compose.prod.yml`。

```bash
cd /opt/vps-watchtower-package
sudo docker compose -f docker-compose.yml stop
# 完整备份包含 .env、SQLite 的 db/WAL/SHM、旧源码和编排，不上传公开位置
sudo install -d -m 700 /var/backups/watchtower
stamp=$(date -u +%Y%m%dT%H%M%SZ)
sudo tar -czf "/var/backups/watchtower/full-$stamp.tgz" -C /opt vps-watchtower-package
sudo chmod 600 "/var/backups/watchtower/full-$stamp.tgz"
```

先把新包解压到临时目录，审阅后替换所有源码、模板、requirements、Dockerfile、编排及说明，**不覆盖真实 `.env`、`data/` 或备份**。例如临时目录已是 `/tmp/new-release/vps-watchtower-package`：

```bash
sudo rsync -a --exclude='.env' --exclude='data/' --exclude='backups/' --exclude='.venv/' --exclude='__pycache__/' /tmp/new-release/vps-watchtower-package/ /opt/vps-watchtower-package/
cd /opt/vps-watchtower-package
# 将 .env.example 新配置手动合入现有 .env，保持真实 Token/渠道设置
# 确保 APP_SECRET 达到长度要求；旧 ADMIN_PASSWORD 不能再直接登录
sudo chown -R 10001:10001 data
sudo chmod 700 data
sudo docker compose -f docker-compose.yml build
sudo docker compose -f docker-compose.yml run --rm -e DISABLE_SCHEDULER=true watchtower flask --app app create-admin --username admin
sudo docker compose -f docker-compose.yml up -d
sudo docker compose -f docker-compose.yml ps
```

应用导入时会幂等新增表、列及索引。旧 sites/snapshots/events/notifications 保留原 ID 和内容，新列 `public` 默认 0、库存状态 unknown。首次后续检查建立或更新状态；已有基线仍用于变化比较。旧 events 的 kind 默认 change，但公开查询排除管理员全站汇总。

升级后核对：旧站点数量、快照与事件、通知环境是否仍有配置、随机实际端口、管理员登录、普通用户无管理权限；逐项审核后再发布套餐、开放注册和启用商家 Webhook。没有公网 DNS／证书时先用隧道模式，不要把 HTTP 登录直接开放公网。

## 6. 备份、回滚与日常维护

可用附带工具生成一致性 SQLite 备份（不导入应用，不触发迁移或监控）：

```bash
# 宿主机用户需有读取 data 的权限；建议先停应用再操作
sudo python3 manage_db.py check data/watchtower.db
sudo python3 manage_db.py backup data/watchtower.db "/var/backups/watchtower/sqlite-$(date -u +%Y%m%dT%H%M%SZ).db"
```

目标文件已存在会拒绝覆盖。数据库含密码哈希、chat_id、监控内容和 Webhook 地址；备份应加密保存、限制读取，并与代码／.env 的版本一一对应。

**回滚应恢复升级前完整备份，不要只回滚 app.py。** 停止新版服务，将当前目录移到另一个受保护位置，解压备份恢复旧源码、`.env` 和完整 data（包括当时的 WAL/SHM），再使用旧编排启动。若决定仅恢复单文件一致性 DB 备份，必须先停服务并把原 data 目录整体移开，再在新 data 内放回备份，避免混用旧 WAL；恢复后修正运行 UID 所需权限。回滚会丢失备份之后的新账户／订阅／事件，先另存当前完整状态。

当前版本为保留旧历史，**不自动删除快照、事件和审计**。高频检查会使 SQLite 和备份增长，应监控磁盘、合理设置间隔，定期备份并自行制定经过审核的保留策略；不要在运行中直接删除 WAL 文件。监控和发送串行执行，站点／订阅很多或目标很慢时会延迟，不承诺实时送达。

## 7. 验收和排错

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest -v
```

测试安装依赖需联网，测试本身不外发。`TEST_RESULTS.md` 记录实际执行的验证，不把未跑的 Docker 构建或真实 Telegram 投递说成成功。

- **启动提示密钥不合规**：替换 APP_SECRET 占位符并检查 `.env` 是否真的加载。
- **unable to open database / permission denied**：检查容器挂载、data UID 10001 和目录权限，勿用 chmod 777。
- **登录后又退出**：检查 HTTP／HTTPS 与 Cookie Secure 是否匹配；浏览器清理旧 Cookie 再试。
- **CSRF 400**：刷新页面获取新令牌；多标签页在另一页登录／退出后，旧表单会失效。
- **普通账户打开管理页面 403**：这是权限保护；管理员须由 CLI 显式创建。
- **Webhook failed/pending**：检查 HTTPS 公网地址、签名密钥、接收端日志和幂等处理；不自动重试。
- **403、验证码、selector 未匹配**：查看管理员最近快照与错误，修正规则或使用商家允许的来源，不绕过访问保护。
- **Caddy 证书失败**：核对 DNS、错误 AAAA、80/443 占用、安全组及 Caddy 日志。

不要增加 Gunicorn worker 数、开 debug reload、`--preload` 或同时运行多份相同数据库的应用。若将来需要高并发、可靠队列或多实例，请先拆分调度器和投递队列，再迁移数据库。
