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
codingRAG Document Registry（PostgreSQL）
  - 一个文档库一组版本化记录
  - 一篇文档一条记录，记录 hash / source_url / platform / path / title / enabled / version
  - 可搜索、可预览、可单篇更新、可启用/禁用
  - 支持按文档库导出 tar/zip，并导入到另一个环境
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

新增一个文档注册中心。考虑到文档管理是持续更新型能力，并且需要跨环境同步、导出、导入和版本追踪，v2 直接使用 PostgreSQL，不再先落 SQLite。

PostgreSQL 只保存文档库、文档、版本、chunk 元数据、索引状态、导入导出清单等管理数据；Qdrant / OpenSearch 仍作为可由 PostgreSQL + 原文内容派生出来的运行索引。


### 4.0 原始文档存储策略

原始文档不要直接塞进 PostgreSQL 主表，也不要只依赖本地路径。推荐采用“对象存储 + PostgreSQL 元数据”的方式：

```text
原始文档文件 / Markdown / HTML / PDF
  ↓
对象存储：SeaweedFS / MinIO / S3 兼容存储
  ↓
PostgreSQL：只记录 storage_key、hash、版本、启停状态、来源信息
  ↓
Qdrant / OpenSearch：只保存 chunk 与可回溯 payload
```

#### 4.0.1 SeaweedFS / S3 兼容存储选择

codingRAG 原始文档优先采用 **SeaweedFS**：本地 Docker 部署轻量，原生提供 filer HTTP 与 S3-compatible API，后续迁移到 MinIO / S3 生态也更顺滑。FastDFS 不再作为推荐实现路径，避免额外上传服务、SDK/运维生态弱和跨环境迁移成本高的问题。

建议判断：

| 方案 | 适用情况 | 风险 |
|------|----------|------|
| SeaweedFS | 希望轻量部署，同时保留 HTTP/S3 访问能力；适合作为默认本地/内网对象存储 | 需要维护 master/volume/filer 三个服务 |
| MinIO / S3 | 希望本地、测试、生产完全统一；希望备份、迁移、生命周期管理更标准 | 需要额外部署对象存储服务 |
| 本地文件目录 | 开发期最简单 | 多环境同步差，不适合作为长期唯一来源 |

推荐抽象为 `ObjectStorage` 接口，第一版支持 local 与 SeaweedFS，业务层不要绑定具体存储。

#### 4.0.2 原始文档定位

chunk 返回给 RAG 后，必须能反查原始文档。Qdrant payload 至少包含：

```json
{
  "library_id": "...",
  "doc_id": "...",
  "document_version": 3,
  "chunk_id": "...",
  "chunk_index": 12,
  "source_file": "docs/network/http.md",
  "relative_path": "docs/network/http.md",
  "source_url": "...",
  "storage_key": "libraries/harmonyos/docs/network/http.v3.md",
  "content_hash": "sha256:...",
  "start_offset": 1024,
  "end_offset": 2048
}
```

前端查看引用时流程：

```text
检索结果 chunk
  ↓ doc_id + document_version + chunk_index
GET /api/docs/:id/content?version=3
  ↓
从对象存储读取原文
  ↓
按 start_offset/end_offset 高亮 chunk 原文位置
```

#### 4.0.3 storage 字段建议

`document_versions` 增加对象存储字段：

```sql
ALTER TABLE document_versions ADD COLUMN storage_backend TEXT NOT NULL DEFAULT 'local'; -- local / seaweedfs / s3 / minio
ALTER TABLE document_versions ADD COLUMN storage_bucket TEXT;
ALTER TABLE document_versions ADD COLUMN storage_key TEXT;
ALTER TABLE document_versions ADD COLUMN storage_etag TEXT;
ALTER TABLE document_versions ADD COLUMN storage_size BIGINT;
ALTER TABLE document_versions ADD COLUMN storage_status TEXT NOT NULL DEFAULT 'active'; -- active / deleting / deleted / missing
ALTER TABLE document_versions ADD COLUMN expires_at TIMESTAMPTZ;
```

`documents` 当前行只保存当前版本摘要；真正可下载的原文位置以 `document_versions.storage_*` 为准。

### 4.1.1 文档库表：`doc_libraries`

```sql
CREATE TABLE doc_libraries (
  id UUID PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,          -- harmonyos / ios / redis62 / kafka28 / nginx
  name TEXT NOT NULL,
  description TEXT,
  domain TEXT NOT NULL,
  source_type TEXT NOT NULL DEFAULT 'filesystem', -- filesystem / git / archive
  source_uri TEXT,
  root_path TEXT,                     -- 环境本地路径，不作为跨环境强约束
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  version TEXT NOT NULL DEFAULT '1.0.0',
  retrieval_mode TEXT NOT NULL DEFAULT 'hybrid_rerank', -- semantic / bm25 / hybrid / hybrid_rerank
  embedding_model TEXT,
  embedding_model_name TEXT,
  embedding_dim INTEGER,
  rerank_model_name TEXT,
  keyword_backend TEXT,              -- opensearch / local_bm25 / none
  qdrant_collection TEXT,
  opensearch_index TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (source_type IN ('filesystem', 'git', 'archive')),
  CHECK (retrieval_mode IN ('semantic', 'bm25', 'hybrid', 'hybrid_rerank'))
);

CREATE INDEX idx_doc_libraries_domain ON doc_libraries(domain);
CREATE INDEX idx_doc_libraries_enabled ON doc_libraries(enabled);
```

职责：

- 定义一个可整体迁移的文档库边界。
- 支持启用 / 禁用整个文档库。
- 记录文档库当前版本号、来源、根路径和元数据。
- 显式记录该文档库的检索模式、embedding 模型、rerank 模型、关键词后端和索引名称。
- 作为导出 / 导入的最小单位。

检索配置必须进入 `doc_libraries`，不能只留在 `config.py`：

- `retrieval_mode`：默认建议 `hybrid_rerank`，可按文档库覆盖。
- `embedding_model / embedding_model_name / embedding_dim`：用于判断索引是否需要重建。
- `rerank_model_name`：用于检索链路可追踪和跨环境迁移。
- `keyword_backend / qdrant_collection / opensearch_index`：用于说明该文档库当前派生索引落点。

当上述任一配置变化时，应将该文档库下文档标记 `index_required=true`，避免新模型读取旧向量索引。

### 4.1.2 文档表：`documents`

建议表：`documents`

```sql
CREATE TABLE documents (
  id UUID PRIMARY KEY,
  library_id UUID NOT NULL REFERENCES doc_libraries(id),
  domain TEXT NOT NULL,              -- harmonyos / ios / redis62 / kafka28 / nginx / other
  doc_key TEXT NOT NULL,             -- 稳定业务键，建议由 library_code + normalized source_file 生成
  title TEXT NOT NULL,
  source_url TEXT,
  source_file TEXT,
  local_path TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  mime_type TEXT,
  language TEXT,
  content_hash TEXT NOT NULL,
  content_length INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  status TEXT NOT NULL DEFAULT 'new', -- new / changed / indexed / failed / disabled / deleted
  indexed_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ,
  last_scanned_at TIMESTAMPTZ,
  scan_run_id UUID,
  index_required BOOLEAN NOT NULL DEFAULT TRUE,
  last_index_error_at TIMESTAMPTZ,
  chunk_count INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(library_id, doc_key),
  UNIQUE(library_id, relative_path),
  CHECK (status IN ('new', 'changed', 'indexed', 'failed', 'disabled', 'deleted'))
);

CREATE INDEX idx_documents_library_id ON documents(library_id);
CREATE INDEX idx_documents_domain ON documents(domain);
CREATE INDEX idx_documents_status ON documents(status);
CREATE INDEX idx_documents_enabled ON documents(enabled);
CREATE INDEX idx_documents_hash ON documents(content_hash);
CREATE INDEX idx_documents_source_file ON documents(source_file);
```

职责：

- 扫描原始文档目录。
- 计算文档 hash。
- 记录文档来源、标题、路径、状态。
- 判断新增、变更、删除、启用、禁用。
- 维护文档版本号：同一 `doc_key` 内容 hash 变化时，`version + 1`。
- 为前端提供文档列表和详情。

### 4.1.3 文档版本表：`document_versions`

```sql
CREATE TABLE document_versions (
  id UUID PRIMARY KEY,
  document_id UUID NOT NULL REFERENCES documents(id),
  version INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  content_length INTEGER NOT NULL DEFAULT 0,
  title TEXT,
  source_url TEXT,
  source_file TEXT,
  relative_path TEXT NOT NULL,
  storage_path TEXT,                 -- 兼容字段：归档后的原文路径或对象存储 key
  storage_backend TEXT NOT NULL DEFAULT 'local', -- local / seaweedfs / s3 / minio
  storage_bucket TEXT,
  storage_key TEXT,
  storage_etag TEXT,
  storage_size BIGINT,
  storage_status TEXT NOT NULL DEFAULT 'active', -- active / deleting / deleted / missing
  expires_at TIMESTAMPTZ,
  change_type TEXT NOT NULL DEFAULT 'update', -- create / update / delete / restore
  tombstone BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(document_id, version),
  CHECK (change_type IN ('create', 'update', 'delete', 'restore')),
  CHECK (storage_backend IN ('local', 'seaweedfs', 's3', 'minio', 'fastdfs')), -- fastdfs 仅用于兼容历史记录
  CHECK (storage_status IN ('active', 'deleting', 'deleted', 'missing'))
);

CREATE INDEX idx_document_versions_document_id ON document_versions(document_id);
CREATE INDEX idx_document_versions_hash ON document_versions(content_hash);
```

职责：

- 保留文档关键版本信息，便于审计、回滚和迁移比对。
- 导出时记录当前启用版本，也可选择带历史版本。
- 导入时根据 `doc_key + content_hash + version` 判断新增、覆盖、跳过或冲突。
- `document_versions` 按不可变记录处理；`documents` 只保存当前版本指针和当前状态。
- 同一 `doc_key + version` 但 hash 不同必须视为冲突，不能静默覆盖。
- 删除通过 tombstone 版本表达，避免跨环境同步时被误恢复。


### 4.1.4 版本保留与自动清理策略

为了避免原始文件和历史版本无限增长，默认每个文档最多保留 **2 个版本**：

- 当前版本：`documents.version` 指向的最新有效版本。
- 上一个版本：用于回滚、对比和排查。
- 更早版本：进入过期清理队列。

建议新增清理任务表：

```sql
CREATE TABLE document_retention_jobs (
  id UUID PRIMARY KEY,
  library_id UUID REFERENCES doc_libraries(id),
  document_id UUID REFERENCES documents(id),
  status TEXT NOT NULL DEFAULT 'pending', -- pending / running / completed / failed
  retention_versions INTEGER NOT NULL DEFAULT 2,
  dry_run BOOLEAN NOT NULL DEFAULT TRUE,
  summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  CHECK (status IN ('pending', 'running', 'completed', 'failed'))
);
```

清理规则：

1. 每个 `document_id` 按 `version DESC` 排序，只保留前 2 个版本。
2. 过期版本先标记 `storage_status='deleting'`，再删除对象存储文件。
3. 对象存储删除成功后，标记 `storage_status='deleted'`。
4. `document_versions` 元数据默认保留摘要记录，不物理删除，便于审计；但原始文件可删除。
5. 如果某个过期版本仍被 transfer job / index job 引用，则跳过本轮清理。
6. tombstone 版本参与版本序列，但清理时必须保留最新 tombstone，用于跨环境同步删除语义。

建议 CLI / API：

```bash
codingrag retention preview --library harmonyos --keep 2
codingrag retention run --library harmonyos --keep 2
```

```http
POST /api/libraries/:id/retention/preview
POST /api/libraries/:id/retention/run
GET  /api/retention-jobs/:id
```

建议定时任务：

- 每天凌晨执行一次 retention preview + run。
- 默认只清理原始文件和 chunk content，不删版本元数据。
- 清理失败要可重试，不能影响当前版本检索。

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
  id UUID PRIMARY KEY,
  doc_id UUID NOT NULL,
  library_id UUID NOT NULL,
  domain TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  document_version INTEGER NOT NULL,
  start_offset INTEGER,
  end_offset INTEGER,
  has_code BOOLEAN NOT NULL DEFAULT FALSE,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY(doc_id) REFERENCES documents(id),
  UNIQUE(doc_id, document_version, chunk_index)
);

CREATE INDEX idx_document_chunks_doc_id ON document_chunks(doc_id);
CREATE INDEX idx_document_chunks_library_id ON document_chunks(library_id);
CREATE INDEX idx_document_chunks_domain ON document_chunks(domain);
```

如果担心 PostgreSQL 存大文本导致表膨胀，可只存 chunk metadata，内容从原文动态切出；但初期为了调试方便，可以先存 content。

### 4.4 文档库迁移与导入导出

文档库迁移的目标是：一个环境中整理好的文档库，可以一键导出为 `tar.gz` 或 `zip`，在另一个环境导入后保留文档关键元信息、版本号、启用状态和索引状态，并可选择是否立即重建派生索引。

#### 4.4.1 导出包结构

```text
codingrag-library-harmonyos-20260523.tar.gz
  manifest.json
  schema.sql                       # 可选：当前导出版本对应 schema 快照
  data/
    doc_libraries.jsonl
    documents.jsonl
    document_versions.jsonl
    document_chunks.jsonl          # 可选，默认导出 chunk metadata，可配置是否含 content
  files/
    <relative_path 原文文件>
  indexes/
    qdrant.snapshot                # 可选，不作为默认交付物
    opensearch.snapshot            # 可选，不作为默认交付物
  checksums.txt
```

`manifest.json` 建议字段：

```json
{
  "format": "codingrag-library-archive",
  "format_version": "1.0",
  "schema_version": "2026-05-23.pg-registry-v1",
  "export_tool_version": "codingrag-v2",
  "exported_at": "2026-05-23T10:15:00+08:00",
  "source_env": "dev-mac-mini",
  "library": {
    "code": "harmonyos",
    "name": "HarmonyOS Docs",
    "domain": "harmonyos",
    "version": "1.3.0",
    "enabled": true
  },
  "counts": {
    "documents": 1234,
    "enabled_documents": 1200,
    "chunks": 89488
  },
  "options": {
    "include_files": true,
    "include_chunk_content": true,
    "include_index_snapshots": false
  },
  "checksum_algorithm": "sha256",
  "content_checksum": "sha256:...",
  "manifest_checksum": "sha256:..."
}
```

#### 4.4.2 导入策略

导入时必须先 dry-run，输出变更计划，再执行实际写入。默认策略为：目标环境已存在相同 `library.code` 时只做 dry-run，不自动覆盖；实际导入必须显式指定 `--mode upsert` 或其他模式。

冲突处理策略：

- `skip`：目标环境已有相同 `library.code + doc_key + content_hash` 时跳过。
- `upsert`：同一 `doc_key` 内容变化时新建 document version，并更新 documents 当前版本。
- `replace-library`：禁用或归档目标库旧版本后整体替换。
- `rename-library`：当目标环境已有同 code 文档库但不想覆盖时，导入为新 code。

导入完成后：

1. 写入 `doc_libraries / documents / document_versions / document_chunks`。
2. 校验原文文件 checksum。
3. 拒绝 archive path traversal：禁止 `../`、绝对路径和未授权 symlink；检查 macOS/Linux 大小写路径碰撞。
4. 清理或脱敏 `source_uri / metadata` 中可能包含的本地路径和敏感信息。
5. disabled library/doc 导入后保持 disabled，且不自动触发 indexing。
6. 默认不直接导入 Qdrant / OpenSearch 快照，而是标记 `index_required=true`。
7. 由目标环境按本机 embedding / Qdrant / OpenSearch 配置重建索引。

这样可以避免不同环境的向量模型、Qdrant collection、OpenSearch 分词器配置不一致导致索引不可用。

#### 4.4.3 导入导出任务表

```sql
CREATE TABLE library_transfer_jobs (
  id UUID PRIMARY KEY,
  library_id UUID REFERENCES doc_libraries(id),
  direction TEXT NOT NULL,           -- export / import
  archive_path TEXT,
  status TEXT NOT NULL DEFAULT 'pending', -- pending / running / completed / failed
  mode TEXT,                         -- skip / upsert / replace-library / rename-library
  dry_run BOOLEAN NOT NULL DEFAULT TRUE,
  summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  CHECK (direction IN ('export', 'import')),
  CHECK (status IN ('pending', 'running', 'completed', 'failed')),
  CHECK (mode IS NULL OR mode IN ('skip', 'upsert', 'replace-library', 'rename-library'))
);

CREATE INDEX idx_library_transfer_jobs_library_id ON library_transfer_jobs(library_id);
CREATE INDEX idx_library_transfer_jobs_status ON library_transfer_jobs(status);
```

#### 4.4.4 推荐 CLI

```bash
codingrag library list
codingrag library export --library harmonyos --output ./exports --format tar.gz --include-files
codingrag library import --archive ./exports/codingrag-library-harmonyos.tar.gz --dry-run
codingrag library import --archive ./exports/codingrag-library-harmonyos.tar.gz --mode upsert
codingrag library enable --library harmonyos
codingrag library disable --library harmonyos
```

#### 4.4.5 推荐 API

```http
GET    /api/libraries
POST   /api/libraries
PATCH  /api/libraries/:id
POST   /api/libraries/:id/enable
POST   /api/libraries/:id/disable

POST   /api/libraries/:id/export
POST   /api/libraries/import/preview
POST   /api/libraries/import
GET    /api/library-transfer-jobs/:id
```

#### 4.4.6 大批量导入异步化（Phase 2.1）

- `POST /api/libraries/import` 默认只创建 `library_transfer_jobs` 记录并立即返回 `job_id`；`/preview` 继续同步 dry-run。
- 兼容同步导入：请求体或 query 可传 `async=false`，仅用于小归档/调试。
- Worker 通过 `python3 scripts/library_import_worker.py --once` 或 `--job-id <job_id>` 串行处理导入，避免 HTTP 请求承载大文件入库。
- Docker Compose 提供常驻内部服务 `library-import-worker`，命令为 `python3 scripts/library_import_worker.py`，通过 PostgreSQL 轮询 `library_transfer_jobs` 消费待处理导入任务。
- `library-import-worker` 不暴露任何 `ports`，不提供外部 HTTP 入口；进度统一由 API 的 `GET /api/library-transfer-jobs/:id` 查询。
- 批处理大小由 `CODING_RAG_IMPORT_BATCH_SIZE` 控制，默认 100；SeaweedFS 上传默认不并发。
- Job summary 增量记录 `total_documents / processed / created / updated / skipped / conflict / failed / current_doc_key / errors`，便于前端轮询 `GET /api/library-transfer-jobs/:id` 展示进度。
- 导入按 `library_code + doc_key + content_hash + version` 做幂等跳过；单文档失败写入 errors，已成功批次不回滚。

### 4.5 Retriever 调试增强

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
Domain | Title | Source | Retrieval Mode | Embedding | Rerank | Hash | Status | Chunks | Indexed At | Actions
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
GET    /api/libraries
POST   /api/libraries
PATCH  /api/libraries/:id
POST   /api/libraries/:id/enable
POST   /api/libraries/:id/disable
POST   /api/libraries/:id/export
POST   /api/libraries/import/preview
POST   /api/libraries/import

GET    /api/docs
GET    /api/docs/:id
GET    /api/docs/:id/content
GET    /api/docs/:id/chunks
POST   /api/docs/scan
POST   /api/docs/:id/reindex
POST   /api/docs/:id/enable
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

### 6.3 导入导出安全约束

- 导入必须支持 dry-run，且 dry-run 不写入正式库。
- archive 解包必须限制在临时目录内，禁止路径穿越、绝对路径和默认 symlink 跟随。
- DB 写入与文件落盘要么同事务完成，要么记录为可恢复 failed job，不能半导入后静默成功。
- 默认导入模式：同 code 文档库存在时仅 preview；实际覆盖必须显式选择 `upsert / replace-library / rename-library`。
- Phase 1 默认同步 tombstone，用于保留删除语义；但不物理删除目标环境原文，先进入 `deleted/disabled` 状态。

### 6.4 `POST /api/search/debug`

用于前端调试检索质量，返回完整 trace。

## 7. 实施计划

### Phase 1：建立 PostgreSQL Document Registry

目标：让所有原始文档可见、可搜索、可定位，并且文档库本身可迁移。

任务：

1. 新增 PostgreSQL registry，Phase 1 必做 `doc_libraries / documents / document_versions / library_transfer_jobs`；`document_chunks` 可先做 metadata-only 或延后到 per-doc indexing 阶段。
2. 实现扫描原始 docs 目录。
3. 计算 hash、提取 title/source_file/domain/relative_path/doc_key。
4. 从 domain config 写入 retrieval_mode、embedding_model、embedding_dim、rerank_model_name、keyword_backend、qdrant_collection、opensearch_index。
5. 实现文档库与文档列表 API。
6. 实现文档详情和原文 API。
7. 支持文档库与单文档启用 / 禁用。
7. 抽象原始文档 ObjectStorage，支持 local 与 SeaweedFS；默认通过 SeaweedFS filer HTTP 写入原始文档，并保留 S3-compatible 端口便于后续迁移。
8. 实现最多保留 2 个文档版本的 retention preview/run。

验收：

- 能看到 HarmonyOS / iOS 每篇文档数量。
- 能按标题、source_file、source_url 搜索。
- 能定位任意文档的本地路径和 hash。
- 能看到文档库版本号、启用状态、文档启用状态。
- chunk 引用能通过 doc_id/version 回看原始文档并定位原文片段。
- 同一文档最多保留 2 个原始文件版本，过期版本文件可自动清理。

### Phase 2：文档库导入导出与跨环境迁移

目标：支持把一个环境中的文档库完整迁移到另一个环境。

任务：

1. 实现 `library export`，生成 `tar.gz` 或 `zip`，Phase 1 默认包含 manifest + metadata + 原文文件，不包含索引快照。
2. 导出 manifest、原文文件、documents、versions、chunks 元数据和 checksum。
3. 实现 `library import --dry-run`，输出新增、更新、跳过、冲突统计。
4. 实现 `library import --mode skip|upsert|replace-library|rename-library`。
5. 导入后标记需要重建索引，默认不跨环境直接复用 Qdrant / OpenSearch 索引。
6. 在 `docker-compose.yml` 中提供常驻 `library-import-worker` 内部服务，复用 app 镜像并执行 `python3 scripts/library_import_worker.py`，不开放外部端口。

验收：

- 能从 dev 导出一个文档库 archive。
- 能在另一个环境 dry-run 并看到迁移计划。
- 能导入后保留文档库版本、文档版本、启用状态和 hash。
- 能导入后触发 changed-only reindex。

### Phase 3：单文档增量索引

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

### Phase 4：检索质量与 trace

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

### Phase 5：前端管理界面

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

### Phase 6：llmproxy / assistant 路由集成

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

1. PostgreSQL Document Registry。
2. 原始文档对象存储抽象 + chunk 引用回看原文。
3. 文档版本最多保留 2 个 + 自动清理过期原始文件。
4. 文档库导入 / 导出 / dry-run import。
5. per-doc index / reindex。
4. Qdrant payload 回溯字段。
5. llmproxy-gateway-mgr 文档管理页。
6. 检索 debug trace。

完成这些后，codingRAG 就能从“批处理脚本 + 大 jsonl + Qdrant”升级为真正可维护的代码文档 RAG 系统。
