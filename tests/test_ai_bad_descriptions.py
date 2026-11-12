import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core import aicore, nfocore
P = aicore.ai_description_problem
good = "Zwei Kinder spielen im Garten mit einem roten Ball, im Hintergrund steht ein Apfelbaum."
assert P(good, strict=True) == ""
for bad in ["Es tut mir leid, ich kann dieses Bild nicht beschreiben.", "I'm sorry, but I cannot describe this image.",
            "<think>hmm</think> Ein Hund", "```json\n{\"a\":1}```", "Beschreibung: Ein Hund im Park.",
            "Ein Mann steht da. Ein Mann steht da. Ein Mann steht da. Ein Mann steht da.",
            "Ein Hund 狗 im Park mit einem Ball und viel Gras."]:
    assert P(bad), bad
assert P("The image shows a dog and a man with the ball in the park.", strict=True) == "Englisch statt Deutsch"
assert P("The image shows a dog and a man with the ball in the park.") == ""      # fuzzy checks only in strict mode
assert "abgeschnitten" in P("Zwei Kinder spielen im Garten mit einem roten Ball, im Hintergrund steht ein Apfel", strict=True)
assert aicore.post_process("Es tut mir leid, ich kann das nicht.") is None
assert aicore.post_process(good) == good
# NFO scan
d = tempfile.mkdtemp()
for n, plot in (("a", good), ("b", "Es tut mir leid, ich kann dieses Bild nicht beschreiben."), ("c", "[I]Ein Mann. Ein Mann. Ein Mann. Ein Mann.[/I]")):
    open(os.path.join(d, n + ".nfo"), "w").write(f"<movie>\n                <plot>{plot}</plot>\n</movie>")
found = [os.path.basename(x) for x in nfocore.scan_bad_ai_nfos(d, 3)]
assert found == ["b.nfo", "c.nfo"], found
from app.modules.nfo_maker import router as nr
assert "repair-bad-ai" in nr.MODES
print("all passed")

# browse_start_work(fix_bad=True): bad existing -> row with reason; good existing -> skipped
os.environ.setdefault("MEDIA_ROOT", d)
from app.modules.ai_description import router as ar
from app.core import stepper, jobs
files = [os.path.join(d, n) for n in ("g.jpg", "x.jpg")]
for f in files: open(f, "w").write("x")
descs = {files[0]: good, files[1]: "Es tut mir leid, ich kann das nicht."}
aicore.get_existing_ai_description = lambda f, strict=False: descs[f]
aicore.build_ai_geo_context_from_existing = lambda f: ""
ar.geoservice.context_info = lambda f: ("", {})
def run(fix):
    j = jobs.Job("t"); st = stepper.Step(paths=files, total=2, first=1, last=2, next_offset=2)
    return ar.browse_start_work(j, "", files, 3, True, False, d, d, st, "IMAGE", fix_bad=fix)
r = run(True)
assert [x["filename"] for x in r["rows"]] == ["x.jpg"] and r["rows"][0]["bad_reason"] and r["skipped_done"] == 1, r["rows"]
r = run(False)
assert r["rows"] == [] and r["skipped_done"] == 2
print("browse ok")
