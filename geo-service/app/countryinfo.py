"""countryinfo.py -- a short German 'about this country' line from GeoNames' countryInfo.txt (your GeoNames volume)."""
from __future__ import annotations

LANG_DE = {"de": "Deutsch", "en": "Englisch", "fr": "Französisch", "it": "Italienisch", "es": "Spanisch", "pt": "Portugiesisch",
           "nl": "Niederländisch", "sv": "Schwedisch", "no": "Norwegisch", "nb": "Norwegisch", "da": "Dänisch", "fi": "Finnisch",
           "pl": "Polnisch", "cs": "Tschechisch", "sk": "Slowakisch", "hu": "Ungarisch", "sl": "Slowenisch", "hr": "Kroatisch",
           "sr": "Serbisch", "bs": "Bosnisch", "ro": "Rumänisch", "bg": "Bulgarisch", "el": "Griechisch", "tr": "Türkisch",
           "ru": "Russisch", "uk": "Ukrainisch", "ar": "Arabisch", "he": "Hebräisch", "fa": "Persisch", "hi": "Hindi",
           "ur": "Urdu", "bn": "Bengalisch", "si": "Singhalesisch", "ta": "Tamil", "th": "Thailändisch", "vi": "Vietnamesisch",
           "id": "Indonesisch", "ms": "Malaiisch", "zh": "Chinesisch", "ja": "Japanisch", "ko": "Koreanisch", "sw": "Suaheli",
           "af": "Afrikaans", "dv": "Dhivehi", "ne": "Nepali", "km": "Khmer", "lo": "Laotisch", "my": "Birmanisch",
           "sq": "Albanisch", "mk": "Mazedonisch", "lt": "Litauisch", "lv": "Lettisch", "et": "Estnisch", "is": "Isländisch",
           "ga": "Irisch", "mt": "Maltesisch", "ka": "Georgisch", "hy": "Armenisch", "kk": "Kasachisch", "uz": "Usbekisch",
           "mn": "Mongolisch", "am": "Amharisch", "fil": "Filipino", "tl": "Tagalog", "lb": "Luxemburgisch"}
CONTINENT_DE = {"EU": "Europa", "AS": "Asien", "AF": "Afrika", "NA": "Nordamerika", "SA": "Südamerika", "OC": "Ozeanien",
                "AN": "Antarktis"}


def languages_de(raw: str, limit: int = 3) -> list:
    out = []
    for item in (raw or "").split(","):
        code = item.strip().split("-")[0].lower()
        name = LANG_DE.get(code)
        if name and name not in out:
            out.append(name)
    return out[:limit]


def describe(row: dict, country_name: str = "") -> dict:
    """row = Countries.rows[ISO] -> {'name', 'capital', 'currency', 'languages', 'continent', 'text_de'}."""
    cap, cur = row.get("Capital", ""), row.get("CurrencyCode", "")
    langs = languages_de(row.get("Languages", ""))
    bits = []
    if cap:
        bits.append(f"Hauptstadt {cap}")
    if cur:
        bits.append(f"Währung {cur}")
    if langs:
        bits.append("Sprachen: " + ", ".join(langs))
    name = country_name or row.get("Country", "")
    return {"name": name, "capital": cap, "currency": cur, "languages": langs,
            "continent": CONTINENT_DE.get(row.get("Continent", ""), ""), "text_de": ", ".join(bits)}
