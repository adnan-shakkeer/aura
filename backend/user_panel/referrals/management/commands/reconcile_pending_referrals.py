from django.core.management.base import BaseCommand

from user_panel.referrals.models import Referral
from user_panel.referrals.services import (
    REWARD_STATUS_REWARDED,
    find_first_qualifying_order,
    process_referral_reward,
)


class Command(BaseCommand):
    help = (
        "Reconcile pending referrals by checking each referred customer's "
        "first qualifying order and crediting any missing wallet rewards."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be rewarded without changing data.",
        )
        parser.add_argument(
            "--referral-id",
            type=int,
            default=None,
            help="Reconcile only this pending referral.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Process at most this many pending referrals.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        referral_id = options["referral_id"]
        limit = options["limit"]

        if limit is not None and limit < 1:
            self.stderr.write(self.style.ERROR("--limit must be at least 1."))
            return

        referrals = (
            Referral.objects
            .filter(status=Referral.Status.PENDING)
            .select_related("referred_user")
            .order_by("created_at", "id")
        )
        if referral_id is not None:
            referrals = referrals.filter(pk=referral_id)
        if limit is not None:
            referrals = referrals[:limit]

        referrals = list(referrals)
        if referral_id is not None and not referrals:
            self.stdout.write(
                self.style.WARNING(
                    f"Referral #{referral_id} is not pending or does not exist."
                )
            )
            return

        rewarded = 0
        skipped = 0
        failed = 0

        for referral in referrals:
            if dry_run:
                order = find_first_qualifying_order(referral.referred_user)
                if order is None:
                    skipped += 1
                    self.stdout.write(
                        f"SKIPPED referral #{referral.pk}: "
                        "no qualifying order found."
                    )
                else:
                    rewarded += 1
                    self.stdout.write(
                        f"REWARDED referral #{referral.pk}: "
                        f"would credit for order {order.order_number}."
                    )
                continue

            try:
                order = find_first_qualifying_order(referral.referred_user)
                if order is None:
                    skipped += 1
                    self.stdout.write(
                        f"SKIPPED referral #{referral.pk}: "
                        "no qualifying order found."
                    )
                    continue

                result = process_referral_reward(order)
                if result.status == REWARD_STATUS_REWARDED:
                    rewarded += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"REWARDED referral #{referral.pk}: "
                            f"{result.message}"
                        )
                    )
                else:
                    skipped += 1
                    self.stdout.write(
                        f"SKIPPED referral #{referral.pk}: "
                        f"{result.message}"
                    )
            except Exception as exc:
                failed += 1
                self.stderr.write(
                    self.style.ERROR(
                        f"FAILED referral #{referral.pk}: {exc}"
                    )
                )

        summary = (
            f"Processed {len(referrals)} pending referral(s): "
            f"{rewarded} rewarded, {skipped} skipped, {failed} failed."
        )
        if dry_run:
            summary = f"Dry run: {summary}"

        self.stdout.write(self.style.SUCCESS(summary))
