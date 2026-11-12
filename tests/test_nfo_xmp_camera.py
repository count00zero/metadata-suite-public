"""NFO Maker: camera from XMP-mon:CameraName first, camera_names.json in the fallback, Temp/Wetter lines in the plot."""
import os, sys, json, tempfile, shutil, subprocess
d = tempfile.mkdtemp(prefix="msuite_nfo_")
os.environ["MEDIA_ROOT"] = d
cfg = os.path.join(d, "camera_names.json")
json.dump({"rules": [{"match": ["FC9470", "DJI?NEO2"], "name": "DJI Neo 2"}]}, open(cfg, "w"))
os.environ["CAMERA_NAMES_FILE"] = cfg
from app.core import nfocore, devicenames, geocore

# --- read_mon_fields: element + attribute form, unescaping
x = os.path.join(d, "a.mp4.xmp")
open(x, "w").write("""<x:xmpmeta xmlns:x='adobe:ns:meta/'><rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
<rdf:Description rdf:about='' xmlns:mon='http://ns.neuhofer.home/media/1.0/' xmlns:tiff='http://ns.adobe.com/tiff/1.0/'
 mon:CameraName="DJI Neo 2" mon:CameraNameSource="override" mon:CtxTempC="11.0">
<mon:CtxWeather>Regen &amp; Wind</mon:CtxWeather><tiff:Make>DJI</tiff:Make><tiff:Model>FC9470</tiff:Model>
</rdf:Description></rdf:RDF></x:xmpmeta>""")
m = nfocore.read_mon_fields(x)
assert m["CameraName"] == "DJI Neo 2" and m["CtxWeather"] == "Regen & Wind" and m["CtxTempC"] == "11.0", m
line = nfocore.build_context_plot_line(m)
assert line == "[CR][COLOR lightblue]Temp: 11 °C[/COLOR][CR][COLOR lightblue]Wetter: Regen &amp; Wind[/COLOR]", line
assert nfocore.build_context_plot_line({"CtxTempC": "12.5"}) == "[CR][COLOR lightblue]Temp: 12.5 °C[/COLOR]"
assert nfocore.build_context_plot_line({}) == ""
assert nfocore.camera_name_from_sidecar(x)[0] == "DJI Neo 2"

# raw name in xmp is re-translated with the current rules
open(x, "w").write("""<rdf:Description xmlns:mon='http://ns.neuhofer.home/media/1.0/' xmlns:tiff='http://ns.adobe.com/tiff/1.0/'
 mon:CameraName="DJI FC9470" mon:CameraNameSource="raw"><tiff:Make>DJI</tiff:Make><tiff:Model>FC9470</tiff:Model></rdf:Description>""")
assert nfocore.camera_name_from_sidecar(x) == ("DJI Neo 2", "override"), nfocore.camera_name_from_sidecar(x)
assert nfocore.read_mon_fields(os.path.join(d, "nope.xmp")) == {}

# --- fallback: override applies to strings found in the video
assert devicenames.override_for(["foo", "dji neo2"]) == "DJI Neo 2"
assert devicenames.override_for(["nothing"]) is None

# --- Encoded_Application 'DJI NEO2' (mocked tool output) must not become 'Videoediting Software'
def fake(cmd, timeout):
    if cmd[0] == "mediainfo":
        return {"media": {"track": [{"@type": "General", "Encoded_Application": "DJI NEO2", "Duration": "5000", "Format": "MPEG-4"},
                                    {"@type": "Video", "Width": "64", "Height": "64"}]}}
    return [{"Encoder": "DJI NEO2", "Category": "pb_file:dvtm_NEO2.proto;model_name:FC9470;pb_version:02.00.07;"}]
nfocore._run_json = fake
mt = nfocore.extract_video_metadata("/x/n.mp4")
assert mt.vsource == "DJI Neo 2", mt.vsource
json.dump({"rules": []}, open(cfg, "w")); os.utime(cfg, (1, 1))
mt = nfocore.extract_video_metadata("/x/n.mp4")
assert mt.vsource == "DJI NEO2", mt.vsource      # no rule: raw encoder, never "Videoediting Software"

# --- plot contains the lines right before </plot>
from app.core import settings
xml = nfocore.build_nfo_xml(title="t", sort_title="t", year="2026", premiered="2026-10-04", duration_secs=5, width="1", height="1",
    aspect="1,78", codec="c", format_id="f", bitrate_mbps=1, container="mp4", framerate=30, bitdepth="8", aformat="a", achannels="2",
    vsource="DJI Neo 2", video_size_display="1 MB", sub_path="x", geo_line="Ort: P", ai_line=" [CR]beschr", stem="s",
    created_date="2026-10-04", defaults=settings.load_nfo_defaults() if hasattr(settings, "load_nfo_defaults") else None,
    context_line=line)
assert xml.split("<plot>")[1].split("</plot>")[0].endswith("Wetter: Regen &amp; Wind[/COLOR]"), xml
print("PASS")
