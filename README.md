# VPS Watchtower 多用户版

一个可自行部署的 VPS 套餐目录与库存监控应用。使用原创中文界面、Flask、SQLite、单进程 Gunicorn 和 Docker Compose，不依赖第三方站点账户或素材。

## 已包含的功能

- 公开套餐目录、独立商家页面、搜索、地区／商家／库存筛选、RAM／磁盘／流量筛选、分页与排序。价格比较必须同时指定币种和计费周期，避免把年费和月费混排。
- 管理员维护商家名称、官网、介绍、关联套餐，以及通知中点击跳转的商家／购买链接。
- 每个商家独立配置接收通知的 HTTPS Webhook，支持事件开关、HMAC 签名、防重复投递和发送审计；Webhook 地址不公开。
- 用户注册、登录、退出、修改密码；账户和管理员权限分离，普通用户不能配置监控目标。
- 套餐和商家订阅；每条订阅可暂停、设置事件类型及状态汇总，个人 Telegram 偏好还可统一关闭某类消息。
- 共享 Telegram Bot，每个用户通过验证码验证自己的私聊 chat_id，支持测试消息、解绑、个人发送记录。
- 保留原版 Telegram 全站状态汇总、自动分段、随机宿主端口分配、CSV 导入导出、Email／Discord／通用 Webhook 管理员通知。
- 按 URL 和 CSS 选择器检查公开页面，记录基线与快照，识别补货、售罄、普通变化；完整快照只供管理员查看。
- 兼容旧 SQLite：增量迁移，不删除旧站点、快照、事件和通知。旧监控默认不发布，需管理员审核后手动公开。

这里的“订阅”是关注商家／套餐接收通知，**不包含收费套餐、支付或自动购买 VPS**。

## 从哪里开始

完整安装、HTTPS、原版升级、备份回滚及 Telegram 设置见 **[DEPLOY_CN.md](DEPLOY_CN.md)**。商家 Webhook 接口与验签说明见 **[WEBHOOK_CN.md](WEBHOOK_CN.md)**。

常用页面：

| 页面 | 用途 |
|---|---|
| `/catalog`、`/providers`、`/activity` | 公开目录、商家、库存动态 |
| `/register`、`/login` | 注册、登录 |
| `/account`、`/subscriptions` | 个人设置、订阅管理 |
| `/account/telegram`、`/account/notifications` | 绑定与偏好、个人发送记录 |
| `/admin` | 监控站点、检查、CSV、快照管理 |
| `/admin/providers` | 商家设置及各商家的 Webhook 入口 |
| `/events`、`/settings` | 管理员事件审计、系统通知配置状态 |
| `/healthz` | 不含业务数据的健康检查 |

## 最小运行约束

必须设置不少于 32 字符的随机 `APP_SECRET`。`.env.example` 默认关闭注册；准备好后改为 `REGISTRATION_ENABLED=true`。管理员用交互式 CLI 显式创建，原版 `ADMIN_PASSWORD` 不再直接用于网页登录。

生产部署只使用 `docker-compose.prod.yml`，启用 HTTPS 与 Secure Cookie；默认 `docker-compose.yml` 仅在宿主机 `127.0.0.1` 上发布随机端口，适合 SSH 隧道验收。两份文件**不要合并使用**。

只能运行一个应用实例、一个 Gunicorn worker，不能横向扩容或启用重载，否则会重复启动嵌入式监控调度器。适合中小规模、管理员维护监控目录的部署；不是让所有用户任意提交 URL 的抓取平台。

## 安全和行为边界

密码使用 scrypt；会话通过数据库中的哈希令牌校验，可撤销；改密码会撤销所有旧会话。所有修改操作使用 POST 与 CSRF；有登录和注册限流、Telegram 验证限次与发送冷却、内容安全响应头。

监控及商家 Webhook 会校验解析出的公网地址、固定连接 IP、保留 TLS 域名校验，拒绝本地／私网／云元数据目标；监控逐跳校验重定向，Webhook 不跟随重定向。不处理登录后的页面，不绕过 403、认证、验证码或反爬挑战。错误不会被当作售罄。

库存依赖管理员设置的正向／售罄关键词，**不是官方库存接口**。售罄词优先；无法确定时记为 unknown，不伪造库存。价格为管理员维护的目录元数据，原 USD 阈值识别仍是保守文本启发式，并不自动换汇或保证结算价格。

通知为尽力而为，不保证送达。个人事件及商家 Webhook 先登记 claim 防止重复发送；失败或进程中断不自动重试，避免重复触发商家业务。请在发送记录中检查 `failed`／`pending`。不要将 Webhook 直接绑定无幂等保护的购买或付款动作。

本包不含真实监控网址、账号、Bot Token 或用户数据库；不包含 VPSDeer 品牌、图片或页面素材。功能参考仅限其公开的套餐／商家／库存动态模式。

## 离线测试

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest -v
```

安装依赖需要联网；测试使用临时数据库和 mock，不会抓取真实监控页面，也不会发送真实 Telegram／Email／Discord／Webhook。测试覆盖旧库重复迁移、库存转换、原版汇总、账户权限、CSRF、用户订阅隔离、Telegram 验证、商家签名与防 SSRF。验证范围及未执行项见 [TEST_RESULTS.md](TEST_RESULTS.md)。
