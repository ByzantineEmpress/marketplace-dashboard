"""Outbound email, provider-agnostic.

Three transports are supported behind one interface:

* ``smtp``     — any SMTP server (``smtplib`` from the standard library);
* ``http``     — an HTTP API, selected by ``MAIL_PROVIDER``
                 (``resend`` | ``brevo`` | ``postmark`` | ``generic``);
* unset        — email is considered unconfigured and sends refuse cleanly.

HTTP transport uses ``httpx``, which is already a dependency for the
marketplace adapters, so supporting API providers adds no new packages.

Every send returns ``(ok, error)`` instead of raising, so a mail outage shows
up as a message the user can act on rather than a 500.

The HTTP call is blocking (``httpx.Client``). That is acceptable for a
self-hosted instance sending a handful of signup mails; if this ever needs to
send at volume, switch to ``httpx.AsyncClient`` and make the callers await.
"""

from email.message import EmailMessage
import html as _html
import smtplib

import httpx

from src.config import config


def _esc(value) -> str:
    """Escape text for safe interpolation into an HTML email body.

    The recipient's display name comes from the signup form, so it is
    untrusted input being placed inside markup. Escaping keeps a crafted name
    from injecting tags into the message (which would at best corrupt the
    layout and at worst look like phishing to a spam filter).
    """
    return _html.escape(str(value if value is not None else ""), quote=True)

# In-process capture used by the test suite. When set, messages are appended
# here and nothing is sent, so tests can assert on real message content
# without needing a mail server or a network.
OUTBOX: list[dict] | None = None

# HTTP statuses worth retrying later, as opposed to a permanent config error.
_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


def mail_backend() -> str:
    """Which transport is configured: "smtp", "http", or "" (none)."""
    if config.MAIL_BACKEND == "http" and config.MAIL_API_KEY:
        return "http"
    if config.MAIL_BACKEND == "smtp" and config.SMTP_HOST:
        return "smtp"
    # Be forgiving if MAIL_BACKEND was left unset: infer from what is present.
    if config.MAIL_API_KEY and config.MAIL_PROVIDER:
        return "http"
    if config.SMTP_HOST and (config.SMTP_USER or config.SMTP_FROM):
        return "smtp"
    return ""


def smtp_configured() -> bool:
    """True when there is enough configuration to attempt a send.

    A transport plus a sender address. A host on its own is not enough: the
    message needs a From, whether that comes from MAIL_FROM or SMTP_FROM.
    """
    return bool(mail_backend()) and bool(from_address())


def from_address() -> str:
    """The full From header, e.g. ``Dashboard <noreply@example.com>``."""
    if config.MAIL_FROM:
        return config.MAIL_FROM
    if mail_backend() == "smtp":
        return config.SMTP_FROM or config.SMTP_USER
    return ""


def from_email() -> str:
    """Just the address part of the From header.

    HTTP provider APIs take the address separately, so a display name in
    MAIL_FROM is stripped here rather than being sent as part of the address.
    """
    raw = from_address()
    if "<" in raw and ">" in raw:
        return raw[raw.rfind("<") + 1:raw.rfind(">")].strip()
    return raw.strip()


def from_display_name() -> str:
    """The display name part of MAIL_FROM, or "" if there isn't one."""
    raw = from_address()
    if "<" in raw and ">" in raw:
        return raw[:raw.rfind("<")].strip().strip('"')
    return ""



# ------------------------------------------------------------------ #
#  Transports
# ------------------------------------------------------------------ #

def _send_via_smtp(to: str, subject: str, text_body: str,
                   html_body: str = "") -> tuple[bool, str]:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_address()
    message["To"] = to
    message.set_content(text_body)
    if html_body:
        # Multipart/alternative: clients that render HTML use it, everything
        # else falls back to the plain-text part. Both carry the same links.
        message.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP(config.SMTP_HOST, int(config.SMTP_PORT), timeout=15) as server:
            if config.SMTP_USE_TLS:
                server.starttls()
            if config.SMTP_PASSWORD:
                server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.send_message(message)
        return True, ""
    except Exception as exc:  # smtplib raises a wide range of socket/SSL errors
        return False, f"{type(exc).__name__}: {exc}"


def _provider_url(provider: str) -> str:
    """The API endpoint for the configured HTTP provider."""
    if provider == "resend":
        return "https://api.resend.com/emails"
    if provider == "brevo":
        return "https://api.brevo.com/v3/smtp/email"
    if provider == "postmark":
        return "https://api.postmarkapp.com/email"
    if provider == "generic":
        return config.MAIL_API_URL or ""
    return ""


def _provider_headers(provider: str) -> dict:
    """Auth headers for the configured HTTP provider."""
    key = config.MAIL_API_KEY
    if provider == "resend":
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if provider == "brevo":
        return {"api-key": key, "Content-Type": "application/json", "accept": "application/json"}
    if provider == "postmark":
        return {"X-Postmark-Server-Token": key,
                "Content-Type": "application/json", "Accept": "application/json"}
    if provider == "generic":
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    return {}


def _build_payload(provider: str, to: str, subject: str, text_body: str,
                   html_body: str = "") -> dict:
    sender_addr = from_email()
    sender_name = from_display_name() or config.APP_NAME
    if provider == "brevo":
        payload = {
            "sender": {"email": sender_addr, "name": sender_name},
            "to": [{"email": to}],
            "subject": subject,
            "textContent": text_body,
        }
        if html_body:
            payload["htmlContent"] = html_body
        return payload
    if provider == "postmark":
        payload = {
            "From": from_address(),
            "To": to,
            "Subject": subject,
            "TextBody": text_body,
        }
        if html_body:
            payload["HtmlBody"] = html_body
        return payload
    # resend and generic: `from` accepts "Name <addr>"
    payload = {"from": from_address(), "to": [to], "subject": subject,
               "text": text_body}
    if html_body:
        payload["html"] = html_body
    return payload


def _send_via_http(to: str, subject: str, text_body: str,
                   html_body: str = "") -> tuple[bool, str]:
    provider = (config.MAIL_PROVIDER or "").strip().lower()
    if not provider:
        return False, "MAIL_PROVIDER is not set (resend | brevo | postmark | generic)"
    url = _provider_url(provider)
    if not url:
        if provider == "generic":
            return False, "MAIL_API_URL is not set for the generic provider"
        return False, f"Unknown MAIL_PROVIDER {provider!r}"
    headers = _provider_headers(provider)
    payload = _build_payload(provider, to, subject, text_body, html_body)
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        return False, f"Could not reach {provider}: {type(exc).__name__}: {exc}"

    if 200 <= resp.status_code < 300:
        return True, ""
    detail = ""
    try:
        data = resp.json()
        # Providers each name the error field differently.
        detail = data.get("message") or data.get("error") or data.get("Message") or ""
        if isinstance(detail, dict):
            detail = detail.get("message") or str(detail)
    except Exception:
        detail = (resp.text or "")[:200]
    kind = "temporary" if resp.status_code in _TRANSIENT_STATUS else "rejected"
    return False, f"{provider} {kind} error (HTTP {resp.status_code}): {detail}"


# ------------------------------------------------------------------ #
#  Public interface
# ------------------------------------------------------------------ #

def send_email(to: str, subject: str, body: str,
               html_body: str = "") -> tuple[bool, str]:
    """Send an email. Returns (ok, error_message).

    ``body`` is the plain-text part and is always required — it is what
    text-only clients and spam filters see. ``html_body`` is optional; pass it
    and the message is sent as multipart/alternative.
    """
    if not to:
        return False, "No recipient address"

    if OUTBOX is not None:
        OUTBOX.append({"to": to, "subject": subject, "body": body,
                       "html_body": html_body, "from": from_address()})
        return True, ""

    backend = mail_backend()
    if not backend:
        return False, "Outbound email is not configured"
    if not from_address():
        return False, "No sender address configured (set MAIL_FROM)"

    if backend == "smtp":
        return _send_via_smtp(to, subject, body, html_body)
    return _send_via_http(to, subject, body, html_body)


# ------------------------------------------------------------------ #
#  Message templates
# ------------------------------------------------------------------ #

def _layout(heading: str, paragraphs: list[str], cta_label: str = "",
            cta_url: str = "", footnote: str = "") -> str:
    """Build a simple, email-client-safe HTML body.

    Deliberately restrained: inline styles only (external CSS is stripped by
    most clients), no images, no tracking pixels, no emoji. Heavy marketing
    markup hurts deliverability for transactional mail, and this message only
    needs to be readable and get one click.
    """
    blocks = []
    for para in paragraphs:
        # Callers pass already-escaped fragments where needed; plain copy is
        # passed through as-is.
        blocks.append(
            '<p style="margin:0 0 16px;font-size:15px;line-height:1.6;color:#374151;">'
            + para + "</p>"
        )
    button = ""
    if cta_label and cta_url:
        button = (
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            'style="margin:24px 0;"><tr><td style="border-radius:8px;background:#4f46e5;">'
            f'<a href="{cta_url}" style="display:inline-block;padding:12px 24px;'
            'font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;">'
            f"{cta_label}</a></td></tr></table>"
        )
    note = ""
    if footnote:
        note = (
            '<p style="margin:24px 0 0;font-size:13px;line-height:1.6;color:#6b7280;">'
            + footnote + "</p>"
        )
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f3f4f6;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:32px 16px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:12px;padding:32px;">
<tr><td style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<h1 style="margin:0 0 20px;font-size:20px;line-height:1.3;color:#111827;">{heading}</h1>
{"".join(blocks)}
{button}
{note}
</td></tr></table>
</td></tr></table>
</body></html>"""


def send_verification_email(to: str, name: str, verify_url: str,
                            expires_minutes: int) -> tuple[bool, str]:
    """Send the 'confirm your address' message."""
    greeting = f"Hi {name}," if name else "Hi,"
    text = (
        f"{greeting}\n\n"
        f"Confirm your email address to finish setting up your "
        f"{config.APP_NAME} account.\n\n"
        f"{verify_url}\n\n"
        f"This link expires in {expires_minutes} minutes and can only be used "
        f"once.\n\n"
        f"If you didn't create an account, you can safely ignore this email — "
        f"nothing happens until the link above is opened.\n"
    )
    # `name` is user-supplied (from the signup form), so it is escaped before
    # going anywhere near the HTML part.
    html = _layout(
        heading="Confirm your email address",
        paragraphs=[
            _esc(greeting),
            f"Confirm your email address to finish setting up your "
            f"{_esc(config.APP_NAME)} account.",
        ],
        cta_label="Confirm email address",
        cta_url=_esc(verify_url),
        footnote=(
            f"This link expires in {int(expires_minutes)} minutes and can only "
            f"be used once.<br><br>"
            f"If you didn't create an account, you can safely ignore this "
            f"email — nothing happens until the link above is opened."
        ),
    )
    return send_email(to, f"Confirm your email for {config.APP_NAME}", text, html)


def send_test_email(to: str) -> tuple[bool, str]:
    """Send a settings-test message so an admin can prove delivery works."""
    delivered_by = (
        f"Resend ({config.MAIL_PROVIDER})"
        if mail_backend() == "http"
        else "SMTP"
    )
    text = (
        f"This is a test message from {config.APP_NAME}.\n\n"
        f"If you're reading it, outbound email is configured correctly and "
        f"signup confirmation emails will be delivered.\n\n"
        f"Sent from: {from_address()}\n"
        f"Delivered via: {delivered_by}\n"
    )
    html = _layout(
        heading="Test email",
        paragraphs=[
            f"This is a test message from {config.APP_NAME}.",
            "If you're reading it, outbound email is configured correctly and "
            "signup confirmation emails will be delivered.",
        ],
        footnote=f"Sent from {from_address()}<br>Delivered via {delivered_by}",
    )
    return send_email(to, f"{config.APP_NAME} test email", text, html)


# ------------------------------------------------------------------ #
#  Provider usage / quota
# ------------------------------------------------------------------ #

def fetch_provider_usage() -> tuple[dict | None, str]:
    """Return the provider's sending quota, or (None, reason).

    Only Resend is supported; their ``GET /usage`` reports the quota that
    actually applies to the key in use, which is more trustworthy than
    counting our own sends (the quota covers mail sent by anything using the
    same account, and counts received mail too).

    Shape returned:
        {
          "provider": "resend",
          "daily":   {"used", "limit", "sent", "received", "resets_at"},
          "monthly": {...},
          "critical": bool,     # >= 80% of any limit
        }
    """
    provider = (config.MAIL_PROVIDER or "").strip().lower()
    # Trust the explicit setting rather than the inferred backend: a half
    # configured SMTP setup should still report "not applicable" here.
    if config.MAIL_BACKEND != "http" or mail_backend() != "http":
        return None, "Quota reporting needs the provider API transport"
    if provider != "resend":
        return None, f"Quota reporting is not implemented for {provider or 'this provider'}"
    if not config.MAIL_API_KEY:
        return None, "No API key configured"

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                "https://api.resend.com/usage",
                headers={"Authorization": f"Bearer {config.MAIL_API_KEY}"},
            )
    except httpx.HTTPError as exc:
        return None, f"Could not reach resend: {type(exc).__name__}"

    if resp.status_code == 401:
        # Resend returns 401 (not 403) for a send-only key, with this exact
        # marker. Quota reporting needs a Full access key; sending does not.
        if "restricted_api_key" in (resp.text or "").lower():
            return None, ("Quota reporting needs a Full access API key — "
                          "the current key is restricted to sending only "
                          "(and sending works fine with it)")
        return None, "Resend rejected the API key (401)"
    if resp.status_code == 403:
        return None, "Resend refused access to usage data (403)"
    if resp.status_code >= 400:
        return None, f"Resend returned HTTP {resp.status_code}"

    try:
        data = resp.json()
    except Exception:
        return None, "Resend returned a non-JSON response"

    emails = (data or {}).get("emails") or {}
    daily = emails.get("daily") or {}
    monthly = emails.get("monthly") or {}
    if not daily and not monthly:
        return None, "Resend returned no email quota information"

    def ratio(section):
        limit, used = section.get("limit"), section.get("used")
        if not isinstance(limit, (int, float)) or not limit:
            return 0.0  # no cap on this plan
        return float(used or 0) / float(limit)

    return {
        "provider": "resend",
        "daily": daily,
        "monthly": monthly,
        "critical": max(ratio(daily), ratio(monthly)) >= 0.8,
    }, ""

