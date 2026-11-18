# Telegram 公开频道 → 钉钉转发器

这是一个零持续费用的个人提醒工具：Supabase Cron 每 30 秒唤醒一条
Edge Function，读取公开 Telegram 频道预览页，并把尚未投递的帖子推送到
钉钉群机器人。

它不需要 Telegram Bot、Telegram API ID、手机号验证码、常驻 VPS 或额外
代理。本实现只能监听带公开用户名的 Telegram 频道；频道正文不会写入
Supabase，数据库只保存消息 ID、时间、公开链接和投递状态。

```text
Supabase Cron（每 30 秒）
  → Edge Function
  → https://t.me/s/<频道用户名>
  → Supabase 去重游标 / 投递账本
  → 钉钉群机器人
```

GitHub Actions 保留为**手动应急补发入口**，不再承担定时调度。这样不会依赖
GitHub 对公开仓库定时工作流的触发可靠性。

## 安全规则

- 不要把钉钉 Webhook、钉钉加签密钥、`RELAY_CRON_SECRET` 或 Supabase Secret
  key 写进源码、README、Issues、SQL 历史、Actions 日志或 Git 提交。
- 若钉钉 Webhook 曾公开出现在聊天、截图、代码或公开网页，应在钉钉中重置
  Webhook 和加签密钥后更新 Function Secrets。
- 函数端点关闭 Supabase 的 JWT 验证，但每次请求仍须携带随机
  `x-relay-cron-secret`；该值只保存在 Edge Function Secrets 和 Supabase Vault。

## 项目内容

```text
.
├── .github/workflows/telegram-dingtalk-relay.yml  # 仅手动应急补发
├── relay.py                                        # 手动补发的 Python 实现
├── supabase/functions/telegram-dingtalk-relay/     # 生产 Edge Function
├── supabase/migrations/202611160001_*.sql          # 去重状态表与 RLS
├── supabase/migrations/202611170002_*.sql.example  # Cron / Vault 模板
└── tests/                                          # 不接触真实凭据的单元测试
```

## Supabase 部署

### 1. 建立状态表

在 Supabase Dashboard 的 **SQL Editor** 执行一次：

[`supabase/migrations/202611160001_create_telegram_relay.sql`](supabase/migrations/202611160001_create_telegram_relay.sql)

### 2. 部署 Edge Function

在 **Edge Functions → Deploy a new function → Via Editor** 创建
`telegram-dingtalk-relay`，将
[`supabase/functions/telegram-dingtalk-relay/index.ts`](supabase/functions/telegram-dingtalk-relay/index.ts)
的内容粘贴为 `index.ts` 并部署。

在该函数的 **Settings** 中保持 **Verify JWT with legacy secret** 为关闭状态。
函数使用自定义的随机请求头鉴权，而不是开放匿名调用。

### 3. 配置 Function Secrets

在 **Edge Functions → Secrets** 添加下列值；所有密钥都应只存在于 Supabase
的加密 Secret 存储中。

| Secret 名称 | 用途 |
|---|---|
| `TG_CHANNEL` | 公开频道用户名，例如 `heyue151`，不带 `@` |
| `DINGTALK_WEBHOOK_URL` | 完整钉钉机器人 Webhook URL |
| `DINGTALK_SIGNING_SECRET` | 钉钉 `SEC...` 加签密钥 |
| `RELAY_CRON_SECRET` | 新生成、长度至少 32 字节的随机值 |

首次看到一个此前未初始化的频道时，函数只记录最新消息 ID，不回灌历史消息；
已有游标会被直接沿用。

### 4. 创建 Cron 任务

将
[`supabase/migrations/202611170002_schedule_edge_relay.sql.example`](supabase/migrations/202611170002_schedule_edge_relay.sql.example)
粘贴到 SQL Editor，把两个占位符分别替换为与 `RELAY_CRON_SECRET` 相同的值和
项目的 publishable API key 后执行。迁移会启用 `pg_cron` / `pg_net`、把这些
调用所需的值写入 Supabase Vault，并创建名为
`telegram-dingtalk-relay-edge` 的 30 秒任务。模板可以安全重跑。

## 手动应急补发

GitHub Actions 的 **Run workflow** 可作为应急入口。它使用相同的 Supabase
游标和投递账本，因此不会重复发送已投递的帖子。仓库的 Actions Secrets 仍应
保持为服务器端机密，且只授予仓库写权限给可信管理员。

## 本地自检

```bash
python3 -m unittest discover -s tests -v
tsc --noEmit --strict --target ES2022 --lib ES2022,DOM \
  supabase/functions/telegram-dingtalk-relay/index.ts
```

## 限制与恢复行为

- 当前轮询间隔为 30 秒；它不是秒级或商业 SLA 告警系统。
- 每次运行都会从游标之后分页补拉公开帖子。若积压大于
  `PUBLIC_PREVIEW_MAX_PAGES`，函数会失败且不前移游标，避免静默丢帖。
- 函数逐条建立投递账本，发送失败会保留为失败状态，下一次运行会重试；已经
  标记为 `sent` 的帖子不会重复发出。
- 如果频道转为私有、删除公开用户名、禁止预览，或 Telegram 改变预览页结构，
  需要改用个人账号 MTProto 方案。
