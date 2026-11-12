import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BATCH_SIZE"] = "3"
from app.core import stepper
d = tempfile.mkdtemp(); fs = []
for i in range(10):
    p = os.path.join(d, f"f{i:02}.jpg"); open(p, "w").write("x"); fs.append(p)
s = stepper.start("gps", fs)
assert len(s.paths) == 3 and s.remaining == 7 and s.to_dict()["all_offset"] == -4
n = stepper.resume("gps", s.scan_id, s.next_offset)
assert len(n.paths) == 3 and n.has_more
a = stepper.resume("gps", s.scan_id, s.all_offset)
assert len(a.paths) == 7 and a.first == 4 and a.last == 10 and not a.has_more, (a.first, a.last)
from jinja2 import Environment, FileSystemLoader
t = Environment(loader=FileSystemLoader("app/templates_shared")).get_template("_stepbar.html")
h = t.render(step=s.to_dict(), step_url="/x")
assert "Alle verbleibenden laden (7)" in h and 'offset": "-4"' in h
h2 = t.render(step=n.to_dict(), step_url="/x")
assert "Alle verbleibenden laden (4)" in h2
assert "hx-confirm" not in h and "hx-confirm" not in h2         # no OK/Abbrechen question: just load
assert "Alle verbleibenden" not in t.render(step=a.to_dict(), step_url="/x")
print("all passed")
