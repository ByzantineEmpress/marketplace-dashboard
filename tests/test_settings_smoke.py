"""Run the My Accounts page's JavaScript, instead of only reading it.

The platform panels are built by a loop that appends as it goes, so a
ReferenceError inside one panel's builder aborts the loop and leaves the page
EMPTY. That is what happened: the Connect and Sync buttons were declared with
`const` inside an `if`, so every later reference to them threw
"connectBtn is not defined" -- for every platform, including the ones that did
have a Connect button. The page rendered zero of five panels.

None of the other checks could see it. `node --check` proves the file parses, and
it parsed fine. Every other test here greps the source, and the source looked
right. It shipped, and the report was "you seem to have gotten rid of all the
fields". This test renders the page and counts what survives.
"""

import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "settings_smoke_check.js"
TARGET = ROOT / "static" / "js" / "marketplace-settings.js"


class SettingsPageSmokeTest(unittest.TestCase):
    def _run(self, target):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        return subprocess.run(
            [node, str(HARNESS), str(target)],
            capture_output=True, text=True, cwd=str(ROOT),
        )

    def test_every_platform_panel_renders(self):
        result = self._run(TARGET)
        self.assertEqual(
            result.returncode, 0,
            "the settings page failed to render:\n"
            + (result.stdout or "") + (result.stderr or ""),
        )
        self.assertIn("5 panels", result.stdout)

    def test_the_harness_can_actually_fail(self):
        """A check that cannot fail is not a check.

        The broken version is reconstructed here -- the buttons declared inside the
        if-block -- so that if the harness ever stops detecting this class of bug,
        this test says so instead of silently passing forever.
        """
        broken = (ROOT / "tests" / "_broken_settings_probe.js")
        source = TARGET.read_text(encoding="utf-8")
        # Put the declarations back inside the conditional, which is the exact
        # shape that emptied the page.
        broken_source = source.replace(
            'const connectBtn = el("button", "btn btn--success", "Connect");',
            'let connectBtn;\n        if (info.connectable) { connectBtn = el("button", "btn btn--success", "Connect");',
        ).replace(
            'const syncBtn = el("button", "btn btn--outline", "Sync now");',
            'let syncBtn;\n        if (info.connectable) { syncBtn = el("button", "btn btn--outline", "Sync now");',
        )
        if broken_source == source:
            self.skipTest("the declarations have moved; update this probe")
        broken.write_text(broken_source, encoding="utf-8")
        try:
            result = self._run(broken)
        finally:
            broken.unlink(missing_ok=True)
        self.assertNotEqual(
            result.returncode, 0,
            "the harness passed a file it should have failed -- it is not "
            "detecting this class of bug any more",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
