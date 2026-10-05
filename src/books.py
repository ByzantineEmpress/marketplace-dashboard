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

        # The EDITION carries the binding when anyone has recorded it. It often
        # has no subjects at all, which is why they are read from the work below.
        out["physical_format"] = data.get("physical_format") or ""
        subjects = [s for s in (data.get("subjects") or []) if isinstance(s, str)]
        out["subjects"] = subjects[:12]
        out["pages"] = data.get("number_of_pages")

        # The WORK is where the subjects live. Reading only the edition left Genre
        # and Topic empty on every book that had them recorded against the work
        # rather than the printing -- which is most of them.
        works = data.get("works") or []
        if works:
            wkey = (works[0] or {}).get("key")
            if wkey:
                try:
                    w = httpx.get(f"{OPEN_LIBRARY}{wkey}.json", timeout=25)
                    if w.status_code == 200:
                        work = w.json() or {}
                        out["work_subjects"] = [
                            s for s in (work.get("subjects") or [])
                            if isinstance(s, str)
                        ]
                        out["subject_places"] = [
                            s for s in (work.get("subject_places") or [])
                            if isinstance(s, str)
                        ]
                        out["subject_times"] = [
                            s for s in (work.get("subject_times") or [])
                            if isinstance(s, str)
                        ]
                        if not out["title"]:
                            out["title"] = work.get("title") or ""
                except Exception:
                    pass
    except Exception:
        return {}
    return out


def _google_books(isbn: str, api_key: str = "") -> Dict[str, Any]:
    import os

    import httpx

    out: Dict[str, Any] = {}
    try:
        params = {"q": f"isbn:{isbn}"}
        # The caller's own key, saved on the settings page, wins over the
        # environment: without any key at all these requests share an anonymous
        # pool that is routinely already spent, and Google answers 429 -- which
        # looks like "this book has no categories" rather than "we were refused".
        key = (api_key or "").strip() or os.environ.get("GOOGLE_BOOKS_API_KEY") or ""
        if key:
            params["key"] = key

        resp = httpx.get(GOOGLE_BOOKS, params=params, timeout=30)
        if resp.status_code == 429:
            out["quota_exceeded"] = True
            return out
        if resp.status_code in (400, 403):
            # A malformed key, a key for the wrong API, or the Books API not
            # enabled on the project. Distinguished from a quota refusal because
            # the fix is different and the seller can act on it.
            out["key_rejected"] = f"HTTP {resp.status_code}"
            return out
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


# Subjects that are library-metadata artefacts rather than anything a buyer would
# recognise as a genre or a topic.
_SUBJECT_NOISE = {
    "open library staff picks", "accessible book", "protected daisy",
    "in library", "internet archive wishlist", "large type books", "overdrive",
    "electronic books", "new york times reviewed", "staff picks", "award winner",
    "series", "miscellanea", "general", "criticism and interpretation",
}

# Words that mark a subject as a GENRE rather than a topic, so the two fields can
# be told apart. Without this the split was arbitrary, and "Foxes" would land in
# Genre while "Juvenile fiction" landed in Topic.
_GENRE_HINTS = (
    "fiction", "novel", "stories", "story", "mystery", "romance", "fantasy",
    "science fiction", "history", "historical", "biography", "autobiography",
    "thriller", "horror", "adventure", "poetry", "drama", "juvenile",
    "children", "young adult", "comic", "graphic novel", "essays", "travel",
    "crime", "detective", "western", "humor", "humour", "religion", "philosophy",
)


def _split_subjects(pool: List[str]) -> Dict[str, str]:
    """Sort a bag of subjects into a Genre and a Topic.

    Open Library's work records carry a dozen or more subjects of mixed kinds --
    "Juvenile fiction" next to "Foxes" next to "Open Library Staff Picks". The
    order they arrive in is roughly relevance, so the first usable ones are taken
    rather than trying to score them.
    """
    genre: List[str] = []
    topic: List[str] = []
    seen = set()

    for raw in pool:
        subject = (raw or "").strip()
        if not subject:
            continue
        key = subject.lower()
        if key in seen or key in _SUBJECT_NOISE:
            continue
        seen.add(key)

        is_genre = any(hint in key for hint in _GENRE_HINTS)
        target = genre if is_genre else topic
        if len(target) < 2:
            target.append(subject)

    return {"genre": " / ".join(genre), "topic": " / ".join(topic)}


def lookup_book(isbn: str, google_api_key: str = "") -> Dict[str, Any]:
    """Everything an eBay book listing needs, from two free catalogues.

    Google Books fills what Open Library leaves blank and the other way round:
    neither is complete. Google Books answers 429 once its quota is gone, and that
    is reported rather than silently treated as "this book has no categories" --
    which is exactly how Genre and Topic came out empty.

    google_api_key is the caller's own key from the settings page, if they saved
    one. It is optional: the lookup works on Open Library alone.
    """
    normalised = normalise_isbn(isbn)
    key = normalised["isbn13"]

    ol = _open_library(key)
    gb = _google_books(key, google_api_key)

    # Open Library's author strings are the more reliable of the two when present.
    author = ol.get("author") or gb.get("author") or ""
    title = gb.get("title") or ol.get("title") or ""
    publisher = ol.get("publisher") or gb.get("publisher") or ""
    year = ol.get("publication_year") or gb.get("publication_year") or ""

    # Subjects come from the WORK, categories from Google Books. The edition's own
    # subjects are usually empty, which is why only reading the edition left these
    # two fields blank on nearly every book.
    pool = (list(gb.get("categories") or [])
            + list(ol.get("work_subjects") or [])
            + list(ol.get("subjects") or []))
    split = _split_subjects(pool)

    # Format: the edition's recorded binding if anyone wrote it down, otherwise
    # inferred from wording that mentions one. Left blank when neither says --
    # a wrong Format on a book is a returned item, and the seller can see the
    # binding in their hand in a way no catalogue can.
    fmt = (ol.get("physical_format") or "").strip()
    if not fmt:
        fmt = infer_format(title, " ".join(pool), gb.get("description") or "")

    found = bool(title or author)

    sources = []
    if ol:
        sources.append("openlibrary")
    # A refusal is not a source. Counting the quota error as one made the page look
    # as though Google Books had answered and simply had no categories; counting a
    # rejected key as one would hide a misconfigured key the same way.
    if gb and not gb.get("quota_exceeded") and not gb.get("key_rejected"):
        sources.append("googlebooks")

    return {
        **normalised,
        "found": found,
        "author": author,
        "title": title,
        "publisher": publisher,
        "publication_year": year,
        "format": fmt,
        "genre": split["genre"],
        "topic": split["topic"],
        "pages": ol.get("pages") or gb.get("pages"),
        "summary": gb.get("description") or "",
        "subjects": pool[:20],
        "sources": sources,
        # Surfaced so the page can say WHY a field is empty instead of leaving the
        # seller to guess that the lookup half-failed.
        "google_quota_exceeded": bool(gb.get("quota_exceeded")),
        "google_key_rejected": gb.get("key_rejected") or "",
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


# -- Reading an ISBN off a photo -------------------------------------------

# OCR reads the hyphen as a space, a 1 as an l and an 8 as a B often enough that
# accepting only clean digits would miss most old books. These are the swaps that
# actually turn up on an ISBN string.
_OCR_CONFUSIONS = str.maketrans({
    "O": "0", "o": "0", "Q": "0", "D": "0",
    # Both cases, and i. OCR output is routinely upper-cased, and a table holding
    # only the lower-case forms silently fails on it: an ISBN ending "...-l"
    # arrives as "...-L" and is left untranslated.
    "l": "1", "L": "1", "I": "1", "i": "1", "|": "1", "!": "1",
    "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2",
    "G": "6", "b": "6", "g": "9", "q": "9", "A": "4",
})

# The character classes include the letters OCR substitutes for digits, not just
# digits, because an ISBN very often ENDS on one: "978-O14-O32872-l" has a
# misread 1 at the end, and a class of [0-9X] stops the match one character short
# and yields a completely different, wrong-length candidate.
_ISBN_EDGE = "0-9XxOXolI|!"
_ISBN_MIDDLE = "0-9XxOXolI|!\\-\\s"
_ISBN_CANDIDATE = re.compile(
    rf"[{_ISBN_EDGE}][{_ISBN_MIDDLE}]{{8,20}}[{_ISBN_EDGE}]")


def _candidates_from_text(text: str) -> List[str]:
    """Every plausible ISBN in a block of OCR output, best first.

    Validation is left to normalise_isbn: the check digit is what decides whether
    a candidate is real, so this only has to be generous enough not to throw the
    right answer away before it gets there.
    """
    found: List[str] = []

    def add(value: str) -> None:
        # The confusions are applied BEFORE the non-digits are stripped. Doing it
        # the other way round throws away exactly the characters that need
        # translating: "978-O14-O32872-l" stripped first becomes "9781432872",
        # which is the right length and the wrong ISBN.
        upper = value.upper()
        for form in (upper, upper.translate(_OCR_CONFUSIONS)):
            cleaned = re.sub(r"[^0-9X]", "", form)
            if len(cleaned) in (10, 13) and cleaned not in found:
                found.append(cleaned)

    # A line mentioning ISBN is the strongest signal on the page, so those go
    # first. The letters are spaced out because OCR routinely splits them.
    for line in text.splitlines():
        if re.search(r"i\s*s\s*b\s*n", line, re.IGNORECASE):
            for match in _ISBN_CANDIDATE.findall(line):
                add(match)

    for match in _ISBN_CANDIDATE.findall(text):
        add(match)

    for run in re.findall(r"\d[\d\s\-]{8,}\d", text):
        add(run)

    return found


def isbn_from_image(data: bytes) -> Dict[str, Any]:
    """Read an ISBN from a photo, by barcode first and printed text second.

    The barcode is tried first because it is exact: a decoded EAN-13 either has a
    valid check digit or it does not, so there is nothing to interpret. The printed
    ISBN is the fallback for books old enough to predate barcodes, and it is OCR,
    so every candidate goes through the same check-digit validation and the first
    that passes wins. That is what stops a misread becoming a confidently wrong
    book -- which matters, because a wrong ISBN silently lists the wrong title.
    """
    result: Dict[str, Any] = {
        "isbn": None, "method": None, "candidates": [], "raw_text": "",
        "error": None,
    }
    if not data:
        result["error"] = "No image was uploaded."
        return result

    # 1. Barcode.
    try:
        import io as _io

        import zxingcpp
        from PIL import Image

        image = Image.open(_io.BytesIO(data))
        for barcode in zxingcpp.read_barcodes(image) or []:
            text = (getattr(barcode, "text", "") or "").strip()
            if not text:
                continue
            result["candidates"].append(text)
            try:
                normalised = normalise_isbn(text)
            except BooksError:
                continue
            result["isbn"] = normalised["isbn13"]
            result["method"] = "barcode"
            return result
    except ImportError as exc:
        result["error"] = f"Barcode reading is unavailable: {exc}"
    except Exception as exc:
        result["error"] = f"Could not read a barcode: {exc}"

    # 2. Printed ISBN, for books too old to have one.
    try:
        import io as _io

        import pytesseract
        from PIL import Image

        image = Image.open(_io.BytesIO(data)).convert("L")
        # Enlarged and greyscaled: tesseract does markedly better on a small ISBN
        # string that has been scaled up than on the raw photo.
        if min(image.size) < 1000:
            image = image.resize((image.width * 2, image.height * 2))
        text = pytesseract.image_to_string(image, config="--psm 6")
        result["raw_text"] = text[:2000]

        # _candidates_from_text already offers a confusion-corrected variant of
        # each match, so these only need validating.
        for candidate in _candidates_from_text(text):
            try:
                normalised = normalise_isbn(candidate)
            except BooksError:
                continue
            result["candidates"].append(candidate)
            result["isbn"] = normalised["isbn13"]
            result["method"] = "printed"
            return result

        if not result["error"]:
            result["error"] = ("No ISBN found in that image. Type it in, or try a "
                               "closer photo of the barcode or the copyright page.")
    except ImportError as exc:
        if not result["error"]:
            result["error"] = f"Text reading is unavailable: {exc}"
    except Exception as exc:
        if not result["error"]:
            result["error"] = f"Could not read text: {exc}"

    return result


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


def search_urls(meta: Dict[str, Any], marketplace: str = "EBAY_CA") -> Dict[str, Any]:
    """eBay search links the seller can open while signed in, by ISBN and by title.

    Sold history is only available to the seller's own account, so it is handed
    over as a link rather than faked.

    BOTH searches are offered because sellers list under both: many put the title
    in and never enter an ISBN, so an ISBN-only search silently misses them, and a
    title-only search pulls in every other printing of the book. Showing the two
    side by side is what makes the difference visible.
    """
    host = _marketplace_host(marketplace)
    isbn = (meta.get("isbn13") or meta.get("isbn10") or "").strip()
    title = (meta.get("title") or "").strip()
    author = (meta.get("author") or "").strip()

    from urllib.parse import quote_plus

    def build(query: str, label: str, kind: str) -> Dict[str, str]:
        base = f"https://{host}/sch/i.html?_nkw={quote_plus(query)}"
        return {
            "kind": kind,
            "label": label,
            "query": query,
            "active": f"{base}&_ipg=60",
            # LH_Sold + LH_Complete is eBay's own "sold listings" filter.
            "sold": f"{base}&LH_Sold=1&LH_Complete=1&_ipg=60",
        }

    searches: List[Dict[str, str]] = []
    if isbn:
        searches.append(build(isbn, f"ISBN {isbn}", "isbn"))

    # Title plus author, because the title alone returns other books with the same
    # name and the author is what narrows it back down.
    title_query = " ".join(x for x in (title, author) if x)
    if title_query:
        searches.append(build(title_query, title or title_query, "title"))

    primary = searches[0] if searches else {}
    return {
        "searches": searches,
        # Kept for callers that only want the one link, which is the ISBN search
        # when there is an ISBN.
        "query": primary.get("query", ""),
        "active": primary.get("active", ""),
        "sold": primary.get("sold", ""),
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
