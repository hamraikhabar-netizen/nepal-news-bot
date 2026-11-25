import os, sys, glob, requests

PAGE_ID = os.environ["FB_PAGE_ID"]
TOKEN = os.environ["FB_PAGE_TOKEN"]
MODE = os.environ.get("MODE", "morning")

# main.py le banaeko final video matra (clip1.mp4, raw_*.mp4 haina)
VIDEO = f"out/final_{MODE}.mp4"
if not os.path.exists(VIDEO):
    found = sorted(glob.glob("out/final_*.mp4"))
    VIDEO = found[0] if found else None

if not VIDEO:
    print("Video bhetiyena: out/final_*.mp4")
    sys.exit(1)

print("Uploading:", VIDEO, os.path.getsize(VIDEO) // 1024 // 1024, "MB")

title = "आजका प्रमुख समाचार" if MODE == "morning" else "आजको चर्चित विषय"
url = f"https://graph-video.facebook.com/v21.0/{PAGE_ID}/videos"

with open(VIDEO, "rb") as f:
    r = requests.post(
        url,
        data={
            "access_token": TOKEN,
            "title": title,
            "description": f"{title} | I Love Chitwan",
        },
        files={"source": f},
        timeout=1800,
    )

print(r.status_code, r.text)
sys.exit(0 if r.ok else 1)
