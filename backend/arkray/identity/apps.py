from django.apps import AppConfig


class IdentityConfig(AppConfig):
    name = "arkray.identity"
    label = "identity"
    verbose_name = "Identity"

    def ready(self) -> None:
        from . import handlers  # noqa: F401 — registers outbox handlers
