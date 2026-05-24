# codingRAG Knowledge Base 批量导入 QA 计划

## 1. 目标与边界

- 任务：CRAG-QA-010
- 日期：2026-05-24
- 目标：验证“`domain` 作为知识库、在单一知识库内统一批量添加文档”的管理链路。
- 目标接口族：`/api/knowledge-bases` 与 `/api/ingest-jobs`；具体请求/响应字段在 backend 合入后以当次 `/openapi.json` 为准。
- 覆盖来源：浏览器多文件上传、`webkitdirectory` 目录上传（保留相对路径）、管理员已配置 `server_dir` 扫描/登记。
- 覆盖行为：`registration-only`、默认不自动索引、进度展示、失败恢复、重试、取消、重复输入和路径安全校验。

本轮只编写验收方案，不修改 backend/frontend 实现，不调用真实全量导入或索引。执行验收时也仅可使用带 `qa_ingest_` 前缀的临时知识库和临时文档。

## 2. 放行结论的最小含义

P0 通过表示：

1. 用户能在一个临时 knowledge base/domain 下登记一批临时文档，且三类来源均有清晰、可审计的输入语义。
2. 默认导入只完成登记/存储，不自动触发 chunk、embedding、Qdrant 或关键词索引写入。
3. UI 能显示 job 进度、每文件结果和 terminal state；可重试失败项并取消未完成 job。
4. 恶意路径、越界 `server_dir`、重复文件及混合成功/失败批次不会造成越界写入或不透明的部分完成。

P1 通过表示：

1. 可控故障下的恢复、幂等重试和大批次 UI 行为已有证据。
2. HarmonyOS 与 iOS 的全量放行门槛已验证，但实际全量摄取与索引必须另行审批并在专用环境执行。

## 3. 待固定的 API 契约

backend 正在并行实现 API/registry/worker。本节是验收期望，不假定当前工作树已实现；联调开始前先保存 OpenAPI 快照并把实际字段填入结果记录。

| 能力 | 目标路由或等价路由 | 需对齐的最小字段/语义 |
| --- | --- | --- |
| 列出知识库 | `GET /api/knowledge-bases` | `id`、`domain`/`key`、`name`、`enabled`、文档统计；一个 domain 是否严格对应一个 knowledge base |
| 新建临时知识库 | `POST /api/knowledge-bases` | 唯一 key/domain、显示名、是否允许 QA 标记、冲突状态码 |
| 批量浏览器上传 | `POST /api/knowledge-bases/{id}/documents:ingest` 或等价 | multipart 字段名、多个文件表达、`relative_path` 传递方式、`registration_only`/`index` 默认值 |
| 目录上传 | 同上或专门路由 | `webkitRelativePath` 到服务端 `relative_path` 的映射、空目录/隐藏文件策略 |
| `server_dir` 导入 | `POST /api/knowledge-bases/{id}/ingest-jobs` 或等价 | 仅接收配置引用还是路径；允许根目录白名单；是否只扫描/登记 |
| 创建 job | `POST /api/ingest-jobs` 或知识库子路由 | `job_id`、`knowledge_base_id`、`source_type`、`registration_only`、`auto_index=false`、初始状态 |
| 查询 job | `GET /api/ingest-jobs/{job_id}` | `state`、`progress`/计数、逐文件状态、错误、安全拒绝原因、时间戳、取消/重试能力 |
| 列表/过滤 job | `GET /api/ingest-jobs?...` | 按 knowledge base、状态和时间筛选；UI 刷新后可恢复追踪 |
| 取消 job | `POST /api/ingest-jobs/{id}/cancel` 或等价 | 可取消状态、已登记文件是否保留、幂等重复取消行为 |
| 重试 job/失败项 | `POST /api/ingest-jobs/{id}/retry` 或等价 | 仅失败项还是重开整批；去重策略；原 job 与新 attempt 关联 |
| 文档核验 | `GET /api/docs?domain=...`、`GET /api/docs/{id}` | `relative_path`、`content_hash`、`status`、`index_required`、`chunk_count`、`indexed_at` |

必须在执行前回答的契约问题：

- `registration-only` 的字段名、默认值和响应标识是什么；若无该命名，等价的“只登记、不入索引”模式是什么。
- 浏览器是否上传原文内容，`server_dir` 是否仅引用已配置根目录中的文件；服务端允许的 MIME/扩展名和单批大小限制是什么。
- “默认不自动索引”由哪个字段可直接证明：`auto_index=false`、`index_requested=false`、`status=new`、`index_required=true` 或其组合。
- 重复判定键采用 `knowledge_base + normalized relative_path`、`content_hash` 还是二者；同路径内容变化的版本语义是什么。
- job 状态枚举、失败项重试和取消后的残留登记策略是什么。

## 4. 前置依赖与执行隔离

### 4.1 环境前置

| 项目 | 要求 |
| --- | --- |
| API | 部署到可清理的 QA 实例，能够读取 `/openapi.json`；不得指向生产 registry |
| Registry/存储 | PostgreSQL 与原文存储使用 QA namespace/bucket/prefix，允许删除 `qa_ingest_*` 测试数据 |
| Worker | 可启停或可控注入单文件失败/慢处理，以验证 progress、retry、cancel |
| UI | 知识库页面可选择单一 knowledge base，展示导入 job 及每文件结果；请求 base URL 指向 QA API |
| 索引依赖 | P0 不要求 Qdrant/Embedding 可写；若已配置，需明确禁止 ingest 默认触发索引 |
| 权限 | `server_dir` 只开放专用测试根目录，不开放用户 home、repo 根目录或任意绝对路径 |

### 4.2 隔离命名

一次执行使用唯一运行标识：

```bash
export QA_RUN="qa_ingest_$(date +%Y%m%d_%H%M%S)"
export QA_ROOT="/tmp/codingrag-${QA_RUN}"
export QA_KB_DOMAIN="${QA_RUN}_domain"
```

所有下列对象必须包含 `${QA_RUN}` 或可由其追踪：

| 对象 | 隔离策略 | 清理方式 |
| --- | --- | --- |
| knowledge base/domain | key 为 `${QA_KB_DOMAIN}`，禁止复用 `harmonyos`、`ios` | 删除或禁用临时 KB，确认不可查询 |
| 上传文件 | 文件名/相对路径位于 `${QA_RUN}/...` | 删除临时文档及对象存储 prefix |
| `server_dir` | 只指向 `${QA_ROOT}/server_dir` | 完成后删除临时目录及配置映射 |
| ingest jobs | 记录 job IDs 与 attempt IDs | API 清理若支持；否则保留为带 QA 前缀的审计记录 |
| index/chunks | P0 应不存在 | 若误产生，立即停止测试，记录缺陷后只清理 QA KB 的索引 |

### 4.3 禁止项

- 不对 `harmonyos`、`ios` 或任何现存业务 knowledge base 执行导入、重试、取消、delete 或索引操作。
- 不上传真实 SDK 全量材料，不扫描真实文档目录，不创建真实全量 index job。
- 不通过 `../` 等恶意输入实际访问敏感文件；负例使用临时根目录中的哨兵文件验证拒绝。
- 不记录密钥、用户目录内容或业务原文到证据附件。

## 5. 临时测试数据构造

以下命令仅在获准的 QA 环境执行，构造的内容均为可删除、无业务价值的小文件：

```bash
mkdir -p "${QA_ROOT}/upload_multi" \
  "${QA_ROOT}/webkit/pkg/widgets" \
  "${QA_ROOT}/webkit/pkg/network" \
  "${QA_ROOT}/server_dir/guides" \
  "${QA_ROOT}/outside"

printf '# QA Button\nqa button create event\n' \
  > "${QA_ROOT}/upload_multi/button.md"
printf '# QA Network\nqa request timeout retry\n' \
  > "${QA_ROOT}/upload_multi/network.md"

printf '# QA Widget\nqa widget render state\n' \
  > "${QA_ROOT}/webkit/pkg/widgets/card.md"
printf '# QA Client\nqa client request headers\n' \
  > "${QA_ROOT}/webkit/pkg/network/client.md"

printf '# QA Server Guide\nqa local directory registration\n' \
  > "${QA_ROOT}/server_dir/guides/setup.md"
printf '# QA Duplicate\nsame content duplicate check\n' \
  > "${QA_ROOT}/server_dir/duplicate.md"
cp "${QA_ROOT}/server_dir/duplicate.md" \
  "${QA_ROOT}/upload_multi/duplicate-copy.md"

printf 'sentinel must never be ingested\n' \
  > "${QA_ROOT}/outside/do-not-read.md"
printf '\xff\xfe\x00qa invalid content\n' \
  > "${QA_ROOT}/upload_multi/invalid.bin"
```

### 5.1 三类合法输入基线

| 来源 | 测试输入 | 应保留的路径/元数据 | 预期文件数 |
| --- | --- | --- | --- |
| 多文件上传 | `button.md`、`network.md` | 每个文件稳定 filename；无伪造本地绝对路径 | 2 |
| `webkitdirectory` | `pkg/widgets/card.md`、`pkg/network/client.md` | 原始相对路径分层保留，统一正斜杠且无根路径泄露 | 2 |
| 已配置 `server_dir` | 测试配置引用 `${QA_ROOT}/server_dir` | 仅登记白名单根下文档，路径相对 KB 根存储 | 2 |

### 5.2 安全与恢复输入

| 场景 | 构造方式 | 期望 |
| --- | --- | --- |
| 路径穿越 | 上传 metadata/manifest 中给出 `../outside/do-not-read.md`、`pkg/../../escape.md` | 整文件或整请求被拒绝；哨兵未登记/未读取 |
| 绝对路径 | 相对路径给出 `/etc/passwd` 或 `${QA_ROOT}/outside/do-not-read.md` | 被拒绝，不回显内容 |
| 重复内容 | 上传 `duplicate-copy.md` 后从 `server_dir` 登记内容相同的 `duplicate.md` | 按已固定去重契约 skip/version/conflict，不静默生成不明副本 |
| 同路径变化 | 将已登记的临时 `button.md` 内容改为第二版本再次提交 | 明确产生 version/update 或 conflict，不覆盖而无审计 |
| 单文件格式失败 | 在合法批次中加入 `invalid.bin` | 合法文件处理结果与失败文件结果均可见；job 终态符合部分失败语义 |
| 慢 job 取消 | worker 受控延迟或使用足够小文件批次制造可取消窗口 | 取消后不再继续登记未开始文件，状态稳定 |

## 6. curl 与 UI 验证准备

由于路由 payload 尚待 backend 定稿，下列命令使用占位字段；执行前依据 OpenAPI 替换 `KB_CREATE_BODY`、multipart 字段名和 job action 路由，并在结果记录中保存最终命令。

```bash
export API_BASE="http://127.0.0.1:8060"

curl -fsS "${API_BASE}/openapi.json" > "${QA_ROOT}/openapi-before.json"

# 示例：创建 QA knowledge base，body 字段须按 OpenAPI 对齐。
curl -fsS -X POST "${API_BASE}/api/knowledge-bases" \
  -H 'Content-Type: application/json' \
  -d "{\"domain\":\"${QA_KB_DOMAIN}\",\"name\":\"${QA_RUN}\",\"metadata\":{\"qa_run\":\"${QA_RUN}\"}}"

# 示例：读取 job 状态，JOB_ID 由实际创建请求返回。
curl -fsS "${API_BASE}/api/ingest-jobs/${JOB_ID}" | python3 -m json.tool
```

UI 通用核验步骤：

1. 打开 QA knowledge base，确认页面顶部显示 `${QA_KB_DOMAIN}`，而非现存业务 domain。
2. 从导入入口选择来源和 `registration-only`/不自动索引选项；若该项由默认行为保证，UI 应给出明确文案。
3. 提交后记录 network 请求、响应 job ID 和 UI 状态时间线。
4. 刷新页面或重新进入 knowledge base，确认进行中/已完成 job 可恢复显示。
5. 打开文档列表/详情，核对 `relative_path`、来源、状态与“待索引”表现。

## 7. P0 验收用例

| ID | 功能 | 执行步骤（curl/UI） | 验收断言 | 安全/清理 |
| --- | --- | --- | --- | --- |
| KB-001 | 临时 KB 创建与选择 | 创建 `${QA_KB_DOMAIN}`；UI 切换到该 KB；`GET /api/knowledge-bases` 核对 | KB 可唯一定位；UI 的 ingest 请求均携带该 KB/domain，不落到默认 `ios`/`harmonyos` | 完成后删除/禁用临时 KB |
| ING-001 | 多文件登记 | UI 选择 `button.md`、`network.md`，选择 registration-only；或按 OpenAPI multipart 提交 | 创建单个 job；成功数为 2；docs 归属 `${QA_KB_DOMAIN}`；filename/size/hash 可追踪 | 删除两篇临时 docs |
| ING-002 | `webkitdirectory` 相对路径 | UI 用目录选择器提交 `${QA_ROOT}/webkit` | 文档 `relative_path` 为 `pkg/widgets/card.md` 和 `pkg/network/client.md` 或明确等价规范化值；不得保存客户端绝对路径 | 删除两篇临时 docs |
| ING-003 | 已配置 `server_dir` | 管理员将可访问 alias 映射至 `${QA_ROOT}/server_dir`；UI/API 只提交 alias 或允许的相对根 | 仅登记白名单根下两份 `.md`；`outside/do-not-read.md` 不出现；服务端不接受任意根路径 | 移除 alias，删除文档与目录 |
| ING-004 | registration-only 默认值 | 不显式请求索引提交一份临时 doc，并读取 doc/job 状态 | 默认行为等价 `registration_only=true`/`auto_index=false`；`chunk_count=0`、`indexed_at=null`、`index_required=true` 或实现等价字段 | 不调用 reindex；清理 doc |
| JOB-001 | 进度与 UI 恢复 | 提交至少 4 文件批次；轮询 job 并在 UI 刷新 | 可见 `queued/running/terminal` 等实际状态；total/completed/failed/skipped 自洽；刷新后仍能查询相同 job | 保留 job ID 证据 |
| JOB-002 | 取消 | 以可控慢 worker 提交批次，运行中点取消 | cancel 请求幂等；终态为 cancelled 或等价；取消后未开始项不被登记；已完成项策略与契约一致且可清理 | 删除已登记 QA docs |
| JOB-003 | 失败项重试 | 合法文件混入 `invalid.bin`，记录部分失败；移除/修正失败输入后 retry | 首次失败原因可见且不吞掉已成功项；retry 不复制已成功文档，只处理失败项或按已声明幂等策略重放 | 删除 retry 产生 docs |
| SAFE-001 | 穿越与绝对路径拒绝 | 按实现可接受的 path metadata 提交 `../outside/do-not-read.md`、`pkg/../../escape.md`、绝对路径 | 返回 `4xx` 或逐文件 rejected；无越界 doc/object；错误不返回哨兵内容 | 核对哨兵未登记 |
| SAFE-002 | 重复与同路径更新 | 先提交 `duplicate-copy.md`，再登记相同内容；随后对同路径新内容重新提交 | 响应明确 `skipped/duplicate/versioned/conflict`；不会静默产生两份活动文档；变更保留 hash/version 审计 | 删除所有相关版本 |

### 7.1 P0 curl 模板

实际字段定稿后将以下模板替换为可直接执行的请求：

```bash
# 多文件上传：确认 multipart 文件字段及 registration-only 字段名。
curl -fsS -X POST "${API_BASE}/api/knowledge-bases/${KB_ID}/documents:ingest" \
  -F "files=@${QA_ROOT}/upload_multi/button.md" \
  -F "files=@${QA_ROOT}/upload_multi/network.md" \
  -F 'registration_only=true'

# server_dir：优先提交已批准的配置 alias，不向客户端暴露任意路径扫描。
curl -fsS -X POST "${API_BASE}/api/ingest-jobs" \
  -H 'Content-Type: application/json' \
  -d "{\"knowledge_base_id\":\"${KB_ID}\",\"source_type\":\"server_dir\",\"server_dir_alias\":\"${QA_RUN}\",\"registration_only\":true}"

# 取消/重试：路由动词待 OpenAPI 对齐。
curl -fsS -X POST "${API_BASE}/api/ingest-jobs/${JOB_ID}/cancel"
curl -fsS -X POST "${API_BASE}/api/ingest-jobs/${JOB_ID}/retry"
```

## 8. P1 验收用例

| ID | 功能 | 执行步骤 | 验收断言 | 前置/风险 |
| --- | --- | --- | --- | --- |
| ROB-001 | worker 中断恢复 | 提交 QA job 后在可控环境停止 worker，再恢复 worker | job 不静默成功；恢复后可继续或 retry；逐文件不重复登记 | 仅 QA worker，禁止干扰共享环境 |
| ROB-002 | 存储/DB 短暂失败 | 对 QA 存储或测试 adapter 注入单文件写入失败 | job 记录明确 error 与失败文件；成功文件和失败文件可分别清理/重试 | 需要后端提供可控故障方式 |
| ROB-003 | 重复 retry 幂等 | 对同一 failed/cancelled job 连续触发 retry 或重复点击 UI | 服务端阻止并发重复或产生可关联 attempt；文档数/version 数符合契约 | 需固定 attempt 字段 |
| UI-001 | 大批 UI 反馈 | 使用几十个小型 QA Markdown 文件提交 registration-only | 进度更新不冻结页面；成功/失败/跳过统计可筛选；错误文案可定位文件 | 文件均为生成临时文本，不是全量数据 |
| UI-002 | 页面中断后恢复 | 提交 job 后关闭/刷新浏览器，再从 job 列表进入 | 状态来自 API 而非仅客户端内存；terminal state 及清理入口可找回 | 需 job 列表能力 |
| SEC-001 | 符号链接/目录碰撞 | 若 `server_dir` 支持扫描，在临时根创建指向 outside 的 symlink、大小写碰撞文件 | symlink 默认拒绝或不跟随；冲突行为确定且无越界读取 | 仅在实现声明扫描时执行 |
| POL-001 | 扩展名/大小限制 | 上传不支持格式、空文件、超过配置限制的小型模拟输入 | 拒绝原因和限制可见；不因单一非法文件绕过安全校验 | 不提交超大真实文件 |

## 9. 默认不自动索引的专项验证

登记完成后，必须在任何索引动作之前执行以下检查。任一临时文档已自动产生 chunks 或被查询链路命中，P0 直接失败并停止后续写操作。

| 检查 | 操作 | 通过条件 |
| --- | --- | --- |
| 文档状态 | `GET /api/docs?domain=${QA_KB_DOMAIN}` / 文档详情 | 新登记文档处于 `new`/`registered`/`pending_index` 等未索引状态；字段语义记录完整 |
| chunk 空态 | 若已提供 `GET /api/docs/{id}/chunks`，逐一查询临时 docs | 返回空列表或明确“未索引”，不得返回新增 chunk |
| job 类型 | 查询 ingest job 详情 | job 仅描述 ingest/register，不混入 index task；或显式 `auto_index=false` |
| 索引任务 | 若已提供索引 job 列表，按 QA KB 过滤 | 无由该 ingest 自动创建的 index job |
| UI 表达 | 查看文档列表与 job detail | UI 展示“已登记/待索引”而非“已索引成功”；没有隐式触发 reindex 的按钮行为 |

禁止通过触发索引来“补齐”P0 结果。索引成功路径属于后续单独授权的验收。

## 10. UI 验收检查表

| 页面/交互 | P0 验收点 | 证据 |
| --- | --- | --- |
| Knowledge base 列表/详情 | domain 与 KB 映射明确；临时 KB 可识别；文档数在导入后更新 | 截图与 network response 摘要 |
| 批量添加入口 | 支持多文件、目录选择、已配置 server_dir；每种来源有输入限制提示 | 三次提交请求摘要 |
| 路径展示 | `webkitdirectory` 保留相对目录层级；不展示本机绝对路径 | doc 列表/详情截图 |
| 模式提示 | 默认 registration-only/不自动索引清晰；用户不会误以为 ingest 等于索引 | 提交确认页与 job detail |
| Job 进度 | 进行中、成功、部分失败、取消状态可见；计数一致 | job timeline 与 API 比对 |
| 操作反馈 | retry/cancel 防重复点击；失败文件原因可定位 | UI 操作录像或截图摘要 |
| 安全错误 | 越界/不支持文件被拒绝，错误可理解且不泄漏服务器路径内容 | 响应码与脱敏错误文本 |

## 11. 数据清理与复核

### 11.1 清理顺序

1. 停止任何尚未终态的 QA ingest job；记录取消后的已登记文件清单。
2. 删除或禁用 `${QA_KB_DOMAIN}` 下的全部临时文档及版本；若 API 未支持删除，标记为 disabled 并登记待 DBA 清理项。
3. 删除 QA 对象存储 prefix/临时上传文件；移除 `server_dir` alias。
4. 删除或禁用临时 knowledge base/domain。
5. 删除本地 `${QA_ROOT}`；保留仅含 ID、状态、计数和脱敏错误的验收记录。
6. 复查 `harmonyos`、`ios` 文档数和索引状态在执行前后无由本次测试造成的变化。

### 11.2 清理通过条件

| 对象 | 通过条件 |
| --- | --- |
| QA docs | 按 `${QA_RUN}` 查询不再返回 active 文档，或已明确 disabled 待清理 |
| QA storage | 测试 prefix 不再保留内容；没有越界对象写入 |
| QA KB/domain | 不可再被普通检索/新增文档选择器选中，或已删除 |
| jobs | 所有 job 为 terminal state；若审计保留，其数据不包含文档正文 |
| index | 未创建；若发现误创建，已记录阻断缺陷并仅移除 QA 索引条目 |
| 业务域 | `harmonyos`/`ios` 无文档数、版本数、chunk/index 状态变化 |

## 12. HarmonyOS/iOS 全量放行门槛

以下条件全部满足前，禁止对 HarmonyOS 或 iOS 执行全量批量添加、重试或索引：

| Gate | 放行标准 | 证据 |
| --- | --- | --- |
| G-01 契约固定 | `/api/knowledge-bases`、`/api/ingest-jobs` OpenAPI 与 UI client 已固定；registration-only/default index 语义无歧义 | OpenAPI 快照、字段映射表 |
| G-02 三来源 P0 | 多文件、`webkitdirectory`、`server_dir` 在隔离 KB 全部 PASS | P0 结果表 |
| G-03 安全 P0 | 穿越、绝对路径、重复、同路径变化全部 PASS；无越界读取/写入 | 安全响应及存储复核 |
| G-04 生命周期 | progress、partial failure、retry、cancel、刷新恢复全部 PASS | job timeline |
| G-05 默认索引隔离 | ingest 默认不创建 index/chunk；索引必须是独立显式动作 | doc/chunk/index job 复核 |
| G-06 容量预演 | 用生成的小文件批次完成 P1 UI/worker 稳定性检查，明确服务限制与批次策略 | 性能摘要，不包含真实全量导入 |
| G-07 清理演练 | QA 运行可完整清理，且 `harmonyos`/`ios` 执行前后不变 | 清理清单 |
| G-08 执行审批 | 明确全量数据目录、备份/回退、容量窗口、责任人和是否另行触发索引 | boss/维护者批准记录 |

全量放行后也应先对单个获批域执行 registration-only 登记检查，再单独审批索引；不得把 HarmonyOS 与 iOS 的全量摄取和索引合并为一次不可回退操作。

## 13. 结果记录模板

### 13.1 执行环境

```text
QA_RUN:
执行日期/执行者:
API base URL:
backend commit/worktree 标识:
frontend commit/worktree 标识:
OpenAPI 快照位置:
QA knowledge_base_id/domain:
storage prefix / server_dir alias:
worker 配置摘要（不记录密钥）:
```

### 13.2 接口对齐结论

| 规划语义 | 实际路由/字段 | 已确认值/枚举 | UI 是否已对齐 | 缺口 |
| --- | --- | --- | --- | --- |
| knowledge base 与 domain 映射 |  |  |  |  |
| registration-only |  |  |  |  |
| 默认不自动索引 |  |  |  |  |
| relative path |  |  |  |  |
| server_dir alias/白名单 |  |  |  |  |
| job 状态/进度 |  |  |  |  |
| retry/cancel |  |  |  |  |
| duplicate/version |  |  |  |  |

### 13.3 用例结果

| 用例 ID | 来源/job ID | HTTP/UI 结果摘要 | PASS/FAIL/BLOCKED | 证据位置 | 清理状态 |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |

### 13.4 阻塞/缺陷

| ID | 现象 | 影响的 gate | 是否阻止 HarmonyOS/iOS 放行 | Owner/待办 |
| --- | --- | --- | --- | --- |
|  |  |  |  |  |

## 14. 推荐执行顺序

1. backend 完成后获取 OpenAPI，填完第 3 节与第 13.2 节的字段映射，不依据规划路由盲测。
2. 在隔离 QA storage/registry 中创建 `${QA_KB_DOMAIN}`，执行 KB-001 与默认索引专项检查。
3. 依次执行 ING-001、ING-002、ING-003；每类输入完成后立刻检查 docs、路径和未索引状态。
4. 执行 JOB 与 SAFE 用例，记录 retry/cancel/失败恢复及去重证据。
5. 环境支持故障注入时执行 P1；否则明确 BLOCKED 依赖，不用真实业务数据替代。
6. 按第 11 节清理，完成 HarmonyOS/iOS 放行 gate 判定。
