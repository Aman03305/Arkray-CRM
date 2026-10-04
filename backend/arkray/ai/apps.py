from django.apps import AppConfig


class AiConfig(AppConfig):
    name = "arkray.ai"
    label = "ai"
    verbose_name = "Ask Arkray"

    def ready(self) -> None:
        # Outbox handlers (indexing) and domain-event subscribers (enqueue re-indexing).
        from . import handlers, subscribers  # noqa: F401
