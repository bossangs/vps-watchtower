# Debian 13（Trixie）部署与手动更新

本文只提供命令和操作指导，不执行安装、部署或推送。示例假定应用目录为 `/opt/vps-watchtower-package`，可替换为你的路径。

## 1. 前置检查

目标系统为 Debian 13（Trixie）。先确认：

```bash
cat /etc/debian_version
. /etc/os-release && printf '%s\n' "$PRETTY_NAME"
```

Docker 官方文档也提醒：Docker 发布的端口可能绕过 ufw/firewalld 规则；防火墙规则应理解 Docker 的 iptables 行为，并在 `DOCKER-USER` 链处理，同时核对云安全组。不要因为使用了端口范围就认为服务安全。

## 2. 按 Docker 官方 apt 仓库安装

以下步骤根据 [Docker 官方 Debian 安装文档](https://docs.docker.com/engine/install/debian/)；仅在 Debian 13 上执行。官方文档列出的兼容架构包括 amd64、armhf、arm64 和 ppc64el。

如机器已有发行版冲突包，先卸载（会影响这些包对应的软件；先确认主机用途）：

```bash
sudo apt remove $(dpkg --get-selections docker.io docker-compose docker-doc docker-buildx podman-docker containerd runc | cut -f1)
```

安装官方仓库所需工具并导入 keyring：

```bash
sudo apt update
sudo apt install -y ca-certificates curl python3 python3-venv rsync
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

写入 Docker 官方 deb822 源（Debian 13 的 codename 是 `trixie`；下面使用系统值并强制校验）：

```bash
test "$(. /etc/os-release && echo "$VERSION_CODENAME")" = trixie
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: trixie
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
```

上述命令会自动写入当前机器的 CPU 架构。安装 Docker Engine、Buildx 和 Compose 插件：

```bash
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo systemctl status docker --no-pager
sudo docker run hello-world
sudo docker compose version
```

## 3. 新装：上传完整目录

在本地构建/下载本项目后，**新服务器可上传全部项目文件**，包括隐藏文件 `.env.example`、`.gitignore`、`.dockerignore`。不要上传真实 `.env`、监控 URL、秘密或数据库。

```bash
sudo mkdir -p /opt/vps-watchtower-package
sudo chown -R "$USER":"$USER" /opt/vps-watchtower-package
# 在本地执行示例：保留隐藏文件，排除真实 .env、数据库和缓存
rsync -a --exclude='.env' --exclude='data/' --exclude='.venv/' ./ user@SERVER:/opt/vps-watchtower-package/
ssh user@SERVER
cd /opt/vps-watchtower-package
cp .env.example .env
mkdir -p data
chmod 600 .env
```

编辑 `.env`：至少设置强随机 `APP_SECRET` 和 `ADMIN_PASSWORD`。可用以下方式生成值（只显示在终端，不要写进仓库）：

```bash
python3 - <<'PY'
import secrets
print('APP_SECRET=' + secrets.token_urlsafe(48))
print('ADMIN_PASSWORD=' + secrets.token_urlsafe(32))
PY
```

按需设置 `DOMAIN`（生产模式）和通知变量。不要在本文、仓库或工单中填写真实值。

## 4. 默认模式（随机宿主机 TCP 端口）

默认文件必须单独使用：

```bash
sudo docker compose -f docker-compose.yml up -d --build
# 查询当前实际宿主机端口；容器端口始终是 8080/tcp
sudo docker compose -f docker-compose.yml port watchtower 8080
```

浏览器访问 `http://服务器IP:实际端口`，不要将输出中的 `0.0.0.0` 或 `[::]` 直接用作访问地址。登录密码即 `.env` 中设置的 `ADMIN_PASSWORD`。

Compose 会从宿主机 `10000–65535/tcp` 中自动分配一个空闲端口映射到容器 `8080/tcp`（不保证密码学随机）。默认发布到主机所有接口。重建或重新创建后实际端口可能改变，所以每次都查询，并相应更新云安全组、`DOCKER-USER`/主机防火墙规则及反向代理。只放行**当前查询到的实际端口**；端口分配不是安全边界，不替代登录密码、补丁、访问控制或 HTTPS。

查看日志/状态：

```bash
sudo docker compose -f docker-compose.yml ps
sudo docker compose -f docker-compose.yml logs --tail=100 watchtower
```

## 5. 生产模式（Caddy HTTPS）

生产模式必须**单独**使用 `docker-compose.prod.yml`，不要使用 `-f docker-compose.yml -f docker-compose.prod.yml`，也不要把默认文件作为隐式 override：

```bash
# .env 中设置 DOMAIN=monitor.example.com，并将 DNS A/AAAA 指向本机
# 按实际使用的防火墙与云安全组放行 80/tcp、443/tcp；HTTP/3 可选 443/udp
sudo docker compose -f docker-compose.prod.yml up -d --build
sudo docker compose -f docker-compose.prod.yml ps
sudo docker compose -f docker-compose.prod.yml logs --tail=100 caddy
```

如果从默认模式切换到生产模式，先执行 `sudo docker compose -f docker-compose.yml down`；不要加 `-v`，并保留 `data/`。反向切换时先用 prod 文件执行 down。

生产 Compose 只向内部网络暴露 Watchtower 的 `8080`，Caddy 对外提供 `80/443`（含 HTTP/3 UDP 443）。Caddy 使用仓库的 `Caddyfile.example` 挂载为配置文件，并将 `${DOMAIN}` 反代到 `watchtower:8080`。确保 80/443 未被其他服务占用、域名解析正确，且 80/443 可被公网访问以便证书申请。

## 6. 手动更新（已有 repo）

更新前不要覆盖运行数据或秘密：

```bash
cd /opt/vps-watchtower-package
umask 077
mkdir -p backups
chmod 700 backups
stamp=$(date -u +%Y%m%dT%H%M%SZ)
# 先停止应用，确保 SQLite 数据库与 WAL 一致；生产模式改用 prod 文件
sudo docker compose -f docker-compose.yml stop
tar -czf "backups/full-$stamp.tgz" .env data app.py Dockerfile requirements.txt docker-compose.yml docker-compose.prod.yml Caddyfile.example
# 备份包含敏感配置与数据库，请妥善保护，不上传到 GitHub
```

将新版本上传到临时目录，再仅替换受控源码/配置；或在已有 repo 中先审阅变更。若拿到的是根目录 `patch`/`diff` 文件，先删除它们，避免被误当源码或构建上下文：

```bash
find . -maxdepth 1 -type f \( -name '*.patch' -o -name '*.diff' \) -print
# 确认无用后再删除（不要删除你需要审阅的补丁）
rm -f ./*.patch ./*.diff
```

务必保留服务器 `.env` 和 `data/`；只上传本次发布需要的文件。不要执行 `git add .`。若用 Git，明确列出文件，例如：

```bash
git add app.py Dockerfile requirements.txt docker-compose.yml docker-compose.prod.yml Caddyfile.example README.md DEPLOY_CN.md .env.example .gitignore .dockerignore test_*.py examples/sites-template.csv
# 如果仓库中原先跟踪了根目录补丁文件，另将删除操作加入暂存
git add -u -- '*.patch' '*.diff'
# 仅在你已审阅 diff 且确实需要提交时执行 commit/push
```

若使用面板/GUI 上传，开启“显示隐藏文件”，并确认 `.dockerignore`、`.gitignore`、`.env.example` 没有漏传；真实 `.env` 仍不应上传。重建命令按实际模式选择其一：

```bash
sudo docker compose -f docker-compose.yml up -d --build
# 或
sudo docker compose -f docker-compose.prod.yml up -d --build
```

## 7. 备份、回滚与停止

备份 `data/` 与 `.env` 到受控、加密的位置；不要把它们提交仓库。回滚时恢复已审阅的项目文件和服务器 `.env`，保留数据库目录，再用对应的**单一** compose 文件重建：

```bash
sudo docker compose -f docker-compose.prod.yml down
# 或默认模式使用 docker-compose.yml
# 恢复代码后：
sudo docker compose -f docker-compose.prod.yml up -d --build
```

## 8. 离线测试与边界

在项目目录运行：

```bash
sudo apt install -y python3 python3-venv
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest -v
```

安装依赖需要联网，安装完成后的测试使用 mock，不会实际请求监控 URL，也不会实际发送 Telegram、Email、Discord 或 Webhook。应用是 Flask 内置服务器 MVP；SSRF 防护不完整，建议私有部署并限制访问来源。请勿将用户监控 URL、任何秘密或 SQLite 数据库加入仓库、补丁包或公开日志。
