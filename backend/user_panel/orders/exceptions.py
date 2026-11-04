"""Domain errors shared by order-management services."""


class OrderServiceError(Exception):
    """Raised when an order operation violates a business rule."""

    code = "order_error"

    def __init__(self, message=None, *, code=None):
        if code is not None:
            self.code = code
        super().__init__(message or "We could not process your order.")


class EmptyCartError(OrderServiceError):
    code = "empty_cart"


class VariantUnavailableError(OrderServiceError):
    code = "variant_unavailable"


class InsufficientStockError(OrderServiceError):
    code = "insufficient_stock"


class InvalidQuantityError(OrderServiceError):
    code = "invalid_quantity"


class InvalidAddressError(OrderServiceError):
    code = "invalid_address"


class InvalidReviewTokenError(OrderServiceError):
    code = "invalid_review_token"


class ExpiredReviewTokenError(OrderServiceError):
    code = "expired_review_token"


class UnexpectedOrderFailure(OrderServiceError):
    code = "unexpected_failure"


class OrderNotFoundError(OrderServiceError):
    code = "order_not_found"


class OrderPermissionError(OrderServiceError):
    code = "order_permission_denied"


class AdminPermissionError(OrderServiceError):
    code = "admin_permission_denied"


class InvalidOrderStatusError(OrderServiceError):
    code = "invalid_order_status"


class InvalidOrderStatusTransitionError(OrderServiceError):
    code = "invalid_order_status_transition"


class OrderItemNotFoundError(OrderServiceError):
    code = "order_item_not_found"


class ItemNotCancellableError(OrderServiceError):
    code = "item_not_cancellable"


class ItemAlreadyCancelledError(OrderServiceError):
    code = "item_already_cancelled"


class ReturnNotAllowedError(OrderServiceError):
    code = "return_not_allowed"


class ReturnReasonRequiredError(OrderServiceError):
    code = "return_reason_required"


class ReturnAlreadyRequestedError(OrderServiceError):
    code = "return_already_requested"


class ReturnNotFoundError(OrderServiceError):
    code = "return_not_found"


class ReturnAlreadyDecidedError(OrderServiceError):
    code = "return_already_decided"


class ReturnDecisionNoteRequiredError(OrderServiceError):
    code = "return_decision_note_required"


class ReturnQuantityMismatchError(OrderServiceError):
    code = "return_quantity_mismatch"


class ReturnIntegrityError(OrderServiceError):
    code = "return_integrity_error"
