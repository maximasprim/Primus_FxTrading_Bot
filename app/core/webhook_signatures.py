"""
Inbound webhook signature verification (notification-feature spec §14: "The
system must prevent... forged webhook requests").

Two schemes implemented, matching the two inbound webhooks in
app/api/routes/notifications.py:

  - Meta (WhatsApp Cloud API): HMAC-SHA256 over the raw request body, keyed
    by the app secret, sent as `X-Hub-Signature-256: sha256=<hex>`.
  - Twilio (SMS): HMAC-SHA1 over the full webhook URL + sorted form params
    (concatenated, not separately hashed), keyed by the auth token,
    base64-encoded, sent as `X-Twilio-Signature`.

Both comparisons use hmac.compare_digest to avoid timing attacks.
"""
import base64
import hashlib
import hmac


def verify_meta_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """`signature_header` is the raw `X-Hub-Signature-256` header value,
    e.g. 'sha256=abcdef...'. Returns False (not an exception) on any
    malformed input, missing header, or mismatch -- callers should treat
    False as "reject the request."
    """
    if not signature_header or not app_secret:
        return False
    if not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def verify_twilio_signature(
    full_url: str, form_params: dict[str, str], signature_header: str | None, auth_token: str
) -> bool:
    """Twilio's scheme: sort params by key, concatenate key+value pairs
    directly onto the URL (no separators), HMAC-SHA1 with the auth token,
    base64-encode. See Twilio's request validation docs for the exact spec.
    """
    if not signature_header or not auth_token:
        return False

    data = full_url
    for key in sorted(form_params.keys()):
        data += key + form_params[key]

    digest = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature_header)
