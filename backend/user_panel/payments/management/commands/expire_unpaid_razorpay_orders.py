from django.core.management.base import BaseCommand

from user_panel.payments.services import expire_unpaid_orders


class Command(BaseCommand):
    help = (
        "Cancel unpaid Razorpay orders whose 5-minute payment window has "
        "expired and release their reserved stock and coupon usage."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Process at most this many expired orders.",
        )

    def handle(self, *args, **options):
        limit = options.get("limit")
        if limit is not None and limit < 1:
            self.stderr.write(self.style.ERROR("--limit must be at least 1."))
            return

        expired = expire_unpaid_orders(limit=limit)
        self.stdout.write(
            self.style.SUCCESS(
                f"Expired {expired} unpaid Razorpay order(s)."
            )
        )
