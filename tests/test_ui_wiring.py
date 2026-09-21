"""Static check: every getElementById('x') in the JS has a matching id="x"
in the templates. Catches the most common UI wiring mistake.

Run from anywhere:  python tests/test_ui_wiring.py
"""
import re
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

base = (ROOT / "templates/base.html").read_text(encoding="utf-8")
dash = (ROOT / "templates/dashboard.html").read_text(encoding="utf-8")
admin = (ROOT / "templates/admin.html").read_text(encoding="utf-8")
login_path = ROOT / "templates/login.html"
login = login_path.read_text(encoding="utf-8") if login_path.exists() else ""
html = base + dash + admin + login
ids = set(re.findall(r'id="([^"]+)"', html))

missing = []
for js in ["app.js", "dashboard.js", "admin.js"]:
    src = (ROOT / "static/js" / js).read_text(encoding="utf-8")
    refs = set(re.findall(r"getElementById\(['\"]?([\w-]+)", src))
    for m in refs:
        if m not in ids:
            missing.append((js, m))

print("element ids in templates:", len(ids))
print("missing element ids:", missing or "NONE")
# also confirm the new pieces are present in the HTML
for marker, where in [
    ("filter-team", dash),
    ("teams-list", admin),
    ("new-team-name", admin),
    ("create-team", admin),
    ("signout-btn", base),
]:
    print(f"  {marker} in {where[:12]}...:", "OK" if marker in where else "MISSING")
