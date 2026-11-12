"""check_env.py: finds missing private/sensitive values, never prints secrets, exit code reflects errors."""
import os, subprocess, sys, tempfile
R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = os.path.join(R, "check_env.py")


def run(text, *extra):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, ".env")
        open(p, "w").write(text)
        os.chmod(p, 0o600)
        r = subprocess.run([sys.executable, S, "--env", p, "--example", os.path.join(R, ".env.example"), "--no-color", *extra],
                           capture_output=True, text=True)
        return r.returncode, r.stdout


good = ("MEDIA_HOST_DIR=/mnt/NAS\nMEDIA_ROOT=/mnt/NAS\nROOT_FOLDERS=VIDEOS=/mnt/NAS/v/,PHOTOS=/mnt/NAS/p/\nTZ=Europe/Vienna\nNFO_DIRECTOR=Jane\nNFO_STUDIO=Home\n"
        "IPTC_CREDIT=Jane\nNFO_ACTORS=Jane|Dad|/a.jpg\nTRACCAR_URL=http://10.0.0.5:8082\nTRACCAR_TOKEN=supersecrettokenvalue123\n"
        "TRACCAR_DEVICES=7:Phone\nGEO_SERVICE_TOKEN=0123456789abcdef0123456789abcdef\nAI_PROVIDER=google\nGOOGLE_AI_API_KEY=AIzaSECRETKEY\n")
rc, out = run(good)
assert rc == 0, out
assert "supersecret" not in out and "AIzaSECRET" not in out          # secrets are never echoed

rc, out = run(good.replace("TRACCAR_TOKEN=supersecrettokenvalue123\n", ""))
assert rc == 1 and "TRACCAR_TOKEN" in out

rc, out = run(good.replace("GOOGLE_AI_API_KEY=AIzaSECRETKEY\n", ""))
assert rc == 1 and "GOOGLE_AI_API_KEY" in out

rc, out = run(good + "NFO_DIRECTOR=Your Name\nTRACCAR_TOKEN=\n")            # last value wins; empty token again
assert rc == 1

rc, out = run(good.replace("NFO_DIRECTOR=Jane", "NFO_DIRECTOR=Your Name"))
assert rc == 0 and "Beispielwert" in out                                      # placeholder -> warning only

rc, out = run(good + "NFO_DIRECTR=x\n")
assert "NFO_DIRECTOR" in out and "unbekannter Name" in out                    # typo hint

rc, out = run(good + "BATCH_SIZE=abc\n")
assert rc == 1 and "BATCH_SIZE" in out
print("PASS")
