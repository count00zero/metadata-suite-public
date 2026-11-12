"""All user config files default to the folder of presets.json (=/config in Docker), not to <app>/config."""
import os, subprocess, sys
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
code = ("from app.core import settings, devicenames;"
        "print(devicenames.OVERRIDES_PATH)")
def run(env_extra, drop=()):
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(PYTHONPATH=root, **env_extra)
    return subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True).stdout.strip().split("\n")
cam, = run({"TIMESTAMP_PRESETS_FILE": "/config/presets.json"}, drop=("CAMERA_NAMES_FILE",))
assert cam == "/config/camera_names.json", cam
# explicit variables still win
cam, = run({"TIMESTAMP_PRESETS_FILE": "/config/presets.json", "CAMERA_NAMES_FILE": "/x/cams.json"})
assert cam == "/x/cams.json", cam
# local run without any variable: <project>/config as before
cam, = run({}, drop=("TIMESTAMP_PRESETS_FILE", "CAMERA_NAMES_FILE"))
assert os.path.normpath(cam) == os.path.join(root, "config", "camera_names.json"), cam
df = open(os.path.join(root, "Dockerfile")).read(); dc = open(os.path.join(root, "docker-compose.yml")).read()
assert "CAMERA_NAMES_FILE" in df and "/config/camera_names.json" in df and "CAMERA_NAMES_FILE=${CAMERA_NAMES_FILE:-/config/camera_names.json}" in dc
print("PASS")
