"""Book data for eBay listings.

The ISBN maths is the part that must be exactly right: a check digit that passes
when it should not sends the whole lookup to the wrong book, which is what
happened while this was being built (see the discrepancy test at the end).
"""

import unittest

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from src import books


class IsbnTest(unittest.TestCase):
    def test_a_hyphenated_isbn10_from_a_spine(self):
        """The example book this feature was built against: a 1982 hardcover whose
        ISBN is printed without a barcode."""
        got = books.normalise_isbn("0-241-10825-X")
        self.assertEqual(got["isbn10"], "024110825X")
        self.assertEqual(got["isbn13"], "9780241108253")

    def test_a_bare_scanner_string(self):
        self.assertEqual(books.normalise_isbn("024110825X")["isbn13"],
                         "9780241108253")

    def test_a_13_digit_barcode(self):
        self.assertEqual(books.normalise_isbn("9780241108253")["isbn10"],
                         "024110825X")

    def test_extra_words_and_spacing_are_tolerated(self):
        for form in ("ISBN 978-0-241-10825-3", "978 0241 108253", " 9780241108253 "):
            self.assertEqual(books.normalise_isbn(form)["isbn13"], "9780241108253",
                             form)

    def test_a_wrong_check_digit_is_refused(self):
        """The whole point of validating: a misread digit of the right length
        would otherwise be looked up as a different book."""
        with self.assertRaises(books.BooksError):
            books.normalise_isbn("9780241108259")
        with self.assertRaises(books.BooksError):
            books.normalise_isbn("024110825Y")

    def test_the_wrong_length_is_refused(self):
        for bad in ("", "12345", "97802411082531"):
            with self.assertRaises(books.BooksError):
                books.normalise_isbn(bad)

    def test_a_979_isbn_has_no_isbn10(self):
        """979-prefixed ISBN-13s were assigned after ISBN-10 was retired."""
        self.assertIsNone(books.isbn13_to_10("9791234567896"))

    def test_round_trip(self):
        for isbn10 in ("024110825X", "0140328726", "043942089X"):
            thirteen = books.isbn10_to_13(isbn10)
            self.assertEqual(books.isbn13_to_10(thirteen), isbn10)


class DescriptionTest(unittest.TestCase):
    META = {
        "author": "Alan Scholefield", "title": "The Stone Flower",
        "publisher": "Hamish Hamilton", "publication_year": "1982",
        "format": "Hardcover with Dust Jacket",
        "genre": "Historical Fiction / Adventure",
        "topic": "Historical Drama / Epic Novel",
        "isbn13": "9780241108253", "isbn10": "024110825X",
    }

    def test_the_facts_block_matches_the_sellers_own_layout(self):
        text = books.build_description(self.META, condition="Good.")
        for line in ("Author: Alan Scholefield", "Book Title: The Stone Flower",
                     "Publisher: Hamish Hamilton", "Publication Year: 1982",
                     "Format: Hardcover with Dust Jacket",
                     "Genre: Historical Fiction / Adventure",
                     "Topic: Historical Drama / Epic Novel"):
            self.assertIn(line, text)

    def test_the_isbn_shows_both_forms(self):
        text = books.build_description(self.META, condition="Good.")
        self.assertIn("ISBN: 9780241108253 (024110825X)", text)

    def test_the_sections_are_in_order(self):
        text = books.build_description(self.META, condition="Good.")
        self.assertLess(text.index("Author:"), text.index("Item Description"))
        self.assertLess(text.index("Item Description"), text.index("Condition:"))

    def test_the_isbn_is_repeated_at_the_end(self):
        """That is how the seller's own examples read, and eBay surfaces the tail
        of a description in some views."""
        text = books.build_description(self.META, condition="Good.")
        self.assertTrue(text.rstrip().endswith("ISBN: 9780241108253"))

    def test_missing_fields_are_omitted_rather_than_left_blank(self):
        text = books.build_description(
            {"title": "A Book", "isbn13": "9780241108253"}, condition="Good.")
        self.assertIn("Book Title: A Book", text)
        self.assertNotIn("Author:", text)
        self.assertNotIn("Publisher:", text)

    def test_a_default_condition_line_is_used_when_none_is_given(self):
        text = books.build_description(self.META)
        self.assertIn("Condition:", text)
        self.assertIn("review all included listing images", text)


class FormatTest(unittest.TestCase):
    def test_binding_is_inferred_from_the_catalogue_wording(self):
        self.assertEqual(books.infer_format("Hardcover"), "Hardcover")
        self.assertEqual(books.infer_format("x", "Trade Paperback"), "Paperback")

    def test_an_unknown_binding_is_left_blank_rather_than_guessed(self):
        """A wrong Format on a book listing is a returned item."""
        self.assertEqual(books.infer_format("", None, "a nice old book"), "")
        self.assertEqual(books.infer_format(), "")


class SearchUrlTest(unittest.TestCase):
    META = {"isbn13": "9780241108253", "isbn10": "024110825X",
            "title": "The Stone Flower", "author": "Alan Scholefield"}

    def test_the_sold_link_carries_ebays_own_filters(self):
        urls = books.search_urls(self.META, "EBAY_CA")
        self.assertIn("LH_Sold=1", urls["sold"])
        self.assertIn("LH_Complete=1", urls["sold"])
        self.assertNotIn("LH_Sold", urls["active"])

    def test_it_uses_the_sellers_own_marketplace(self):
        self.assertIn("ebay.ca", books.search_urls(self.META, "EBAY_CA")["sold"])
        self.assertIn("ebay.co.uk", books.search_urls(self.META, "EBAY_GB")["sold"])

    def test_it_searches_the_isbn_rather_than_the_title(self):
        """An ISBN identifies the edition; a title returns every printing."""
        urls = books.search_urls(self.META, "EBAY_CA")
        self.assertEqual(urls["query"], "9780241108253")

    def test_it_falls_back_to_title_and_author(self):
        urls = books.search_urls({"title": "The Stone Flower",
                                  "author": "Alan Scholefield"}, "EBAY_CA")
        self.assertIn("Stone+Flower", urls["active"])


class MetadataDiscrepancyTest(unittest.TestCase):
    """Catalogue data can be wrong, so nothing here may be trusted blindly.

    While building this, the ISBN from the seller's own worked example
    (0-241-10825-X) resolved at Open Library to a different book entirely: "Timmy's
    dog" by Rory O'Brine, same publisher and year. The publisher and year matching
    while the author and title do not is the shape of a bad catalogue record, not
    of a bug in the lookup -- but either way it is why every field this returns has
    to be shown to the seller for editing rather than written straight to eBay.
    """

    def test_a_partial_record_is_reported_as_partial(self):
        import src.books as b

        original = b._open_library
        b._open_library = lambda isbn: {"publisher": "Hamish Hamilton",
                                        "publication_year": "1982"}
        b._google_books = lambda isbn: {}
        try:
            meta = b.lookup_book("0-241-10825-X")
        finally:
            b._open_library = original

        self.assertFalse(
            meta["found"],
            "publisher and year alone cannot identify a book, so this is not a hit")
        # The useful part still comes through, so the field can be pre-filled.
        self.assertEqual(meta["publisher"], "Hamish Hamilton")
        self.assertEqual(meta["publication_year"], "1982")
        self.assertEqual(meta["author"], "")
        # And the description builds without inventing an author.
        text = b.build_description(meta, condition="Good.")
        self.assertNotIn("Author:", text)

    def test_a_book_in_neither_catalogue_is_reported_as_not_found(self):
        import src.books as b

        original_ol, original_gb = b._open_library, b._google_books
        b._open_library = lambda isbn: {}
        b._google_books = lambda isbn: {}
        try:
            meta = b.lookup_book("0-241-10825-X")
        finally:
            b._open_library, b._google_books = original_ol, original_gb

        self.assertFalse(meta["found"])
        self.assertEqual(meta["title"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
