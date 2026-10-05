from django.apps import AppConfig


class ActivitiesConfig(AppConfig):
    name = "arkray.activities"
    label = "activities"
    verbose_name = "Activities"

    def ready(self) -> None:
        # Subscribes to the lead and pipeline domain events (current work follows a
        # reassigned lead; the timeline records lead and opportunity history). Registered
        # after the pipeline's own subscribers (INSTALLED_APPS order), which keeps the lock
        # order lead -> opportunities -> activities inside a reassignment.
        from . import handlers, subscribers, tasks  # noqa: F401
