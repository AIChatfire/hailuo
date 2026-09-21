# hailuo-service

hailuo（`hailuoai.video`）图片生成的**异步**出口。参考 `../jimeng` 的骨架落地，
做成 `POST` 受理 → `GET` 轮询的两段式。

```
POST   /async/v1/images/generations        → 202 {"task_id": "hailuo_…"}   只回一个 id
GET    /async/v1/images/generations/{id}   → 202 排队态 / 200 {data,created,usage}
GET    /async/v1/images/generations        → 本 Key 的任务列表
DELETE /async/v1/images/generations/{id}   → 删除**已终态**的任务
GET    /async/v1/models                    → 能力清单 + 上游模型注册表
```

契约全文见 **[`docs/INTERFACE.md`](docs/INTERFACE.md)**（冻结）；
上游契约与逆向导出的结论见 **[`docs/UPSTREAM.md`](docs/UPSTREAM.md)**。

**重点能力是图生图（`hailuo-i2i`）** —— 见 §3。

---

## 0. 我要做什么 → 看哪个文件

| 我想… | 看这里 |
|---|---|
| 接这个服务 | `docs/INTERFACE.md` |
| **改 `yy` 签名** | `app/upstream/hailuo/sign.py`（**5 条抓包向量自证**） |
| 改"谁能做什么 / 默认模型" | `app/models.py`（唯一的能力注册表） |
| 改请求翻译（`size`/`n`/图片张数） | `app/service.py::build_plan`（**纯函数，重点覆盖**） |
| 改上游请求构造 / 错误码分类 | `app/upstream/hailuo/client.py` |
| 改输入图上传 | `app/upstream/hailuo/upload.py` |
| 改输入图下载/归一化 | `app/media.py` |
| 改"什么时候轮到谁跑" | `app/coordinator.py` + `app/gate.py` |
| 改任务存取 | `app/store.py` |
| 改响应形状 | `app/service.py::view` —— **唯一出口** |
| 改埋点 | `app/observability.py` —— **唯一收拢点** |

---

## 1. 跑起来

```bash
cp .env.example .env      # 填 HAILUO_TOKEN
docker compose up -d --build
curl -s localhost:8300/healthz          # {"status":"ok"}
```

⚠️ **数据库刻意不发布宿主端口**（`db` 只有容器内网的 `5432/tcp`）：
应用走 compose 内网 `db:5432`，宿主端口毫无必要，而发布它只会制造冲突源
（本机 5432 常年被别的项目占着 ⇒ `compose up` 报 "port is already allocated"，
看着像本服务的问题，其实不是）。要在宿主上做管理：

```bash
docker compose exec db psql -U hailuo -d hailuo
```

本机直接跑（SQLite 即可，无需 PostgreSQL）：

```bash
export TASK_DB='sqlite+pysqlite:///./hailuo.db'
export HAILUO_TOKEN='<浏览器 localStorage 里的 JWT>'
export API_KEYS='sk-xxxxxxxx'           # 留空 = 关闭鉴权（仅内网）
gunicorn -c gunicorn_conf.py "app.main:create_app()"
```

🔴 末尾那对**括号不能省**：目标是**工厂**而不是模块级 `app` 对象。
写成 `app.main:app` 会得到 `Failed to find attribute 'app' in 'app.main'` /
`App failed to load.` —— **单测全绿也照样炸**，因为它们都直接调 `create_app()`。
`tests/test_wiring.py::test_dockerfile_cmd_target_resolves` 钉的就是这条。

⚠️ 直接用 gunicorn 跑时**记得设 `COORDINATOR_ENABLED=0`**，
否则它会真的去建任务（**计费**动作）。

### 凭据只要一个

hailuo **唯一的硬前提**是 `token` 头里的 JWT（浏览器 localStorage）。
没有 cookie、没有 aksk、没有 OAuth。
取法：登录 hailuoai.video → DevTools → Application → Local Storage → 复制 JWT。

⚠️ 它会**过期**（抓包样本 `exp` 距抓取约 26 天），过期后上游回 HTTP 401。

### 直接跑测试

```bash
python -m pytest -q        # 215 用例，零真实上游调用，约 4 秒
```

---

## 2. 这套设计的七条主线

### 2.1 受理不碰上游，协调器才是唯一执行者

`POST` **只落库就返回**（请求内零上游往返）。真正建任务由后台协调器做，
因为它要受节奏闸门约束、要能重试、要防止重复提交 —— 而**建任务是计费动作**。

hailuo 没有回调，只能自己轮询。⇒ 协调器**不是可选增强，是链路的一环**：
没有它，任务永远停在 `queued`（连建任务都不会发生），超时看门狗也永不触发。

### 2.2 🔴 一轮 tick 只打**一次**上游查询

这是本项目与即梦最大的**架构差异**，也是"把并发提上去"的前提。

即梦要按 id 逐个/合并查询；而 hailuo 的
`POST /api/feed/creation/my/batch` **不带 id 参数** —— 它回的是"我最近的 N 条"。
⇒ **一轮 tick 的上游查询次数与在途任务数完全无关，恒为 1 次。**

否则提并发等于把上游请求量一起乘 N，而那正是风控最敏感的维度。
`tests/test_service.py::test_poll_uses_one_upstream_call_for_many_tasks`
断言 4 个在途任务只产生 1 次查询。

### 2.3 并发上限放在库里数，不放在信号量里

`HL_CONCURRENCY` 的判据是 `count(status='in_progress')`。

用 `Semaphore` 的话，**进程重启后信号量归零**，而库里那些任务其实还在上游跑
⇒ 重启后会超发。按库计数天然重启安全，也天然跨 worker。

### 2.4 降级必须可见

任何"请求了 A、实际做了 B"都进响应的 `degradations`：默认模型代入、`size` 换算、
比例吸附、张数吸附、输入图归一化、能力表读不到而退回冻结快照。

**静默降级 = 让人按 A 的预期为 B 付钱。** 这条在 `build_plan` 里是硬约束：
凡是会改变结果或花费的取舍，都必须在那里留痕。

### 2.5 图生图（重点能力）的四条适配规则

1. **`fileList` 顺序 = 请求顺序**（上游按顺序理解垫图语义）；
2. **张数上限按"所选模型自己声明的" `maxSupportImageCount` 校验**
   （`gpt-image-1.5` 只有 **3**，`nano_banana*` 是 14，`gpt-image-2*` 是 16）——
   这是**服务端声明值**，不是经验值。超了就 400，**绝不"收下 N 张只用第 1 张"**；
3. **`referenceMode` 默认不发** —— 抓包的 i2i 请求里 `imageParameter` **没有**这个键；
4. **`useOriginPrompt` 恒 `true`** = "不要改写我的 prompt"。
   反过来等于让上游偷偷重写调用方的提示词，那是**没被要求的降级**。

### 2.6 鉴权：静态 Bearer Key 白名单

`Authorization: Bearer <key>` 与 `API_KEYS`（逗号分隔）做白名单比对。
没有用户体系、没有令牌签发、没有过期时间 —— 就是一份静态名单。

- `API_KEYS` 为空 ⇒ **鉴权整体关闭**，启动会打 WARNING；
- 通过后 Key 被换成**不可逆指纹** `credential_id = HMAC-SHA256(secret, key)`，
  `secret` 首次启动随机生成并存在任务库 `meta` 表 ⇒ **明文 Key 永不落库**；
- 任务与该指纹绑定：换一把 Key 读别人的任务 ⇒ **404，且不发上游请求**（本地拦死）。

**已知弱点（不粉饰）**

| # | 问题 | 影响 |
|---|---|---|
| 1 | `key not in api_keys` 是**明文元组的 `in`**，逐元素 `==`，**不是恒定时间比较** | 理论上可按响应时间差逐字节猜 Key。Key 是高熵随机串，实际利用难度大，但正确写法是 `hmac.compare_digest` |
| 2 | **没有按 Key 的配额/限流** | 闸门是**全局**的；任何一把合法 Key 都能持续消耗额度 |
| 3 | **轮换 Key 会孤儿化历史任务** | 任务绑定旧指纹，换 Key 后旧任务 404（刻意设计，但运维要知道） |
| 4 | **鉴权关闭是默认态** | 只有一条启动 WARNING 兜底；`/readyz` 不看它 |
| 5 | 无 IP 白名单 / 无 mTLS / 无审计日志（只有一行请求摘要） | — |

⇒ 生产部署建议：**必须设 `API_KEYS`**，并在网关层再叠一层鉴权与限流。

### 2.7 不制造假能力

- **视频链路不注册**：上游模型信息端点一并返回 24 个 `videoModels`，
  但视频链路未取证 ⇒ 不出现在 `/async/v1/models`（见 `models.DELIBERATE_ABSENCES`）；
- 未配凭据时 `POST` 回 **503** 而不是假装受理；
- 上游凭据失效也是 **503**（部署问题），不是 401（调用方的问题）；
- **查不到单价就不给 `forecast_credits`** —— 编一个数字等于说谎。

---

## 3. 图生图怎么调

```bash
curl -s -X POST localhost:8300/async/v1/images/generations \
  -H 'Authorization: Bearer sk-xxxxxxxx' -H 'Content-Type: application/json' \
  -d '{"model":"hailuo-i2i","prompt":"把背景换成雪山，保留人物光影",
       "image":["https://…/ref.png"],"size":"4096x4096"}'
# → {"task_id":"hailuo_…"}

curl -s localhost:8300/async/v1/images/generations/hailuo_… \
  -H 'Authorization: Bearer sk-xxxxxxxx'
# → 非终态：202 {"task_id":"…","status":"in_progress"}
# → 成功  ：200 {"data":[{"url":"https://cdn.hailuoai.video/…png"}],
#                "created":1789923012,"usage":{"images":1,"forecast_credits":8}}
```

要点：

- **`image` 必须是数组**，即使一张也要写 `["…"]`（传字符串会被明确拒绝并给出正确写法）；
- **张数上限取决于模型**：`gpt-image-1.5` 只能 3 张，`nano_banana*` 14 张，`gpt-image-2*` 16 张；
- 不写 `model` ⇒ 默认 `nano_banana21_flash`（**唯一有端到端抓包证据**的模型），并在 `degradations` 写明；
- 结果 URL 是**去水印**那条（`downloadURL.withoutWatermarkURL`）；
- 产物是上游直链，**不做转存**；有效期**未取证**。

---

## 4. 扩容前必读

**默认 `WORKERS=1` 是架构约束，不是保守参数：**

- 节奏闸门（最小间隔 / 每分钟上限 / 风控冷却）是**进程内**状态，
  副本数 N 等于把限速整体乘 N —— 恰好踩在上游风控最敏感的维度；
- 协调器靠数据库租约选主，多 worker 虽安全但非持锁进程只会空转。

**提吞吐的正确顺序**：先把 `HL_CONCURRENCY` 从 1 提上去
（**本项目未实测上游并发上限** —— 默认 1 是策略选择，不是实测结论），
再考虑多副本。放宽并发会成倍放大额度消耗速率与风控暴露面，
是**策略选择**不是技术限制。

---

## 5. 测试

```bash
python -m pytest -q
```

- **零真实上游调用**：所有用例注入假上游（`httpx.MockTransport` 覆盖**全部**端点），
  一个字节都不发出去。建任务是计费动作，这条红线由夹具保证，
  而不是靠"测试里记得别调真接口"。
- **缺依赖就响亮失败，不静默跳过** —— 跳过会让人把"没跑"当成"跑过了"。
- **测试绝不碰真实 DNS**：`_is_private_host` 会对域名解析，
  在受限环境里那是一次真实网络调用（实测把整进程跑挂）——
  `tests/test_media.py` 有个 autouse 夹具把解析换成本地确定值。

| 文件 | 覆盖 |
|---|---|
| `test_sign.py` | **5 条抓包向量** + `encodeURIComponent` 语义 + 键序/紧凑格式 |
| `test_service.py` | **翻译层**（`build_plan`）+ 提交/轮询/响应形状/归属/删除 |
| `test_api.py` | HTTP 契约（`docs/INTERFACE.md` 的可执行版本） |
| `test_upstream.py` | 客户端信封与错误映射 + 上传 + 能力表解析 |
| `test_media.py` | 魔数嗅探 / SSRF 防线 / 归一化 |
| `test_wiring.py` | 可观测性纪律 / **配置旋钮** / 闸门 / 存储 / 协调器 / 注册表 |

### 端到端探针：零成本优先，花钱要显式开闸

```bash
# 全部零成本阶段（默认）
python scripts/e2e.py

# 加上真实上传（免费，但会在账号里留一个素材）
python scripts/e2e.py --phases sign,capabilities,accept,dry,upload --allow-upload

# 🔴 真实出图（**扣额度**；不加 --allow-real-submit 只打印"将要发生什么"）
python scripts/e2e.py --phases generate --allow-real-submit
```

| 阶段 | 花额度 | 做什么 |
|---|---|---|
| `sign` | ❌ | 5 条抓包向量**离线**复算（**不出网**） |
| `capabilities` | ❌ | 读两个公开配置端点，打印实读到的模型表 |
| `probe` | ❌ | 用 `my/processing` 做凭据自检（不建任务） |
| `accept` | ❌ | 受理并断言**只回一个 task_id**（协调器没启 ⇒ 零上游往返） |
| `dry` | ❌ | 走完整条翻译链：签名 + 查询串 + body 全构造，**不发请求** |
| `upload` | ❌ | 真打上游跑 OSS 三段式（免费；需 `--allow-upload`） |
| `generate` | 🔴 | 真建任务 + 轮询到终态（需 `--allow-real-submit`） |

`generate` 单独隔出来，是因为建任务是**计费动作**。

### 接线门禁（`tests/test_wiring.py` + `ruff.toml`）

门禁分两层，缺一层都会漏：

- **静态**：`ruff check`（`F` / `E9` / `BLE` / `S110` / `PLC0415` / `SLF001`），
  **刻意不收风格规则** —— 一次报上百条的门禁等于没有门禁；
- **动态**：`test_wiring.py` 里的用例**真的把链路跑一遍**
  （用 `httpx.MockTransport` 打上游，断言**确实产生了一条上报**）——
  "接线"这件事只有跑才证明得了。

两条配置门禁尤其值得留：**每个被读的配置项都必须存在**（否则 `AttributeError`）、
**每个配置项都必须有人读**（没人读的旋钮 = 假配置）。
后者在本次开发中**真的抓到了 3 个死旋钮**：`HL_MAX_WAIT`（与 `TASK_TIMEOUT` 重复）、
`MAX_INPUT_BYTES`（与 `MAX_DOWNLOAD_BYTES` 重复）、`POLL_GRACE`（读了却没消费）。
前两个已删除（一个概念只留一个旋钮），第三个已接线并有用例守着。

---

## 6. 诚实边界（未取证的事）

完整清单在 `docs/UPSTREAM.md` §8。最关键的三条：

| 事项 | 状态 |
|---|---|
| **端到端真实出图** | 🔴 **本项目未做**（建任务即计费）。上游链路已按抓包与非计费只读调用逐层验证，但"建任务 → 出图"这一段**没有实跑过** |
| 产物 URL 有效期 | **未取证** ⇒ 原样透传、不做转存 |
| 上游并发/频控阈值 | **未界定** ⇒ 默认并发 1 是策略选择，不是实测上限 |

---

## 7. 目录

```
app/
  main.py             FastAPI 装配：路由 / 错误信封 / lifespan / 埋点接线
  config.py           Settings（每个旋钮都必须有人读）
  errors.py           错误分类体系（对外错误信封）
  models.py           能力注册表 + 上游模型冻结快照（唯一真相）
  media.py            输入图嗅探 / 下载（含 SSRF 防线）/ 归一化
  service.py          编排：受理 / 翻译（build_plan）/ 提交 / 轮询 / 响应构造
  coordinator.py      后台协调器（推进任务的唯一执行者）
  gate.py             节奏闸门（间隔 / 每分钟上限 / 风控冷却）
  store.py            SQLAlchemy 任务存储 + 指纹密钥 + 协调器租约
  observability.py    logfire + loguru 单一收拢点
  upstream/hailuo/    sign（yy 签名）/ client / upload（OSS STS）/ capabilities
tests/                见 §5
docs/                 INTERFACE.md（对外契约）/ UPSTREAM.md（上游契约与逆向结论）
scripts/e2e.py        零成本优先的端到端探针
```
