"""NFO Maker modes repair-missing-ai / repair-missing (ported from HomeVideo_NFO_Maker_new.sh)."""
import os, sys, tempfile, subprocess as sp
MR = tempfile.mkdtemp(prefix="nfomiss_"); os.environ["MEDIA_ROOT"] = MR
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core import nfocore
fails = []
def check(n, c, x=""):
    print(("ok   " if c else "FAIL ") + n, "" if c else x)
    if not c: fails.append(n)
WHITE, ORT, NOLOC = "[COLOR white]Text[/COLOR]", "Ort: [COLOR yellow]Wien[/COLOR]", "Ort: [COLOR yellow]No Location[/COLOR]"
def mk(stem, body, gps=None):
    open(f"{MR}/{stem}.mp4", "wb").write(b"x")
    open(f"{MR}/{stem}.nfo", "w").write(f"<movie><plot>{body}</plot></movie>")
    if gps is not None:
        x = f"{MR}/{stem}.mp4.xmp"
        open(x, "w").write('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about=""/></rdf:RDF></x:xmpmeta>')
        if gps:
            sp.run(["exiftool", "-overwrite_original", "-q", "-XMP:GPSLatitude=48.1591", "-XMP:GPSLongitude=16.3328", x], check=True)
mk("ok", f"{ORT} {WHITE}")                       # complete
mk("noai", f"{ORT}")                             # no description
mk("noloc_gps", f"{NOLOC} {WHITE}", gps=True)    # no location, sidecar has GPS now
mk("noloc_nogps", f"{NOLOC} {WHITE}", gps=False) # no location, still no GPS -> leave alone
mk("noloc_nosidecar", f"{NOLOC} {WHITE}")        # no sidecar at all -> leave alone
b = lambda l: sorted(os.path.basename(x)[:-4] for x in l)
check("repair-missing-ai finds only the one without description", b(nfocore.scan_missing_ai_nfos(MR, 1)) == ["noai"], b(nfocore.scan_missing_ai_nfos(MR, 1)))
nfos, n_ai, n_loc = nfocore.scan_missing_nfos(MR, 1)
check("repair-missing = no description OR (no location AND GPS now)", b(nfos) == ["noai", "noloc_gps"] and (n_ai, n_loc) == (1, 1), (b(nfos), n_ai, n_loc))

# via the module: the mode is accepted and needs AI enabled
from app.modules.nfo_maker import router as n
check("modes registered", "repair-missing" in n.MODES and "repair-missing-ai" in n.MODES)
from app.core import settings
orig = settings.load_nfo_defaults
class D:  # AI on
    def __getattr__(self, k): return getattr(orig(), k)
settings.load_nfo_defaults = lambda: type("X", (), {"ai_description_enabled": True})()
v, msg = n._candidate_videos(MR, 1, "repair-missing")
check("module: repair-missing -> 2 videos", b([x[:-4] + ".nfo" for x in v]) == ["noai", "noloc_gps"], (v, msg))
settings.load_nfo_defaults = lambda: type("X", (), {"ai_description_enabled": False})()
v, msg = n._candidate_videos(MR, 1, "repair-missing")
check("module: AI disabled -> nothing + explanation", v == [] and "nicht aktiviert" in msg, msg)
import inspect
check("limit param (script's --limit)", "limit" in inspect.signature(n.scan_start_work).parameters and "limit" in inspect.signature(n.scan_start).parameters)
print("\nFAILED: %s" % fails if fails else "\nall passed"); sys.exit(1 if fails else 0)
