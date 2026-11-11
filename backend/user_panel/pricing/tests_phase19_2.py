"""Phase 19.2 coupon arithmetic and allocation tests."""

from decimal import Decimal

from django.test import SimpleTestCase

from .services import (
    COUPON_TYPE_FIXED,
    COUPON_TYPE_PERCENTAGE,
    OFFER_SOURCE_NONE,
    LinePricing,
    allocate_coupon_discount,
    calculate_coupon_discount,
    eligible_merchandise_after_offers,
    summarize,
)


ZERO = Decimal("0.00")


def make_line(
    *,
    variant_id=1,
    quantity=1,
    unit_price="1000.00",
    offer_discount="0.00",
    coupon_discount="0.00",
):
    unit_price = Decimal(unit_price)
    line_total = unit_price * quantity
    offer_discount = Decimal(offer_discount)
    coupon_discount = Decimal(coupon_discount)
    return LinePricing(
        variant_id=variant_id,
        quantity=quantity,
        unit_price=unit_price,
        line_total=line_total,
        offer_source=OFFER_SOURCE_NONE,
        offer_percentage=ZERO,
        offer_discount=offer_discount,
        coupon_discount=coupon_discount,
        paid_amount=line_total - offer_discount - coupon_discount,
    )


class CouponDiscountCalculationTests(SimpleTestCase):
    def test_percentage_discount_is_capped(self):
        discount = calculate_coupon_discount(
            discount_type=COUPON_TYPE_PERCENTAGE,
            discount_value=Decimal("20.00"),
            max_discount_amount=Decimal("150.00"),
            eligible_merchandise=Decimal("1000.00"),
        )

        self.assertEqual(discount, Decimal("150.00"))

    def test_percentage_discount_uses_post_offer_merchandise(self):
        discount = calculate_coupon_discount(
            discount_type=COUPON_TYPE_PERCENTAGE,
            discount_value=Decimal("10.00"),
            max_discount_amount=Decimal("500.00"),
            eligible_merchandise=Decimal("800.00"),
        )

        self.assertEqual(discount, Decimal("80.00"))

    def test_fixed_discount_cannot_exceed_eligible_merchandise(self):
        discount = calculate_coupon_discount(
            discount_type=COUPON_TYPE_FIXED,
            discount_value=Decimal("500.00"),
            max_discount_amount=None,
            eligible_merchandise=Decimal("300.00"),
        )

        self.assertEqual(discount, Decimal("300.00"))

    def test_zero_or_invalid_inputs_return_zero(self):
        self.assertEqual(
            calculate_coupon_discount(
                COUPON_TYPE_PERCENTAGE,
                Decimal("10.00"),
                Decimal("100.00"),
                ZERO,
            ),
            ZERO,
        )
        self.assertEqual(
            calculate_coupon_discount(
                "UNKNOWN",
                Decimal("10.00"),
                Decimal("100.00"),
                Decimal("500.00"),
            ),
            ZERO,
        )


class CouponAllocationTests(SimpleTestCase):
    def test_allocation_is_proportional_and_sums_exactly(self):
        lines = [
            make_line(variant_id=1, unit_price="1000.00"),
            make_line(variant_id=2, unit_price="2000.00"),
            make_line(variant_id=3, unit_price="3000.00"),
        ]

        allocations = allocate_coupon_discount(Decimal("333.33"), lines)

        self.assertEqual(sum(allocations), Decimal("333.33"))
        self.assertEqual(allocations[0], Decimal("55.56"))
        self.assertEqual(allocations[1], Decimal("92.59"))
        self.assertEqual(allocations[2], Decimal("185.18"))

    def test_allocation_uses_post_offer_values(self):
        lines = [
            make_line(
                variant_id=1,
                unit_price="1000.00",
                offer_discount="500.00",
            ),
            make_line(variant_id=2, unit_price="1000.00"),
        ]

        allocations = allocate_coupon_discount(Decimal("300.00"), lines)

        self.assertEqual(allocations[0], Decimal("100.00"))
        self.assertEqual(allocations[1], Decimal("200.00"))
        self.assertEqual(sum(allocations), Decimal("300.00"))

    def test_allocation_never_exceeds_a_line_eligible_amount(self):
        lines = [
            make_line(
                variant_id=1,
                unit_price="100.00",
                offer_discount="90.00",
            ),
            make_line(variant_id=2, unit_price="1000.00"),
        ]

        allocations = allocate_coupon_discount(Decimal("500.00"), lines)

        self.assertLessEqual(allocations[0], Decimal("10.00"))
        self.assertEqual(sum(allocations), Decimal("500.00"))

    def test_summarize_includes_coupon_and_offer_totals(self):
        lines = [
            make_line(
                variant_id=1,
                unit_price="1000.00",
                offer_discount="100.00",
                coupon_discount="50.00",
            ),
            make_line(
                variant_id=2,
                unit_price="500.00",
                coupon_discount="25.00",
            ),
        ]

        totals = summarize(lines)

        self.assertEqual(totals["subtotal"], Decimal("1500.00"))
        self.assertEqual(
            totals["offer_discount_total"], Decimal("100.00")
        )
        self.assertEqual(
            totals["coupon_discount_total"], Decimal("75.00")
        )
        self.assertEqual(totals["discount_total"], Decimal("175.00"))
        self.assertEqual(totals["total"], Decimal("1325.00"))
        self.assertEqual(
            eligible_merchandise_after_offers(lines),
            Decimal("1400.00"),
        )
