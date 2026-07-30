from __future__ import annotations

import unittest

from api.article_sources import clean_summary, normalize_article_url, reconstruct_openalex_abstract


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


if __name__ == "__main__":
    unittest.main()
