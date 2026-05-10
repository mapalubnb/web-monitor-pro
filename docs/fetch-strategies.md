# 抓取策略详解

web-monitor-pro 采用**多策略递进式抓取**，不依赖 Playwright 等浏览器，资源占用极低。

---

## 策略优先级（auto 模式）

```
┌────────────────────────────────┐
│ 策略 1: curl_cffi 伪造 Chrome TLS │  ← 主力
│  能过多数 Cloudflare / TLS 指纹防护│
└────────────┬───────────────────┘
             │ 失败或内容不可用
             ↓
┌────────────────────────────────┐
│ 策略 2: httpx + 浏览器请求头      │  ← 备选
│  轻量，适合 L1-L2 静态页面        │
└────────────┬───────────────────┘
             │ 失败
             ↓
┌────────────────────────────────┐
│ 策略 3: Jina Reader API        │  ← 兜底
│  外部渲染，纯文本/Markdown 输出    │
└────────────────────────────────┘
```

---

## 网页难度分级与对应策略

| 等级 | 特征 | 代表站点 | 推荐策略 |
| --- | --- | --- | --- |
| **L1 静态 HTML** | 服务端渲染，内容在 HTML 里 | 政府网站、博客 | `httpx` |
| **L2 Cookie/UA 检查** | 弱反爬，浏览器请求头即过 | 企业官网 | `httpx` + 浏览器头 |
| **L3 TLS 指纹校验** | Cloudflare 基础防护 | x.com、电商 | **`curl_cffi`** |
| **L4 SPA / Next.js** | HTML 是空壳，数据走内部 API | four.meme, pump.fun | `curl_cffi` + `--extract-next-data` 或 **API 逆向** ⭐ |
| **L5 强动态 + JS 挑战** | Turnstile、滑块、WAF | 交易所、Booking | `jina` 或 `firecrawl` |

---

## 核心技巧：API 逆向（L4 级页面的最佳实践）

**原则**：90% 的 SPA 网页，内部都在用 JSON API 加载数据。**直接请求 API 比渲染页面更稳定、更快、更省资源**。

### 操作步骤

1. 在 Chrome 打开目标页面
2. 按 `F12` 打开开发者工具 → 切到 **Network** 面板 → 筛选 **Fetch/XHR**
3. 刷新页面，观察列表中哪个请求返回了你关心的数据
4. 右键该请求 → `Copy as cURL`

### 识别 API 的技巧

- 响应 Content-Type 是 `application/json`
- URL 常带 `/api/`、`/v1/`、`/graphql` 等路径
- 预览窗口能看到你关心的数据字段

### 使用 API 监控

```
/add https://example.com/api/v1/items \
  --type json \
  --json-path "data[*].name,data[*].price" \
  --name "商品列表"
```

**优势**：
- 比浏览器抓取**快 100 倍**（几 KB JSON vs 几 MB 渲染）
- 风控风险**降低 90%**
- 变更识别**精度 100%**（结构化字段对比）

---

## curl_cffi 伪装浏览器

`curl_cffi` 基于 [curl-impersonate](https://github.com/lexiforest/curl-impersonate)，
能伪造**真实浏览器的 TLS / JA3 / HTTP2 指纹**，过多数 Cloudflare 基础防护。

### 支持的 impersonate 目标

| 值 | 模拟的浏览器 |
| --- | --- |
| `chrome131` | Chrome 131（默认推荐） |
| `chrome124` | Chrome 124 |
| `chrome120` | Chrome 120 |
| `firefox133` | Firefox 133 |
| `firefox135` | Firefox 135 |
| `safari18_0` | Safari 18 |
| `safari17_0` | Safari 17 |

### 使用方法

```
/add https://example.com --strategy curl_cffi --impersonate chrome131
```

---

## 提取 Next.js `__NEXT_DATA__`

很多基于 Next.js 的 SPA 会把首屏数据塞在 `<script id="__NEXT_DATA__">`。
这是比 API 逆向**更简单的 L4 方案**：

```
/add https://four.meme/en/create-token \
  --strategy curl_cffi \
  --extract-next-data
```

工具会自动：
1. 用 curl_cffi 拉到 HTML（过 Cloudflare）
2. 提取 `__NEXT_DATA__` 里的 JSON
3. 对比 `props.pageProps` 的变化

---

## Jina Reader（兜底）

[r.jina.ai](https://r.jina.ai) 是 Jina 提供的 Reader API，能渲染任何网页为纯文本。
免费额度 1M 次/月（配 API Key）。

**何时用**：
- curl_cffi 也过不了的硬核防护
- 页面有复杂的 JS 渲染/无限滚动
- 作为完全自动的兜底

```
/add https://hard-site.com --strategy jina
```

---

## 反风控最佳实践

服务已内置以下风控措施，**你无需手动配置**：

- ✅ TLS/JA3 指纹伪装（curl_cffi）
- ✅ 浏览器级请求头（Sec-Fetch-*, Sec-Ch-Ua）
- ✅ User-Agent 随机化
- ✅ 单域名最小请求间隔（默认 10s）
- ✅ 请求抖动（±30%，避免固定周期）
- ✅ 失败指数退避（1min → 5min → 15min → 1h）
- ✅ 全局并发信号量（默认 5）
- ✅ Cookie 自动持久化
- ✅ Cloudflare 挑战页自动识别 + fallback

**进阶**：如需代理，在 `.env` 中配置 `HTTPS_PROXY=socks5://host:port` 即可。
