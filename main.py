"""हाम्रै खबर - daily Nepali news video bot.

Usage:  python main.py morning     (10 मुख्य समाचार, ~4 min)
        python main.py afternoon   (आजको सबैभन्दा चर्चित विषय, ~3-4 min)
"""
import os, sys, json, re, glob, subprocess, datetime, asyncio, time
import requests, feedparser
from PIL import Image, ImageDraw, ImageFont
import edge_tts

MODE = sys.argv[1] if len(sys.argv) > 1 else "morning"
NPT = datetime.timezone(datetime.timedelta(hours=5, minutes=45))
NOW = datetime.datetime.now(NPT)
OUT = "out"
os.makedirs(OUT, exist_ok=True)
W, H = 1920, 1080

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
MODEL = None  # आफैं छानिन्छ (pick_model)
VOICE = os.environ.get("TTS_VOICE", "ne-NP-SagarNeural")
PRIVACY = os.environ.get("UPLOAD_PRIVACY", "private")  # private / unlisted / public

# समाचार स्रोत (RSS). चल्दैन भने यहाँ बदल्न सकिन्छ; फेल भएको feed आफैं skip हुन्छ।
FEEDS = [
    "https://www.onlinekhabar.com/feed",
    "https://www.setopati.com/feed",
    "https://feeds.bbci.co.uk/nepali/rss.xml",
    "https://news.google.com/rss/search?q=%E0%A4%A8%E0%A5%87%E0%A4%AA%E0%A4%BE%E0%A4%B2&hl=ne&gl=NP&ceid=NP:ne",
    "https://news.google.com/rss/headlines/section/topic/WORLD?hl=ne&gl=NP&ceid=NP:ne",
]

MONTHS = ["जनवरी", "फेब्रुअरी", "मार्च", "अप्रिल", "मे", "जुन",
          "जुलाई", "अगस्ट", "सेप्टेम्बर", "अक्टोबर", "नोभेम्बर", "डिसेम्बर"]


def deva(s):
    return str(s).translate(str.maketrans("0123456789", "०१२३४५६७८९"))


DATE_STR = deva(f"{NOW.day} {MONTHS[NOW.month - 1]} {NOW.year}")


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# ---------------------------------------------------------------- समाचार
def clean(t):
    t = re.sub(r"<[^>]+>", " ", t or "")
    return re.sub(r"\s+", " ", t).strip()


def fetch_news():
    items, seen = [], set()
    for url in FEEDS:
        try:
            d = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
            src = clean(d.feed.get("title", url))
            for e in d.entries[:15]:
                title = clean(e.get("title", ""))
                if not title or title in seen:
                    continue
                seen.add(title)
                items.append({"title": title, "summary": clean(e.get("summary", ""))[:300],
                              "source": src, "link": e.get("link", "")})
            print(f"feed ok: {src} ({len(d.entries)})")
        except Exception as ex:
            print("feed failed:", url, ex)
    return items


# ---------------------------------------------------------------- Gemini
def pick_model():
    """Google ले पुराना model बन्द गर्दै जान्छ, त्यसैले चल्ने model आफैं खोज्ने।"""
    pref = os.environ.get("GEMINI_MODEL")
    if pref:
        return pref
    names = []
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         headers={"x-goog-api-key": GEMINI_KEY}, params={"pageSize": 200}, timeout=60)
        r.raise_for_status()
        for m in r.json().get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                names.append(m["name"].split("/")[-1])
    except Exception as ex:
        print("model list failed:", ex)
    print("available models:", names)
    for p in ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-3-flash-preview",
              "gemini-2.5-flash", "gemini-4-hydrogen"]:
        if p in names:
            return p
    bad = ("tts", "live", "audio", "image", "embedding", "robotics", "computer")
    cand = [n for n in names if ("flash" in n or "hydrogen" in n) and not any(b in n for b in bad)]
    if cand:
        return sorted(cand)[-1]
    raise SystemExit("चल्ने Gemini model भेटिएन। API key ठिक छ कि हेर्नुहोस्।")


def gemini(prompt):
    global MODEL
    if MODEL is None:
        MODEL = pick_model()
        print("using model:", MODEL)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4,
                                 "maxOutputTokens": 16000}}
    last = None
    for _ in range(3):
        try:
            r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=240)
            r.raise_for_status()
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(txt)
        except Exception as ex:
            last = ex
            print("gemini retry:", ex)
            time.sleep(20)
    raise SystemExit(f"Gemini failed: {last}")


RULES = (
    "नियम: तलको सूचीमा भएका शीर्षक र सारांश मात्र आधार बनाऊ। कुनै नाम, संख्या, मिति वा तथ्य आफैं नथप। "
    "पुष्टि नभएको वा एउटै स्रोतको कुरा 'भनिएको छ' भनेर लेख। कसैको पक्ष नलेऊ, तटस्थ र सरल नेपालीमा लेख। "
    "'ref' मा सूचीको नम्बर (integer) राख। "
)

SCHEMA = (
    'JSON मात्र फर्काऊ: {"title": "YouTube शीर्षक (नेपालीमा, ९० अक्षरभित्र)", '
    '"description": "२-३ वाक्यको विवरण", "intro": "नमस्कार... ले सुरु हुने १-२ वाक्य", '
    '"items": [{"headline": "७० अक्षरभित्रको छोटो शीर्षक", "narration": "वाचन पाठ", "ref": 1}], '
    '"outro": "धन्यवाद सहितको १-२ वाक्य, च्यानल हाम्रै खबर सब्सक्राइब गर्न भन"}. '
)


def make_prompt(listing):
    if MODE == "morning":
        task = ("तिमी 'हाम्रै खबर' नामक नेपाली समाचार च्यानलको सम्पादक हौ। तलका समाचारबाट आजका १० ओटा "
                "सबैभन्दा महत्त्वपूर्ण समाचार छान (नेपालको राजनीति र समाचार बढी, केही विश्व समाचार)। "
                "प्रत्येक items को narration करिब ४५-५० शब्द हुनुपर्छ। ")
    else:
        task = ("तिमी 'हाम्रै खबर' नामक नेपाली समाचार च्यानलको विश्लेषक हौ। तलको सूचीमा धेरै स्रोतमा दोहोरिएको "
                "आजको सबैभन्दा चर्चित एउटा विषय छान। त्यसबारे ५-६ ओटा भागमा विश्लेषण लेख: के भयो, "
                "पृष्ठभूमि, को-को संलग्न, किन महत्त्वपूर्ण, अब के हुन सक्छ (अनुमान भए स्पष्ट 'सम्भावना' भन)। "
                "प्रत्येक narration करिब ७०-८० शब्द हुनुपर्छ। ")
    return task + RULES + SCHEMA + "\n\nसमाचार सूची:\n" + listing


# ---------------------------------------------------------------- आवाज
async def _tts(text, path):
    await edge_tts.Communicate(text, VOICE).save(path)


def tts(text, path):
    for _ in range(3):
        try:
            asyncio.run(_tts(text, path))
            if os.path.getsize(path) > 1000:
                return
        except Exception as ex:
            print("tts retry:", ex)
    raise SystemExit("TTS failed (Edge TTS ले GitHub server लाई रोक्न सक्छ)")


# ---------------------------------------------------------------- चित्र
def font(size):
    paths = glob.glob("/usr/share/fonts/**/NotoSansDevanagari-Bold.ttf", recursive=True)
    if not paths:
        raise SystemExit("Devanagari font भेटिएन")
    try:
        return ImageFont.truetype(paths[0], size, layout_engine=ImageFont.Layout.RAQM)
    except Exception:
        print("WARNING: RAQM छैन, अक्षर बिग्रन सक्छ")
        return ImageFont.truetype(paths[0], size)


def wrap(draw, text, fnt, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=fnt) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def slide(path, headline, number=None, size=(W, H)):
    img = Image.new("RGB", (W, H), (11, 31, 92))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, 150), fill=(7, 20, 64))
    d.rectangle((0, H - 110, W, H), fill=(7, 20, 64))
    logo = Image.open("logo.png").convert("RGBA").resize((120, 120))
    img.paste(logo, (30, 15), logo)
    d.text((170, 38), "हाम्रै खबर", font=font(64), fill="white")
    d.text((W - 60, 50), DATE_STR, font=font(52), fill=(220, 190, 160), anchor="ra")
    d.text((60, H - 95), "सधै समाचार, सत्य समाचार", font=font(54), fill=(220, 190, 160))
    left = 140
    if number is not None:
        cx, cy = 230, 540
        d.ellipse((cx - 110, cy - 110, cx + 110, cy + 110), fill=(176, 112, 74))
        d.text((cx, cy), deva(number), font=font(120), fill="black", anchor="mm")
        left = 400
    f = font(92)
    lines = wrap(d, headline, f, W - left - 100)[:5]
    lh = 130
    y = 540 - lh * len(lines) // 2
    for ln in lines:
        d.text((left, y), ln, font=f, fill="white")
        y += lh
    img.save(path)
    return path


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True)
    return float(out.stdout.strip())


# ---------------------------------------------------------------- भिडियो
def build_video(segments, final):
    clips = []
    for i, (img, aud) in enumerate(segments):
        c = f"{OUT}/clip{i}.mp4"
        run(["ffmpeg", "-y", "-loop", "1", "-i", img, "-i", aud, "-c:v", "libx264",
             "-tune", "stillimage", "-r", "25", "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
             "-ac", "2", "-pix_fmt", "yuv420p", "-shortest", c])
        clips.append(os.path.basename(c))
    with open(f"{OUT}/list.txt", "w") as f:
        for c in clips:
            f.write(f"file '{c}'\n")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", f"{OUT}/list.txt", "-c", "copy", final])


# ---------------------------------------------------------------- YouTube
def upload(video, thumb, meta):
    need = ["YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"]
    if not all(os.environ.get(k) for k in need):
        print("YouTube secrets छैनन्, upload skip गरियो (भिडियो artifact मा छ)")
        return
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"],
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=os.environ["YT_CLIENT_ID"],
                        client_secret=os.environ["YT_CLIENT_SECRET"],
                        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    yt = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {"title": meta["title"][:100], "description": meta["description"][:4900],
                    "tags": ["नेपाल", "समाचार", "हाम्रै खबर", "Nepal news", "Hamrai Khabar"],
                    "categoryId": "25", "defaultLanguage": "ne"},
        "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False,
                   "containsSyntheticMedia": True},
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(video, chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    print("uploaded:", resp["id"], "privacy:", PRIVACY)
    try:
        yt.thumbnails().set(videoId=resp["id"], media_body=MediaFileUpload(thumb)).execute()
    except Exception as ex:
        print("thumbnail skip:", ex)


# ---------------------------------------------------------------- मुख्य
def main():
    news = fetch_news()
    if len(news) < 10:
        raise SystemExit("पर्याप्त समाचार भेटिएन")
    news = news[:90]
    listing = "\n".join(f"{i + 1}. {n['title']} — {n['summary']}" for i, n in enumerate(news))
    data = gemini(make_prompt(listing))
    items = data["items"]
    print(f"{len(items)} items, title: {data['title']}")

    segs = []
    # intro
    p = slide(f"{OUT}/s_intro.png", data["title"])
    tts(data["intro"], f"{OUT}/a_intro.mp3")
    segs.append((p, f"{OUT}/a_intro.mp3"))
    thumb_src = p
    for i, it in enumerate(items, 1):
        ip = slide(f"{OUT}/s_{i}.png", it["headline"], number=i if MODE == "morning" else None)
        ap = f"{OUT}/a_{i}.mp3"
        tts(it["narration"], ap)
        segs.append((ip, ap))
    op = slide(f"{OUT}/s_outro.png", "हेर्नुभएकोमा धन्यवाद। च्यानल सब्सक्राइब गर्नुहोस्")
    tts(data["outro"], f"{OUT}/a_outro.mp3")
    segs.append((op, f"{OUT}/a_outro.mp3"))

    total = sum(duration(a) for _, a in segs)
    print(f"कुल अवधि: {total / 60:.1f} मिनेट")

    final = f"{OUT}/final_{MODE}.mp4"
    build_video(segs, final)

    thumb = f"{OUT}/thumb_{MODE}.jpg"
    Image.open(thumb_src).resize((1280, 720)).convert("RGB").save(thumb, quality=88)

    # विवरणमा स्रोतहरू
    src_lines = []
    for it in items:
        try:
            n = news[int(it["ref"]) - 1]
            src_lines.append(f"- {n['source']}: {n['link']}")
        except Exception:
            pass
    desc = (data["description"] + "\n\nस्रोतहरू:\n" + "\n".join(dict.fromkeys(src_lines)) +
            "\n\nनोट: यो भिडियोको वाचनमा AI आवाज प्रयोग गरिएको छ। समाचार स्रोतका शीर्षकमा आधारित सारांश हो।\n"
            "हाम्रै खबर - सधै समाचार, सत्य समाचार")
    upload(final, thumb, {"title": data["title"], "description": desc})


if __name__ == "__main__":
    main()
