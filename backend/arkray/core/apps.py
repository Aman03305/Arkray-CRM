from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "arkray.core"
    label = "core"
    verbose_name = "Core"

    def ready(self) -> None:
        from . import checks, schema  # noqa: F401 — registers checks, OpenAPI extensions
