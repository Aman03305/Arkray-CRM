#!/usr/bin/env python
import sys


def main() -> None:
    # The development settings when nothing says otherwise (docs/development.md); in the
    # production image (ARKRAY_RUNTIME=production) production's, and nothing else
    # (config/settings_module.py; adversarial review of 73ff1e3, L14).
    from config.settings_module import LOCAL, choose_settings_module

    choose_settings_module(LOCAL)
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
