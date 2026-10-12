# R133 (independent review): wrong second-factor codes also accumulate where a new password
# doesn't clear them, up to a cap that only an MFA reset lifts.

from django.db import migrations, models

from arkray.core.migrations._reverse_guard import RefuseReverse


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0006_mfa_consecutive_failures"),
    ]

    operations = [
        # db_default: the previous release (which never names the column) can still insert.
        migrations.AddField(
            model_name="mfadevice",
            name="cumulative_failures",
            field=models.PositiveIntegerField(db_default=0, default=0),
        ),
        # Last, so it runs first when reversing (docs/deployment.md#rollback).
        RefuseReverse(
            "identity.0007_mfa_cumulative_failures",
            "the count of wrong second-factor codes that keeps a factor locked until an MFA reset",
        ),
    ]
