#!/bin/bash
cd /Users/shadow/Workspace/codingRAG
source .venv/bin/activate

export CODING_RAG_DATABASE_URL="postgresql://codingrag:***@localhost:5432/codingrag"
export CODING_RAG_QDRANT_HOST=localhost
export CODING_RAG_QDRANT_PORT=6333
export CODING_RAG_QDRANT_API_KEY=*** ES_URL=http://localhost:9200
export CODING_RAG_AIMODELS_API_BASE=http://10.90.247.182:8030
export CODING_RAG_STORAGE_BACKEND=local
export CODING_RAG_LOCAL_STORAGE_DIR=/Users/shadow/Workspace/codingRAG/data/originals

exec python3 -m uvicorn api.app:app --host 0.0.0.0 --port 8060
