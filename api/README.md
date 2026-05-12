# codingRAG HTTP API

轻量 HTTP 接口，供 llmproxy 等上游服务调用 codingRAG 检索能力。

## 快速启动

```bash
cd /Users/niuma/Workspace/ragworkspace/codingRAG

# 安装依赖（如尚未安装）
pip install fastapi uvicorn

# 启动服务（默认领域由 CODING_RAG_DOMAIN 环境变量控制，默认 ios）
python3 -m uvicorn api.app:app --host 0.0.0.0 --port 8060

# 或指定默认领域
CODING_RAG_DOMAIN=harmonyos python3 -m uvicorn api.app:app --host 0.0.0.0 --port 8060
```

## 接口

### `POST /api/v1/rag/query`

执行 RAG 检索，返回上下文和检索结果。

**请求体：**

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | ✅ | — | 检索查询文本 |
| `domain` | string | — | 服务端默认领域 | 领域名称，如 `ios` / `harmonyos` |
| `topK` | number | — | 5 | 返回结果数量 (1-50) |
| `method` | string | — | `hybrid` | 检索方法：`hybrid` / `semantic` / `bm25` / `rerank` |
| `category` | string | — | null | 文档分类过滤 |
| `hasCode` | boolean | — | null | 是否只检索含代码的文档块 |

**响应体：**

```json
{
  "query": "UIButton 怎么创建",
  "domain": "ios",
  "topK": 3,
  "method": "hybrid",
  "context": "---\n[1] 来源: ...\n...",
  "results": [
    {
      "score": 0.85,
      "domain": "ios",
      "text": "...",
      "context": "...",
      "source_file": "...",
      "has_code": true
    }
  ]
}
```

### `GET /health`

健康检查，返回服务状态和可用领域列表。

## Smoke Test

```bash
# 健康检查
curl http://localhost:8060/health

# iOS 领域查询
curl -s -X POST http://localhost:8060/api/v1/rag/query \
  -H 'Content-Type: application/json' \
  -d '{"query": "UIButton 怎么创建并响应点击事件", "domain": "ios", "topK": 3}' \
  | python3 -m json.tool

# HarmonyOS 领域查询
curl -s -X POST http://localhost:8060/api/v1/rag/query \
  -H 'Content-Type: application/json' \
  -d '{"query": "ArkTS 怎么创建按钮组件", "domain": "harmonyos", "topK": 3}' \
  | python3 -m json.tool

# 不指定 domain，使用服务端默认领域
curl -s -X POST http://localhost:8060/api/v1/rag/query \
  -H 'Content-Type: application/json' \
  -d '{"query": "如何创建按钮"}' \
  | python3 -m json.tool
```

## Docker 部署

```bash
# 构建镜像
docker build -t codingrag-api .

# 运行（需要 Qdrant 和 Embedding API 可达）
docker run -d --name codingrag-api \
  -p 8060:8060 \
  -e CODING_RAG_PRELOAD_DOMAINS=ios,harmonyos \
  -e CODING_RAG_QDRANT_HOST=host.docker.internal \
  -e CODING_RAG_AIMODELS_API_BASE=http://host.docker.internal:8030 \
  -v $(pwd)/output:/app/output:ro \
  codingrag-api
```

## 启动预热

通过 `CODING_RAG_PRELOAD_DOMAINS` 环境变量指定启动时需要预热的领域（逗号分隔）：

```bash
# 启动时加载 ios 和 harmonyos 的 BM25 索引，避免首次请求延迟
CODING_RAG_PRELOAD_DOMAINS=ios,harmonyos python3 -m uvicorn api.app:app --host 0.0.0.0 --port 8060
```

预热在 FastAPI startup 事件中执行。如果某个领域加载失败，会记录警告但不阻止服务启动。

## 依赖服务

| 服务 | 默认地址 | 用途 |
|------|---------|------|
| Qdrant | `localhost:6333` | 向量数据库 |
| AIMODELS (Embedding) | `localhost:8030` | 文本向量化 |
| AIMODELS (Rerank) | `localhost:8030` | 结果重排序（`method=rerank` 时） |

如果以上服务未启动，API 调用会返回 502 错误并附带原因。

## 架构说明

- `api/engine.py` — `DomainQueryEngine` 类，封装单个领域的检索逻辑，支持 per-request domain 切换
- `api/schemas.py` — Pydantic 请求/响应模型
- `api/app.py` — FastAPI 应用，按 domain 懒加载 engine 实例
- BM25 索引按 domain 缓存，首次请求某领域时加载，后续复用

## 不影响现有代码

- `scripts/test_rag.py` 不受影响
- `indexer/retriever.py` 不做任何修改
- `config.py` 不做任何修改
