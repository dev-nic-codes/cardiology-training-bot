from __future__ import annotations

import unittest

from api.article_sources import (
    clean_summary,
    host_matches_domain,
    infer_pdf_url_from_article_url,
    normalize_article_url,
    reconstruct_openalex_abstract,
)


class ArticleSourceTests(unittest.TestCase):
    def test_normalize_article_url_removes_query_and_fragment(self) -> None:
        self.assertEqual(
            normalize_article_url("https://example.org/article?id=7#results"),
            "https://example.org/article",
        )

    def test_clean_summary_strips_markup_and_bounds_length(self) -> None:
        self.assertEqual(clean_summary("<p>Short&nbsp;summary</p>"), "Short summary")
        self.assertEqual(clean_summary("abcdefghij", limit=8), "abcde...")

    def test_reconstruct_openalex_abstract_orders_tokens(self) -> None:
        abstract = reconstruct_openalex_abstract({"Heart": [0], "rate": [1], ".": [2]})
        self.assertEqual(abstract, "Heart rate.")

    def test_host_matching_accepts_subdomains_and_rejects_lookalikes(self) -> None:
        self.assertTrue(host_matches_domain("www.nature.com", "nature.com"))
        self.assertFalse(host_matches_domain("evilnature.com", "nature.com"))
        self.assertFalse(host_matches_domain("nature.com.attacker.example", "nature.com"))

    def test_pdf_inference_rejects_lookalike_domains(self) -> None:
        self.assertIsNone(
            infer_pdf_url_from_article_url("https://nature.com.attacker.example/articles/example")
        )
        self.assertEqual(
            infer_pdf_url_from_article_url("https://www.nature.com/articles/example"),
            "https://www.nature.com/articles/example.pdf",
        )


if __name__ == "__main__":
    unittest.main()
