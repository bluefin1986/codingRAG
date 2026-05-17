# codingRAG v2 升级改造方案

> 创建时间：2026-05-17 12:13 GMT+8

## 1. 背景与问题

当前 codingRAG 主要面向代码生成场景做 RAG，已经针对 HarmonyOS / iOS 等技术文档做了 chunk 切分和检索优化。但现有流程存在明显维护问题：

```text
爬虫工程拉取原始文档
  ↓
codingRAG 批量切分
  ↓
生成一个很大的 jsonl
  ↓
导入 Qdrant
```

主要痛点：

1. **大 jsonl 不可维护**
   - 所有文档被压成一个巨大中间文件。
   - 很难知道某个 chunk 来自哪篇原始文档。
   - 简单检索、排查、编辑都不方便。

2. **单篇文档无法独立更新**
   - 某一份原始文档有问题时，难以定位并单独重建。
   - 更新通常意味着重新生成一份完整 jsonl。

3. **索引状态不可见**
   - 不知道哪些文档已索引。
   - 不知道哪些文档索引失败。
   - 不知道每篇文档生成了多少 chunks。

4. **检索质量调试不透明**
   - 难以解释一次 query 为什么命中某些 chunks。
   - semantic / BM25 / hybrid / rerank 各阶段结果不可视。
   - HarmonyOS 场景中曾出现 BM25 全量加载导致内存风险、语义召回偏移等问题。

## 2. 总体目标

将 codingRAG 升级为：

```text
codingRAG v2 = 文档管理 + 增量索引 + 检索调试 + 代码文档 RAG 引擎
```

核心目标：

- 不再依赖大 jsonl 作为主流程。
- 每篇原始文档可见、可查、可独立更新。
- 每个 chunk 可回溯到原始文档。
- 支持单文档 reindex / delete-index / disable。
- 检索过程可 debug、可解释。
- 前端复用 `llmproxy-gateway-mgr` 作为交互界面。

## 3. 推荐架构

```text
爬虫工程
  ↓
原始文档目录 / Git repo
  ↓
codingRAG Document Registry
  - 一篇文档一条记录
  - hash / source_url / platform / path / title
  - 可搜索、可预览、可单篇更新
  ↓
codingRAG Indexer
  - 单文档 chunk
  - 单文档 embedding
  - 单文档 upsert/delete Qdrant
  - 不再依赖大 jsonl
  ↓
codingRAG Retriever
  - semantic / bm25 / hybrid / rerank
  - query expansion
  - trace/debug
  ↓
llmproxy / assistant
  - SDK/API/代码生成问题 → codingRAG
  - 普通知识/业务文档 → WeKnora
```

WeKnora 继续服务 assistant 的普通文档/业务知识库；codingRAG 专注代码/API/SDK 文档检索和代码生成上下文。

## 4. 模块设计

### 4.1 Document Registry 文档登记模块

新增一个轻量文档注册中心。建议初期使用 SQLite，后续可切 PostgreSQL。

建议表：`documents`

```sql
CREATE TABLE documents (
  id TEXT PRIMARY KEY,
  domain TEXT NOT NULL,              -- harmonyos / ios / other
  title TEXT NOT NULL,
  source_url TEXT,
  source_file TEXT,
  local_path TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  content_length INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'new', -- new / indexed / failed / disabled
  indexed_at TEXT,
  updated_at TEXT NOT NULL,
  chunk_count INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  metadata_json TEXT
);

CREATE INDEX idx_documents_domain ON documents(domain);
CREATE INDEX idx_documents_status ON documents(status);
CREATE INDEX idx_documents_hash ON documents(content_hash);
CREATE INDEX idx_documents_source_file ON documents(source_file);
```

职责：

- 扫描原始文档目录。
- 计算文档 hash。
- 记录文档来源、标题、路径、状态。
- 判断新增、变更、删除、禁用。
- 为前端提供文档列表和详情。

### 4.2 增量索引模块

现有全量 jsonl 流程改为 per-document indexing。

```text
单篇文档
  ↓
读取 content
  ↓
使用 codingRAG chunker 切分
  ↓
embedding
  ↓
删除 Qdrant 中 doc_id = 当前文档的旧 chunks
  ↓
upsert 新 chunks
  ↓
更新 documents.indexed_at / chunk_count / status
```

建议 CLI：

```bash
codingrag scan --domain harmonyos
codingrag index --doc-id <doc_id>
codingrag reindex --doc-id <doc_id>
codingrag reindex --domain harmonyos --changed-only
codingrag delete-index --doc-id <doc_id>
```

Qdrant payload 必须包含：

```json
{
  "doc_id": "...",
  "domain": "harmonyos",
  "title": "...",
  "source_url": "...",
  "source_file": "...",
  "chunk_index": 12,
  "has_code": true,
  "content_hash": "..."
}
```

### 4.3 Chunk 管理模块

新增 chunks 可视化和反查能力。

建议能力：

- 按 `doc_id` 查看 chunks。
- 查看 chunk 内容、metadata、hash。
- 查看 chunk 在原文中的位置。
- 查看该 chunk 是否已写入 Qdrant。
- 检索结果可回跳到文档详情和 chunk 详情。

可选表：`document_chunks`

```sql
CREATE TABLE document_chunks (
  id TEXT PRIMARY KEY,
  doc_id TEXT NOT NULL,
  domain TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  start_offset INTEGER,
  end_offset INTEGER,
  has_code INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(doc_id) REFERENCES documents(id)
);

CREATE INDEX idx_document_chunks_doc_id ON document_chunks(doc_id);
CREATE INDEX idx_document_chunks_domain ON document_chunks(domain);
```

如果担心 SQLite 存大文本，可只存 chunk metadata，内容从原文动态切出；但初期为了调试方便，可以先存 content。

### 4.4 Retriever 调试增强

新增 debug search API，返回完整检索链路。

返回结构示例：

```json
{
  "query": "鸿蒙怎么生成随机 uuid",
  "domain": "harmonyos",
  "method": "hybrid_rerank",
  "expanded_query": "util.generateRandomUUID generateRandomUUID @ohos.util @kit.ArkTS",
  "semantic_top": [],
  "keyword_top": [],
  "rerank_top": [],
  "final_chunks": [],
  "context_length": 12345
}
```

需要重点优化：

- API 标识符精确命中加权。
- HarmonyOS 包名 / 类名 / 方法名 query expansion。
- BM25 不再全量加载超大索引，优先轻量在线或按 domain/doc 范围控制。
- 为历史问题建立回归样例，例如：
  - `httpRequest.request 网络请求失败 error code 如何处理`
  - `鸿蒙NEXT开发中如何生成随机的uuid`
  - iOS UIKit / SwiftUI API 查询。

## 5. 前端复用 llmproxy-gateway-mgr

在 `llmproxy-gateway-mgr` 中新增菜单：

```text
请求追踪
用户调用情况
codingRAG 管理
```

### 5.1 文档列表页

字段：

```text
Domain | Title | Source | Hash | Status | Chunks | Indexed At | Actions
```

操作：

- 查看原文
- 查看 chunks
- Reindex
- Disable
- Delete index

### 5.2 文档详情 Drawer

展示：

- 标题、domain、source_url、source_file
- content_hash、indexed_at、chunk_count、status
- 原文预览
- chunks 列表
- 最近索引日志
- Qdrant payload 示例

### 5.3 索引任务页

展示：

- 当前任务
- 最近任务
- 成功/失败数量
- 错误日志
- 单文档重试

### 5.4 检索调试页

输入：

```text
query
domain
method: semantic / bm25 / hybrid / hybrid_rerank
topK
```

输出：

- expanded query
- semantic top
- keyword top
- rerank top
- final context
- 命中文档和 chunks
- score / rerank_score

## 6. 后端 API 设计

codingRAG API 新增：

```http
GET    /api/docs
GET    /api/docs/:id
GET    /api/docs/:id/content
GET    /api/docs/:id/chunks
POST   /api/docs/scan
POST   /api/docs/:id/reindex
POST   /api/docs/:id/disable
DELETE /api/docs/:id/index

GET    /api/index/jobs
POST   /api/index/rebuild?domain=harmonyos&changedOnly=true

POST   /api/search
POST   /api/search/debug
```

### 6.1 `GET /api/docs`

查询参数：

```text
domain
status
q
limit
offset
```

返回：

```json
{
  "items": [],
  "total": 0,
  "limit": 20,
  "offset": 0
}
```

### 6.2 `POST /api/docs/:id/reindex`

行为：

1. 读取指定文档。
2. 删除 Qdrant 中该 `doc_id` 的旧 chunks。
3. 重新 chunk + embedding + upsert。
4. 更新 registry 状态。

### 6.3 `POST /api/search/debug`

用于前端调试检索质量，返回完整 trace。

## 7. 实施计划

### Phase 1：建立 Document Registry

目标：让所有原始文档可见、可搜索、可定位。

任务：

1. 新增 SQLite registry。
2. 实现扫描原始 docs 目录。
3. 计算 hash、提取 title/source_file/domain。
4. 实现文档列表 API。
5. 实现文档详情和原文 API。

验收：

- 能看到 HarmonyOS / iOS 每篇文档数量。
- 能按标题、source_file、source_url 搜索。
- 能定位任意文档的本地路径和 hash。

### Phase 2：单文档增量索引

目标：去掉大 jsonl 主流程。

任务：

1. 改造 indexer，支持 `doc_id` 输入。
2. Qdrant upsert payload 增加 `doc_id/source_url/source_file/title/hash`。
3. 实现单文档 delete-index。
4. 实现 changed-only reindex。
5. 更新 `indexed_at/chunk_count/status/error_message`。

验收：

- 修改一篇文档，只重建这一篇。
- 不再需要生成完整 jsonl。
- 检索结果能回溯到原始文档。

### Phase 3：检索质量与 trace

目标：让检索过程透明、可解释、可回归。

任务：

1. search API 增加 debug trace。
2. 展示 semantic / keyword / rerank 各阶段结果。
3. 加 query expansion。
4. 优化 API/code identifier boost。
5. 建立 HarmonyOS / iOS 回归 query 集。

验收：

- 能解释为什么命中某篇文档。
- 历史问题 query 能稳定返回正确文档。
- BM25 不再因全量加载导致内存风险。

### Phase 4：前端管理界面

目标：在 `llmproxy-gateway-mgr` 中管理 codingRAG。

任务：

1. 新增 codingRAG 菜单。
2. 新增文档列表页。
3. 新增文档详情 Drawer。
4. 新增 chunks 查看页。
5. 新增检索调试页。
6. 新增索引任务页。

验收：

- 前端可搜索文档。
- 可查看原文和 chunks。
- 可点击单篇 reindex。
- 可调试 query 并看到命中链路。

### Phase 5：llmproxy / assistant 路由集成

目标：统一入口，根据问题类型路由 RAG。

规则：

```text
SDK / API / 代码生成 / 报错 / 组件用法 → codingRAG
普通知识 / 业务文档 / 文案资料 → WeKnora
```

验收：

- llmproxy 能将 HarmonyOS / iOS 技术问题稳定路由到 codingRAG。
- 请求追踪中能看到 query、domain、命中文档、context 长度。

## 8. 不建议现在做的事情

暂不建议：

1. 在 codingRAG 中重做完整 WeKnora 式权限/组织/多人协作。
2. 修改 WeKnora 源码强行增加 raw-only。
3. 一开始就上复杂工作流系统。
4. 把 codingRAG 完全替换成通用 RAG 平台。

当前最重要的是：

```text
能定位、能更新、能回溯、能调试
```

## 9. 推荐优先级

优先做：

1. Document Registry。
2. per-doc index / reindex。
3. Qdrant payload 回溯字段。
4. llmproxy-gateway-mgr 文档管理页。
5. 检索 debug trace。

完成这些后，codingRAG 就能从“批处理脚本 + 大 jsonl + Qdrant”升级为真正可维护的代码文档 RAG 系统。
