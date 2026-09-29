"""Static check: undefined names and other dead code, via pyflakes.

Why this exists
---------------
A change to the Etsy adapter started using ``api_secret`` in the request
headers, but that function only ever defined ``api_key``. The user saw it live:

    Sync reported: name 'api_secret' is not defined

The check written alongside that change looked only for *assignments* that
shadowed a parameter. It never looked for a *read* of a name that was never
bound, so it passed while the code was broken.

An earlier attempt at hand-rolling this produced false positives (it could not
see ``global`` declarations, or names bound inside a module-level ``if``), and a
check that cries wolf gets ignored. pyflakes is the right tool: it is small, has
no dependencies, and understands Python scoping properly.

What it caught beyond the reported bug
--------------------------------------
``list_listings`` used ``os.environ`` but ``os`` was only imported inside other
methods, so it was undefined there too. That would have failed immediately after
the user connected their account - the very next step.

Only *undefined name* findings fail this check. Unused imports and unused
locals are reported for information but do not fail, because they are untidy
rather than broken and a failing suite over cosmetics would be noise.
"""

import os
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def run_pyflakes():
    """Run pyflakes over src/ and return its output lines."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pyflakes", str(SRC)],
            capture_output=True, text=True, cwd=str(ROOT),
        )
    except FileNotFoundError:
        raise unittest.SkipTest("could not run pyflakes")
    return [line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()]


class UndefinedNameTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import pyflakes  # noqa: F401
        except ImportError:
            raise unittest.SkipTest(
                "pyflakes is not installed. It is a development dependency:\n"
                "    pip install pyflakes"
            )
        cls.findings = run_pyflakes()

    def test_no_undefined_names(self):
        """A name that is read but never bound raises NameError at runtime."""
        undefined = [f for f in self.findings if "undefined name" in f]
        if undefined:
            self.fail(
                "undefined names found (these raise NameError when reached):\n  "
                + "\n  ".join(undefined)
            )

    def test_the_check_can_actually_fail(self):
        """Guard against the check passing everything.

        A checker that silently succeeds is worse than none, so this compiles a
        snippet shaped exactly like the real defect and asserts it is reported.
        """
        import tempfile

        snippet = (
            "def send(credentials):\n"
            "    api_key = 'k'\n"
            "    return {'x-api-key': f'{api_key}:{api_secret}'}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "sample.py"
            path.write_text(snippet, encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-m", "pyflakes", str(path)],
                capture_output=True, text=True,
            )
        output = proc.stdout + proc.stderr
        self.assertIn("undefined name", output)
        self.assertIn("api_secret", output)

    def test_useful_findings_are_visible_without_failing(self):
        """Unused imports and locals are printed so they can be tidied, but they
        do not fail the suite: they are not runtime errors."""
        tidy_ups = [
            f for f in self.findings
            if "imported but unused" in f or "never used" in f
        ]
        if tidy_ups:
            print(f"\n  {len(tidy_ups)} non-fatal finding(s) in src/:")
            for line in tidy_ups[:20]:
                print(f"    {os.path.basename(line.split(':')[0])}:{line.split(':')[1]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
