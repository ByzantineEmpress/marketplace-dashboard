"""Book data for eBay listings.

Turns an ISBN into the fields an eBay book listing needs, builds the description,
and gathers what the market looks like.

Two things are deliberately NOT done here, because eBay will not let this
application do them:

* **Sold prices.** The Marketplace Insights API answers 403 "insufficient
  permissions" for this app, and the old Finding API's findCompletedItems has been
  decommissioned (eBay answers 418). Rather than invent a number, the sold history
  is handed to the browser as a search URL the seller can open while signed in,
  where their own account can see it.
* **Selling frequency.** Same source, same problem. It cannot be derived from
  asking prices without pretending.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

OPEN_LIBRARY = "https://openlibrary.org"
GOOGLE_BOOKS = "https://www.googleapis.com/books/v1/volumes"


class BooksError(Exception):
    """Raised for input that cannot be turned into a listing."""


# -- ISBN ------------------------------------------------------------------

def _isbn10_check_digit(first_nine: str) -> str:
    total = sum((10 - i) * int(d) for i, d in enumerate(first_nine))
    remainder = (11 - (total % 11)) % 11
    return "X" if remainder == 10 else str(remainder)


def _isbn13_check_digit(first_twelve: str) -> str:
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(first_twelve))
    return str((10 - (total % 10)) % 10)


def isbn10_to_13(isbn10: str) -> str:
    core = "978" + isbn10[:9]
    return core + _isbn13_check_digit(core)


def isbn13_to_10(isbn13: str) -> Optional[str]:
    """Only 978-prefixed ISBN-13s have an ISBN-10; 979 does not."""
    if not isbn13.startswith("978"):
        return None
    core = isbn13[3:12]
    return core + _isbn10_check_digit(core)


def normalise_isbn(raw: str) -> Dict[str, Any]:
    """Clean up whatever came off a barcode or a spine, and validate it.

    A scanner returns digits with no punctuation; a printed ISBN on an old book
    has hyphens and possibly an X. Both end up here, and the check digit is
    verified rather than trusted: a misread digit that happens to be the right
    length would otherwise send the whole lookup to the wrong book.
    """
    if not raw:
        raise BooksError("No ISBN was given.")

    text = str(raw).strip().upper()
    # Keep only what an ISBN can contain, so a stray prefix like "ISBN" or a
    # scanned "9 780241 108259" spacing does not defeat the parse.
    cleaned = re.sub(r"[^0-9X]", "", text)

    if len(cleaned) == 13:
        if _isbn13_check_digit(cleaned[:12]) != cleaned[12]:
            raise BooksError(f"ISBN {cleaned} failed its check digit.")
        isbn13 = cleaned
        isbn10 = isbn13_to_10(cleaned)
    elif len(cleaned) == 10:
        if _isbn10_check_digit(cleaned[:9]) != cleaned[9]:
            raise BooksError(f"ISBN {cleaned} failed its check digit.")
        isbn10 = cleaned
        isbn13 = isbn10_to_13(cleaned)
    else:
        raise BooksError(
            f"{cleaned or raw!r} is not a 10 or 13 digit ISBN "
            f"({len(cleaned)} digits found)."
        )

    return {"isbn13": isbn13, "isbn10": isbn10, "digits": isbn13}


# -- Metadata --------------------------------------------------------------

def _year_from(value: Any) -> str:
    """The first four-digit year in a free-text date field."""
    if not value:
        return ""
    match = re.search(r"(1[5-9]\d\d|20\d\d)", str(value))
    return match.group(1) if match else ""


def _open_library(isbn: str) -> Dict[str, Any]:
    import httpx

    out: Dict[str, Any] = {}
    try:
        resp = httpx.get(f"{OPEN_LIBRARY}/isbn/{isbn}.json", timeout=30,
                         follow_redirects=True)
        if resp.status_code != 200:
            return out
        data = resp.json() or {}

        out["title"] = data.get("title") or ""
        subtitle = data.get("subtitle")
        if subtitle:
            out["title"] = f"{out['title']}: {subtitle}"

        authors = []
        for entry in data.get("authors") or []:
            key = (entry or {}).get("key")
            if not key:
                continue
            try:
                a = httpx.get(f"{OPEN_LIBRARY}{key}.json", timeout=20)
                if a.status_code == 200:
                    name = (a.json() or {}).get("name")
                    if name:
                        authors.append(name)
            except Exception:
                continue
        out["author"] = ", ".join(authors)

        publishers = data.get("publishers") or []
        out["publisher"] = publishers[0] if publishers else ""
        out["publication_year"] = _year_from(data.get("publish_date"))

        # Open Library's subjects are the closest thing it has to genre and topic.
        subjects = [s for s in (data.get("subjects") or []) if isinstance(s, str)]
        out["subjects"] = subjects[:12]
        out["pages"] = data.get("number_of_pages")
    except Exception:
        return {}
    return out


def _google_books(isbn: str) -> Dict[str, Any]:
    import httpx

    out: Dict[str, Any] = {}
    try:
        resp = httpx.get(GOOGLE_BOOKS, params={"q": f"isbn:{isbn}"}, timeout=30)
        if resp.status_code != 200:
            return out
        items = (resp.json() or {}).get("items") or []
        if not items:
            return out
        info = (items[0] or {}).get("volumeInfo") or {}

        out["title"] = info.get("title") or ""
        subtitle = info.get("subtitle")
        if subtitle:
            out["title"] = f"{out['title']}: {subtitle}"
        out["author"] = ", ".join(info.get("authors") or [])
        out["publisher"] = info.get("publisher") or ""
        out["publication_year"] = _year_from(info.get("publishedDate"))
        out["description"] = info.get("description") or ""
        out["pages"] = info.get("pageCount")
        out["categories"] = [c for c in (info.get("categories") or []) if c]
        out["language"] = info.get("language") or ""
    except Exception:
        return {}
    return out


# Format wording for eBay's "Format" field, driven by what the metadata says.
_HARDCOVER_HINTS = ("hardcover", "hardback", "hard board", "cloth")
_PAPERBACK_HINTS = ("paperback", "softcover", "soft cover", "mass market", "trade paper")


def infer_format(*hints: str) -> str:
    """Guess the binding, and say so plainly when the metadata does not."""
    blob = " ".join(h for h in hints if h).lower()
    if any(word in blob for word in _HARDCOVER_HINTS):
        return "Hardcover"
    if any(word in blob for word in _PAPERBACK_HINTS):
        return "Paperback"
    return ""


def lookup_book(isbn: str) -> Dict[str, Any]:
    """Everything an eBay book listing needs, from two free catalogues.

    Google Books fills what Open Library leaves blank and the other way round:
    neither is complete, and between them they cover almost everything with an
    ISBN. A book found in neither is reported as such rather than half-filled.
    """
    normalised = normalise_isbn(isbn)
    key = normalised["isbn13"]

    ol = _open_library(key)
    gb = _google_books(key)

    # Open Library's author strings are the more reliable of the two when present.
    author = ol.get("author") or gb.get("author") or ""
    title = gb.get("title") or ol.get("title") or ""
    publisher = ol.get("publisher") or gb.get("publisher") or ""
    year = ol.get("publication_year") or gb.get("publication_year") or ""

    subjects = list(ol.get("subjects") or [])
    categories = list(gb.get("categories") or [])
    genre = categories[0] if categories else (subjects[0] if subjects else "")
    topic = ""
    pool = categories[1:] + subjects[1:]
    if pool:
        topic = " / ".join(pool[:2])

    found = bool(title or author)

    return {
        **normalised,
        "found": found,
        "author": author,
        "title": title,
        "publisher": publisher,
        "publication_year": year,
        "format": infer_format(
            " ".join(categories), " ".join(subjects), gb.get("description") or ""),
        "genre": genre,
        "topic": topic,
        "pages": ol.get("pages") or gb.get("pages"),
        "summary": gb.get("description") or "",
        "subjects": subjects,
        "sources": [name for name, data in
                    (("openlibrary", ol), ("googlebooks", gb)) if data],
    }


# -- Description -----------------------------------------------------------

CONDITION_DEFAULT = (
    "Please review all included listing images for full physical condition "
    "details."
)

_FIELD_ORDER = (
    ("author", "Author"),
    ("title", "Book Title"),
    ("publisher", "Publisher"),
    ("publication_year", "Publication Year"),
    ("format", "Format"),
    ("genre", "Genre"),
    ("topic", "Topic"),
)


def build_description(meta: Dict[str, Any], condition: str = "",
                      opening: str = "") -> str:
    """The listing description, in the layout the seller already uses.

    A block of stated facts, then the prose, then the condition note. The facts
    are repeated at the end on purpose: that is how the seller's own examples read,
    and eBay surfaces the tail of a description in some views.
    """
    lines: List[str] = []
    for key, label in _FIELD_ORDER:
        value = (meta.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")

    isbn13 = (meta.get("isbn13") or "").strip()
    isbn10 = (meta.get("isbn10") or "").strip()
    if isbn13 or isbn10:
        if isbn10 and isbn13:
            lines.append(f"ISBN: {isbn13} ({isbn10})")
        else:
            lines.append(f"ISBN: {isbn13 or isbn10}")

    facts = "\n".join(lines)

    title = (meta.get("title") or "").strip()
    author = (meta.get("author") or "").strip()
    publisher = (meta.get("publisher") or "").strip()
    year = (meta.get("publication_year") or "").strip()
    fmt = (meta.get("format") or "").strip()

    if opening:
        prose = opening.strip()
    else:
        described = " ".join(x for x in (fmt.lower(), "edition") if x)
        bits = [f'Up for sale is a copy of "{title}"' if title else "Up for sale is this book"]
        if author:
            bits.append(f"by {author}")
        tail = []
        if publisher:
            tail.append(f"published by {publisher}")
        if year:
            tail.append(f"in {year}")
        if tail:
            bits.append(", ".join(tail))
        prose = " ".join(bits) + "."
        if described.strip() and fmt:
            prose = prose.replace("Up for sale is", f"Up for sale is a {described}", 1)

    condition_note = (condition or "").strip() or CONDITION_DEFAULT

    parts = [facts, "Item Description", prose, f"Condition: {condition_note}"]
    if isbn13 or isbn10:
        parts.append(f"ISBN: {isbn13 or isbn10}")

    return "\n\n".join(p for p in parts if p).strip()


# -- Market ----------------------------------------------------------------

# Deliberately NOT filtered by category.
#
# An ISBN query is already specific to the edition, so a category filter buys
# nothing. It also cannot be written correctly here: eBay's category ids come from
# a per-marketplace tree, so the US Books id means something else, or nothing, on
# ebay.ca. Measured on both a common paperback and an obscure hardcover, adding it
# changed no result -- which is the point: it carries a marketplace-specific
# assumption for no benefit, and would fail silently the day it did matter.
_BOOKS_CATEGORY_HINT = "267"


def _marketplace_host(marketplace: str) -> str:
    return {
        "EBAY_CA": "www.ebay.ca",
        "EBAY_GB": "www.ebay.co.uk",
        "EBAY_AU": "www.ebay.com.au",
        "EBAY_DE": "www.ebay.de",
    }.get(marketplace, "www.ebay.com")


def search_urls(meta: Dict[str, Any], marketplace: str = "EBAY_CA") -> Dict[str, str]:
    """eBay search links the seller can open while signed in.

    Sold history is only available to the seller's own account, so it is handed
    over as a link rather than faked. Both links carry the same query so the two
    views are directly comparable.
    """
    host = _marketplace_host(marketplace)
    isbn = (meta.get("isbn13") or meta.get("isbn10") or "").strip()

    # Prefer the ISBN: it identifies the exact edition, where a title and author
    # would return every printing of the book.
    if isbn:
        query = isbn
    else:
        query = " ".join(
            x for x in ((meta.get("title") or "").strip(),
                        (meta.get("author") or "").strip()) if x)

    from urllib.parse import quote_plus

    base = f"https://{host}/sch/i.html?_nkw={quote_plus(query)}"
    return {
        "query": query,
        "active": f"{base}&_ipg=60",
        # LH_Sold + LH_Complete is eBay's own "sold listings" filter.
        "sold": f"{base}&LH_Sold=1&LH_Complete=1&_ipg=60",
    }


def active_market(isbn: str, access_token: str,
                  marketplace: str = "EBAY_CA") -> Dict[str, Any]:
    """How many copies are listed right now, and what they are asking.

    Asking prices, not sold prices. eBay will not give this application sold data
    (Marketplace Insights answers 403; the Finding API is retired), so this is
    reported as what it is. An average of what sellers want is still useful for
    pricing a book, and calling it a sold average would be a lie.
    """
    import httpx

    out: Dict[str, Any] = {
        "available": 0, "average_asking": None, "lowest_asking": None,
        "highest_asking": None, "currency": "CAD", "sampled": 0, "error": None,
    }

    try:
        normalised = normalise_isbn(isbn)
        query = normalised["isbn13"]
    except BooksError as exc:
        out["error"] = str(exc)
        return out

    try:
        resp = httpx.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={"Authorization": f"Bearer {access_token}",
                     "X-EBAY-C-MARKETPLACE-ID": marketplace},
            params={"q": query, "limit": 50},
            timeout=40,
        )
        if resp.status_code != 200:
            out["error"] = f"eBay search HTTP {resp.status_code}: {(resp.text or '')[:160]}"
            return out

        data = resp.json() or {}
        out["available"] = int(data.get("total") or 0)

        prices = []
        currency = ""
        for item in data.get("itemSummaries") or []:
            price = (item or {}).get("price") or {}
            try:
                value = float(price.get("value"))
            except (TypeError, ValueError):
                continue
            prices.append(value)
            currency = currency or (price.get("currency") or "")

        if prices:
            out["sampled"] = len(prices)
            out["average_asking"] = round(sum(prices) / len(prices), 2)
            out["lowest_asking"] = round(min(prices), 2)
            out["highest_asking"] = round(max(prices), 2)
            out["currency"] = currency or out["currency"]
    except Exception as exc:
        out["error"] = str(exc)

    return out
