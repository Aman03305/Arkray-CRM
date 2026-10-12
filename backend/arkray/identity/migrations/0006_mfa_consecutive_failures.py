# Deep security audit M-4: wrong second-factor codes accumulate across sign-in attempts.

from django.db import migrations, models

from arkray.core.migrations._reverse_guard import RefuseReverse


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0005_mfa"),
    ]

    operations = [
        # db_default: the previous release (which never names the column) can still insert.
        migrations.AddField(
            model_name="mfadevice",
            name="consecutive_failures",
            field=models.PositiveIntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name="mfadevice",
            name="failure_notified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        # Last, so it runs first when reversing (docs/deployment.md#rollback).
        RefuseReverse(
            "identity.0006_mfa_consecutive_failures",
            "the count of wrong second-factor codes that keeps a guessed account locked",
        ),
    ]
