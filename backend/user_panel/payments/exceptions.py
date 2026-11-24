class PaymentError(Exception):
    """Base exception for expected payment failures."""

    code = "payment_error"

    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code:
            self.code = code


class PaymentConfigurationError(PaymentError):
    code = "payment_configuration_error"


class PaymentGatewayError(PaymentError):
    code = "payment_gateway_error"


class PaymentVerificationError(PaymentError):
    code = "payment_verification_error"


class PaymentNotAllowedError(PaymentError):
    code = "payment_not_allowed"


class PaymentAttemptNotFoundError(PaymentError):
    code = "payment_attempt_not_found"
