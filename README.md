# dual-web

给 Hermes Agent 用的聚合搜索后端：一次 `web_search` 同时调用多个 web provider，按 URL 去重后轮流合并结果。

默认聚合 **DeepSeek** 与 **Firecrawl**。子 provider 可以通过配置改成任意已注册的 Hermes web provider。

推荐搭配：

```yaml
web:
  search_backend: dual-web    # 本插件
  extract_backend: firecrawl  # 正文抓取交给 Firecrawl
```

---

## 一、它做什么

- 注册一个名为 `dual-web` 的 Hermes web search provider
- 每次搜索按配置顺序调用子 provider，把结果合并成一份返回给 Agent
- 每条命中附带 `vendor` 字段，标明来源（`deepseek` 或 `firecrawl`）
- 单个子 provider 失败不会中断整次搜索；部分失败会放在 `data.errors` 里
- **只实现搜索，不实现正文抓取**；`web_extract` 仍由你单独配置的后端负责

---

## 二、前置 Hermes 配置

### 1. Hermes 版本

需要 Hermes Agent `>= 0.22.0`。检查：

```bash
hermes --version
```

### 2. 安装并启用 DeepSeek 搜索插件

本插件默认聚合的 `deepseek` 来自官方目录里的 `deepseek-search-fetch` 插件：

```bash
hermes plugins install deepseek-search-fetch --enable
hermes config set DEEPSEEK_API_KEY sk-xxxxxxxx
```

确认它已启用：

```bash
hermes plugins list --enabled --user
```

### 3. 确认 Firecrawl 可用

Firecrawl 是 Hermes 内置插件（`web-firecrawl`），默认自动加载，不需要写进 `plugins.enabled`。

至少满足以下一种：

```bash
# 方式一：自己的 Firecrawl API Key
hermes config set FIRECRAWL_API_KEY fc-xxxxxxxx

# 方式二：自建 Firecrawl
hermes config set FIRECRAWL_API_URL http://localhost:3002

# 方式三：Nous 托管的 Tool Gateway（已登录且有对应权益）
```

同时确认没有把内置 Firecrawl 禁用：

```bash
hermes plugins list | grep -i firecrawl
```

如果 `plugins.disabled` 里有 `web-firecrawl`，先移除。

### 4. 至少一个子 provider 可用

`dual-web` 的规则是“至少一个子 provider 可用就算可用”。两个都不可用时，搜索会返回失败，并在日志里说明原因。

### 5. 关于 Nous 托管搜索

如果你平时用 Nous 托管的 web search，注意显式 `web.search_backend: dual-web` 会覆盖托管搜索的选择。Firecrawl 自己的托管路由仍由 Firecrawl provider 解析，不受影响。

---

## 三、安装 dual-web

### 方式一：从 GitHub 安装（推荐）

```bash
hermes plugins install lrita/dual-web --enable
```

固定到某个提交，避免上游变化：

```bash
hermes plugins install lrita/dual-web --ref <40 位 commit SHA> --enable
```

`--ref` 只接受完整的 40 位 commit SHA。

### 方式二：本地开发时安装

```bash
hermes plugins install file:///Users/huyanqing.16/lib/python/dual-web --enable
```

### 方式三：手动克隆

```bash
git clone https://github.com/lrita/dual-web.git ~/.hermes/plugins/dual-web
hermes plugins adopt dual-web
hermes plugins enable dual-web
```

### 安装后确认

```bash
hermes plugins list --enabled --user
hermes plugins show dual-web
```

安装时 Hermes 会执行安全扫描：`dangerous` 会直接阻断，`caution` 需要确认；`--force` 不能覆盖 `dangerous`。

---

## 四、配置：搜索用 dual-web，抓取用 firecrawl

推荐配置：

```bash
hermes config set web.search_backend dual-web
hermes config set web.extract_backend firecrawl
```

对应的 `config.yaml`：

```yaml
web:
  search_backend: dual-web
  extract_backend: firecrawl
```

说明：

- `web.search_backend` 只影响 `web_search`
- `web.extract_backend` 只影响 `web_extract`
- 两者都优先于共享键 `web.backend`；不要用 `web.backend: dual-web`，因为 `dual-web` 没有实现 `extract`

### 配置子 provider

默认值：

```yaml
plugins:
  entries:
    dual-web:
      settings:
        providers:
          - deepseek
          - firecrawl
```

通过 CLI 修改：

```bash
# 列表形式（推荐）
hermes config set plugins.entries.dual-web.settings.providers '["deepseek","firecrawl"]'

# 逗号分隔的字符串也可以
hermes config set plugins.entries.dual-web.settings.providers deepseek,firecrawl
```

可以换成任意已注册的 web provider，例如：

```bash
hermes config set plugins.entries.dual-web.settings.providers '["deepseek","exa","tavily"]'
```

规则：

- 名称必须与 provider 的 `name` 一致，全部小写
- 重复项会自动去重
- 写成 `dual-web` 会被忽略，避免自调用
- 列表为空或无效时回退到 `deepseek` + `firecrawl`
- 配置在**每次搜索时读取**，改完不需要重启

### 从 0.1.x 升级

0.2.0 起 provider 名从 `dual` 改为 `dual-web`。如果之前已经配置过后端名，需要改过来：

```bash
hermes config unset web.search_backend
hermes config set web.search_backend dual-web
```

插件目录名、插件键 `dual-web` 和 `plugins.entries.dual-web` 不受影响。

---

## 五、验证

```bash
hermes config get web.search_backend
hermes config get web.extract_backend
hermes tools
```

`hermes tools` 的 Web Search & Extract 列表里应出现 **dual-web** 一行。

在会话里让 Agent 搜索一次，然后看日志：

```bash
hermes logs -n 200 | grep dual-web
```

正常的三类日志：

```text
[dual-web] search start query='今天的汇率' limit=5 providers=['deepseek', 'firecrawl']
[dual-web] provider=deepseek query='今天的汇率' results=5 elapsed_ms=820.4
[dual-web] provider=firecrawl query='今天的汇率' results=5 elapsed_ms=1130.2
[dual-web] search done query='今天的汇率' counts={'deepseek': 5, 'firecrawl': 5} merged=8 duplicates=2 errors=0 elapsed_ms=1951.1
```

每行都包含：

- 实际搜索词（超过 500 字符会截断）
- 子 provider 名称
- 该 provider 实际返回的条数
- 耗时、合并后的条数、去重条数、错误数

---

## 六、日志与排障

日志 logger 名称：`plugins.dual_web`。所有 Hermes 内部 API 相关的问题都带稳定前缀：

```text
[dual-web][HERMES-API]
```

同一个缺失 provider、配置错误或内部 API 错误只完整告警一次，后续重复出现降为 DEBUG，避免 Hermes 的 `check_fn` 每个 turn 轮询时刷屏；每次搜索的 `provider=...` 调用日志不受影响，仍然逐次记录。

### 常见日志与含义

| 日志 | 含义 | 处理方式 |
|---|---|---|
| `provider=xxx 未注册` | 子 provider 未安装、未启用，或名字写错 | `hermes plugins list` 确认；检查 `settings.providers` 拼写 |
| `provider=xxx 不支持 search` | 该 provider 只做抓取，不能搜索 | 从 `settings.providers` 移除它 |
| `[HERMES-API] get_provider(...) 抛异常` | Hermes 内部 API 变化或注册表异常 | 对照当前版本的 `agent/web_search_registry.py`；必要时固定 Hermes 版本 |
| `[HERMES-API] provider 缺少 supports_search()` | provider 接口变化 | 升级或修复本插件 |
| `[HERMES-API] search() 返回 ... 预期 dict` | provider 返回结构变化 | 对照 `agent/web_search_provider.py` 的响应契约 |
| `[HERMES-API] data.web 不是列表` | 搜索响应结构变化 | 同上 |
| `search(query, limit) 抛 TypeError` | 搜索方法签名变化 | 检查 provider 的 `search` 参数 |
| `search produced no results` | 所有子 provider 都没返回结果 | 看同一次调用前面的 `provider=...` 日志 |

### 内部 API 变化时怎么排查

本插件依赖两个内部路径：

- `agent.web_search_provider.WebSearchProvider`
- `agent.web_search_registry.get_provider`

它们不是稳定的公开 API。Hermes 升级后如果日志里出现 `[dual-web][HERMES-API]`：

1. 记录日志里的 Hermes 版本（注册时会打印 `hermes=` 字段）
2. 对照当前源码的 `agent/web_search_registry.py` 和 `agent/web_search_provider.py`
3. 运行 `hermes plugins doctor ~/.hermes/plugins/dual-web --ci`
4. 必要时固定 hermes-agent 到已知兼容的提交，或修改本插件适配新接口

---

## 七、行为与限制

- **串行调用**：子 provider 按配置顺序逐个调用，延迟是各家之和；这样更不容易触发限流
- **轮流合并**：两家结果交替取出，避免第一家的结果占满配额
- **按 URL 去重**：以完整的 URL 字符串为准，不做域名归一化
- **只聚合搜索**：`web_extract` 不会聚合；推荐继续用 Firecrawl
- **缓存**：Hermes 以 provider 名 `dual-web` 为键缓存搜索结果，默认 20 分钟；修改 `providers` 配置后，已缓存的 query 在 TTL 内仍可能返回旧结果
- **费用**：每次搜索会对每个子 provider 各发起一次请求，费用和延迟都会累加
- **绕过厂商级缓存/兜底**：插件直接调用子 provider 的 `search()`，不经过 Hermes 对单个 provider 的搜索缓存与 keyless rescue；聚合结果本身仍由 Hermes 以 `dual-web` 为键缓存

---

## 八、更新与卸载

```bash
hermes plugins update dual-web
hermes plugins remove dual-web
```

卸载前建议先把搜索后端切回其他 provider：

```bash
hermes config unset web.search_backend
```

---

## 九、本地开发

```bash
# 语法检查
python3 -m py_compile __init__.py

# 用 Hermes 自己的插件诊断流程校验
hermes plugins doctor . --ci
```

修改后如果是以 `file://` 安装的，重新安装一次：

```bash
hermes plugins install file:///Users/huyanqing.16/lib/python/dual-web --force --enable
```

---

## 十、兼容性说明

本插件通过 Hermes 的 `PluginContext.register_web_search_provider()` 注册 provider，这是插件扩展点；但解析其他 provider 时用了内部函数 `agent.web_search_registry.get_provider()`。Hermes 升级后请先跑一次 `hermes plugins doctor` 和实际搜索，确认日志中没有 `[dual-web][HERMES-API]` 错误。
