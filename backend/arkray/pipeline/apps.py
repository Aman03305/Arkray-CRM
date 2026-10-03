from django.apps import AppConfig


class PipelineConfig(AppConfig):
    name = "arkray.pipeline"
    label = "pipeline"
    verbose_name = "Pipeline"

    def ready(self) -> None:
        from . import subscribers  # noqa: F401 — subscribes to the lead domain events
