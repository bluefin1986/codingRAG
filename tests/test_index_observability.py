import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from indexer import per_doc_indexer as indexer_module
from indexer.per_doc_indexer import PerDocumentIndexer


def _document() -> dict:
    return {
        "id": "doc-1",
        "enabled": True,
        "domain": "docs",
        "document_version": 1,
        "vector_chunk_count": 0,
        "bm25_chunk_count": 0,
    }


def _config() -> dict:
    return {
        "collection": "docs-vectors",
        "embedding_model": "BAAI/bge-large-zh-v1.5",
        "embedding_model_name": "bge-large-zh-v1.5",
        "embedding_dim": 1024,
        "noise_patterns": [],
    }


class PerDocumentIndexObservabilityTest(unittest.TestCase):
    def _indexer(self) -> PerDocumentIndexer:
        return PerDocumentIndexer.__new__(PerDocumentIndexer)

    def test_vector_index_logs_actual_embedding_model(self) -> None:
        indexer = self._indexer()
        chunk = SimpleNamespace(text="indexed content", metadata={})
        with patch.object(indexer, "_load_document", return_value=_document()), patch.object(
            indexer, "_domain_config", return_value=_config()
        ), patch.object(indexer, "_read_current_content", return_value="content"), patch.object(
            indexer_module, "parse_blocks", return_value=[]
        ), patch.object(indexer_module, "split_blocks", return_value=[chunk]), patch.object(
            indexer_module, "embed_texts", return_value=[[0.1]]
        ) as embed_texts, patch.object(
            indexer, "_build_points", return_value=[{"id": "point-1"}]
        ), patch.object(
            indexer, "_qdrant_client", return_value=nullcontext(object())
        ), patch.object(
            indexer, "_ensure_collection"
        ), patch.object(
            indexer, "_delete_qdrant_points"
        ), patch.object(
            indexer, "_upsert_points"
        ), patch.object(
            indexer, "_mark_vector_indexed"
        ), self.assertLogs(
            "indexer.per_doc_indexer", level="INFO"
        ) as logs:
            result = indexer.index_document("doc-1", target="vector")

        embed_texts.assert_called_once_with(
            ["indexed content"],
            api_base=indexer_module.EMBEDDING_API_BASE,
            model_name="bge-large-zh-v1.5",
        )
        self.assertIn(
            "Embedding request start domain=docs document_id=doc-1 target=vector "
            "collection=docs-vectors embedding_model_name=bge-large-zh-v1.5",
            "\n".join(logs.output),
        )
        self.assertEqual(result["embedding_model_name"], "bge-large-zh-v1.5")

    def test_bm25_index_logs_embedding_as_not_applicable(self) -> None:
        indexer = self._indexer()
        chunk = SimpleNamespace(text="keyword content", metadata={})
        es_indexer = Mock()
        es_indexer.index_document_chunks.return_value = 1
        with patch.object(indexer, "_load_document", return_value=_document()), patch.object(
            indexer, "_domain_config", return_value=_config()
        ), patch.object(indexer, "_read_current_content", return_value="content"), patch.object(
            indexer_module, "parse_blocks", return_value=[]
        ), patch.object(indexer_module, "split_blocks", return_value=[chunk]), patch.object(
            indexer_module, "embed_texts"
        ) as embed_texts, patch.object(
            indexer, "_get_es_indexer", return_value=es_indexer
        ), patch.object(
            indexer, "_build_es_chunks", return_value=[{"chunk_id": "chunk-1"}]
        ), patch.object(
            indexer, "_mark_bm25_indexed"
        ), self.assertLogs(
            "indexer.per_doc_indexer", level="INFO"
        ) as logs:
            result = indexer.index_document("doc-1", target="bm25")

        embed_texts.assert_not_called()
        rendered_logs = "\n".join(logs.output)
        self.assertIn(
            "BM25 indexing start domain=docs document_id=doc-1 target=bm25 "
            "collection=docs-vectors embedding_model_name=not_applicable",
            rendered_logs,
        )
        self.assertNotIn("Embedding request start", rendered_logs)
        self.assertIsNone(result["embedding_model_name"])


if __name__ == "__main__":
    unittest.main()
