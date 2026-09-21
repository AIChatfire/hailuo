# 上游契约（hailuoai.video）

> 本文件记录**上游实际长什么样**、我们怎么证出来的、以及**哪些还没证**。
> 对外契约在 `docs/INTERFACE.md`。
>
> 取证方式：前端 JS 分包（Next.js，`prod-en-0.1.869`）+ 用真实浏览器会话做**只读**
> 验证（列表查询 / 配置读取 / 上传凭据），**零建任务**、零额度消耗。
> 项目自身的 5 条抓包向量固化在 `app/upstream/hailuo/sign.py::VECTORS`。

---

## 0. 一句话总览

```
token（JWT） + yy（MD5 签名）
   │
   ├── POST /v2/api/multimodal/generate/image    建任务（**计费**）→ {id, task.batchID}
   ├── POST /api/feed/creation/my/batch          批量查（回"我最近的 N 条"）
   ├── POST /v4/api/multimodal/video/processing  按 id 点名直查（图片/视频共用，见 §4.3）
   ├── POST /api/feed/creation/my/processing     查"我有没有任务在跑"（廉价）
   ├── GET  /v1/api/files/request_policy         取 OSS STS 上传凭据
   ├── PUT  https://{bucket}.{endpoint}/{dir}/{name}   直传 OSS
   └── POST /v1/api/files/policy_callback        上传确认 → fileID
```

外加两个**公开、免鉴权、GET** 的配置端点（能力表来源，零成本）：

```
GET /public/api/config/web/common_config                 → create_image_models（清单 + 张数上限）
GET /public/v2/api/multimodal/video/model/info           → imageModels[]（档位 + 单价）
```

---

## 1. 鉴权

| 头 | 内容 | 必填 |
|---|---|---|
| `token` | 浏览器 localStorage 里的 JWT | **是** |

- **只有一个硬前提：`token`。** 不需要 cookie、不需要 aksk、不需要 OAuth。
- 取法：登录 hailuoai.video → DevTools → Application → Local Storage → 复制 JWT。
- 缺 `token` ⇒ 上游回 **HTTP 401**（不是业务码）。
- 🔴 它等价于登录态，且会**过期**（抓包样本的 `exp` 距抓取时间约 26 天）。
  过期后是 401 ⇒ 本服务映射为 `invalid_api_key`（调用方视角）/ 部署方需换 token。

### 1.1 `yy` 签名 —— 从 webpack 逐字还原

前端 chunk `3340` 的 axios 请求拦截器：

```js
W.interceptors.request.use(e => {
  e.headers.token = getLocalStorageToken();
  let i = Date.parse(new Date().toString());      // 毫秒，已截断到秒
  let l = mC(i);                                  // 公共参数
  e.params = {...P6(e.params), ...l};
  let u = wQ(e.url, e.params);                    // path?<合并后的查询串>
  let {encrypt: s, bodyString: c} =
      b({time: i, body: e.data, hasSearchParamsPath: u,
         method: e.method, bodyToYY: e.headers.yy || null});
  e.headers.yy = s; e.data = c; e.url = u; e.params = {};
});
```

其中 `b`（同一 chunk）：

```js
b = ({hasSearchParamsPath: t, bodyToYY: o, method: n, time: r, body: a}) => {
  let i = {};
  if (n && (n.toLowerCase() === "post" || n.toLowerCase() === "delete")) i = a || {};
  let l = i = JSON.stringify(i);          // bodyString：GET 恒为 "{}"
  if (o) i = o;                           // 已有 yy 时用 yy 顶替 body 段
  let d = encodeURIComponent(t) + "_" + i + MD5(r.toString()) + "ooui";
  return {encrypt: MD5(d), bodyString: l};
};
```

⇒ **算法**：

```
time      = floor(now_ms / 1000) * 1000          # Date.parse(new Date().toString())
query     = 公共参数（见 §2）
full      = path + "?" + query
body_json = JSON.stringify(body)   (POST/DELETE 且 body 非空)   否则 "{}"
yy        = md5( encodeURIComponent(full) + "_" + body_json + md5(str(time)) + "ooui" )
```

实现见 `app/upstream/hailuo/sign.py`。**5 条真实抓包逐条复算通过**
（`tests/test_sign.py`）：

| 向量 | 端点 | 复算 |
|---|---|---|
| batch 列表（cursor/limit/feedTypes） | `/api/feed/creation/my/batch` | ✅ |
| processing #1 / #2 / #3 | `/api/feed/creation/my/processing` | ✅ ✅ ✅ |
| 图片上传确认 | `/v1/api/files/policy_callback` | ✅ |

### 1.2 三个最容易写错的点

1. **`encodeURIComponent` ≠ `quote(safe="")`。** JS 不转义 `-_.!~*'()`，
   而 Python 的 `quote(safe="")` 会转义它们。多转义 ⇒ 签名必错，
   而错误文案是笼统的 `code:2 请求异常，请检查请求参数`，几乎指不到这里。
2. **body 段的键序与紧凑格式都是签名输入。** 上游是 `JSON.stringify(e.data)`：
   保留键序、无空格、中文不转义（`ensure_ascii=False`）。
   先"normalize 成排序键 + indent=2"会让签名全错。
3. **GET 的 body 段是 `"{}"`，不是空串。** 写空串会让签名在 POST 上全对、
   在 GET 上全错 —— 而 GET 恰好是上传策略那一步。

### 1.3 `yy` 是**必须**的吗

**是。** 实测对照（同一 token、同一 body）：

| 情形 | 结果 |
|---|---|
| 不带 `yy` | `400 {"statusInfo":{"code":2,"message":"请求异常，请检查请求参数"}}` |
| 带**错误**的 `yy` | 同上，**同一个错误** |

⇒ 从错误文案**分辨不出**是签名错还是参数错。这正说明必须先把签名做对
（我们靠抓包向量离线自证，而不是靠试探上游）。

---

## 2. 公共查询参数

由前端 `mC()` 生成，**全部参与签名**。服务端没有 `navigator`/`screen`，
所以由 `Settings.device_profile()` 显式注入（默认 = 抓包环境）：

| 键 | 抓包环境取值 | 来源 |
|---|---|---|
| `device_platform` | `web` | 常量 |
| `app_id` | `3001` | 常量 |
| `version_code` | `22203` | 常量 |
| `biz_id` | `0` | 常量 |
| `unix` | 毫秒（截断到秒） | `Date.parse(new Date().toString())` |
| `lang` | `zh-Intl` | locale 映射 |
| `uuid` | `3de88ad0-…` | localStorage |
| `device_id` | `100000000000000000` | localStorage |
| `os_name` | `Mac` | 平台探测 |
| `browser_name` | `chrome` | UA 探测 |
| `device_memory` | `32` | `navigator.deviceMemory` |
| `cpu_core_num` | `10` | `navigator.hardwareConcurrency` |
| `browser_language` | `zh-CN` | `navigator.language` |
| `browser_platform` | `MacIntel` | `navigator.platform` |
| `screen_width` / `screen_height` | `2560` / `1440` | `screen.width/height` |

⚠️ 服务端**不校验这些值的真实性**（实测改 `screen_*` 仍 200），
但要**保持会话内稳定** —— 同一账号短时间从多个"设备"发起会被风控关注。

---

## 3. 建任务（🔴 计费）

```http
POST /v2/api/multimodal/generate/image
token: <JWT>
yy: <签名>

{
  "quantity": 1,
  "parameter": {
    "modelID": "nano_banana21_flash",
    "desc": "a cat",
    "fileList": [ { …fileList entry… } ],
    "useOriginPrompt": true,
    "aspectRatio": "Auto",
    "resolution": "4K"
  },
  "projectID": "0"
}
```

字段来源：前端 chunk `9563` 的 `MS()`：

```js
let r = {quantity: e.quantity,
         parameter: {modelID, desc, fileList, useOriginPrompt,
                     aspectRatio, resolution, quality, referenceMode},
         projectID: e.projectID};
// imageExtra 仅在 e.extra 存在时才带
```

**响应**（🔴 2026-09-21 真实文生图实测修正 —— 之前把两个 id 混为一谈）：

```json
{"statusInfo": {"code": 0, "…": "…"},
 "data": {"id": "<feed 记录 id>",
          "task": {"batchID": "<批次 id>", "videoIDs": ["<feed 记录 id>"]},
          "isFirstGenerate": true}}
```

- `data.id` 是 **feed 记录 id**（= 之后 `my/batch` 里 `feeds[].commonInfo.id`）；
- **`data.task.batchID` 才是批次 id**（= `my/batch` 的 `batchFeeds[].batchID`、v4 的 `batchID`）
  —— **轮询必须用它**。拿 `data.id` 当批次 id ⇒ 永远查不到任务
  （实测教训：图已成功、钱已花，轮询却"查无此任务"，180s 超时）。
- 两个 id 同秒生成、数值相邻但**不同**（上游雪花 id：批次先建、记录后建）。
- `isFirstGenerate` 未取证其语义，本服务只透传到 trace。

### 3.1 `fileList[]` 一项的字段集（来自 feed 回显，逐字段照抄）

```json
{
  "id": "558122239187886085",        // ← policy_callback 返回的 fileID
  "name": "4591a45f-….jpeg",         // ← 上传时的 fileName（UUID 名）
  "type": "jpeg",
  "url": "https://cdn.hailuoai.video/moss/prod/…/xxx.jpeg",
  "characterID": "",
  "coverUrl": "…（同上，带 ?x-oss-process=image/resize,p_50/format,webp）",
  "frameType": 3,
  "referenceType": 0,
  "characterUrl": "",
  "duration": 0,
  "assetFileType": 1,
  "videoID": "",
  "durationMs": 0
}
```

⚠️ 这些常量（`frameType=3` / `referenceType=0` / `assetFileType=1` / `duration=0`）
在抓包里是固定形态，**缺失会导致上游不接受参考图**。
`tests/test_upstream.py` 逐字段钉住它。

### 3.2 `promptStruct`

上游在 feed 回显里带 `promptStruct`（富文本结构）：

```json
{"value":[{"type":"paragraph","children":[{"text":"a cat"}]}],
 "length":5,"plainLength":5,"rawLength":5}
```

调用方给的是纯文本 ⇒ 本服务**只造单段落**结构（拆多段落是我们没有依据的加工）。
三个长度字段都取纯文本长度（与抓包一致）。

### 3.3 `useOriginPrompt`

- 前端：`useOriginPrompt: !isEnablePromptOptimization`。
- 本服务**恒发 `true`** ⇒ 即"**不要改写调用方的提示词**"。
  反过来（`false`）等于让上游偷偷重写 prompt，那是**没被要求的降级**。

### 3.4 `referenceMode`

抓包的 i2i 请求里 `imageParameter` **没有**这个键 ⇒ 本服务**默认不发送**，
只有调用方显式传 `reference_mode` 时才带上。
已知取值（前端 `41332`/`extend` 等）：`image-reference` / `subject-reference` /
`extend` / `edit`。`extend` 是"扩图"，**不要**当默认。

---

## 4. 查询任务

### 4.1 批量查（本项目的"批量轮询"来源）

```http
POST /api/feed/creation/my/batch
{ "cursor":"", "limit":30, "type":"next", "scene":"create",
  "projectID":"0", "feedTypes":[0,1,2,3,4,5,6] }
```

🔴 **该端点不带 id 参数** —— 它回的是"**我最近的 N 条**"。
⇒ 一轮 tick 的上游查询次数与在途任务数**无关，恒为 1 次**。
这正是"把并发提上去"的前提：否则提并发等于把上游请求量一起乘 N，
而那正是风控最敏感的维度。

**响应结构**：

```
data.batchFeeds[]                 # 一次生成 = 一个 batch
  ├─ batchID                      # ← 建任务响应里的 data.task.batchID（不是 data.id！）
  ├─ batchCreateTime
  ├─ feedType                     # 1 = 图片
  └─ feeds[]                      # quantity>1 时多张
       ├─ feedType                # 1 = 图片
       ├─ commonInfo{id, batchID, createTime, status, humanCheckStatus, postStatus}
       ├─ feedCoverInfo{coverURL, width, height, thumbURL}
       ├─ feedTags[]{tagText}     # 例如 ["参考图 1", "Nano Banana 2", "4K", "自动"]
       ├─ modelParameter.imageParameter{modelID, desc, fileList, aspectRatio, resolution}
       └─ metaInfo.imageMetaInfo.mediaInfo
            ├─ url                                    # 带水印
            ├─ downloadURL.watermarkURL
            ├─ downloadURL.withoutWatermarkURL         # ← 本服务默认给这条
            ├─ downloadURL.fileName / fileID
            └─ width / height
data.processing : bool
data.hasNext / hasTotal
```

⚠️ **quantity=N ⇒ 该 batch 有 N 条 feeds，且不是同时就绪**（2026-09-21 n=2 实测：
第一条 status=2 带产物时第二条还在跑）⇒ 多张任务的收口必须**凑齐计划张数**，
"见到成功就终态"会丢产物。聚合规则实现见 `service._apply_feeds`。

### 4.2 在途查（廉价存在性判断）

```http
POST /api/feed/creation/my/processing
{ "projectID": "0" }
```

```json
{"processing": false, "onProcessingImageNum": 0, "onProcessingVideoNum": 0,
 "onProcessingToolNum": 0, "onProcessingAudioNum": 0, "batchFeeds": [], "hasMore": false}
```

⚠️ **用户最初给的抓包把 `my/processing` 标成了"创建任务" —— 那不是创建端点。**
真正的创建端点由 chunk `9563` 的 `MS()` 指明（见 §3）。
`my/processing` 只回"我有没有任务在跑"。**建任务是计费动作**，
认错端点等于对着一个查询接口不停地"创建"。

### 4.3 按 id 直查（v4）—— 图片与视频**共用同一端点**，两种形态不一样

来源：MeUtils `meutils/apis/hailuoai`（`images.get_task` / `openai_videos.get_task`，
两份都在生产跑）。`my/batch` 无法按 id 过滤（它回"我最近 N 条"），
这条端点补的就是**点名查**的能力：

```http
POST /v4/api/multimodal/video/processing
```

· **图片**（batchType=1，`images.get_task` 在用）：

```json
{"batchInfoList": [{"batchID": "…", "batchType": 1}]}
```

  顶层**不带** `type` 字段。

· **视频**（batchType=0，`openai_videos.get_task` 在用，内嵌真实抓包注释）：

```json
{"batchInfoList": [{"batchID": "…", "batchType": 0}], "type": 1}
```

  顶层**带 `"type": 1`**。⚠️ 这就是"视频的查询端点不太一样"的全部差异：
  端点相同，`batchType` 不同，且视频形态多一个顶层 `type`。
  （`batchType` 与 feedType **同数值同语义**：0=video，1=image。）

· 历史形态：`GET /api/multimodal/video/processing?idList=…`（无 v4、query 传 id）
  已被上两者取代 —— 本服务**不实现**，仅留档。

**响应结构**（🔴 2026-09-21 用真实**图片**批次 batchType=1 实测钉死 —— 图片与视频容器同名）：

```
data.batchVideos[]                # ⚠️ 容器名就叫 batchVideos（历史命名），图片也走它；条目回显 batchType
  ├─ batchID / batchCreateTime / batchType / parameter
  └─ assets[]                     # quantity>1 时多条
       ├─ id                      # = feed 记录 id（即建任务响应的 data.id）
       ├─ status                  # 与 §4.4 的 FeedStatus **同一套**（实测成功=2）
       ├─ downloadURL             # ⚠️ 字符串直链，且**就是去水印版**（与 my/batch 的 withoutWatermarkURL 同一条）
       ├─ percent                 # 进度；实测已完成时为 null（my/batch 没有这个字段）
       ├─ createTime
       ├─ message / desc          # 失败原因参考实现按这两个键拼；正常时 message=null
       ├─ modelID / width / height / fileID / coverURL / modelParameter / …
       └─ videoURL(s)             # 视频批次才有内容，图片批次为空
```

⚠️ 查不到时**不报错**：`batchVideos: []` + `processInfo.unReadProcessedList`（无关噪音，勿解析）。
⚠️ 响应里可能混入**未请求的最近批次**（2026-09-21 实测：查 1 个 id，回来 3 个容器）——
必须按 `batchID` 匹配请求过的 id，其余一律忽略。
用 `data.id`（记录 id）当 batchID 查 ⇒ 同样回空 —— 两个 id 必须分清（见 §3）。

**本服务的用法**（`Service.poll_many`）：`my/batch` 仍是主路径（窗口覆盖 ⇒ 恒 1 次）；
窗口没覆盖到的任务**合并成一次** v4 点名直查 —— 一轮 tick 至多 2 次上游查询，
仍与在途任务数无关。兜底失败保持非终态（"查不到"不是"任务失败"）。

### 4.4 任务状态（前端 chunk `9206` 的 `FeedStatus`，逐字照抄）

| 值 | 名字 | 本服务语义 |
|---|---|---|
| 12 | `BEFORE_WAIT_CREATE` | 非终态（排队） |
| 11 | `WAIT_FOR_CREATE` | 非终态（排队） |
| 1 | `CREATING` | 非终态（进行中） |
| **2** | **`SUCCESS`** | **成功** |
| 10 | `APPEAL_APPROVED` | **成功**（申诉后放行） |
| 3 | `Fail` | 失败 |
| 5 | `SENSITIVE` | 失败（内容策略） |
| 14 | `SENSITIVE_FAIL` | 失败（内容策略） |
| 7 | `REJECTED` | 失败（审核拒绝） |
| 9 | `APPEAL_REJECTED` | 失败（申诉驳回） |
| 6 | `WAIT_FORE_REVIEW` | 非终态（等审核） |
| 16 | `REAL_PERSON_REVIEW` | 非终态（人工复核） |
| 8 | `APPEALING` | 非终态（申诉中） |

前端三个集合（`xE` / `fV` / `R`）与本服务的 `IN_PROGRESS` / `SUCCEEDED` / `FAILED` 一一对应，
由 `tests/test_upstream.py::test_status_sets_match_frontend_enums` 钉住。

**`feedType`**（前端 chunk `9206`）：`0`=video，**`1`=image**，2/3=模板，4/5=Tool，6=Audio。
本服务只认 `1`。

> 实证补充：一次 50 条历史抽样中 `status` **全部为 2**（该账号无失败样本）
> ⇒ 失败态的语义来自**前端枚举**而非实测，故失败文案统一标注
> "上游未给出原因时按 `task_failed`"。

---

## 5. 输入图上传（三段式）

### ① 取 STS 凭据

```http
GET /v1/api/files/request_policy
```

```json
{"accessKeyId":"STS.…","accessKeySecret":"…","securityToken":"…",
 "expiration":"2026-09-20T17:47:10Z",
 "dir":"moss/prod/2026-09-21-00/user/multi_chat_file",
 "endpoint":"oss-us-east-1.aliyuncs.com","bucketName":"hailuo-video",
 "serverTime":"2026-09-20T17:03:24Z"}
```

⚠️ 是 **GET**（用 POST 打会 404）。凭据有效期约 45 分钟。

### ② 直传 OSS

```
PUT https://{bucketName}.{endpoint}/{dir}/{fileName}
Authorization: OSS {accessKeyId}:{签名}
x-oss-security-token: {securityToken}
Content-MD5: base64(md5(bytes))
Content-Type: {mime}
Date: {RFC1123 GMT}
```

签名是阿里云 OSS **V1**：

```
StringToSign = VERB \n Content-MD5 \n Content-Type \n Date \n
               CanonicalizedOSSHeaders + CanonicalizedResource
签名 = base64(HMAC-SHA1(accessKeySecret, StringToSign))
```

⚠️ `x-oss-security-token` **必须**进 `CanonicalizedOSSHeaders`
（小写键 + `key:value\n`）。漏了它签名必错，
而 OSS 只回笼统的 `SignatureDoesNotMatch` —— 很难反查。

### ③ 上传确认

```http
POST /v1/api/files/policy_callback
{
  "fileName": "4591a45f-0dfe-48cc-84ad-b32f28322f0b.jpeg",   // UUID 名
  "originFileName": "021789133119513402c3547e09b7409e300241a04980e221786e7.jpeg",
  "dir": "moss/prod/2026-09-21-00/user/multi_chat_file",
  "endpoint": "oss-us-east-1.aliyuncs.com",
  "bucketName": "hailuo-video",
  "size": "454426",                 // ← **字符串**，不是数字
  "mimeType": "jpeg",               // ← 裸扩展名，不带 "image/"
  "fileMd5": "a34b5d98fbbbeb2f25da935f928ea2f8",
  "fileScene": 10,                  // 图片=10（音频=2 / 视频=3）
  "durationMs": 0,
  "assetFileType": 1                // 图片=1
}
```

返回 `data.fileID`（喂给 §3.1 的 `fileList[].id`）与 `data.url`。

**`fileName` 用 UUID 名 + `originFileName` 保留原名** —— 抓包就是这个形态。

### 5.1 连接级失败与重试策略（2026-09-21 实测）

**现象**：冷连接池下 N 个请求**同时新建连接**时，本机到上游/CDN 的连接约 1/3
概率被掐（`RemoteProtocolError: Server disconnected without sending response`、
`ConnectTimeout: handshake operation timed out`）；而连接**复用后**的并发稳定通过。

**策略**（幂等性决定）：

| 请求 | 可重试 | 理由 |
|---|---|---|
| 查询（`my/batch` / v4 / `my/processing`） | ✅ ≤3 次 | GET 语义，幂等 |
| 取上传凭据 `request_policy` | ✅ ≤3 次 | GET，幂等 |
| OSS `PUT` | ✅ ≤3 次 | 同 object_key + 同内容 = 覆盖写 |
| 输入图下载（CDN） | ✅ ≤3 次 | 下载幂等 |
| **建任务 `generate/image`** | ❌ **绝不重试** | **计费动作**：重试 = 重复扣费 |

重试退避取小值（0.2s×n）—— 这是**建连抖动**不是限流，长退避只会拖慢任务。

**重试判据按"请求是否可能已送达"两级划分**：

· **连接根本没建立**（`ConnectError` / `ConnectTimeout`）⇒ 请求不可能到达上游
  ⇒ **连建任务都可重试**（没送达就不可能计费）；
· **已送达但响应丢失**（`RemoteProtocolError` / 读超时）⇒ 只有**幂等**请求重试；
  建任务绝不重试（无法排除上游已受理）。

### 5.2 STS 凭据单飞缓存（并发 ingest 的关键，2026-09-21 实测）

**现象**：8 张图并发 ingest 时，8 个 `request_policy` 同时新建连接会被掐
（1/4 轮 `RemoteProtocolError` 且**三次重试全失败**）。

**洞察**：凭据是**账号级**的 —— `accessKeyId/securityToken/dir/endpoint/bucketName`
与具体文件无关（callback 里 per-file 字段由我们按文件填）
⇒ **一次取、多图共用完全等价**。实现为「短 TTL（30s）缓存 + 双检单飞」：
N 张图只产生 **1 次** `request_policy`。

⚠️ 该优化已在**真实上游**验证：基准 8 图 × 4 轮 = 32 次共享凭据上传，
全部拿到 fileID（96 次上传跨三种并行度全成功）。

**并行 ingest 实测收益**（`scripts/bench_ingest.py`，**零计费**：只下载+上传，不建任务）：

| 图数 | 并行度 | 中位数 | 加速 | 失败 |
|---|---|---|---|---|
| 3 | 1（串行） | 27.3s | — | 0/4 |
| 3 | 4 | 9.7s | **2.83×** | 0/4 |
| 8 | 1（串行） | 71.7s | — | 0/4 |
| 8 | 4 | 20.6s | 3.49× | 0/4 |
| 8 | **8** | **15.0s** | **4.79×** | **0/4** |

（8 并发那组在凭据单飞修复前是 1/4 轮失败；修复后 12/12 轮零失败。
未到理论上限 8× 的原因是网络长尾 —— 单张偶发 15~47s，并行墙钟由**最慢一张**决定。）

⚠️ 相同字节的输入图会命中 md5 上传缓存**去重**（同内容只传一次）——
这是刻意的省额度行为，不是 bug。

---

## 6. 能力表（两个公开端点，零成本）

### 6.1 `GET /public/api/config/web/common_config`

`create_image_models` 给出**清单与张数上限**：

```
create_image_models.isI2IUnsupportedList[]           # 空 ⇒ 没有"不支持 i2i"的模型
create_image_models.models[].modelKey                # 展示名（如 "Nano Banana 2"）
create_image_models.models[].filterTags[]            # 如 ["edit","4k"]
create_image_models.models[].modelList[].id          # ← 上游 modelID
create_image_models.models[].modelList[].mode        # image-reference / subject-reference
create_image_models.models[].modelList[].maxSupportImageCount   # ← 能垫几张图
create_image_models.models[].modelList[].maxPromptLength
```

### 6.2 `GET /public/v2/api/multimodal/video/model/info`

⚠️ 名字里带 `video`，但响应**同时**含 `videoModels` / `imageModels` / `audioModels`。
本服务只取 `imageModels`：

```
imageModels[].modelID
imageModels[].parameter.resolutions[]{value, name, defaultSelect}
imageModels[].parameter.aspectRatios[]{value, name, icon, defaultSelect}
imageModels[].costs[]{resolutions[], qualities[], realCost, rawCost, unitFileCost}
imageModels[].defaultCost
imageModels[].promotionInfo
```

⇒ 两个端点合起来才是完整注册表：**缺清单就不知道张数上限，
缺档位就不知道合法取值与单价**。两处都读不到时退回冻结快照并留降级痕。

### 6.3 实测注册表（2026-09-21）

| modelID | 别名（本服务接受） | 展示名 | 分辨率 | 比例数 | 单价 | 垫图上限 |
|---|---|---|---|---|---|---|
| `nano_banana21_flash` | `nano-banana-2` | Nano Banana 2 | 1K/2K/4K | 15 | 4/5/8 | 14 |
| `nano-banana2` | `nano-banana-pro` | Nano Banana Pro | 1K/2K/4K | 11 | 6（≤2K）/10（4K） | 14 |
| `seedream-5.0` | `seedream-5.0-lite` | Seedream 5.0 Lite | 2K/4K | 11 | 4 | 14 |
| `seedream-4.5` | — | Seedream 4.5 | 2K/4K | 11 | 4 | 14 |
| `gpt-image-2` | — | GPT Image 2 | 1K/2K/4K | 10 | 2..160（3 档 quality） | 16 |
| `gpt-image-2.5-sunburst` | — | GPT Image 2.5 Sunburst | 1K/2K/4K | 11 | 2..180（5 档 quality） | 16 |
| `gpt-image-2.5-flare` | — | GPT Image 2.5 Flare | 1K/2K/4K | 11 | 2..180（5 档 quality） | 16 |
| `gpt-image-1.5` | — | GPT Image 1.5 | Low/Medium/High | 4 | 4/8/15 | **3** |
| `mj_v7` | `midjourney-v7` / `mj-v7` | Midjourney V7 | — | 11 | 3 | 14 |
| `mj_niji7` | `midjourney-niji7` / `mj-niji7` | Midjourney Niji7 | — | 11 | 3 | 14 |
| `image-01` | `image-1.0` | Image-1.0 | — | 6 | 1 | 未声明 |

⚠️ **别名只差一个字符的高危区**：`nano-banana-2`（flash，多一个 `-`）≠ `nano-banana2`（pro）。
别名与精确枚举值**同待遇：零映射零说明**；别名规范化后模型不在能力表里 ⇒ 走"落默认+留痕"回退
（`models.MODEL_ALIASES`，测试 `test_model_aliases_resolve_to_canonical_ids` 钉死）。

**UI 展示名与官方描述**（2026-09-21 截图取证；GPT 家族 slug 与 modelID 一致，无需别名）：

| 展示名（UI） | modelID | UI 描述 |
|---|---|---|
| GPT Image 2.5 Sunburst `New` | `gpt-image-2.5-sunburst` | 细节与控制力出色，擅长精准图像编辑 |
| GPT Image 2.5 Flare `New` | `gpt-image-2.5-flare` | 快速灵活，适合高效的日常图像创作 |
| GPT Image 2 | `gpt-image-2` | 擅长指令遵循与编辑，文字渲染、版式控制极强 |
| Midjourney Niji7 | `mj_niji7` | 专门针对动漫，细节入微，线条精致流畅 |
| Midjourney V7 | `mj_v7` | 全新架构，更丰富的纹理，无与伦比的提示精度 |

**观察到的输出尺寸样本**：`4K`+`1:1` ⇒ 4096×4096；`4K`+`Auto`(t2i) ⇒ 5632×3072；
`4K`+`Auto`(i2i，参考图 1:1) ⇒ 4096×4096；`1K`+`Auto` ⇒ 1408×768。

⚠️ **`Auto` = 跟随参考图比例**（i2i 时），t2i 时走模型自己的默认 —— 这是从样本**推断**的，
不是文档；所以 `size` 换算一律留痕。

### 6.4 实测生成速度（2026-09-21，t2i 端到端 = 提交 → 终态）

| modelID | 档位 | 端到端耗时 | 产物体积 | 单价 |
|---|---|---|---|---|
| `nano_banana21_flash` | 1K | **43s**（更早样本 ~17s） | ~1.0MB | 4 |
| `nano_banana21_flash` | 2K+9:16 | **96s** | — | 5 |
| `nano-banana2`（Pro） | 1K | **66s**（同 prompt 对照） | ~0.7MB | 6 |
| `nano-banana2`（Pro） | 2K | **407s** | 3.6MB | 6 |
| `mj_v7`（经别名 `mj-v7` 提交） | —（无分辨率档） | **95s** | 1.3MB | 3 |
| `mj_niji7` | —（无分辨率档） | **105s**（同 prompt 对照 v7=95s） | 1.4MB | 3 |

**结论**：Pro 比 Flash 慢 —— 1K 约 **1.5×**（43s vs 66s），2K 约 **4×**（96s vs 407s）。
Pro 的 1K/2K 同价（6），追求速度选 Flash、追求细节选 Pro（2K 产物 3.6MB vs Flash 1K 1MB）。
MJ v7 速度与 Flash 2K 同量级（95s）但**最便宜**（3），且是**无分辨率档**模型
（请求里的 resolution 不会转发，比例走默认 Auto）—— 服务里第一条"无档位模型"实测路径；
niji7 与 v7 同量级（105s vs 95s，单样本）。
⚠️ 单样本对照，上游负载会波动；量级（而非精确值）可作为选型依据。

**MJ 比例枚举（2026-09-21 公开端点实测，与 UI 标签逐字符一致）**：
`mj_v7` / `mj_niji7` 均为 `Auto* / 21:9 / 16:9 / 5:4 / 4:3 / 3:2 / 1:1 / 2:3 / 3:4 / 4:5 / 9:16`
（`*`=默认，共 11 档；`nano_banana21_flash` 额外有 `1:4 / 4:1 / 1:8 / 8:1` 极端档，共 15 档）。
`size` 传任意宽高 ⇒ 按宽高比**吸附**到最近档（如 2560x1080 ⇒ 21:9），无档位模型不转发 resolution。

---

## 7. 错误码

### 7.1 两种错误层，**不要混**

| 层 | 表现 | 本服务映射 |
|---|---|---|
| HTTP 层 | `401`（缺/坏 token）、`429`、`5xx` | `invalid_api_key` / `upstream_rate_limited` / `upstream_error` |
| 业务层 | **HTTP 200 或 400 + `statusInfo.code != 0`** | 按 `code` 与 message 文本分类 |

🔴 **只看 HTTP 状态会把失败当成功** —— 上游大量业务错误走 HTTP 200。

### 7.2 已观察到的 `statusInfo`

| code | message | 触发条件 | 本服务映射 |
|---|---|---|---|
| `0` | `成功` | 正常 | — |
| `2` | `请求异常，请检查请求参数` | **签名错 / 参数错都回这个** | `invalid_parameter` |
| `1`（推测） | 含"登录"字样 | token 失效的业务层表现 | `invalid_api_key` |
| —（文本匹配） | 含"积分"/"额度"/"余额" | 额度耗尽 | `upstream_quota_exhausted` |
| —（文本匹配） | 含"频繁"/"限流"/"风控" | 频控 | `risk_control_challenge` |
| —（文本匹配） | 含"敏感"/"审核"/"违规" | 内容策略 | `content_policy_violation` |

⚠️ 除 `0` 与 `2` 之外，**其余均为文本启发式**（我们只实测到这两个码）。
启发式集中在 `client._raise_for_status_info` 一处，便于将来用实测替换。

---

## 8. 诚实边界（未取证的事）

| 事项 | 状态 |
|---|---|
| **端到端真实出图** | 🔴 **本项目未做**（建任务即计费）。上游链路已按抓包与非计费只读调用逐层验证，但"建任务 → 出图"这一段**没有实跑过** |
| 产物 URL 有效期 | **未取证**。故原样透传、不做转存；需要长期链接时得另做 |
| 多图（>1 张）的实际语义 | **未取证**。`maxSupportImageCount` 是服务端声明，但"14 张图如何被理解"没有样本 ⇒ 一律按请求顺序透传并留痕 |
| `isFirstGenerate` 的语义 | 未取证，只透传到 trace |
| `Auto` 比例的像素映射规则 | **推断**（见 §6.3），故 `size` 换算必留痕 |
| `quality` 与 `costs` 的对应 | 参数结构已实读；**具体计费口径未对账** ⇒ `forecast_credits` 明确标注为预估 |
| 上游是否回收未引用的上传素材 | 未知 → 上传缓存取保守 6h |
| `statusInfo` 除 0/2 外的码 | 未实测（靠文本启发式） |
| 上游频控阈值 | **未界定**（本服务默认并发 1 是保守策略，不是实测上限） |
| ~~图片批次经 v4 直查的响应容器名~~ | ✅ **已实测（2026-09-21）**：与视频同名 `data.batchVideos[].assets[]`，条目回显 `batchType:1`；`downloadURL`=字符串去水印直链、`percent` 完成时为 null |
| ~~v4 对未知/未落库 id 的响应形态~~ | ✅ **已实测（2026-09-21）**：HTTP 200 + `code:0` + `batchVideos: []`（**不报错、静默缺席**）；用记录 id 冒充批次 id 也是回空 |
| **DNS rebinding 防护** | **未做**。SSRF 防线在请求前校验主机，但连接时按域名重解析 ⇒ 理论上存在 rebinding 窗口（`app/media.py` 已注明） |

---

## 9. 与即梦（jimeng）上游的关键差异

本项目参照 `../jimeng` 的骨架落地，但两处上游形态**根本不同**，迁移时别照抄：

| 维度 | 即梦 | hailuo |
|---|---|---|
| 凭据 | cookie 里的 `sessionid`（`sign` 不参与鉴权） | `token` 头 JWT + **`yy` 必须正确** |
| 签名 | 有，但实测不参与鉴权 | **无签名即 400**（且错误文案与参数错完全一样） |
| 查询 | 需逐个 id 查 ⇒ 靠**合并 id** 降请求量 | 主路径回"我最近 N 条"恒 1 次；窗口外的任务再合并成一次 v4 点名直查（§4.3）⇒ **至多 2 次** |
| 建任务 | 有 `ret` 与 `task.status` 两层 | `statusInfo.code` + HTTP 层 |
| 上传 | 火山 ImageX 四段式 | 阿里云 OSS STS 三段式 |
| 张数 | 服务端 `get_common_config` 声明 | 声明在 `common_config` + `model/info` 两处 |
| 产物 | TOS 预签名 URL | `cdn.hailuoai.video` 直链，**带/去水印两版** |
