import os, sys, glob, json, requests

PAGE_ID = os.environ["FB_PAGE_ID"]
TOKEN = os.environ["FB_PAGE_TOKEN"]
MODE = os.environ.get("MODE", "morning")

VIDEO = f"out/final_{MODE}.mp4"
if not os.path.exists(VIDEO):
    found = sorted(glob.glob("out/final_*.mp4"))
    VIDEO = found[0] if found else None

if not VIDEO:
    print("Video bhetiyena: out/final_*.mp4")
    sys.exit(1)

# main.py le banaeko title + headlines + date + hashtags
try:
    meta = json.load(open("out/meta.json", encoding="utf-8"))
except Exception:
    meta = {}

title = meta.get("fb_title") or "आजका प्रमुख समाचार"
text = meta.get("fb_text") or f"{title}\n\n#hamrochitwan #todaynews #todaynepalinews #nepali"

print("Uploading:", VIDEO, os.path.getsize(VIDEO) // 1024 // 1024, "MB")
print(text)

url = f"https://graph-video.facebook.com/v21.0/{PAGE_ID}/videos"
with open(VIDEO, "rb") as f:
    r = requests.post(
        url,
        data={"access_token": TOKEN, "title": title, "description": text},
        files={"source": f},
        timeout=1800,
    )

print(r.status_code, r.text)
sys.exit(0 if r.ok else 1)
