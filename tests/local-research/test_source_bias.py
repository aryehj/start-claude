"""
Unit tests for Phase 5 source-quality biasing modules.
All tests use fixture data — no live services required.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))

import lib.omlx  # noqa: F401 — makes lib.omlx patchable
import lib.expand
import lib.search
import lib.pipeline


# ---------------------------------------------------------------------------
# lib/expand.py — prompt variant selection
# ---------------------------------------------------------------------------

class TestExpandPromptVariants(unittest.TestCase):
    def _captured_prompt(self, env_overrides):
        captured = {}

        def capture_chat(model, messages, **kw):
            captured["content"] = messages[0]["content"]
            return "a\nb\nc\nd"

        with patch.dict(os.environ, env_overrides, clear=False), \
             patch("lib.omlx.chat", side_effect=capture_chat):
            # Reload expand module so it picks up env changes
            import importlib
            importlib.reload(lib.expand)
            lib.expand.expand("test query", n=4)

        return captured.get("content", "")

    def test_generic_prompt_is_default(self):
        env = {"EXPAND_PROMPT_NAME": "", "EXPAND_PROMPT": ""}
        import importlib
        import lib.expand as ex_mod
        importlib.reload(ex_mod)
        self.assertIn("alternative phrasings", ex_mod._PROMPTS["generic"].lower())

    def test_scholarly_tilt_prompt_exists(self):
        import importlib
        import lib.expand as ex_mod
        importlib.reload(ex_mod)
        self.assertIn("scholarly-tilt", ex_mod._PROMPTS)
        prompt = ex_mod._PROMPTS["scholarly-tilt"]
        # Must mention scholarly signals (site:edu, methodology term, etc.)
        self.assertTrue(
            any(kw in prompt.lower() for kw in ["site:edu", "meta-analysis", "review article"]),
            "scholarly-tilt prompt must include academic search signals",
        )

    def test_anti_seo_prompt_exists(self):
        import importlib
        import lib.expand as ex_mod
        importlib.reload(ex_mod)
        self.assertIn("anti-seo", ex_mod._PROMPTS)
        prompt = ex_mod._PROMPTS["anti-seo"]
        self.assertTrue(
            any(kw in prompt.lower() for kw in ["evidence", "research", "avoid", "prohibit", "no"]),
            "anti-seo prompt must include directives against marketing phrasing",
        )

    def test_expand_prompt_name_env_selects_prompt(self):
        captured = {}

        def capture_chat(model, messages, **kw):
            captured["content"] = messages[0]["content"]
            return "a\nb\nc\nd"

        import importlib
        with patch.dict(os.environ, {"EXPAND_PROMPT_NAME": "scholarly-tilt", "EXPAND_PROMPT": ""}, clear=False):
            importlib.reload(lib.expand)
            with patch("lib.omlx.chat", side_effect=capture_chat):
                lib.expand.expand("test query", n=4)

        # Restore default
        with patch.dict(os.environ, {"EXPAND_PROMPT_NAME": "", "EXPAND_PROMPT": ""}, clear=False):
            importlib.reload(lib.expand)

        self.assertIn("content", captured)
        self.assertTrue(
            any(kw in captured["content"].lower() for kw in ["site:edu", "meta-analysis", "review article"]),
        )

    def test_expand_prompt_raw_env_override(self):
        """EXPAND_PROMPT env var provides a raw template, takes precedence over name."""
        captured = {}

        def capture_chat(model, messages, **kw):
            captured["content"] = messages[0]["content"]
            return "a\nb"

        import importlib
        custom_prompt = "CUSTOM TEMPLATE: {n} phrasings for: {query}"
        with patch.dict(os.environ, {"EXPAND_PROMPT": custom_prompt, "EXPAND_PROMPT_NAME": ""}, clear=False):
            importlib.reload(lib.expand)
            with patch("lib.omlx.chat", side_effect=capture_chat):
                lib.expand.expand("my query", n=3)

        with patch.dict(os.environ, {"EXPAND_PROMPT": "", "EXPAND_PROMPT_NAME": ""}, clear=False):
            importlib.reload(lib.expand)

        self.assertIn("CUSTOM TEMPLATE", captured.get("content", ""))
        self.assertIn("my query", captured.get("content", ""))

    def test_original_query_always_first(self):
        """Prompt variant does not affect original-query-first invariant."""
        import importlib
        with patch.dict(os.environ, {"EXPAND_PROMPT_NAME": "scholarly-tilt", "EXPAND_PROMPT": ""}, clear=False):
            importlib.reload(lib.expand)
            with patch("lib.omlx.chat", return_value="exp1\nexp2\nexp3\nexp4"):
                result = lib.expand.expand("original", n=4)

        with patch.dict(os.environ, {"EXPAND_PROMPT_NAME": "", "EXPAND_PROMPT": ""}, clear=False):
            importlib.reload(lib.expand)

        self.assertEqual(result[0], "original")


# ---------------------------------------------------------------------------
# lib/search.py — categories, engines, pagination
# ---------------------------------------------------------------------------

class TestSearchOptions(unittest.TestCase):
    def _mock_response(self, results):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": results}
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    def _make_result(self, url):
        return {"url": url, "title": url, "content": "c", "engine": "g", "score": 1.0}

    def test_categories_passed_to_searxng(self):
        captured = {}

        def mock_get(url, params=None, timeout=None):
            captured["params"] = params
            return self._mock_response([self._make_result("https://a.com")])

        with patch("lib.search.requests.get", side_effect=mock_get):
            lib.search.search("query", n=5, categories="science")

        self.assertEqual(captured["params"].get("categories"), "science")

    def test_engines_passed_to_searxng(self):
        captured = {}

        def mock_get(url, params=None, timeout=None):
            captured["params"] = params
            return self._mock_response([self._make_result("https://a.com")])

        with patch("lib.search.requests.get", side_effect=mock_get):
            lib.search.search("query", n=5, engines="arxiv,scholar")

        self.assertEqual(captured["params"].get("engines"), "arxiv,scholar")

    def test_no_categories_key_when_none(self):
        captured = {}

        def mock_get(url, params=None, timeout=None):
            captured["params"] = params
            return self._mock_response([self._make_result("https://a.com")])

        with patch("lib.search.requests.get", side_effect=mock_get):
            lib.search.search("query", n=5)

        self.assertNotIn("categories", captured["params"])

    def test_pagination_makes_multiple_requests(self):
        call_count = [0]

        def mock_get(url, params=None, timeout=None):
            call_count[0] += 1
            page = params.get("pageno", 1)
            return self._mock_response([self._make_result(f"https://page{page}.com")])

        with patch("lib.search.requests.get", side_effect=mock_get):
            lib.search.search("query", n=20, pages=3)

        self.assertEqual(call_count[0], 3)

    def test_pagination_dedupes_across_pages(self):
        """Same URL appearing on multiple pages should appear only once."""
        def mock_get(url, params=None, timeout=None):
            return self._mock_response([self._make_result("https://shared.com")])

        with patch("lib.search.requests.get", side_effect=mock_get):
            results = lib.search.search("query", n=20, pages=3)

        urls = [r["url"] for r in results]
        self.assertEqual(urls.count("https://shared.com"), 1)

    def test_pages_default_is_one_request(self):
        call_count = [0]

        def mock_get(url, params=None, timeout=None):
            call_count[0] += 1
            return self._mock_response([self._make_result("https://a.com")])

        with patch("lib.search.requests.get", side_effect=mock_get):
            lib.search.search("query", n=5)

        self.assertEqual(call_count[0], 1)

    def test_pageno_increments_per_page(self):
        pagenums = []

        def mock_get(url, params=None, timeout=None):
            pagenums.append(params.get("pageno"))
            return self._mock_response([self._make_result(f"https://{params['pageno']}.com")])

        with patch("lib.search.requests.get", side_effect=mock_get):
            lib.search.search("query", n=20, pages=3)

        self.assertEqual(pagenums, [1, 2, 3])


# ---------------------------------------------------------------------------
# lib/source_priors.py — boost/penalize logic
# ---------------------------------------------------------------------------

class TestSourcePriors(unittest.TestCase):
    def setUp(self):
        import importlib
        import lib.source_priors as sp
        importlib.reload(sp)
        self.sp = sp

    def _make_result(self, url, score=1.0):
        return {"url": url, "title": "T", "content": "C", "score": score}

    def test_boosted_domain_gets_positive_adj(self):
        result = self._make_result("https://arxiv.org/abs/1234")
        results = self.sp.apply_priors([result])
        self.assertGreater(results[0]["prior_adj"], 0.0, "arxiv.org should get a positive prior adjustment")

    def test_edu_domain_gets_positive_adj(self):
        result = self._make_result("https://stanford.edu/paper")
        results = self.sp.apply_priors([result])
        self.assertGreater(results[0]["prior_adj"], 0.0, ".edu domain should get a positive prior adjustment")

    def test_gov_domain_gets_positive_adj(self):
        result = self._make_result("https://nih.gov/research/topic")
        results = self.sp.apply_priors([result])
        self.assertGreater(results[0]["prior_adj"], 0.0, ".gov domain should get positive prior adjustment")

    def test_penalized_listicle_url_gets_negative_adj(self):
        result = self._make_result("https://healthblog.com/best-10-knee-exercises")
        results = self.sp.apply_priors([result])
        self.assertLess(results[0]["prior_adj"], 0.0, "/best- URL pattern should get negative prior adjustment")

    def test_penalized_top_n_url_gets_negative_adj(self):
        result = self._make_result("https://fitnessblog.com/top-10-tips-for-cyclists")
        results = self.sp.apply_priors([result])
        self.assertLess(results[0]["prior_adj"], 0.0, "/top-10- URL pattern should get negative prior adjustment")

    def test_neutral_domain_gets_zero_adj(self):
        result = self._make_result("https://randomsite.com/article-about-topic")
        results = self.sp.apply_priors([result])
        self.assertEqual(results[0]["prior_adj"], 0.0, "neutral domain should get zero prior adjustment")

    def test_prior_adj_field_added(self):
        result = self._make_result("https://example.com/page")
        results = self.sp.apply_priors([result])
        self.assertIn("prior_adj", results[0])

    def test_other_fields_preserved(self):
        result = self._make_result("https://arxiv.org/abs/1234", score=0.75)
        result["engine"] = "google"
        result["rerank_score"] = 0.9
        results = self.sp.apply_priors([result])
        self.assertEqual(results[0]["score"], 0.75)
        self.assertEqual(results[0]["engine"], "google")
        self.assertEqual(results[0]["rerank_score"], 0.9)

    def test_returns_new_list(self):
        """apply_priors should not modify input list in place (returns new list)."""
        results = [self._make_result("https://arxiv.org/abs/1234")]
        original_id = id(results[0])
        out = self.sp.apply_priors(results)
        # The output dicts should be copies (not the same object)
        self.assertIsNot(out[0], results[0])

    def test_empty_list(self):
        self.assertEqual(self.sp.apply_priors([]), [])


# ---------------------------------------------------------------------------
# lib/rerank.py — prior_adj incorporated into rerank_score
# ---------------------------------------------------------------------------

class TestRerankWithPriors(unittest.TestCase):
    QUERY_VEC = [1.0, 0.0, 0.0]
    HI_VEC = [1.0, 0.0, 0.0]   # cosine 1.0
    LO_VEC = [0.0, 1.0, 0.0]   # cosine 0.0

    def _embed(self, model, texts):
        return [[1.0, 0.0, 0.0]] + [[0.0, 1.0, 0.0]] * (len(texts) - 1)

    def test_prior_adj_shifts_rerank_score(self):
        import lib.rerank
        # Both results would have the same embedding cosine similarity
        results = [
            {"url": "https://a.com", "title": "t", "content": "c", "prior_adj": 0.5},
            {"url": "https://b.com", "title": "t", "content": "c", "prior_adj": -0.2},
        ]

        def embed_equal(model, texts):
            # All texts get the same vector → same cosine similarity
            return [[1.0, 0.0, 0.0]] * len(texts)

        with patch("lib.omlx.embed", side_effect=embed_equal):
            ranked = lib.rerank.rerank("query", results, top_k=2)

        # a.com has prior_adj=0.5, b.com has prior_adj=-0.2; a.com should rank first
        self.assertEqual(ranked[0]["url"], "https://a.com")
        self.assertGreater(ranked[0]["rerank_score"], ranked[1]["rerank_score"])

    def test_rerank_score_includes_prior_adj(self):
        import lib.rerank
        result = {"url": "https://arxiv.org/abs/1234", "title": "t", "content": "c", "prior_adj": 0.3}

        def embed_fixed(model, texts):
            return [[1.0, 0.0, 0.0]] * len(texts)

        with patch("lib.omlx.embed", side_effect=embed_fixed):
            ranked = lib.rerank.rerank("query", [result], top_k=1)

        # rerank_score should be cosine (1.0) + prior_adj (0.3) = 1.3
        self.assertAlmostEqual(ranked[0]["rerank_score"], 1.3, places=5)

    def test_rerank_score_zero_prior_adj_unchanged(self):
        import lib.rerank
        result = {"url": "https://example.com", "title": "t", "content": "c", "prior_adj": 0.0}

        def embed_fixed(model, texts):
            return [[1.0, 0.0, 0.0]] * len(texts)

        with patch("lib.omlx.embed", side_effect=embed_fixed):
            ranked = lib.rerank.rerank("query", [result], top_k=1)

        self.assertAlmostEqual(ranked[0]["rerank_score"], 1.0, places=5)

    def test_rerank_no_prior_adj_unchanged(self):
        """Results without prior_adj field are unaffected (default 0.0)."""
        import lib.rerank
        result = {"url": "https://example.com", "title": "t", "content": "c"}

        def embed_fixed(model, texts):
            return [[1.0, 0.0, 0.0]] * len(texts)

        with patch("lib.omlx.embed", side_effect=embed_fixed):
            ranked = lib.rerank.rerank("query", [result], top_k=1)

        self.assertAlmostEqual(ranked[0]["rerank_score"], 1.0, places=5)


# ---------------------------------------------------------------------------
# lib/pipeline.py — priors application + scholarly categories routing
# ---------------------------------------------------------------------------

class TestPipelineWithPriors(unittest.TestCase):
    def _fake_search(self, query, n=20, categories=None, engines=None, pages=1):
        return [
            {"url": f"https://arxiv.org/{query[:3].replace(' ', '_')}/{i}",
             "title": f"T{i}", "content": f"C{i}", "engine": "google", "score": 1.0}
            for i in range(3)
        ]

    def test_prior_adj_present_in_ranked_results(self):
        with patch("lib.expand.expand", return_value=["q"]), \
             patch("lib.search.search", side_effect=self._fake_search), \
             patch("lib.omlx.embed", return_value=[[1.0, 0.0, 0.0]] * 10):
            result = lib.pipeline.gather_sources("test query")

        for r in result["ranked"]:
            self.assertIn("prior_adj", r, "ranked results must have prior_adj from source priors")

    def test_expand_routing_routes_per_position(self):
        captured: list[dict] = []

        def capture_search(query, n=20, categories=None, engines=None, pages=1):
            captured.append({"categories": categories, "engines": engines})
            return [{"url": f"https://r{len(captured)}.com", "title": "T", "content": "C",
                     "engine": "g", "score": 1.0}
                    for _ in range(2)]

        routing = '[{"categories": "science", "engines": null}, {"categories": null, "engines": "pubmed"}]'
        with patch.dict(os.environ, {"EXPAND_ROUTING": routing}, clear=False), \
             patch("lib.expand.expand", return_value=["seed", "exp1", "exp2"]), \
             patch("lib.search.search", side_effect=capture_search), \
             patch("lib.omlx.embed", return_value=[[1.0, 0.0, 0.0]] * 20):
            lib.pipeline.gather_sources("test query", include_seed=False)

        # include_seed=False: exp1 → pos-0 routing, exp2 → pos-1 routing
        self.assertEqual(captured[0]["categories"], "science", "pos-0 must use categories=science per routing")
        self.assertIsNone(captured[0]["engines"], "pos-0 engines must be None per routing")
        self.assertIsNone(captured[1]["categories"], "pos-1 categories must be None per routing")
        self.assertEqual(captured[1]["engines"], "pubmed", "pos-1 must use engines=pubmed per routing")

    def test_default_routing_no_categories(self):
        captured_categories = []
        captured_engines = []

        def capture_search(query, n=20, categories=None, engines=None, pages=1):
            captured_categories.append(categories)
            captured_engines.append(engines)
            return [{"url": f"https://r{len(captured_categories)}.com", "title": "T",
                     "content": "C", "engine": "g", "score": 1.0}
                    for _ in range(2)]

        with patch.dict(os.environ, {"EXPAND_ROUTING": ""}, clear=False), \
             patch("lib.expand.expand", return_value=["seed", "exp1"]), \
             patch("lib.search.search", side_effect=capture_search), \
             patch("lib.omlx.embed", return_value=[[1.0, 0.0, 0.0]] * 10):
            lib.pipeline.gather_sources("test query", include_seed=False)

        self.assertTrue(all(c is None for c in captured_categories), "all categories must be None without EXPAND_ROUTING")
        self.assertTrue(all(e is None for e in captured_engines), "all engines must be None without EXPAND_ROUTING")


if __name__ == "__main__":
    unittest.main()
