class ReferralError(Exception):
    code = "referral_error"


class InvalidReferralCodeError(ReferralError):
    code = "invalid_referral_code"


class ReferralCodeGenerationError(ReferralError):
    code = "referral_code_generation_error"
