# Telegram 公开频道 → 钉钉转发器

这是一个零持续费用的个人提醒工具：GitHub Actions 每五分钟读取公开 Telegram 频道的预览页，并把新帖子推送到钉钉群机器人。

它**不需要** Telegram Bot、Telegram API ID、手机号验证码、常驻 VPS 或额外代理。本项目仅适用于公开可访问的 Telegram 频道；当前目标频道 `@heyue151` 已确认可以通过 Telegram 的公开预览页读取。

```text
GitHub Actions（每 5 分钟）
  → https://t.me/s/<频道用户名>
  → Supabase Free（游标和去重状态）
  → 钉钉群机器人
```

> 这不是秒级或商业 SLA 告警系统。GitHub 的调度可能延迟；程序会按 Supabase 游标补拉公开帖子，因此通常会延迟而不是因单次任务失败永久漏帖。

## 安全规则

- 不要把钉钉 Webhook、钉钉加签密钥或 Supabase Secret key 写进源码、README、Issues、Actions 日志或 Git 提交。
- 如果一个钉钉 Webhook 曾出现在聊天、截图、代码或公开页面，请在钉钉中重置它和加签密钥后再部署。
- GitHub 公开仓库可以公开源代码，但只有仓库管理员应拥有写权限；能修改 workflow 的人可能尝试读取 Actions Secrets。
- 频道正文不会被写入 Supabase；数据库只保存频道消息 ID、时间、公开链接和投递状态。

## 项目内容

```text
.
├── .github/workflows/telegram-dingtalk-relay.yml  # 五分钟 GitHub 调度
├── relay.py                                      # 公开预览抓取、去重、加签、投递
├── test_dingtalk.py                              # 明确执行时才发一条固定测试通知
├── supabase/migrations/...sql                    # 状态表和 RLS
└── tests/                                        # 不接触真实凭据的单元测试
```

## 部署

### 1. 创建 Supabase Free 项目

在 Supabase Dashboard 新建项目，然后在 **SQL Editor** 中执行完整迁移：

[`supabase/migrations/202611160001_create_telegram_relay.sql`](supabase/migrations/202611160001_create_telegram_relay.sql)

从项目的 **Settings → API Keys** 创建一个专用于本转发器的 Secret key（`sb_secret_...`），并记录项目 URL。不要使用公开的 `anon` / publishable key。

### 2. 创建专用 GitHub 仓库

将本目录内容作为新仓库根目录上传，包含隐藏目录 `.github`。公共仓库使用标准 GitHub-hosted Runner 可以免费运行；私有仓库会消耗 Actions 免费分钟数，不适合五分钟轮询的长期零预算运行。

### 3. 配置 GitHub Actions Secrets

在 **Settings → Secrets and variables → Actions → Secrets** 添加：

| Secret 名称 | 值 |
|---|---|
| `TG_CHANNEL` | 公开频道用户名，不带 `@`，例如 `heyue151` |
| `DINGTALK_WEBHOOK_URL` | 重置后的完整钉钉 Webhook URL |
| `DINGTALK_SIGNING_SECRET` | 重置后的钉钉 `SEC...` 加签密钥 |
| `SUPABASE_URL` | Supabase 项目 URL |
| `SUPABASE_SECRET_KEY` | 新建的 `sb_secret_...` Secret key |

首次执行默认只记录当前最新消息，避免把历史帖子刷进钉钉。之后的新帖子会被推送。

若明确希望首次补推最近 10 条，可在 GitHub 仓库 **Settings → Secrets and variables → Actions → Variables** 设置：

```text
INITIAL_SYNC_MODE=send_recent
```

执行一次后删除该 Variable 或改为 `skip`。

## 本地自检

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

可选的钉钉测试只会发送一条固定文本，不读取 Telegram：

```bash
export DINGTALK_WEBHOOK_URL='新的 Webhook URL'
export DINGTALK_SIGNING_SECRET='新的 SEC... 密钥'
python test_dingtalk.py
```

## 限制与恢复行为

- GitHub Actions 最短五分钟调度，可能在高负载时延迟或错过单次触发。
- 每次运行都会回看游标之后的帖子，并在发现延迟时分页补拉；若积压超过 `PUBLIC_PREVIEW_MAX_PAGES`，程序会失败且不会前移游标，避免静默丢帖。
- 如果频道变成私有、删除公开用户名、禁止预览或 Telegram 改变公开页面结构，任务会失败并需要改用个人账号 MTProto 方案。
- 公共仓库若长时间没有活动，GitHub 可能自动停用定时工作流；应定期检查 Actions 页面。
