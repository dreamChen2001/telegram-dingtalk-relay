// Supabase Edge Function for the Telegram -> DingTalk relay.
// State and the delivery ledger live in the existing PostgREST tables created
// by 202611160001_create_telegram_relay.sql. The function deliberately has no
// Telegram account/session; it reads the channel's public preview page only.

const runtime = globalThis as typeof globalThis & {
  Deno: {
    env: { get(name: string): string | undefined }
    serve(handler: (request: Request) => Response | Promise<Response>): void
  }
}

type Message = { id: number; text: string; date: string | null; media: string | null }

const now = () => new Date().toISOString()
const need = (name: string) => {
  const value = runtime.Deno.env.get(name)?.trim()
  if (!value) throw new Error("missing_" + name)
  return value
}

function serviceKey(): string {
  const raw = runtime.Deno.env.get("SUPABASE_SECRET_KEYS")
  if (raw) {
    try {
      const keys = JSON.parse(raw) as Record<string, unknown>
      const candidate = typeof keys.default === "string"
        ? keys.default
        : Object.values(keys).find((value): value is string => typeof value === "string")
      if (candidate?.trim()) return candidate.trim()
    } catch {
      // Fall through to the legacy built-in service key below.
    }
  }
  return need("SUPABASE_SERVICE_ROLE_KEY")
}

const channel = need("TG_CHANNEL").replace(/^@/, "")
if (!/^[A-Za-z0-9_]{5,}$/.test(channel)) throw new Error("invalid_TG_CHANNEL")
const relay = "telegram-public:" + channel.toLowerCase()
const supabaseUrl = need("SUPABASE_URL").replace(/\/$/, "")
const dbKey = serviceKey()
const webhook = need("DINGTALK_WEBHOOK_URL")
const signingSecret = need("DINGTALK_SIGNING_SECRET")
const cronSecret = need("RELAY_CRON_SECRET")
const maxPages = Math.max(1, Number(runtime.Deno.env.get("PUBLIC_PREVIEW_MAX_PAGES") ?? 20))
const maxPerRun = Math.max(1, Number(runtime.Deno.env.get("MAX_MESSAGES_PER_RUN") ?? 100))

function equal(actual: string, expected: string): boolean {
  if (actual.length !== expected.length) return false
  let difference = 0
  for (let index = 0; index < actual.length; index += 1) {
    difference |= actual.charCodeAt(index) ^ expected.charCodeAt(index)
  }
  return difference === 0
}

function response(body: Record<string, unknown>, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  })
}

async function timedFetch(url: string, init: RequestInit, timeout = 20_000): Promise<Response> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeout)
  try {
    return await fetch(url, { ...init, signal: controller.signal })
  } finally {
    clearTimeout(timer)
  }
}

function dbHeaders(extra: HeadersInit = {}): Headers {
  const headers = new Headers({ apikey: dbKey, "Content-Type": "application/json" })
  if (!dbKey.startsWith("sb_")) headers.set("Authorization", "Bearer " + dbKey)
  new Headers(extra).forEach((value, name) => headers.set(name, value))
  return headers
}

async function db(path: string, init: RequestInit = {}): Promise<Response> {
  let result: Response
  try {
    result = await timedFetch(supabaseUrl + "/rest/v1/" + path, {
      ...init,
      headers: dbHeaders(init.headers),
    })
  } catch {
    throw new Error("supabase_request_failed")
  }
  if (!result.ok) throw new Error("supabase_request_failed")
  return result
}

const statePath = () => "tg_relay_state?relay_key=eq." + encodeURIComponent(relay)
const deliveryPath = (id: number) =>
  "tg_relay_delivery?relay_key=eq." + encodeURIComponent(relay) + "&telegram_message_id=eq." + id

async function getCursor(): Promise<number | null> {
  const result = await db(statePath() + "&select=last_message_id")
  const rows = await result.json() as Array<{ last_message_id: number }>
  return rows[0] ? Number(rows[0].last_message_id) : null
}

async function initialiseCursor(id: number): Promise<void> {
  await db("tg_relay_state?on_conflict=relay_key", {
    method: "POST",
    headers: { Prefer: "resolution=ignore-duplicates" },
    body: JSON.stringify({ relay_key: relay, last_message_id: id, initialized_at: now(), last_run_at: now() }),
  })
}

async function updateCursor(id?: number): Promise<void> {
  const data: Record<string, unknown> = { last_run_at: now() }
  if (id !== undefined) {
    data.last_message_id = id
    data.last_success_at = now()
  }
  await db(statePath(), { method: "PATCH", body: JSON.stringify(data) })
}

async function delivery(id: number): Promise<{ status: string; attempts: number } | null> {
  const result = await db(deliveryPath(id) + "&select=status,attempts")
  const rows = await result.json() as Array<{ status: string; attempts: number }>
  return rows[0] ?? null
}

async function beginDelivery(message: Message): Promise<boolean> {
  const existing = await delivery(message.id)
  if (existing?.status === "sent") return false
  if (existing) {
    await db(deliveryPath(message.id), {
      method: "PATCH",
      body: JSON.stringify({ status: "pending", attempts: Number(existing.attempts ?? 0) + 1, last_error: null }),
    })
    return true
  }
  await db("tg_relay_delivery", {
    method: "POST",
    headers: { Prefer: "resolution=ignore-duplicates" },
    body: JSON.stringify({
      relay_key: relay,
      telegram_message_id: message.id,
      telegram_date: message.date,
      source_url: "https://t.me/" + channel + "/" + message.id,
      status: "pending",
      attempts: 1,
    }),
  })
  return true
}

async function finishDelivery(id: number, status: "sent" | "failed", error?: string): Promise<void> {
  await db(deliveryPath(id), {
    method: "PATCH",
    body: JSON.stringify({
      status,
      sent_at: status === "sent" ? now() : null,
      last_error: error ?? null,
    }),
  })
}

function decode(value: string): string {
  const named: Record<string, string> = { amp: "&", apos: "'", gt: ">", lt: "<", nbsp: " ", quot: '"' }
  return value.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (entity, token: string) => {
    const lower = token.toLowerCase()
    if (lower.startsWith("#x")) {
      const code = Number.parseInt(lower.slice(2), 16)
      return Number.isFinite(code) && code >= 0 && code <= 0x10ffff ? String.fromCodePoint(code) : entity
    }
    if (lower.startsWith("#")) {
      const code = Number.parseInt(lower.slice(1), 10)
      return Number.isFinite(code) && code >= 0 && code <= 0x10ffff ? String.fromCodePoint(code) : entity
    }
    return named[lower] ?? entity
  })
}

function parsePage(html: string): Message[] {
  const markers = [...html.matchAll(/<[^>]*\bdata-post=["']([^"']+)["'][^>]*>/gi)]
  const messages = new Map<number, Message>()
  for (let index = 0; index < markers.length; index += 1) {
    const marker = markers[index]
    const raw = marker[1]
    const separator = raw.lastIndexOf("/")
    const id = Number.parseInt(raw.slice(separator + 1), 10)
    if (separator < 1 || !Number.isInteger(id) || raw.slice(0, separator).toLowerCase() !== channel.toLowerCase()) continue
    const start = marker.index ?? 0
    const end = index + 1 < markers.length ? (markers[index + 1].index ?? html.length) : html.length
    const fragment = html.slice(start, end)
    const textMatch = /<div\b[^>]*class=["'][^"']*\btgme_widget_message_text\b[^"']*["'][^>]*>([\s\S]*?)<\/div>/i.exec(fragment)
    const timeMatch = /<time\b[^>]*\bdatetime=["']([^"']+)["'][^>]*>/i.exec(fragment)
    const text = textMatch
      ? decode(textMatch[1].replace(/<br\s*\/?>/gi, "\n").replace(/<[^>]*>/g, "")).trim()
      : ""
    const media = /tgme_widget_message_photo_wrap/i.test(fragment) ? "📷 图片"
      : /tgme_widget_message_video_player/i.test(fragment) ? "🎬 视频"
      : /tgme_widget_message_document_wrap/i.test(fragment) ? "📎 文件"
      : /tgme_widget_message_voice/i.test(fragment) ? "🎤 语音" : null
    messages.set(id, { id, text, date: timeMatch?.[1] ?? null, media })
  }
  return [...messages.values()].sort((left, right) => left.id - right.id)
}

async function page(before?: number): Promise<Message[]> {
  const url = "https://t.me/s/" + channel + (before ? "?before=" + before : "")
  let result: Response
  try {
    result = await timedFetch(url, { headers: { "User-Agent": "Mozilla/5.0 (TelegramDingTalkRelay/2.0)" } })
  } catch {
    throw new Error("telegram_request_failed")
  }
  if (!result.ok) throw new Error("telegram_request_failed")
  const messages = parsePage(await result.text())
  if (messages.length === 0) throw new Error("telegram_preview_empty")
  return messages
}

async function after(cursor: number): Promise<Message[]> {
  const found = new Map<number, Message>()
  let before: number | undefined
  for (let pageNumber = 0; pageNumber < maxPages; pageNumber += 1) {
    const current = await page(before)
    const oldest = current[0].id
    current.forEach((message) => { if (message.id > cursor) found.set(message.id, message) })
    if (oldest <= cursor) return [...found.values()].sort((left, right) => left.id - right.id)
    if (before !== undefined && oldest >= before) throw new Error("telegram_pagination_failed")
    before = oldest
  }
  throw new Error("telegram_backlog_too_large")
}

function content(message: Message): string {
  const date = message.date ? new Date(message.date) : null
  const time = date && !Number.isNaN(date.getTime())
    ? new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date)
    : "未知"
  const body = [message.media, message.text].filter(Boolean).join("\n") || "[无文字内容]"
  const result = "Telegram 频道更新\n频道：@" + channel + "\n时间：" + time + "\n\n" + body + "\n\n原文：https://t.me/" + channel + "/" + message.id
  return result.length > 3500 ? result.slice(0, 3450) + "\n\n[内容已截断]" : result
}

async function send(message: Message): Promise<void> {
  const timestamp = String(Date.now())
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(signingSecret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"])
  const signature = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(timestamp + "\n" + signingSecret))
  const url = new URL(webhook)
  url.searchParams.set("timestamp", timestamp)
  url.searchParams.set("sign", btoa(String.fromCharCode(...new Uint8Array(signature))))
  let result: Response
  try {
    result = await timedFetch(url.toString(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ msgtype: "text", text: { content: content(message) } }),
    })
  } catch {
    throw new Error("dingtalk_request_failed")
  }
  if (!result.ok) throw new Error("dingtalk_request_failed")
  const resultBody = await result.json() as { errcode?: number }
  if (resultBody.errcode && resultBody.errcode !== 0) throw new Error("dingtalk_rejected")
}

runtime.Deno.serve(async (request) => {
  if (request.method !== "POST") return response({ error: "method_not_allowed" }, 405)
  if (!equal(request.headers.get("x-relay-cron-secret") ?? "", cronSecret)) return response({ error: "unauthorized" }, 401)
  try {
    const cursor = await getCursor()
    if (cursor === null) {
      const latest = await page()
      await initialiseCursor(latest.at(-1)!.id)
      return response({ ok: true, delivered: 0, initialised: true })
    }
    const messages = (await after(cursor)).slice(0, maxPerRun)
    let delivered = 0
    for (const message of messages) {
      if (await beginDelivery(message)) {
        try {
          await send(message)
          await finishDelivery(message.id, "sent")
          delivered += 1
        } catch {
          await finishDelivery(message.id, "failed", "DingTalkDeliveryError")
          throw new Error("dingtalk_delivery_failed")
        }
      }
      await updateCursor(message.id)
    }
    if (messages.length === 0) await updateCursor()
    console.log("Relay completed; delivered " + delivered + " message(s)")
    return response({ ok: true, delivered })
  } catch (error) {
    console.error("Relay failed", error instanceof Error ? error.message : "unknown")
    return response({ error: "relay_failed" }, 500)
  }
})
