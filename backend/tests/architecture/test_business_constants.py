"""The business time zone and currency are fixed, and the web app and the API must agree on
them (whole-software audit, P2).

The backend computes "today", due-date boundaries and Ask Arkray's dates in
`CRM_TIME_ZONE` and labels amounts in `CRM_CURRENCY`; the web app formats every date in a
hard-coded zone and every amount with a hard-coded symbol. When the backend's values came
from the environment, a deployment could change one side only: a meeting typed for 10:00
was stored for another hour, the dashboard's "today" and the lists' "today" differed, and
Ask Arkray said "USD" where the screens said "₹".
"""

import re
from pathlib import Path

from django.conf import settings

FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src"
ZONE = re.compile(
    r"""["']((?:Africa|America|Asia|Atlantic|Australia|Europe|Indian|Pacific|Etc)/[A-Za-z_+-]+)["']"""
)
SYMBOLS = {"INR": "₹"}


def sources() -> list[Path]:
    return [
        path
        for path in FRONTEND.rglob("*.ts*")
        if not path.name.endswith((".test.ts", ".test.tsx", ".gen.ts"))
    ]


def test_the_business_settings_are_not_read_from_the_environment():
    base = (Path(__file__).resolve().parents[2] / "config" / "settings" / "base.py").read_text(
        encoding="utf-8"
    )
    assert 'CRM_TIME_ZONE = "Asia/Kolkata"' in base
    assert 'CRM_CURRENCY = "INR"' in base
    assert not re.search(r"env[.(\w]*\(\s*[\"']CRM_(TIME_ZONE|CURRENCY)", base)


def test_every_time_zone_the_web_app_names_is_the_business_one():
    named = {
        (path.relative_to(FRONTEND).as_posix(), zone)
        for path in sources()
        for zone in ZONE.findall(path.read_text(encoding="utf-8"))
    }
    assert named, "the scan found no time zone at all: it is broken"
    assert {zone for _, zone in named} == {settings.CRM_TIME_ZONE}, sorted(named)


def test_the_web_app_formats_amounts_in_the_business_currency():
    money = (FRONTEND / "lib" / "money.ts").read_text(encoding="utf-8")
    symbol = SYMBOLS[settings.CRM_CURRENCY]
    assert f"${{sign}}{symbol}" in money  # formatInr's output
    others = {"€", "£", "¥", "USD", "EUR"} - {symbol}
    assert not any(other in money for other in others)
