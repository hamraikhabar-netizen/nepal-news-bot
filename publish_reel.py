import os, sys, glob, requests

PAGE_ID = os.environ["FB_PAGE_ID"]
TOKEN = os.environ["FB_PAGE_TOKEN"]

# tapaiko video kaha save huncha, tyo anusar milaunus
cands = ["news_videos/daily_news.mp4", "daily_news.mp4", "output/daily_news.mp4"]
cands += glob.glob("**/*.mp4", recursive=True)
VIDEO = next((p for p in cands if os.path.exists(p)), None)
if not VIDEO:
    print("Video bhetiyena"); sys.exit(1)
print("Uploading:", VIDEO, os.path.getsize(VIDEO) // 1024 // 1024, "MB")

url = f"https://graph-video.facebook.com/v21.0/{PAGE_ID}/videos"
with open(VIDEO, "rb") as f:
    r = requests.post(url,
        data={"access_token": TOKEN,
              "description": "आजका प्रमुख समाचार | I Love Chitwan"},
        files={"source": f}, timeout=1800)
print(r.status_code, r.text)
sys.exit(0 if r.ok else 1)
