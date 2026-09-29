"""Mobile-layout wiring: the nav menu, stacked toolbar, and bottom-sheet modal.

These are the three things that made the dashboard unusable on a phone: the nav
overflowed as desktop links, the toolbar scattered its buttons, and modals had
no comfortable way to dismiss. This suite pins the markup/CSS/JS that make them
behave, so a refactor cannot silently regress one of them.
"""

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(*parts):
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


class MobileNavTest(unittest.TestCase):
    def test_the_hamburger_and_links_exist(self):
        html = _read("templates", "base.html")
        self.assertIn('id="nav-burger"', html)
        self.assertIn('id="nav-links"', html)

    def test_the_burger_is_wired_in_js(self):
        js = _read("static", "js", "app.js")
        self.assertIn('getElementById("nav-burger")', js)
        self.assertIn('getElementById("nav-links")', js)
        # Must close on a link tap, outside tap, and Escape — those are what
        # make the menu feel dismissible rather than trapped.
        self.assertIn('closest("a")', js)
        self.assertIn("Escape", js)

    def test_the_mobile_menu_css_exists(self):
        css = _read("static", "css", "style.css")
        self.assertIn(".nav-burger", css)
        self.assertIn(".navbar.menu-open .nav-links", css)


class MobileToolbarTest(unittest.TestCase):
    def test_the_toolbar_stacks_on_mobile(self):
        css = _read("static", "css", "style.css")
        # The one-column layout lives inside the max-width: 767px block.
        block = css.split("@media (max-width: 767px)")[-1]
        self.assertIn(".toolbar-left", block)
        self.assertIn("grid-template-columns: 1fr 1fr", block)
        self.assertIn(".toolbar-right", block)

    def test_the_primary_add_button_is_full_width(self):
        css = _read("static", "css", "style.css")
        self.assertIn("#add-manual-listing-btn", css)


class MobileModalTest(unittest.TestCase):
    def test_the_modal_is_a_dismissible_bottom_sheet(self):
        css = _read("static", "css", "style.css")
        block = css.split("@media (max-width: 640px)")[-1]
        self.assertIn("align-items: flex-end", block)
        self.assertIn("border-radius: var(--radius-lg) var(--radius-lg) 0 0", block)
        # The close target must be large enough to hit with a thumb.
        self.assertIn(".modal-close", block)
        self.assertIn("44px", block)


class DetailPhotoSizeTest(unittest.TestCase):
    """The detail modal's photo must render large, not as a clipped strip.

    This broke twice. First a fixed 260px height with object-fit:contain
    letterboxed it small; then, after switching to aspect-ratio, the box was
    squashed to a sliver because .modal-body is a COLUMN flex container and the
    photo was a shrinkable flex item. overflow:hidden clipped the result to a
    thin band of the image.
    """

    def _media_block(self):
        """Only the .modal-media rule and its .modal-media img rule."""
        css = _read("static", "css", "style.css")
        start = css.index(".modal-media {")
        img_start = css.index(".modal-media img {", start)
        end = css.index("}", img_start)
        return css[start:end]

    def test_the_photo_box_cannot_be_shrunk_by_flex(self):
        block = self._media_block()
        self.assertIn("flex-shrink: 0", block,
                      "the photo box will be squashed by .modal-body's column flex")

    def test_the_photo_box_has_a_minimum_height(self):
        block = self._media_block()
        self.assertIn("min-height", block,
                      "without a minimum the box can collapse to a sliver")

    def test_the_image_fills_the_box(self):
        block = self._media_block()
        self.assertIn("aspect-ratio", block)
        self.assertIn("object-fit: cover", block)

    def test_the_photo_box_is_not_a_flex_container(self):
        """Flex centering fights the image's height:100%."""
        block = self._media_block()
        self.assertNotIn("display: flex", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
