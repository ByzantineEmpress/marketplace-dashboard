"""Run the dashboard's JavaScript, instead of only reading it.

`node --check` proves a file parses; every other check in this suite greps the
source. Neither can see the failure that took the dashboard down: a function
declared outside the DOMContentLoaded handler reaching for `state`, which lives
inside it. That is a runtime ReferenceError, and it surfaced to the user as
"Could not load listings: state is not defined" on an otherwise healthy page.

tests/js_smoke_check.js loads app.js then dashboard.js into a stubbed DOM, runs
the handler, renders the cards, then switches to the table and renders that too.
Anything it throws fails here.
"""

import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "js_smoke_check.js"


class JavaScriptSmokeTest(unittest.TestCase):
    def test_the_dashboard_renders_both_views_without_error(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")

        proc = subprocess.run(
            [node, str(HARNESS)],
            capture_output=True, text=True, timeout=180, cwd=str(ROOT),
        )
        self.assertEqual(
            proc.returncode, 0,
            "the dashboard failed to run:\n" + (proc.stdout or "") + (proc.stderr or ""),
        )
        # The harness reports what it managed to render; a run that produced
        # nothing but silence would pass a return-code check alone.
        self.assertIn("table:", proc.stdout, proc.stdout)

    def test_the_harness_loads_the_same_scripts_the_page_does(self):
        """app.js first: the dashboard uses helpers it defines, and loading only
        dashboard.js reports those as missing — a harness gap, not a real fault."""
        source = HARNESS.read_text(encoding="utf-8")
        self.assertIn('"app.js"', source)
        self.assertIn("DOMContentLoaded", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
