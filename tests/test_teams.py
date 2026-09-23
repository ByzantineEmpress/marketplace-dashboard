"""End-to-end test of the Teams feature (run against the live server).

Simulates two people:
  - admin  (password login) — owner of the Default Team, and creator of a
    second team "Family Flips"
  - friend (a Google user row, pre-created by the admin via 'add member')

Verifies: team creation, member management, team-scoped listings/stats,
listing-to-team moves, page identity, and session persistence.
"""
import http.cookiejar
import json
import os
import re
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "http://127.0.0.1:8000"

# Read the admin password from .env (the terminal tool redacts known
# secrets from command text, so don't hard-code it here).
env = {}
with open(os.path.join(ROOT, ".env"), encoding="utf-8") as f:
    for line in f:
        m = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*)$", line.strip())
        if m:
            env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
ADMIN_USER = env.get("ADMIN_USERNAME", "admin")
ADMIN_PASS = env.get("ADMIN_PASSWORD", "")

import sys
sys.path.insert(0, ".")

# Reset leftovers from a previous (possibly crashed) run so this test
# can be re-run.
from src.database import SessionLocal
from src.models import Team, User, TeamMembership, AuthSession, Listing

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, url, newurl):
        return None


def make_client():
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedirect)
    return opener, cj


def api(opener, method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        r = opener.open(req)
        return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def run_teams_e2e():
    db = SessionLocal()
    leftover = db.query(Team).filter(Team.name == "Family Flips").first()
    if leftover:
        for m in db.query(TeamMembership).filter(TeamMembership.team_id == leftover.id):
            db.delete(m)
        db.delete(leftover)
    friend = db.query(User).filter(User.email == "friend@example.com").first()
    if friend:
        db.query(AuthSession).filter(AuthSession.user_id == friend.id).delete(synchronize_session=False)
        db.delete(friend)
    db.query(Listing).filter(Listing.title.like("TEST %")).delete(synchronize_session=False)
    db.commit()
    db.close()

    print("== 1. Admin password login (creates local user + session) ==")
    admin, admin_cj = make_client()
    st, data = api(admin, "POST", "/api/auth/login", {"username": ADMIN_USER, "password": ADMIN_PASS})
    check("login returns ok", st == 200 and data.get("ok"), f"status={st}")
    token = data.get("token", "")
    check("token issued (64 chars)", len(token) == 64, f"len={len(token)}")

    print("== 2. /api/teams as admin ==")
    st, teams = api(admin, "GET", "/api/teams")
    check("GET /api/teams -> 200", st == 200, f"status={st}")
    check("Default Team exists", any(t["name"] == "Default Team" for t in teams))
    default_team = next((t for t in teams if t["name"] == "Default Team"), None)
    check("admin is owner of Default Team",
          default_team and default_team.get("role") == "owner"
          and any(m["role"] == "owner" for m in default_team["members"]))

    print("== 3. Create a second team ==")
    st, data = api(admin, "POST", "/api/teams", {"name": "Family Flips"})
    check("POST /api/teams ok", st == 200 and data.get("ok"), f"status={st} data={data}")
    family_id = (data.get("team") or {}).get("id")
    check("new team has an id", family_id is not None)
    st2, data2 = api(admin, "POST", "/api/teams", {"name": "Family Flips"})
    check("duplicate team name rejected", st2 == 400, f"status={st2}")

    print("== 4. Add a member by email (user doesn't exist yet) ==")
    st, data = api(admin, "POST", f"/api/teams/{family_id}/members", {"email": "friend@example.com"})
    check("add member ok", st == 200 and data.get("ok"), f"status={st} data={data}")
    st, data = api(admin, "POST", f"/api/teams/{family_id}/members", {"email": "friend@example.com"})
    check("duplicate member rejected", st == 400, f"status={st}")

    print("== 5. Team list now shows both teams + friend ==")
    st, teams = api(admin, "GET", "/api/teams")
    check("admin is in family and default teams",
          any(t["name"] == "Family Flips" for t in teams) and any(t["name"] == "Default Team" for t in teams),
          f"got {[t['name'] for t in teams]}")
    family = next((t for t in teams if t["name"] == "Family Flips"), None)
    check("family team has admin(owner)+friend(member)",
          family and len(family["members"]) == 2
          and any(m["role"] == "owner" for m in family["members"])
          and any(m["role"] == "member" for m in family["members"]))
    friend = next((m for m in (family or {}).get("members", []) if m["role"] == "member"), None)
    check("friend row was pre-created", friend is not None and friend["email"] == "friend@example.com")

    print("== 6. Team-scoped listings ==")
    # Seed listings directly in the DB: one per team.
    db = SessionLocal()
    db.query(Listing).filter(Listing.title.like("TEST %")).delete(synchronize_session=False)
    db.add(Listing(platform="ebay", platform_listing_id="TEST-DEF-1", title="TEST default team item",
                   price_cents=1500, status="active", is_sold=False, team_id=default_team["id"]))
    db.add(Listing(platform="ebay", platform_listing_id="TEST-FAM-1", title="TEST family item",
                   price_cents=2500, status="active", is_sold=False, team_id=family_id))
    db.commit()
    db.close()

    st, listings = api(admin, "GET", "/api/listings")
    check("admin sees both team listings", st == 200 and listings.get("total", 0) >= 2,
          f"total={listings.get('total')}")
    titles = [l["title"] for l in listings.get("listings", [])]
    check("listing dicts include team_name",
          all("team_name" in l for l in listings.get("listings", [])))

    st, listings_fam = api(admin, "GET", f"/api/listings?team={family_id}")
    fam_titles = [l["title"] for l in listings_fam.get("listings", [])]
    check("?team= filters to that team only",
          "TEST family item" in fam_titles and "TEST default team item" not in fam_titles,
          f"got {fam_titles}")

    st, stats = api(admin, "GET", "/api/stats")
    st2, stats_fam = api(admin, "GET", f"/api/stats?team={family_id}")
    check("stats respect team filter",
          st == 200 and st2 == 200 and stats.get("total_listings", 0) > stats_fam.get("total_listings", 0),
          f"all={stats.get('total_listings')} team={stats_fam.get('total_listings')}")

    print("== 7. Friend (team 2 only) has a narrower view ==")
    # Give friend a session in the DB (simulates their Google sign-in landing here).
    import secrets
    from datetime import datetime, timedelta
    from src.models import User, AuthSession
    db = SessionLocal()
    u = db.query(User).filter(User.email == "friend@example.com").first()
    check("friend user row exists", u is not None)
    ftoken = secrets.token_urlsafe(48)
    db.add(AuthSession(token=ftoken, user_id=u.id, expires_at=datetime.utcnow() + timedelta(days=7)))
    db.commit()
    db.close()

    fop, fcj = make_client()
    # Put the friend's token in the jar so every request from fop is authenticated.
    fcj.set_cookie(http.cookiejar.Cookie(
        version=0, name="auth_token", value=ftoken, port=None, port_specified=False,
        domain="127.0.0.1", domain_specified=True, domain_initial_dot=False,
        path="/", path_specified=True, secure=False, expires=None, discard=False,
        comment=None, comment_url=None, rest={},
    ))
    st = fop.open(urllib.request.Request(BASE + "/api/teams")).status
    fteams = json.loads(fop.open(urllib.request.Request(BASE + "/api/teams")).read().decode())
    check("friend sees only their team", st == 200 and len(fteams) == 1 and fteams[0]["name"] == "Family Flips",
          f"got {[t['name'] for t in fteams]}")

    st, flist = api(fop, "GET", "/api/listings")
    ftitles = [l["title"] for l in flist.get("listings", [])]
    check("friend sees only Family Flips listings",
          "TEST family item" in ftitles and "TEST default team item" not in ftitles,
          f"got {ftitles}")

    # Friend tries to move the default-team listing into their team -> not their team
    default_listing_id = None
    st, alist = api(admin, "GET", "/api/listings")
    for l in alist.get("listings", []):
        if l["title"] == "TEST default team item":
            default_listing_id = l["id"]
    # Friend tries to move the default-team listing into the Default Team —
    # a team the friend is NOT in.
    st, data = api(fop, "PUT", f"/api/listings/{default_listing_id}", {"team_id": default_team["id"]})
    check("friend can't move another team's listing", st == 404, f"status={st}")
    st, data = api(admin, "PUT", f"/api/listings/{default_listing_id}", {"team_id": family_id})
    check("admin can move a listing between own teams", st == 200 and data.get("ok"), f"status={st}")

    print("== 8. Pages show the right identity ==")
    st, html = api(admin, "GET", "/api/settings")
    req = urllib.request.Request(BASE + "/dashboard")
    req.add_header("Cookie", "auth_token=" + token)
    r = admin.open(req)
    check("dashboard renders for admin", r.status == 200)
    body = r.read().decode()
    check("dashboard identity = admin name", ADMIN_USER in body)
    req = urllib.request.Request(BASE + "/dashboard")
    req.add_header("Cookie", f"auth_token={ftoken}")
    r = fop.open(req)
    body = r.read().decode()
    check("dashboard identity = friend name", "friend" in body.lower())

    print("== 9. Removing a member revokes their team view ==")
    st, data = api(admin, "DELETE", f"/api/teams/{family_id}/members/{friend['id']}")
    check("remove member ok", st == 200 and data.get("ok"), f"status={st}")
    st, flist = api(fop, "GET", "/api/listings")
    check("removed friend now sees no team listings", flist.get("total", 0) == 0,
          f"total={flist.get('total')}")
    st, data = api(admin, "POST", f"/api/teams/{family_id}/members", {"email": "friend@example.com"})
    check("re-adding member works", st == 200 and data.get("ok"))

    print("== 10. Logout deletes the stored session ==")
    st, data = api(fop, "POST", "/api/auth/logout")
    st2, flist = api(fop, "GET", "/api/teams")
    check("after logout, API 401s", st2 == 401, f"status={st2}")

    print("== 11. Cleanup: remove TEST listings ==")
    db = SessionLocal()
    db.query(Listing).filter(Listing.title.like("TEST %")).delete(synchronize_session=False)
    db.commit()
    db.close()

    print()
    fails = [r for r in results if not r[1]]
    print(f"{len(results) - len(fails)}/{len(results)} checks passed")
    for name, ok, detail in fails:
        print(f"  FAILED: {name} {detail}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    run_teams_e2e()
