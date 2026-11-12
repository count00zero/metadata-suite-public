"""Mini GeoNames dumps for the gazetteer tests (same column layout as the real files)."""
import os
import tempfile
import zipfile


def place(gid, name, lat, lng, fcl, fcode, cc, a1="", a2="", a3="", a4="", pop=0, alt="", tz="Europe/Vienna"):
    return "\t".join([str(gid), name, name, alt, str(lat), str(lng), fcl, fcode, cc, "", a1, a2, a3, a4, str(pop), "", "", tz, "2024-01-01"])


PLACES = [
    place(2782113, "Republic of Austria", 47.33, 13.33, "A", "PCLI", "AT", pop=8205000),
    place(2770542, "Lower Austria", 48.25, 15.75, "A", "ADM1", "AT", "03", pop=1600000),
    place(2781503, "Bezirk Baden", 47.98, 16.25, "A", "ADM2", "AT", "03", "307", pop=140000),
    place(2781504, "Bezirk Mistelbach", 48.57, 16.57, "A", "ADM2", "AT", "03", "315", pop=75000),
    place(7871054, "Musterdorf", 47.9527, 16.2290, "A", "ADM3", "AT", "03", "307", "30717", pop=0),
    place(2774313, "Musterdorf", 47.9529, 16.2296, "P", "PPL", "AT", "03", "307", "30717", pop=2500, alt=""),
    place(2780410, "Baden", 48.0059, 16.2308, "P", "PPLA2", "AT", "03", "307", "30701", pop=25000),
    place(2761369, "Vienna", 48.2085, 16.3721, "P", "PPLC", "AT", "09", "", "", pop=1800000),
    place(2761367, "Bad Vöslau", 47.9668, 16.2146, "P", "PPL", "AT", "03", "307", "30702", pop=11000),
    place(2905310, "Baden-Baden", 48.76, 8.24, "P", "PPL", "DE", "01", pop=54000),
    place(2778067, "Graz", 47.0707, 15.4395, "P", "PPLA", "AT", "06", pop=290000),
    place(8000001, "Schloss Musterdorf", 47.9531, 16.2300, "S", "CSTL", "AT", "03", "307", "30717", pop=0),
    place(8000002, "Hauptstraße", 47.9532, 16.2297, "R", "RD", "AT", "03", "307", "30717", pop=0),
]
ALT = [  # alternateNameId, geonameid, isolanguage, name, isPreferred, isShort, isColloquial, isHistoric
    ("1", "2761369", "de", "Wien", "1", "", "", ""), ("2", "2761369", "en", "Vienna", "1", "", "", ""),
    ("3", "2761369", "fr", "Vienne", "1", "", "", ""), ("4", "2761369", "de", "Wienn", "", "", "1", "1"),
    ("5", "2770542", "de", "Niederösterreich", "1", "", "", ""), ("6", "2782113", "de", "Österreich", "1", "", "", ""),
]
HIER = [("2782113", "2770542", "ADM"), ("2770542", "2781503", "ADM"), ("2770542", "2781504", "ADM"),
        ("2781503", "7871054", "ADM"), ("7871054", "2774313", "")]
POSTAL = [  # cc, code, place, a1n, a1c, a2n, a2c, a3n, a3c, lat, lng, acc
    ("AT", "2552", "Musterdorf", "Niederösterreich", "03", "Bezirk Baden", "307", "Musterdorf", "30717", "47.9529", "16.2296", "4"),
    ("AT", "2540", "Bad Vöslau", "Niederösterreich", "03", "Bezirk Baden", "307", "Bad Vöslau", "30702", "47.9668", "16.2146", "4"),
    ("AT", "2500", "Baden", "Niederösterreich", "03", "Bezirk Baden", "307", "Baden", "30701", "48.0059", "16.2308", "4"),
    ("AT", "1010", "Wien, Innere Stadt", "Wien", "09", "", "", "", "", "48.2085", "16.3721", "4"),
]


def make(zip_places=False) -> str:
    d = tempfile.mkdtemp(prefix="gaz_")
    pl = "\n".join(PLACES) + "\n"
    if zip_places:
        with zipfile.ZipFile(os.path.join(d, "allCountries.zip"), "w") as z:
            z.writestr("allCountries.txt", pl)
    else:
        open(os.path.join(d, "allCountries.txt"), "w", encoding="utf-8").write(pl)
    open(os.path.join(d, "alternateNamesV2.txt"), "w", encoding="utf-8").write("\n".join("\t".join(r) for r in ALT) + "\n")
    open(os.path.join(d, "admin1CodesASCII.txt"), "w", encoding="utf-8").write("AT.03\tLower Austria\tLower Austria\t2770542\nAT.09\tVienna\tVienna\t2761369\nDE.01\tBaden-Württemberg\tBaden-Wurttemberg\t2953481\n")
    open(os.path.join(d, "admin2Codes.txt"), "w", encoding="utf-8").write("AT.03.307\tBezirk Baden\tBezirk Baden\t2781503\n")
    open(os.path.join(d, "hierarchy.txt"), "w", encoding="utf-8").write("\n".join("\t".join(r) for r in HIER) + "\n")
    with zipfile.ZipFile(os.path.join(d, "postal_allCountries.zip"), "w") as z:
        z.writestr("allCountries.txt", "\n".join("\t".join(r) for r in POSTAL) + "\n")
        z.writestr("readme.txt", "ignore me")
    open(os.path.join(d, "featureCodes_en.txt"), "w", encoding="utf-8").write("P.PPL\tpopulated place\ta city\nA.ADM1\tfirst-order administrative division\tx\nS.CSTL\tcastle\tx\nR.RD\troad\tx\nP.PPLA2\tseat of a second-order administrative division\tx\nP.PPLC\tcapital of a political entity\tx\nnull\tnot available\t\n")
    return d
