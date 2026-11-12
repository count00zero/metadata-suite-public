"""textde.py -- small German time/season vocabulary for caption context.

SHARED FILE: identical copies live in geo-service/app/ and geo-service/clients/
(tests/test_shared_identical.py guards that).
"""
from __future__ import annotations

MONTHS_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
             "September", "Oktober", "November", "Dezember"]
WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def month_de(month: int) -> str:
    return MONTHS_DE[(month - 1) % 12]


def season_de(month: int) -> str:
    return ("Winter", "Winter", "Frühling", "Frühling", "Frühling", "Sommer",
            "Sommer", "Sommer", "Herbst", "Herbst", "Herbst", "Winter")[(month - 1) % 12]


def daypart_de(hour: int) -> str:
    """Part of the day, in the form used after 'am'/'in der': 'Nachmittag', 'Nacht', ..."""
    if hour < 5 or hour >= 22:
        return "Nacht"
    if hour < 9:
        return "Morgen"
    if hour < 12:
        return "Vormittag"
    if hour < 14:
        return "Mittag"
    if hour < 18:
        return "Nachmittag"
    return "Abend"


def daypart_adverb_de(hour: int) -> str:
    """'nachmittags', 'nachts', ... (reads naturally in a sentence)."""
    return {"Nacht": "nachts", "Morgen": "morgens", "Vormittag": "vormittags",
            "Mittag": "mittags", "Nachmittag": "nachmittags", "Abend": "abends"}[daypart_de(hour)]
