from django.apps import AppConfig


class PipelineConfig(AppConfig):
    name = "arkray.pipeline"
    label = "pipeline"
    verbose_name = "Pipeline"

    def ready(self) -> None:
        from . import field_values, subscribers  # noqa: F401 — outbox handler, lead events
