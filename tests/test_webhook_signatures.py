"""Tests for app/core/webhook_signatures.py (notification-feature spec §14)."""
import base64
import hashlib
import hmac

from app.core.webhook_signatures import verify_meta_signature, verify_twilio_signature


def test_meta_signature_valid():
    secret = "app_secret_123"
    body = b'{"entry": []}'
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    header = f"sha256={digest}"
    assert verify_meta_signature(body, header, secret) is True


def test_meta_signature_invalid_when_tampered():
    secret = "app_secret_123"
    body = b'{"entry": []}'
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    header = f"sha256={digest}"
    tampered_body = b'{"entry": ["forged"]}'
    assert verify_meta_signature(tampered_body, header, secret) is False


def test_meta_signature_missing_header():
    assert verify_meta_signature(b"data", None, "secret") is False


def test_meta_signature_wrong_secret():
    body = b'{"entry": []}'
    digest = hmac.new(b"real_secret", body, hashlib.sha256).hexdigest()
    header = f"sha256={digest}"
    assert verify_meta_signature(body, header, "wrong_secret") is False


def test_twilio_signature_valid():
    auth_token = "twilio_token_abc"
    url = "https://example.com/api/notifications/sms/inbound"
    params = {"From": "+15551234567", "Body": "APPROVE EURUSD SIGNAL-1"}

    data = url
    for key in sorted(params.keys()):
        data += key + params[key]
    expected_digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    signature = base64.b64encode(expected_digest).decode()

    assert verify_twilio_signature(url, params, signature, auth_token) is True


def test_twilio_signature_invalid_when_params_tampered():
    auth_token = "twilio_token_abc"
    url = "https://example.com/api/notifications/sms/inbound"
    original_params = {"From": "+15551234567", "Body": "APPROVE EURUSD SIGNAL-1"}

    data = url
    for key in sorted(original_params.keys()):
        data += key + original_params[key]
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    signature = base64.b64encode(digest).decode()

    forged_params = {"From": "+15551234567", "Body": "APPROVE EURUSD SIGNAL-999"}
    assert verify_twilio_signature(url, forged_params, signature, auth_token) is False


def test_twilio_signature_missing_header():
    assert verify_twilio_signature("https://x.com", {}, None, "token") is False
