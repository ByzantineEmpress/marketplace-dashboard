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
- **Signing in never puts you in someone else's team.** A brand-new account
  has no team at all and is asked to choose (see *Joining* below).

## Joining: choosing a team on first sign-in

A new account — created with **Google** or with **email + password** at
`/signup` — has no team. It lands on an onboarding page with two choices:

1. **Create my own workspace** — a private team you own outright. This is
   the default and needs nothing from anyone else.
2. **Ask to join a team** — enter the **email address of the person who runs
   the team**, plus a short note about who you are. The owner is not told
   anything you did not type, and **team names are never shown to you**
   before approval.

Both sign-up routes behave identically. Email signup additionally requires
confirming the address first: nothing is created — no user, no team, no
membership — until the emailed link is opened. Google sign-in proves the
address as part of the OAuth flow, so it needs no extra step.

The owner sees pending requests under **Admin → Join Requests**, with a
count badge, and can **Approve** or **Decline**. Approving adds you to that
team as a member; declining is recorded, and you are never told which team
the request referred to.

### Notes on privacy and abuse

- Requesting by email deliberately gives the **same answer whether or not
  the address belongs to a team owner**, so it cannot be used to discover
  who owns what.
- The note is sanitised on the server (markup and control characters
  removed, length capped) before it is stored, and escaped again when
  displayed.
- Requests are rate limited per account, per target team, per source
  address, and globally. The defaults are 5 requests per day per account
  (one per 10 minutes) and 3 per hour per team. Limits are in-process, so
  they apply per worker.
- **Local signup verifies email ownership.** With email + password signup the
  address must be confirmed via a single-use link (60-minute expiry) before
  the account exists at all — so nobody can register an address they do not
  control. This requires working SMTP (Admin → Outbound Email); without it,
  email signup cannot complete and Google sign-in remains the path.

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

## Invite links

Each team has a shareable invite link (`/join/<code>`) shown on the Teams
card. Opening it while signed in joins that team immediately; opening it
while signed out remembers the invite and asks you to sign in first.

- **Strength** — the code is 16 bytes from Python's `secrets` CSPRNG
  (128 bits, 22 URL-safe characters). It is the only secret in the link, so
  treat the whole URL as a password: anyone holding it can join the team.
- **Expiry** — a remembered invite (the `pending_invite` cookie) lasts
  **15 minutes**. After that the sign-in no longer joins the team.
- **Rotation** — "Regenerate invite link" on the Teams card issues a new
  code and immediately invalidates the old one. Do this if a link leaks.

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
