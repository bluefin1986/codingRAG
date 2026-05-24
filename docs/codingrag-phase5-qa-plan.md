# codingRAG Phase 5 管理界面 QA 计划

## 1. 目标与范围

- 日期：2026-05-23
- 范围：Phase 5 `llmproxy-gateway-mgr` 管理界面与 codingRAG 管理 API 的联调验收。
- 覆盖对象：domains、query expansions、文档列表/详情/content/chunks/reindex、debug query trace、index jobs。
- 安全约束：本轮 QA 不修改 frontend/backend 源码，不提交 git；已执行的 smoke 仅使用无 registry 的隔离 API 实例和只读请求，不触碰现存业务数据。

Phase 5 的完成标准：

- 前端可按 domain、状态、关键词浏览或搜索文档。
- 前端可查看文档详情、原文与当前索引 chunks。
- 对明确创建的 QA 临时文档可执行单篇 reindex，并能验证其索引状态。
- 检索调试页能发起 debug query，并展示 query expansion 与候选/融合/最终命中链路。
- 索引任务页能明确展示当前 API 支持的索引状态语义，并对“历史任务日志尚不可用”给出准确表现。

## 2. 当前基线结论

### 2.1 规划接口与后端实现映射

当前工作区后端 API 来自 `api/app.py` 的 OpenAPI 输出；其中 `chunks`、删除 index 与 index jobs 为并行开发中的未提交工作树能力，联调前需重新拉取当前 OpenAPI 确认未变更。

| 功能 | Phase 5 所需能力 | 当前后端端点 | 基线判定 |
| --- | --- | --- | --- |
| Domains | 展示/选择 domain | `GET /api/domains`, `GET /api/domains/{domain_key}` | 已暴露 |
| Domains 管理 | 管理配置 | `POST /api/domains`, `DELETE /api/domains/{domain_key}`, `POST /api/domains/reload` | 已暴露，属写操作 |
| Query expansions | 展示/编辑扩展词 | `GET /api/query-expansions`, `POST /api/query-expansions`, `DELETE /api/query-expansions/{id}`, `POST /api/query-expansions/reload` | 已暴露，写操作须用临时项 |
| 文档列表 | 筛选/分页 | `GET /api/docs` | 已暴露 |
| 文档详情 | 详情 drawer | `GET /api/docs/{document_id}` | 已暴露 |
| 原文查看 | content preview | `GET /api/docs/{document_id}/content` | 已暴露 |
| 原文编辑 | 新版本更新 | `PUT /api/docs/{document_id}/content` | 已暴露，非本轮 smoke |
| Chunks | 查看已索引 chunks | `GET /api/docs/{document_id}/chunks` | 已暴露；依赖 Qdrant 与 registry 文档 |
| Reindex | 单篇/changed-only | `POST /api/docs/{document_id}/reindex`, `POST /api/docs/reindex?domain=...&changed_only=true` | 已暴露；写索引 |
| Delete index | 清理文档索引 | `DELETE /api/docs/{document_id}/index` | 已暴露；破坏性操作仅限临时文档 |
| Index jobs | 展示任务/状态 | `GET /api/index/jobs` | 已暴露，但仅返回最新文档索引状态，`history_available=false`，不等同历史 job 日志 |
| Debug query trace | 展示检索链路 | `POST /api/v1/rag/query`，body 中 `debug=true` | 已暴露；接口路径不同于规划中的 `/api/search/debug` |

### 2.2 前端只读基线

对 `/Users/niuma/Workspace/llmproxy-stack/llmproxy-gateway-mgr` 做只读检查时，`src/api.ts` 仅发现网关请求记录、用量和相似问题接口封装；未发现 codingRAG API 客户端、菜单或 Phase 5 页面引用。该结论仅作为 2026-05-23 22:19（GMT+8）的检查快照，frontend 并行实现完成后应重查。

### 2.3 环境观测

| 项目 | 观测结果 | 影响 |
| --- | --- | --- |
| `http://127.0.0.1:8060/health` | 未有 API 服务监听 | 无法对真实配置实例直接 smoke |
| Python API 依赖 | `fastapi`、`uvicorn`、`psycopg`、`python-multipart` 可导入 | 可启动隔离 API |
| Docker Compose | Qdrant 与 OpenSearch 运行；未观察到 app/PostgreSQL/SeaweedFS 容器运行 | 管理读写和原文/chunk 完整链路不可在当前容器状态验收 |
| Qdrant | `GET /collections` 返回 `401` | 服务可达，但联调需 API key |
| OpenSearch | 根路径返回 `200` | 服务可达 |
| `.env` | 存在 database URL 与对象存储相关配置 | 未加载该配置执行写入或 DB 读取，避免误触现有数据 |

## 3. 联调前置条件

### P0：只读接口联调

- 启动 codingRAG API，并明确其连接的是 QA 可读环境。
- PostgreSQL 已完成 schema 初始化，至少有一个启用 domain 与一个可读取文档。
- 前端配置的 codingRAG API base 可达，CORS/代理路径已确认。
- 准备一个可公开检查的 `document_id`，其 content 不包含敏感材料。

### P1：检索与 chunks 联调

- P0 全部满足。
- Qdrant collection 与 API key 配置有效；文档已有 `chunk_count > 0`。
- Embedding 服务可达；测试 `semantic`/`hybrid`/`rerank` 时对应依赖均可达。
- 若使用 OpenSearch/BM25，对应 index 已建立并与测试文档同步。
- PostgreSQL 中已有一个 query expansion 测试项，或允许创建带 `qa_phase5_` 前缀的临时项后清理。

### P2：写操作联调

- 使用隔离 QA domain 或明确可删除的 `qa_phase5_*` 文档/扩展词；禁止对现有业务文档执行更新、disable、delete index 或 reindex。
- QA 文档原文、对象存储位置、collection/index 均可被测试结束后的清理流程识别。
- reindex 前记录文档当前 `status`、`content_hash`、`chunk_count`、`indexed_at`，并在完成/清理后复核。

## 4. 本轮已执行的安全 Smoke

### 4.1 执行方式

为阻止 API 启动阶段连接 `.env` 中潜在现有 PostgreSQL，本轮使用显式空 registry URL 启动隔离实例：

```bash
CODING_RAG_DATABASE_URL='' CODING_RAG_PRELOAD_DOMAINS='' \
  python3 -m uvicorn api.app:app --host 127.0.0.1 --port 18060
```

该实例未配置 domain、未调用 scan/reindex/update/enable/disable/delete/import/export 等写路径；验证结束后已停止进程。

### 4.2 已执行结果

| ID | 请求 | 期望 | 实际 | 结果 |
| --- | --- | --- | --- | --- |
| SMK-001 | `GET /health` | 服务可响应，domain 列表为空 | `200`, `status=ok`, `default_domain=ios`, `available_domains=[]` | PASS |
| SMK-002 | `GET /api/domains` | 空 registry 下不报错 | `200`, `[]` | PASS |
| SMK-003 | `GET /api/libraries` | 明确提示缺少 DB | `503`, `CODING_RAG_DATABASE_URL is not configured` | PASS |
| SMK-004 | `GET /api/query-expansions` | 明确提示缺少 DB | `503`, 同上 | PASS |
| SMK-005 | `GET /api/docs?limit=1&offset=0` | 明确提示缺少 DB | `503`, 同上 | PASS |
| SMK-006 | `GET /api/docs/{nil-id}` | 明确提示缺少 DB | `503`, 同上 | PASS |
| SMK-007 | `GET /api/docs/{nil-id}/content` | 明确提示缺少 DB | `503`, 同上 | PASS |
| SMK-008 | `GET /api/docs/{nil-id}/chunks` | 明确提示缺少 DB | `503`, 同上 | PASS |
| SMK-009 | `GET /api/index/jobs?limit=1` | 明确提示缺少 DB | `503`, 同上 | PASS |
| SMK-010 | `POST /api/v1/rag/query`, 不存在 domain，`debug=false` | domain 校验优先返回客户端错误 | `400`, available domains 为空 | PASS |
| SMK-011 | `POST /api/v1/rag/query`, 不存在 domain，`debug=true` | domain 校验优先返回客户端错误 | `400`, available domains 为空 | PASS |
| SMK-012 | `GET /openapi.json` | 包含 Phase 5 需用路由 | `200`，包含 docs/content/chunks/reindex/index、index jobs、domains、query-expansions 与 rag query | PASS |

结论：当前本地源码可启动，并已暴露 Phase 5 的核心 API 契约；实际数据、chunks、reindex 与 trace 成功路径被 PostgreSQL/app/对象存储/Embedding 配置前提阻塞，本轮未以真实库强行执行。

## 5. 验收用例清单

状态值说明：`DONE` 表示本轮已安全验证；`READY` 表示接口存在且具备环境即可执行；`BLOCKED` 表示需要前端能力或后端语义确认。

### 5.1 Domains

| ID | 级别 | 测试步骤 | 验收断言 | 数据安全 | 状态 |
| --- | --- | --- | --- | --- | --- |
| DOM-001 | P0 | 打开管理页并请求 `GET /api/domains` | 列表字段可映射 `domain_key/display_name/collection/embedding_model_name/rerank_model_name/enabled`；下拉仅展示启用 domain | 只读 | READY |
| DOM-002 | P0 | 请求存在 domain 的 `GET /api/domains/{key}` 与不存在 key | 存在项 `200`；不存在项 `404`；前端错误态可读 | 只读 | READY |
| DOM-003 | P1 | 用 `qa_phase5_domain` 创建、刷新、禁用 domain | 新增项可查询；reload 后仍一致；删除语义为 disable，界面不再作为可选 domain | 仅隔离 QA domain，结束后保持 disabled 或由维护者清理 | READY |

### 5.2 Query Expansions

| ID | 级别 | 测试步骤 | 验收断言 | 数据安全 | 状态 |
| --- | --- | --- | --- | --- | --- |
| EXP-001 | P0 | `GET /api/query-expansions?domain={qa-domain}` | 列表展示 `source_term/expanded_terms/enabled`，domain 筛选生效 | 只读 | READY |
| EXP-002 | P1 | 创建 `source_term=qa_phase5_term`，随后查询与 reload | 创建响应和重载后的列表一致，扩展词顺序/内容不丢失 | 只创建可清理临时项 | READY |
| EXP-003 | P1 | 对包含临时 term 的 debug query 发起查询 | `trace.query_expansion` 或 `expanded_bm25_query` 展示新增词 | 仅读检索结果 | READY |
| EXP-004 | P1 | 删除临时 expansion 后重查/debug query | 列表移除；debug trace 不再应用临时词 | 清理 EXP-002 的测试项 | READY |

### 5.3 文档列表、详情与原文

| ID | 级别 | 测试步骤 | 验收断言 | 数据安全 | 状态 |
| --- | --- | --- | --- | --- | --- |
| DOC-001 | P0 | `GET /api/docs?domain=...&status=...&q=...&limit=20&offset=0` | 返回分页结构；过滤条件与 UI 搜索/翻页保持一致 | 只读 | READY |
| DOC-002 | P0 | 从列表选定文档并请求 `GET /api/docs/{id}` | Drawer 展示 title、domain、source、hash、status、chunk count、indexed time 及 versions | 只读 | READY |
| DOC-003 | P0 | 请求 `GET /api/docs/{id}/content` 和指定 `?version=1` | 当前/历史版本内容与版本元数据一致；缺失内容展示明确错误 | 只读 | READY |
| DOC-004 | P1 | 使用不存在 document id 请求详情/content | UI 正确显示 `404`/空态，不无限 loading | 只读 | READY |
| DOC-005 | P2 | 对 QA 文档 `PUT /api/docs/{id}/content?reindex=false` | 新版本产生且 `index_required`/状态符合实现；旧版本仍可读 | 仅临时 QA 文档 | READY |

### 5.4 Chunks 与索引操作

| ID | 级别 | 测试步骤 | 验收断言 | 数据安全 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CHK-001 | P1 | `GET /api/docs/{qa-id}/chunks?limit=50` | 返回 `document_id/domain/collection/total/items/next_offset`；item 可显示 chunk text/context/index | 只读 | READY |
| CHK-002 | P1 | 对 chunks 多页滚动读取 | 使用返回的 `next_offset` 继续加载，无重复/漏页的可观察异常 | 只读 | READY |
| IDX-001 | P2 | 创建或选用 QA 文档后执行 `POST /api/docs/{id}/reindex` | 返回成功；详情中 `status/indexed_at/chunk_count` 更新；chunks 可查看且归属于该 `doc_id` | 只改 QA 文档索引 | READY |
| IDX-002 | P2 | `POST /api/docs/reindex?domain={qa-domain}&changed_only=true` | 仅 `index_required=true` 的启用 QA 文档被处理；结果能解释成功/失败 | 隔离 QA domain | READY |
| IDX-003 | P2 | 对已记录 baseline 的 QA 文档执行 `DELETE /api/docs/{id}/index` | chunks 清空且状态变为需重建；随后可 reindex 恢复 | 仅临时 QA 文档，先后置恢复 | READY |
| IDX-004 | P0 | 对真实业务文档的 index 删除/reindex 操作 | 前端需二次确认或环境禁用该操作 | 不执行 | BLOCKED |

### 5.5 Debug Query Trace

| ID | 级别 | 测试步骤 | 验收断言 | 数据安全 | 状态 |
| --- | --- | --- | --- | --- | --- |
| TRC-001 | P1 | `POST /api/v1/rag/query`，合法 domain，`debug=false` | 返回 `results/context`，`trace` 为空或未填充；页面不展示调试详情 | 只读 | READY |
| TRC-002 | P1 | 相同请求改为 `debug=true` | `trace.query/domain/method` 与请求一致；展示可用阶段：semantic/bm25/fusion/boosts/rerank/final | 只读 | READY |
| TRC-003 | P1 | 使用命中临时 expansion 的 query，比较 debug 前后 | 展示 `query_expansion` 与 `expanded_bm25_query`，且链路不混淆原查询 | 仅临时扩展项 | READY |
| TRC-004 | P1 | 切换 `semantic`、`bm25`、`hybrid`、`rerank`/实现实际支持方法 | 结果及 trace 阶段符合方法语义；不支持的方法明确返回错误而非静默降级 | 只读 | READY |
| TRC-005 | P1 | 不存在 domain、Embedding/Qdrant 不可达场景 | 前端展示 `400` 或 `502` 的明确诊断信息 | 只读 | DONE（不存在 domain）；依赖错误待联调 |

### 5.6 Index Jobs 与后续能力

| ID | 级别 | 测试步骤 | 验收断言 | 数据安全 | 状态 |
| --- | --- | --- | --- | --- | --- |
| JOB-001 | P1 | `GET /api/index/jobs?domain={qa-domain}&limit=20&offset=0` | 展示当前索引状态、错误信息、更新时间与分页；明确标记 `source=document-index-state` | 只读 | READY |
| JOB-002 | P2 | QA 文档 reindex 成功与故障各一次后查询 jobs | 当前状态可反映最后一次结果；若需历史记录，不将当前端点误呈现为日志列表 | 仅 QA 数据；故障注入需可控 | BLOCKED |
| JOB-003 | P2 | 产品要求“最近任务/错误日志/单篇重试历史” | 需要后端新增真实 index-job history 或确认复用方案；前端暂不得伪造历史 | 不执行 | BLOCKED |

## 6. 前端验收要点

| 页面/交互 | 必测项 | 当前依赖 |
| --- | --- | --- |
| codingRAG 菜单与路由 | 可进入管理模块，刷新深链不丢路由 | frontend 并行实现 |
| 文档列表页 | 过滤、分页、加载/空/错误态、列字段映射 | `GET /api/docs`, domains |
| 文档 Drawer | 原文、versions、chunks 分区展示，超长内容可用 | doc detail/content/chunks |
| 检索调试页 | domain/method/topK/debug 输入与 trace 阶段可视化 | `/api/v1/rag/query` |
| 索引任务页 | 状态字段准确，历史能力缺口有清晰 UI 语义 | `/api/index/jobs` |
| 写操作防护 | reindex/disable/delete index 有环境标识和确认 | QA 临时数据 |

## 7. 阻塞接口与风险清单

| 风险/阻塞 | 影响 | 验证或决策要求 |
| --- | --- | --- |
| app/PostgreSQL/SeaweedFS 当前未观察到运行实例 | 无法验证真实 documents/content 与写链路 | boss 提供 QA 环境启动方式或确认可访问测试 registry |
| Qdrant 需要 API key | 无法验证 chunks/reindex 成功路径 | 提供 QA key 注入方式，不在报告中暴露密钥 |
| Embedding/Rerank 可达性未实测 | 无法验证 debug query 成功链路与 reindex | 提供服务可达的 QA 运行配置 |
| `/api/index/jobs` 目前为状态快照而非历史日志 | Phase 5 “最近任务/错误日志”展示可能误导 | 明确 MVP UI 文案，或追加后端 history API |
| Debug API 路径与规划文档不同 | 前端若按 `/api/search/debug` 实现会失败 | 前端采用已实现的 `/api/v1/rag/query` + `debug=true`，或后端另行提供兼容路由 |
| 后端工作树正由其他实现者并行修改 | 联调契约可能变化 | 前端接入前重取 OpenAPI 并固定一次验收快照 |

## 8. 结果记录模板

### 执行信息

```text
执行日期/时间：
执行者：
frontend commit/worktree 状态：
backend commit/worktree 状态：
API base URL：
测试 domain：
QA document id：
QA query expansion id：
Qdrant/Embedding/OpenSearch/PostgreSQL 状态（不记录密钥）：
```

### 用例结果

| 用例 ID | 执行时间 | 环境/测试数据 | HTTP/UI 结果摘要 | PASS/FAIL/BLOCKED | 缺陷或证据位置 | 清理状态 |
| --- | --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |

### 写操作清理确认

```text
临时 domain 是否已禁用/清理：
临时 query expansion 是否已删除：
临时文档对象及索引是否已清理或标记保留：
执行前/后业务数据未受影响的复核方式：
未清理项目及责任人：
```

## 9. 推荐执行顺序

1. 固定当次 backend OpenAPI 与 frontend API client 路径，解决 debug 路径和 index jobs 语义差异。
2. 在 QA registry 上执行 P0 只读用例：domains、docs、detail、content。
3. 配置 Qdrant/Embedding/OpenSearch 后执行 chunks 与 debug trace 的 P1 用例。
4. 创建带 `qa_phase5_` 标识的可删除数据，执行 expansions、reindex、delete index 与 content update 的 P2 用例。
5. 清理临时数据，填写结果模板；若仍需要“任务历史”，在 Phase 5 放行前记录接口阻塞决定。
