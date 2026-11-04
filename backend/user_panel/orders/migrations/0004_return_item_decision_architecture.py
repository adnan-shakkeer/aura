import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError


def _generate_request_number(created_at):
    stamp = (created_at or django.utils.timezone.now()).strftime("%Y%m%d")
    return f"RET-{stamp}-{uuid.uuid4().hex[:8].upper()}"


def backfill_return_item_decisions(apps, schema_editor):
    ReturnRequest = apps.get_model("orders", "ReturnRequest")
    ReturnItem = apps.get_model("orders", "ReturnItem")

    existing_numbers = set(
        ReturnRequest.objects.exclude(request_number__isnull=True)
        .values_list("request_number", flat=True)
    )

    for return_request in ReturnRequest.objects.order_by("pk").iterator():
        items = list(
            ReturnItem.objects.filter(
                return_request_id=return_request.pk
            ).order_by("pk")
        )
        if not items:
            raise RuntimeError(
                "Cannot migrate return request "
                f"{return_request.pk}: it has no return items."
            )
        if return_request.status == "APPROVED" and len(items) > 1:
            raise RuntimeError(
                "Cannot migrate approved return request "
                f"{return_request.pk}: multiple items make the restored stock "
                "ambiguous."
            )

        for item in items:
            item.reason = return_request.reason
            item.status = return_request.status
            item.decided_at = return_request.decided_at
            item.decided_by_id = return_request.decided_by_id
            item.decision_note = return_request.decision_note
            item.requested_at = return_request.requested_at
            if return_request.status == "APPROVED":
                item.stock_restored_at = (
                    return_request.decided_at
                    or return_request.updated_at
                )
            else:
                item.stock_restored_at = None
            item.save(
                update_fields=[
                    "reason",
                    "status",
                    "decided_at",
                    "decided_by",
                    "decision_note",
                    "requested_at",
                    "stock_restored_at",
                ]
            )

        request_number = _generate_request_number(
            return_request.requested_at
        )
        while request_number in existing_numbers:
            request_number = _generate_request_number(
                return_request.requested_at
            )
        existing_numbers.add(request_number)
        return_request.request_number = request_number
        return_request.save(update_fields=["request_number"])


def restore_legacy_return_fields(apps, schema_editor):
    ReturnRequest = apps.get_model("orders", "ReturnRequest")
    ReturnItem = apps.get_model("orders", "ReturnItem")

    for return_request in ReturnRequest.objects.order_by("pk").iterator():
        items = list(
            ReturnItem.objects.filter(
                return_request_id=return_request.pk
            ).order_by("pk")
        )
        if not items:
            continue

        states = {
            (
                item.reason,
                item.status,
                item.decided_at,
                item.decided_by_id,
                item.decision_note,
            )
            for item in items
        }
        if len(states) > 1:
            raise IrreversibleError(
                "Cannot reverse return request "
                f"{return_request.pk}: item-level decisions differ and "
                "cannot be represented at request level."
            )

        item = items[0]
        return_request.reason = item.reason
        return_request.status = item.status
        return_request.decided_at = item.decided_at
        return_request.decided_by_id = item.decided_by_id
        return_request.decision_note = item.decision_note
        return_request.save(
            update_fields=[
                "reason",
                "status",
                "decided_at",
                "decided_by",
                "decision_note",
            ]
        )


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0003_phase6_1_order_management"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameField(
            model_name="returnitem",
            old_name="created_at",
            new_name="requested_at",
        ),
        migrations.AddField(
            model_name="returnitem",
            name="reason",
            field=models.TextField(default=""),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="returnitem",
            name="status",
            field=models.CharField(
                choices=[
                    ("REQUESTED", "Requested"),
                    ("APPROVED", "Approved"),
                    ("REJECTED", "Rejected"),
                ],
                default="REQUESTED",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="returnitem",
            name="decided_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="returnitem",
            name="decided_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="return_item_decisions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="returnitem",
            name="decision_note",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="returnitem",
            name="stock_restored_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="returnrequest",
            name="request_number",
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=32,
                null=True,
            ),
        ),
        migrations.RunPython(
            backfill_return_item_decisions,
            restore_legacy_return_fields,
        ),
        migrations.AlterField(
            model_name="returnrequest",
            name="request_number",
            field=models.CharField(
                editable=False,
                max_length=32,
                unique=True,
            ),
        ),
        migrations.RemoveIndex(
            model_name="returnrequest",
            name="orders_return_order_status_idx",
        ),
        migrations.AlterField(
            model_name="returnrequest",
            name="reason",
            field=models.TextField(default=""),
        ),
        migrations.RemoveField(
            model_name="returnrequest",
            name="reason",
        ),
        migrations.RemoveField(
            model_name="returnrequest",
            name="status",
        ),
        migrations.RemoveField(
            model_name="returnrequest",
            name="decided_at",
        ),
        migrations.RemoveField(
            model_name="returnrequest",
            name="decided_by",
        ),
        migrations.RemoveField(
            model_name="returnrequest",
            name="decision_note",
        ),
        migrations.AlterField(
            model_name="returnrequest",
            name="updated_at",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.RemoveField(
            model_name="returnrequest",
            name="updated_at",
        ),
        migrations.RenameField(
            model_name="returnrequest",
            old_name="requested_at",
            new_name="created_at",
        ),
        migrations.AlterModelOptions(
            name="returnrequest",
            options={
                "ordering": ["-created_at", "-id"],
                "verbose_name": "Return request",
                "verbose_name_plural": "Return requests",
            },
        ),
        migrations.AddIndex(
            model_name="returnitem",
            index=models.Index(
                fields=["status", "requested_at"],
                name="orders_return_item_status_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="returnitem",
            constraint=models.CheckConstraint(
                condition=~models.Q(reason=""),
                name="return_item_reason_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="returnitem",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        status="REQUESTED",
                        decided_at__isnull=True,
                        decided_by__isnull=True,
                        decision_note="",
                        stock_restored_at__isnull=True,
                    )
                    | ~models.Q(status="REQUESTED")
                ),
                name="return_item_requested_has_no_decision",
            ),
        ),
        migrations.AddConstraint(
            model_name="returnitem",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(status__in=["APPROVED", "REJECTED"])
                    | models.Q(decided_at__isnull=False)
                ),
                name="return_item_decided_has_timestamp",
            ),
        ),
        migrations.AddConstraint(
            model_name="returnitem",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(status="REJECTED")
                    | ~models.Q(decision_note="")
                ),
                name="return_item_rejected_note_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="returnitem",
            constraint=models.CheckConstraint(
                condition=(
                    (
                        models.Q(status="APPROVED")
                        & models.Q(stock_restored_at__isnull=False)
                    )
                    | (
                        ~models.Q(status="APPROVED")
                        & models.Q(stock_restored_at__isnull=True)
                    )
                ),
                name="return_item_stock_restored_only_approved",
            ),
        ),
    ]
