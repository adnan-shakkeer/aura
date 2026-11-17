from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

OFFER_MIN = Decimal("0.00")
OFFER_MAX = Decimal("99.00")
OFFER_QUANTUM = Decimal("0.01")


def parse_offer_percentage(raw_value, *, allow_zero=True):
    text = str(raw_value or "").strip()

    if not text:
        return OFFER_MIN, None

    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None, "Please enter a valid offer percentage."

    minimum = OFFER_MIN if allow_zero else OFFER_QUANTUM

    if not value.is_finite() or value < minimum or value > OFFER_MAX:
        if allow_zero:
            return None, "Offer must be between 0% and 99%."
        return None, "Offer must be between 0.01% and 99%."

    try:
        quantized = value.quantize(
            OFFER_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
    except InvalidOperation:
        return None, "Offer can have at most 2 decimal places."

    if value != quantized:
        return None, "Offer can have at most 2 decimal places."

    return quantized, None
