# 抓取策略详解

web-monitor-pro 采用**自动递进式抓取**，全自建无外部 API 依赖。

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
             │ 失败或空壳
             ↓
┌────────────────────────────────┐
│ 策略 3: 深度提取                 │  ← 零网络开销
│  SPA 嵌入数据 / RSC Flight 解析   │
│  trafilatura 正文提取             │
└────────────┬───────────────────┘
             │ 无有效内容
             ↓
┌────────────────────────────────┐
│ 策略 3.5: Scrapling 增强         │  ← 自适应解析 / 隐身抓取
│  static → stealth 自动尝试        │
└────────────┬───────────────────┘
             │ 仍无有效内容
             ↓
┌────────────────────────────────┐
│ 策略 4: Playwright              │  ← 终极兜底
│  headless Chromium 渲染          │
│  stealth 隐藏无头特征            │
│  按需启动，用完释放               │
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
| **L5 纯 CSR / DeFi** | 纯客户端渲染，数据来自链上/API | pfund.tech、交易所 | **`playwright`** |

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

## Scrapling 增强策略

从 v0.4.0 起，服务可选集成 Scrapling，提供更稳的解析器、自适应选择器和隐身浏览器抓取。

### 策略值

| 值 | 用途 |
| --- | --- |
| `scrapling_static` | 基于 Scrapling Fetcher 的静态抓取，适合 HTML/弱风控页面 |
| `scrapling_dynamic` | 基于 Chromium 动态渲染，适合 JS 加载页面 |
| `scrapling_stealth` | 隐身 Chromium，适合 Cloudflare/高风控页面 |
| `scrapling_auto` | 先 static，失败后 stealth |

### 自适应选择器

当目标页面局部改版导致原 CSS 选择器失效时，Scrapling 可以根据首次命中元素的结构特征重定位相似元素。
只要任务配置了 `--selector`，服务会默认启用这项能力，无需额外参数。

```
/add https://example.com/products \
  --selector ".product-list"
```

如需关闭：

```
/add https://example.com/products --selector ".product-list" --no-adaptive-selector
```

### 隐身抓取

```
/add https://example.com --strategy scrapling_stealth --wait-selector "main"
```

`--wait-selector` 会让浏览器等待指定元素出现后再提取，适合纯 CSR 页面。使用浏览器类策略并配置了 `--selector` 时，会自动复用该 selector 作为等待目标。

---

## 深度提取（SSR 嵌入数据 + RSC Flight）

很多基于 Next.js、Nuxt.js、Remix 等框架的 SPA 会把首屏数据嵌入到 HTML 中。
深度提取会自动识别并解析这些嵌入数据，**零额外网络开销**。

### 支持的框架

- Next.js Pages Router（`__NEXT_DATA__`）
- Next.js App Router（RSC Flight 数据流）
- Nuxt.js（`__NUXT__` / `__NUXT_DATA__`）
- Remix（`__remixContext`）
- SvelteKit（`__SVELTEKIT_DATA__`）
- Gatsby（`__GATSBY_DATA__`）
- 通用：`__INITIAL_STATE__`、`__APOLLO_STATE__`、`__REDUX_STATE__`、JSON-LD

### 使用方法

```
/add https://four.meme/en/create-token \
  --strategy curl_cffi \
  --extract-next-data
```

---

## Playwright（纯 CSR 站点兜底）

对于纯客户端渲染的站点（如 DeFi 前端、数据完全由 JS 动态加载），
Playwright 会启动 headless Chromium 渲染页面并提取完整 DOM。

### 特性

- **按需启动**：只在需要时才启动浏览器，大多数任务不触发
- **Stealth 隐藏**：使用 `playwright-stealth` 隐藏无头浏览器特征
- **资源屏蔽**：自动屏蔽图片/CSS/字体/媒体加载，节省内存
- **定期回收**：每处理 N 页或运行 30 分钟后自动回收浏览器实例，防内存泄漏

### 使用方法

```
/add https://pfund.tech/ --strategy playwright --name PFund
```

### 资源占用

- 空闲时（未启动浏览器）：0 额外开销
- 渲染时峰值：~700MB（含 Chromium 子进程）
- 渲染完毕：内存自动释放

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
- ✅ Playwright stealth（隐藏无头浏览器特征）

**进阶**：如需代理，在 `.env` 中配置 `HTTPS_PROXY=socks5://host:port` 即可。

### 可选：Proxifly 免费代理池

项目支持接入 [proxifly/free-proxy-list](https://github.com/proxifly/free-proxy-list)。
Proxifly 的列表约每 5 分钟更新，提供 HTTP、HTTPS、SOCKS4、SOCKS5，并有 `.txt`、`.json`、`.csv` 格式。服务默认使用 txt 直链：

```
https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.txt
```

`.env.example` 和 `upgrade.sh` 默认写入启用配置；不需要免费代理时可把 `ENABLE_FREE_PROXY_POOL` 改为 `false`。

```ini
ENABLE_FREE_PROXY_POOL=true
FREE_PROXY_SOURCE_URL=https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.txt
FREE_PROXY_REFRESH_SECONDS=600
FREE_PROXY_MAX_COUNT=200
```

优先级：

1. 如果配置了 `HTTPS_PROXY` 或 `HTTP_PROXY`，始终优先使用显式代理。
2. 未配置显式代理且 `ENABLE_FREE_PROXY_POOL=true` 时，curl_cffi、httpx、Scrapling 抓取会自动轮换免费代理。
   其中 httpx 只使用 HTTP/HTTPS 代理；curl_cffi 和 Scrapling 会按自身支持情况使用 HTTP/HTTPS/SOCKS。
3. Playwright 复用浏览器池，不动态切换免费代理；需要浏览器代理时建议配置稳定的 `HTTPS_PROXY`，或使用 `scrapling_stealth`。

免费公共代理稳定性和安全性不可控；监控敏感内容时建议关闭代理池，或配置稳定的 `HTTPS_PROXY` / `HTTP_PROXY`。
