"""wikimedia-build/build.sh resumes after a failure: finished steps are skipped, the database is kept, a new dump date starts
over, and the result is validated and handed over. Runs the real script against a fake generator (no PostgreSQL).
python tests/test_wikimedia_build.py"""
import gzip
import os
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "wikimedia-build", "build.sh")
STEPS = ["wikipedia_download", "wikidata_download", "wikidata_api_fetch_placetypes", "wikipedia_sql2csv", "wikidata_sql2csv",
         "wikipedia_import", "wikidata_import", "wikipedia_process", "wikidata_process"]


def sh(path, body):
    with open(path, "w") as f:
        f.write("#!/bin/bash\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def world(date="20261001"):
    t = tempfile.mkdtemp()
    for d in ("bin", "pgbin", "gen/steps", "gen/config", "work", "out", "pg"):
        os.makedirs(os.path.join(t, d))
    sh(f"{t}/bin/gosu", 'shift; exec "$@"')
    sh(f"{t}/bin/chown", "exit 0")
    sh(f"{t}/bin/psql", f'[ -f "{t}/dbflag" ] && echo " wikimedia_build | owner" ; exit 0')
    sh(f"{t}/bin/createdb", f'touch "{t}/dbflag"')
    sh(f"{t}/bin/dropdb", f'rm -f "{t}/dbflag"')
    sh(f"{t}/pgbin/initdb", 'mkdir -p "$2"; echo 16 > "$2/PG_VERSION"')
    sh(f"{t}/pgbin/pg_ctl", "exit 0")
    open(f"{t}/gen/config/languages.txt", "w").write("de\nen\n")
    sh(f"{t}/gen/steps/latest_available_data.sh", f'echo "{date}"')
    for s in STEPS:
        sh(f"{t}/gen/steps/{s}.sh", f'echo {s} >> "{t}/calls.log"; [ -f "{t}/fail_{s}" ] && {{ echo boom; exit 1; }}; exit 0')
    sh(f"{t}/gen/steps/output.sh", f'echo output >> "{t}/calls.log"; mkdir -p "$BUILDID/output"; '
       '{ printf "language\\ttype\\ttitle\\timportance\\twikidata_id\\n"; seq 1 600000 | sed "s/.*/en\\ta\\tTitle_&\\t0.5\\tQ&/"; } '
       '| gzip > "$BUILDID/output/wikimedia_importance.tsv.gz"')
    return t


def run(t):
    env = dict(os.environ, PATH=f"{t}/bin:" + os.environ["PATH"], GEO_WIKIMEDIA_OUT_DIR=f"{t}/out", GEO_WIKIMEDIA_WORK_DIR=f"{t}/work",
               GEO_WIKIMEDIA_PGDATA=f"{t}/pg", GEO_WIKIMEDIA_GENERATOR=f"{t}/gen", GEO_WIKIMEDIA_PGBIN=f"{t}/pgbin", PUID="1", PGID="1")
    open(f"{t}/calls.log", "w").close()
    r = subprocess.run(["bash", SCRIPT, "once"], env=env, capture_output=True, text=True, timeout=60)
    calls = open(f"{t}/calls.log").read().split()
    return r.returncode, calls, r.stdout + r.stderr


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"   {extra}" if not cond and extra else ""))
    if not cond:
        FAILS.append(name)


FAILS = []

# 1. a failing last step: everything before it ran once, nothing was delivered, the work stays
t = world()
open(f"{t}/fail_wikidata_process", "w").close()
rc, calls, out = run(t)
check("failure is reported (exit 1), steps up to the failing one ran", rc == 1 and calls == STEPS, (rc, calls))
check("no result file after a failure", not [f for f in os.listdir(f"{t}/out") if f.startswith("wikimedia_importance")])
check("finished steps are marked, the failed one is not",
      os.path.isfile(f"{t}/work/wikimedia_build/.done/wikipedia_sql2csv") and not os.path.isfile(f"{t}/work/wikimedia_build/.done/wikidata_process"))

# 2. run again: only the failed step and the output run
os.remove(f"{t}/fail_wikidata_process")
open(f"{t}/work/stray.csv", "w").write("x"); open(f"{t}/pg/.hidden", "w").write("x"); open(f"{t}/out/.latest.part", "w").write("x")
rc, calls, out = run(t)
check("resume runs only the failed step and the output", rc == 0 and calls == ["wikidata_process", "output"], (rc, calls, out[-300:]))
check("skipped steps are named in the log", "wikipedia_sql2csv (finished before, skipped)" in out)
outs = sorted(f for f in os.listdir(f"{t}/out") if f.startswith("wikimedia_importance"))
check("result delivered dated + current copy", outs == ["wikimedia_importance-20261001.tsv.gz", "wikimedia_importance.tsv.gz"], outs)
check("work folder removed after success", not os.path.exists(f"{t}/work/wikimedia_build"))
check("only last-run.log stays in work", sorted(os.listdir(f"{t}/work")) == ["last-run.log"], os.listdir(f"{t}/work"))
check("pgdata emptied (hidden files too)", os.listdir(f"{t}/pg") == [], os.listdir(f"{t}/pg"))
check("no .part leftovers in out", not [f for f in os.listdir(f"{t}/out") if f.endswith(".part")], os.listdir(f"{t}/out"))

# 3. an OLD run (before the markers existed) left only a log: its finished steps are recognised
t = world()
os.makedirs(f"{t}/work/wikimedia_build")
open(f"{t}/dbflag", "w").close()
open(f"{t}/work/last-run.log", "w").write("DUMP_DATE=20261001\n" + "".join(f"===> {s}\nwork\n" for s in STEPS) + "ERROR: could not resize shared memory segment\n")
rc, calls, out = run(t)
check("old failed run: only the last step is repeated", rc == 0 and calls == ["wikidata_process", "output"], (rc, calls))

# 4. a newer dump starts from scratch
t = world(date="20261101")
os.makedirs(f"{t}/work/wikimedia_build/.done")
for s in STEPS:
    open(f"{t}/work/wikimedia_build/.done/{s}", "w").close()
open(f"{t}/work/wikimedia_build/.done/date", "w").write("20261001")
rc, calls, out = run(t)
check("new dump date: every step runs again", rc == 0 and calls == STEPS + ["output"], (rc, calls))

# 5. markers but the database is gone: imports and processing repeat, the conversions do not
t = world()
os.makedirs(f"{t}/work/wikimedia_build/.done")
for s in STEPS:
    open(f"{t}/work/wikimedia_build/.done/{s}", "w").close()
open(f"{t}/work/wikimedia_build/.done/date", "w").write("20261001")
rc, calls, out = run(t)
check("database missing: imports + processing repeat", rc == 0 and calls == STEPS[5:] + ["output"], (rc, calls))

# 6. a result without the importance header is not delivered
t = world()
sh(f"{t}/gen/steps/output.sh", 'mkdir -p "$BUILDID/output"; echo "garbage" | gzip > "$BUILDID/output/wikimedia_importance.tsv.gz"')
rc, calls, out = run(t)
check("invalid output is refused", rc == 1 and not [f for f in os.listdir(f"{t}/out") if f.startswith("wikimedia_importance")], out[-200:])

# 7. the output step was finished before and only the hand-over failed: no generator step runs again
t = world()
open(f"{t}/dbflag", "w").close()
os.makedirs(f"{t}/work/wikimedia_build/.done")
os.makedirs(f"{t}/work/wikimedia_build/output")
for s_ in STEPS + ["output"]:
    open(f"{t}/work/wikimedia_build/.done/{s_}", "w").close()
open(f"{t}/work/wikimedia_build/.done/date", "w").write("20261001")
with gzip.open(f"{t}/work/wikimedia_build/output/wikimedia_importance.tsv.gz", "wt") as fh:
    fh.write("language\ttype\ttitle\timportance\twikidata_id\n" + "".join(f"en\ta\tT{i}\t0.5\tQ{i}\n" for i in range(300000)))
rc, calls, out = run(t)
check("finished output is only handed over", rc == 0 and calls == [] and os.path.isfile(f"{t}/out/wikimedia_importance.tsv.gz"), (rc, calls, out[-200:]))

# 6b. a refused output is regenerated next time
t = world()
sh(f"{t}/gen/steps/output.sh", 'echo output >> "' + t + '/calls.log"; mkdir -p "$BUILDID/output"; echo garbage | gzip > "$BUILDID/output/wikimedia_importance.tsv.gz"')
rc, calls, out = run(t)
check("refused output: marker removed so the output step runs again", rc == 1 and not os.path.exists(f"{t}/work/wikimedia_build/.done/output"))

print("\nFAILED: " + ", ".join(FAILS) if FAILS else "\nall wikimedia-build checks passed")
sys.exit(1 if FAILS else 0)
