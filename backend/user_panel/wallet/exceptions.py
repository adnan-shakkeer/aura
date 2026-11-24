class WalletError(Exception):
    """Base exception for expected wallet failures."""

    code = "wallet_error"

    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code:
            self.code = code


class WalletNotFoundError(WalletError):
    code = "wallet_not_found"


class WalletInvalidAmountError(WalletError):
    code = "wallet_invalid_amount"


class WalletInvalidKindError(WalletError):
    code = "wallet_invalid_kind"


class WalletInsufficientBalanceError(WalletError):
    code = "wallet_insufficient_balance"


class WalletOwnershipError(WalletError):
    code = "wallet_ownership_error"


class WalletDuplicateTransactionError(WalletError):
    code = "wallet_duplicate_transaction"


class WalletTopUpAmountError(WalletError):
    code = "wallet_topup_amount_error"


class WalletTopUpNotFoundError(WalletError):
    code = "wallet_topup_not_found"


class WalletTopUpStateError(WalletError):
    code = "wallet_topup_state_error"


class WalletTopUpVerificationError(WalletError):
    code = "wallet_topup_verification_error"
