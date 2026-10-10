from django.apps import AppConfig


class PrivacyConfig(AppConfig):
    """Personal-data operations across every CRM module (Phase 11; docs/privacy.md)."""

    name = "arkray.privacy"
    label = "privacy"
    verbose_name = "Privacy"

    def ready(self) -> None:
        from . import exports  # noqa: F401 — registers the export job
