# 商家接收 Webhook 与通知跳转链接

两类地址用途不同，分别管理，不能混填。

| 字段 | 用途 | 可见范围 |
|---|---|---|
| 商家官网 `website` | 商家公开介绍页的官网入口 | 公开 |
| 商家／购买跳转链接 `push_url` | Telegram 通知中点击打开的页面；为空时回退到套餐监控 URL | 公开，不得包含密钥 |
| 接收推送地址 `webhook_url` | 服务器向商家发送 JSON 通知的 HTTPS endpoint | 仅管理员设置页，不出现在目录或个人通知 |

## 配置一个商家

在 `/admin/providers` 创建商家后，记下 ID，例如 12。在服务器真实 `.env` 增加：

```dotenv
# 示例变量名；值由你用 secrets.token_hex(32) 生成，不使用这里的说明文字
PROVIDER_WEBHOOK_SECRET_12=替换为至少32字符的随机共享密钥
```

把同一密钥安全配置给商家的接收端。使用当前模式 `docker compose ... up -d --force-recreate` 加载环境，再进入该商家的“Webhook 设置与记录”，填入实际接收的 HTTPS URL、选择事件并勾选启用。程序会检查密钥长度，网页只显示是否配置，不显示密钥。

Webhook 独立于旧站点的 channels 字段和全局 GENERIC_WEBHOOK_URL；两个渠道都启用时会分别投递到各自配置地址。商家 Webhook 仅接收关联到该商家的套餐事件；这是管理员授权发送的商家渠道，未发布套餐也可发给该商家。普通用户 Telegram 则严格只接收公开套餐。

不要在源码包中填写实际 URL 或密钥。修改 `APP_SECRET` 不会自动轮换商家 Webhook 密钥；每个商家的密钥独立维护。

## HTTP 请求

仅对 `restock`、`sellout`、`change` 中选中的类型发送一次 POST；首次建立基线、管理员全站汇总、检查报错不会触发商家 Webhook。监控异常不等于售罄。

```http
POST /your-receiver HTTP/1.1
Content-Type: application/json
X-Watchtower-Timestamp: 1780000000
X-Watchtower-Event: restock
X-Watchtower-Delivery: evt-123-provider-12
X-Watchtower-Signature: sha256=<64位十六进制HMAC摘要>
```

JSON 结构示例（只含演示数据）：

```json
{
  "schema_version": 1,
  "event_id": 123,
  "event": "restock",
  "occurred_at": "2026-01-01T00:00:00+00:00",
  "provider": {"id": 12, "name": "示例商家"},
  "plan": {
    "id": 34,
    "name": "示例套餐",
    "url": "https://example.com/plans/example",
    "stock_status": "in_stock"
  },
  "summary": "监控内容与上次快照不同。"
}
```

不发送完整快照、原始内容差异、Bot Token 或内部检查异常。`occurred_at` 为事件发生时间，`X-Watchtower-Timestamp` 为本次发送时间。

## 验证签名

对**收到的原始 body 字节**验签，不要先解析并重新序列化 JSON：

```python
import hashlib
import hmac
import time

# raw_body: request.get_data() 的原始 bytes
# timestamp/header_signature/delivery_id: 从相应 HTTP header 取得
# shared_secret: 接收端保管的该商家共享密钥

def verify(raw_body, timestamp, header_signature, shared_secret):
    try:
        if abs(int(time.time()) - int(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False
    expected = "sha256=" + hmac.new(
        shared_secret.encode("utf-8"),
        (timestamp + ".").encode("ascii") + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, header_signature or "")
```

签名输入是 `ASCII(时间戳 + '.') + 原始body`，算法 HMAC-SHA256。接收端同时检查时间窗口，并用 `X-Watchtower-Delivery` 做持久去重；服务器时钟应同步。验证成功后再解析业务字段，并尽快返回 2xx。

任何收到的 URL 或摘要仍是外部数据，不应直接拼接 shell、SQL 或 HTML。不要仅凭这类库存消息自动支付或购买。

## 安全和失败行为

仅允许无账号密码的 HTTPS 公网地址，限制端口为 80／443；通常应使用 443。不使用环境代理、不带浏览器 Cookie、不跟随 3xx；域名解析包含内网／本地／云元数据地址会被拒绝。实际 socket 固定到已校验的 IP，同时按原域名校验证书与 SNI，防止 DNS 二次解析绕过。

超时为 10 秒，最多读取 4096 字节响应；2xx 视为成功，其他状态或连接失败记为 failed。审计保存事件 ID、商家 ID、状态和可获得的 HTTP 状态码，不保存响应内容、地址、密钥或原始异常。接收端响应体不会被当作命令执行。

每个 `(商家ID,事件ID)` 只会取得一次投递 claim。网络请求前先记录 pending，完成后更新 sent／failed；若进程中断，pending 可能保留。没有自动重试、没有人工重放按钮，也不保证 exactly-once 或最终送达。接收端务必幂等；核查失败时查看双方日志，不要删除审计表来盲目重发。
