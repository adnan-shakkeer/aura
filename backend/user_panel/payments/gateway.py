"""Thin server-side wrapper around the official Razorpay Python SDK."""

from django.conf import settings

from .exceptions import (
    PaymentConfigurationError,
    PaymentGatewayError,
    PaymentVerificationError,
)


def _credentials():
    key_id = (settings.RAZORPAY_KEY_ID or "").strip()
    key_secret = (settings.RAZORPAY_KEY_SECRET or "").strip()
    if not key_id or not key_secret:
        raise PaymentConfigurationError(
            "Razorpay is not configured. Please contact the store owner."
        )
    return key_id, key_secret


def get_client():
    key_id, key_secret = _credentials()

    try:
        import razorpay
    except ImportError as exc:
        raise PaymentConfigurationError(
            "The Razorpay SDK is not installed."
        ) from exc

    return razorpay.Client(auth=(key_id, key_secret))


def create_order(*, amount_paise, receipt, notes=None):
    client = get_client()

    payload = {
        "amount": int(amount_paise),
        "currency": settings.RAZORPAY_CURRENCY,
        "receipt": receipt,
        "payment_capture": 1,
    }
    if notes:
        payload["notes"] = notes

    try:
        return client.order.create(data=payload)
    except Exception as exc:
        raise PaymentGatewayError(
            "We could not start the online payment. Please try again."
        ) from exc


def fetch_payment(payment_id):
    client = get_client()

    try:
        return client.payment.fetch(payment_id)
    except Exception as exc:
        raise PaymentGatewayError(
            "We could not confirm the online payment. Please try again."
        ) from exc


def fetch_order_payments(provider_order_id):
    client = get_client()

    try:
        response = client.order.payments(provider_order_id)
    except Exception as exc:
        raise PaymentGatewayError(
            "We could not confirm the online payment. Please try again."
        ) from exc

    if isinstance(response, dict):
        return response.get("items") or []
    return response or []


def capture_payment(payment_id, amount_paise, currency="INR"):
    client = get_client()

    try:
        return client.payment.capture(
            payment_id,
            int(amount_paise),
            {"currency": currency},
        )
    except Exception as exc:
        raise PaymentGatewayError(
            "We could not capture the online payment. Please try again."
        ) from exc


def verify_payment_signature(
    *,
    provider_order_id,
    payment_id,
    signature,
):
    client = get_client()

    try:
        client.utility.verify_payment_signature(
            {
                "razorpay_order_id": provider_order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": signature,
            }
        )
    except Exception as exc:
        raise PaymentVerificationError(
            "The payment signature could not be verified."
        ) from exc

    return True
