# Outbound Email Setup

Email + password signup **cannot complete without this**. A new signup
creates no user, team or membership until the confirmation link is opened, so
if outbound email does not work, email signup refuses rather than pretending
to succeed. Google sign-in is unaffected.

The app supports two transports, chosen in **Admin → Outbound Email**:

| Transport | `MAIL_BACKEND` | Use when |
|---|---|---|
| Provider API | `http` | You have an API key from Resend / Brevo / Postmark. **Recommended.** |
| SMTP | `smtp` | You have an SMTP server or relay. |

## 1. Decide your sending address

Use a **subdomain**, e.g. `send.yourdomain.com`, not the root domain. Two
reasons:

- Sending reputation attaches to the subdomain, so a deliverability problem
  with signup mail cannot damage your main domain's email.
- You don't need any mailbox on the root domain.

The address users see is `MAIL_FROM`. Include a **display name** so inboxes
show a product name rather than a bare address:

```
MAIL_FROM=Marketplace Dashboard <noreply@send.yourdomain.com>
```

`from_email()` strips the name for provider APIs, which take the address
separately, and `from_display_name()` reads it back — so the same value works
for both transports.

**Never use a personal mailbox as the sender** — providers rewrite or reject
unverified From addresses, and it exposes your address to every recipient.

## What the emails look like

Signup confirmation is sent as **multipart/alternative**: a plain-text part
and a styled HTML part, identical in content. Text clients and spam filters
read the plain part; everyone else sees the HTML with a single call-to-action
button.

The HTML is intentionally minimal — inline styles only, no images, no
tracking pixels, no marketing copy. That is deliberate: heavy markup hurts
deliverability for transactional mail, and this message only needs to be
readable and get one click.

Anything user-supplied that reaches the HTML (the recipient's name, which
comes from the signup form) is HTML-escaped, so a crafted name cannot inject
tags into the message.

## 2. Own a domain and verify it with the provider

This is unavoidable: to send as `@yourdomain.com` you must prove you control
the domain, via DNS records. Buy a domain anywhere (Cloudflare Registrar,
Porkbun, Namecheap), then add it to your email provider and create these
records:

| Type | Name (example) | Purpose |
|---|---|---|
| MX | `send` | Return-path / bounce handling |
| TXT | `send` | SPF — authorises the provider's servers to send for you |
| TXT | `resend._domainkey.send` | DKIM — cryptographically signs each message |
| TXT | `_dmarc` | DMARC — tells receivers what to do when SPF/DKIM fail |

Your provider generates the exact values; copy them verbatim. A single wrong
character in the DKIM value makes signing fail **silently** while everything
still looks configured.

### Cloudflare-specific gotchas

- Leave these records **DNS only** (grey cloud, not orange). Proxying breaks
  MX and DKIM.
- Do **not** enable Cloudflare **Email Routing** on the sending subdomain —
  it contends for the MX and SPF records.
- DNS records added while a newly registered domain is still *Pending*
  activation will not resolve. Wait until Cloudflare shows the domain
  **Active**.

## 3. Configure the app

Admin → Outbound Email. For a provider API:

```
Transport      Provider API
API Provider   Resend
API Key        re_xxxxxxxx
From Address   noreply@send.yourdomain.com
```

or in `.env`:

```
MAIL_BACKEND=http
MAIL_PROVIDER=resend
MAIL_API_KEY=re_xxxxxxxx
MAIL_FROM=noreply@send.yourdomain.com
```

For SMTP instead:

```
MAIL_BACKEND=smtp
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...
MAIL_FROM=noreply@send.yourdomain.com
```

## 4. Prove it works

Click **Send test email** on the Admin page and enter your own address. The
result reflects the real provider response — a failure shows the provider's
own message (for example "domain not verified"), not a generic error.

Then do one real signup at `/signup` and confirm the confirmation link
arrives and completes the account.

## Changing these settings takes effect only after a restart

Two things surprise people here:

- **`.env` is read once, at import time.** Saving new mail settings does *not*
  affect the running process. `--reload` watches `.py` files, not `.env`, so
  the server keeps using the old values and signup reports *"Outbound email is
  not configured"* even though the file looks correct.
- **Killing the `--reload` parent can leave a live worker behind.** The child
  worker inherits the listening socket, so the port stays bound and the old
  process keeps serving. Look for a `python -c "from multiprocessing.spawn
  import spawn_main ..."` process and stop that too.

So: after editing mail settings, **fully stop every uvicorn process** and
start again. Verify the restart actually took by submitting a signup — if it
reports "not configured", the old process is still serving.

## Verifying DNS

`check_email_dns.py` (in the workspace, not part of the app) queries
DNS-over-HTTPS and validates the records:

```bash
python check_email_dns.py send.yourdomain.com
```

It reports whether the apex resolves, and whether MX, SPF, DKIM and DMARC are
published. Re-run it while waiting for propagation.

## How verification works in the app

- The token is 32 random bytes; only its **SHA-256 digest** is stored, so a
  database dump cannot be used to confirm someone else's address.
- Links are **single-use** and expire after **60 minutes**.
- Resends rotate the token (invalidating the previous link) and are rate
  limited: 3 per hour per address, 5 per hour per source, and at most 5
  sends per pending signup.
- While pending, **no** user, team or membership exists. The address is
  reserved for the duration so two people cannot race for it.
