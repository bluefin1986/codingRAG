import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from api.engine import DomainQueryEngine


class DomainQueryEngineTopKTest(unittest.TestCase):
    def _engine(self) -> DomainQueryEngine:
        engine = DomainQueryEngine.__new__(DomainQueryEngine)
        engine.cfg = {
            "domain": "harmonyos",
            "collection": "harmonyos-collection",
            "rerank_model_name": "bge-reranker-base",
            "bm25_weight": 0.3,
            "path_boost_per_match": 0.0,
            "known_identifiers": [],
        }
        engine.domain = "harmonyos"
        engine.collection = "harmonyos-collection"
        engine.embedding_api_base = "http://localhost:8030"
        engine.embedding_model_name = "bge-m3"
        engine.rerank_api_base = "http://localhost:8030"
        engine.rerank_model_name = "bge-reranker-base"
        engine.qdrant_host = "localhost"
        engine.qdrant_port = 6333
        engine.prompt_role = "技术专家"
        engine.bm25_enabled = True
        engine.bm25_weight = 0.3
        engine.path_boost_per_match = 0.0
        return engine

    def _result(self, idx: int) -> dict:
        return {
            "score": float(100 - idx),
            "domain": "harmonyos",
            "text": f"text-{idx}",
            "context": f"context-{idx}",
            "source_file": f"file-{idx}.md",
            "chunk_index": idx,
            "has_code": False,
        }

    def test_search_returns_exact_top_k_after_rerank(self) -> None:
        engine = self._engine()
        semantic_results = [self._result(i) for i in range(10)]
        bm25_results = [self._result(i) for i in range(10)]
        fused_results = [self._result(i) for i in range(10)]
        reranked_results = [self._result(i) for i in range(10)]

        rerank_results = Mock(return_value=reranked_results)

        with ExitStack() as stack:
            semantic_search = stack.enter_context(patch.object(engine, "semantic_search", return_value=semantic_results))
            bm25_search = stack.enter_context(patch.object(engine, "bm25_search", return_value=bm25_results))
            rrf_fuse = stack.enter_context(patch("api.engine.rrf_fuse", return_value=fused_results))
            path_boost = stack.enter_context(
                patch("api.engine.path_boost", side_effect=lambda results, query, boost_per_match: results)
            )
            identifier_boost = stack.enter_context(
                patch(
                    "api.engine.identifier_boost",
                    side_effect=lambda results, query, boost_per_match, symbol_weights: results,
                )
            )
            protect_hits = stack.enter_context(
                patch("api.engine._protect_symbol_bm25_hits", side_effect=lambda results, bm25_results, query, limit: results)
            )
            load_keyword_searcher = stack.enter_context(
                patch("api.engine._load_keyword_searcher_for_domain", return_value=(None, []))
            )
            drop_noise = stack.enter_context(
                patch("api.engine._drop_obvious_noise", side_effect=lambda results, query='': results)
            )
            stack.enter_context(patch("api.engine.rerank_results", rerank_results))

            results, trace = engine.search("鸿蒙 getRectangleById 方法返回值", top_k=5, method="hybrid", rerank=True, debug=False)

        self.assertEqual(len(results), 5)
        self.assertIsNone(trace)
        semantic_search.assert_called_once()
        bm25_search.assert_called_once()
        rrf_fuse.assert_called_once()
        path_boost.assert_called_once()
        identifier_boost.assert_called_once()
        protect_hits.assert_called_once()
        load_keyword_searcher.assert_called_once()
        drop_noise.assert_called_once()
        rerank_results.assert_called_once()
        self.assertEqual(rerank_results.call_args.kwargs["top_k"], 5)


if __name__ == "__main__":
    unittest.main()
