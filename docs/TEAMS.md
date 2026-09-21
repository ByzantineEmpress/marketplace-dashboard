# Teams — User Guide

Teams let multiple people share **one inventory** while keeping their own
accounts. Think: you and a partner both sell from the same box of stock —
when either of you sells an item, the count drops for both of you.

## Core ideas

- **Every user has a personal team by default.** Your own listings live
  there and only you see them counted as "yours".
- **A team is shared stock.** Listings belonging to a team count toward the
  team's total. If two members of "Family Flips" both list the same kind of
  item, the dashboard shows the *combined* number for that team.
- **Personal stock stays separate.** Joining a team does not merge your
  personal team into it — your personal team keeps your own listings.
- **Membership is per-team.** One user can belong to several teams (or none
  beyond their personal one).

## Everyday usage

1. **See the shared view** — on the dashboard, the **team filter**
   (top of the page) lists your teams. Pick a team to see its combined
   listings, stock counts and stats.
2. **Edit stock for a shared listing** — as a member of the team, you can
   adjust stock/price on that team's listings from the dashboard (e.g.
   "sold one → set stock 4 → 3"). The change is visible to every member
   instantly.
3. **Add your own listings to a team** — new listings are created in your
  personal team; move them into a team via **Admin → Listings** (or the
  dashboard's listing actions) if the team should sell them.

## Team management (Admin page → Teams)

Available to **admin** users:

- **Create** a team — enter a name (e.g. "Family Flips").
- **Add members** — pick any existing user of the app (they can sign in
  locally or via Google).
- **Remove members** — their listings stay in the team; only their access
  goes away.
- **Rename / disband** — renaming keeps all listings; disbanding deletes
  the team (check what should happen to its listings first).

## Who can do what

| Action | Who |
|---|---|
| View their personal team | everyone |
| View/edit a team's listings | members of that team |
| Create teams, add/remove members, rename/disband | admin |
| Restrict which Google accounts may sign in | admin (`GOOGLE_ALLOWED_EMAILS`) |

## Typical setup: you + a partner

1. You're already in as `admin` (owner of your personal team).
2. Partner installs the app / opens it at your server URL, and signs in —
   with their own Google account via the Google button (see
   GOOGLE-LOGIN.md), which creates their user automatically.
3. You go to **Admin → Teams → Create**, name the team, and add your
   partner as a member.
4. Move the shared listings into that team.
5. Both of you pick the team in the dashboard filter — same counts, same
   stock, edits sync for both.
