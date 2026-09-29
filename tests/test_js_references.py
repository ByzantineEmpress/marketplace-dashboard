"""Static check: function calls in our JS that are defined nowhere.

Why this exists
---------------
The grouping feature called ``closeListingModal()`` when the real helper is
``closeModal()``. Nothing caught it: the browser only fails at the moment the
button is clicked, so it shipped and surfaced as a runtime popup —

    Could not ungroup: closeListingModal is not defined

That is the JavaScript twin of the Python ``NameError`` that pyflakes now guards
against, and it got through for the same reason: the verification was weaker than
the change. ``node --check`` parses the file but cannot know that a called
function does not exist anywhere.

How it works
------------
1. Comments and string/template literals are stripped first. Without that, prose
   like "Shared (No team)" looks like a call to ``Shared()``.
2. Definitions are collected across ALL the JS files together, because they are
   loaded into one page scope by ``base.html`` — a helper defined in app.js is
   legitimately callable from dashboard.js.
3. Bare calls ``name(...)`` whose name is defined nowhere and is not a browser
   builtin are reported. Method calls (``foo.bar()``) are ignored, since
   properties cannot be resolved statically.

It is deliberately conservative: it cannot prove a call is valid, only that a
call to a name that exists nowhere is wrong — which is exactly the reported bug.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "static" / "js"
JS_FILES = ["app.js", "dashboard.js", "admin.js", "marketplace-settings.js"]

# Browser/language builtins. Kept narrow on purpose: anything added here stops
# being checked.
BUILTINS = {
    "alert", "confirm", "prompt", "fetch", "setTimeout", "clearTimeout",
    "setInterval", "clearInterval", "requestAnimationFrame",
    "cancelAnimationFrame", "queueMicrotask", "structuredClone",
    "parseInt", "parseFloat", "isNaN", "isFinite",
    "Number", "String", "Boolean", "Array", "Object", "JSON", "Math", "Date",
    "RegExp", "Error", "TypeError", "Promise", "Map", "Set", "WeakMap",
    "WeakSet", "Symbol", "BigInt", "Intl", "Function", "eval",
    "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI",
    "URLSearchParams", "URL", "FormData", "FileReader", "Blob", "Image",
    "Audio", "Event", "CustomEvent", "btoa", "atob",
    "getComputedStyle", "matchMedia", "print", "reportError",
    "requestIdleCallback", "isSecureContext",
}

# Keywords and declarations that a naive "word before a paren" regex catches.
KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "typeof", "function",
    "new", "delete", "void", "in", "of", "do", "else", "try", "finally",
    "throw", "case", "default", "break", "continue", "class", "extends",
    "super", "this", "yield", "await", "async", "var", "let", "const",
    "instanceof", "with", "debugger", "export", "import", "from", "static",
    "get", "set",
}

DEF_DECL = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")
DEF_VAR_FN = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function\b")
DEF_VAR_ARROW = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
    r"(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>")
DEF_ANY_VAR = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)")
# Assignment to a plain name (e.g. `state.page = ...` is a member, but
# `foo = function...` or `window.foo = ...` both introduce callables).
DEF_FUNC_PARAM = re.compile(r"\bfunction\s*[A-Za-z_$]*\s*\(([^)]*)\)")
CALL = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")


def strip_comments_and_strings(src: str) -> str:
    """Remove comments and string literals, preserving line numbers."""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                if src[i] == "\n":
                    out.append("\n")
                i += 1
            i += 2
        elif c in ('"', "'", "`"):
            quote = c
            i += 1
            while i < n and src[i] != quote:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == "\n":
                    out.append("\n")
                i += 1
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def defined_names(source: str) -> set:
    names = set()
    for pattern in (DEF_DECL, DEF_VAR_FN, DEF_VAR_ARROW, DEF_ANY_VAR):
        names.update(pattern.findall(source))
    # Function parameters, so `function f(cb) { cb(); }` does not flag cb.
    for params in DEF_FUNC_PARAM.findall(source):
        for param in params.split(","):
            param = param.strip().lstrip("...").strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", param):
                names.add(param)
    # Arrow parameters: `(a, b) =>` and `x =>`
    for params in re.findall(r"\(([^)]*)\)\s*=>", source):
        for param in params.split(","):
            param = param.strip().lstrip("...").strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", param):
                names.add(param)
    for param in re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*=>", source):
        names.add(param)
    return names


def undefined_calls(source: str, global_names: set = None):
    """Bare calls whose name is defined nowhere (locally or in global_names)."""
    code = strip_comments_and_strings(source)
    defined = defined_names(code) | (global_names or set())
    problems, seen = [], set()
    for match in CALL.finditer(code):
        name = match.group(1)
        if name in KEYWORDS or name in BUILTINS or name in defined:
            continue
        prefix = code[max(0, match.start() - 2):match.start()].strip()
        if prefix.endswith((":", "?", ".")):
            continue
        line_no = code.count("\n", 0, match.start()) + 1
        if (name, line_no) in seen:
            continue
        seen.add((name, line_no))
        problems.append(f"line {line_no}: calls '{name}(...)' which is defined nowhere")
    return problems


def all_defined_names() -> set:
    """Every name defined in any of the JS files (they share one page scope)."""
    names = set()
    for filename in JS_FILES:
        path = JS_DIR / filename
        if path.exists():
            names |= defined_names(strip_comments_and_strings(
                path.read_text(encoding="utf-8")))
    return names


class JsUndefinedFunctionTest(unittest.TestCase):
    def test_no_js_file_calls_an_undefined_function(self):
        shared = all_defined_names()
        problems = []
        for filename in JS_FILES:
            path = JS_DIR / filename
            if not path.exists():
                continue
            # Locals plus everything else on the page: base.html loads all of
            # these into the same scope.
            for problem in undefined_calls(path.read_text(encoding="utf-8"), shared):
                problems.append(f"{filename} {problem}")
        if problems:
            self.fail(
                "function calls with no definition (these throw "
                "ReferenceError at runtime):\n  " + "\n  ".join(problems)
            )

    def test_the_check_can_actually_fail(self):
        """Uses the exact defect that shipped: closeListingModal vs closeModal."""
        snippet = (
            "function closeModal() {}\n"
            "function save() {\n"
            "    closeListingModal();\n"
            "}\n"
        )
        problems = undefined_calls(snippet)
        self.assertTrue(problems, "the checker failed to spot an undefined call")
        self.assertIn("closeListingModal", problems[0])

    def test_strings_and_comments_are_not_treated_as_calls(self):
        """Prose like \"Shared (No team)\" must not look like Shared()."""
        snippet = (
            "const label = 'Shared (No team)';\n"
            "// Renderer (legacy) is gone\n"
            "/* toggle( this ) */\n"
            "const html = `<div>Escape (me)</div>`;\n"
        )
        self.assertEqual(undefined_calls(snippet), [])

    def test_the_check_accepts_defined_and_builtin_calls(self):
        """Must not cry wolf on ordinary code."""
        snippet = (
            "function helper() {}\n"
            "const arrow = (x) => x;\n"
            "function run(cb) {\n"
            "    helper();\n"
            "    arrow(1);\n"
            "    cb();\n"
            "    alert('x');\n"
            "    fetch('/api');\n"
            "    document.querySelectorAll('.a').forEach(n => n.remove());\n"
            "    const s = JSON.stringify({ a: 1 });\n"
            "    if (s) { return setTimeout(() => {}, 10); }\n"
            "}\n"
        )
        self.assertEqual(undefined_calls(snippet), [])

    def test_a_helper_defined_in_another_file_counts_as_defined(self):
        """escapeHtml lives in one file and is called from others."""
        shared = {"escapeHtml"}
        snippet = "function render(){ return escapeHtml('x'); }\n"
        self.assertEqual(undefined_calls(snippet, shared), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
