"""हाम्रै खबर - daily Nepali news video bot (v2).

Usage:  python main.py morning     (10 मुख्य समाचार, ~4 min)
        python main.py afternoon   (आजको सबैभन्दा चर्चित विषय, ~3-4 min)
"""
import os, sys, io, json, re, glob, wave, base64, shutil, subprocess, datetime, asyncio, time
import requests, feedparser
from mutagen.mp3 import MP3
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import edge_tts

try:
    import imageio_ffmpeg
    FFMPEG = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = "ffmpeg"

# ============================ सेटिङ (यहाँ बदल्न सकिन्छ) ============================
# False = समाचार साइटका फोटो प्रयोग नगर्ने (copyright को लागि सुरक्षित)
# True  = समाचार साइटको फोटो पनि प्रयोग गर्ने (copyright claim को जोखिम हुन्छ)
ALLOW_ARTICLE_PHOTOS = False
GEMINI_VOICE = os.environ.get("GEMINI_VOICE", "Charon")  # वाचक आवाज
SPEED = 1.15          # वाचन गति (1.0 = सामान्य, 1.15 = अलि छिटो)
MUSIC_VOLUME = 0.30   # पृष्ठभूमि सङ्गीतको आवाज (0.05 - 0.2)
# ================================================================================

MODE = sys.argv[1] if len(sys.argv) > 1 else "morning"
NPT = datetime.timezone(datetime.timedelta(hours=5, minutes=45))
NOW = datetime.datetime.now(NPT)
OUT = "out"
os.makedirs(OUT, exist_ok=True)
W, H = 1920, 1080
UA = {"User-Agent": "HamraiKhabarBot/1.0 (news video bot; contact via YouTube @hamraikhabar)"}

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = None
NAMES = []
EDGE_VOICE = os.environ.get("TTS_VOICE", "ne-NP-SagarNeural")
PRIVACY = os.environ.get("UPLOAD_PRIVACY", "private")  # private / unlisted / public

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
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        print("COMMAND FAILED:", " ".join(cmd[:6]), "...")
        print(p.stderr[-1500:])
        raise SystemExit("ffmpeg failed")


# ---------------------------------------------------------------- समाचार
def clean(t):
    t = re.sub(r"<[^>]+>", " ", t or "")
    return re.sub(r"\s+", " ", t).strip()


def entry_image(e):
    for k in ("media_content", "media_thumbnail"):
        for m in e.get(k, []) or []:
            if m.get("url"):
                return m["url"]
    for l in e.get("links", []) or []:
        if str(l.get("type", "")).startswith("image") and l.get("href"):
            return l["href"]
    blob = e.get("summary", "") + " ".join(c.get("value", "") for c in (e.get("content", []) or []))
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', blob)
    return m.group(1) if m else ""


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
                              "source": src, "link": e.get("link", ""), "image": entry_image(e)})
            print(f"feed ok: {src} ({len(d.entries)})")
        except Exception as ex:
            print("feed failed:", url, ex)
    return items


# ---------------------------------------------------------------- Gemini (लेखन)
def pick_model():
    global NAMES
    pref = os.environ.get("GEMINI_MODEL")
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         headers={"x-goog-api-key": GEMINI_KEY}, params={"pageSize": 200}, timeout=60)
        r.raise_for_status()
        for m in r.json().get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                NAMES.append(m["name"].split("/")[-1])
    except Exception as ex:
        print("model list failed:", ex)
    if pref:
        return pref
    for p in ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-3-flash-preview",
              "gemini-2.5-flash", "gemini-4-hydrogen"]:
        if p in NAMES:
            return p
    bad = ("tts", "live", "audio", "image", "embedding", "robotics", "computer")
    cand = [n for n in NAMES if ("flash" in n or "hydrogen" in n) and not any(b in n for b in bad)]
    if cand:
        return sorted(cand)[-1]
    raise SystemExit("चल्ने Gemini model भेटिएन।")


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
    "narration मा समाचार मात्र लेख: 'नमस्कार', 'यो समाचार', 'हेर्नुहोस्', 'सुन्नुहोस्', लाइक/सब्सक्राइब "
    "जस्ता कुनै अतिरिक्त कुरा नलेख; सिधै समाचारको मुख्य कुराबाट सुरु गर। "
    "headline अत्यन्त छोटो (४०-४५ अक्षरभित्र), पूरा अर्थ दिने, एक पङ्क्तिमा अटाउने र टुङ्गिएको हुनुपर्छ "
    "(जस्तै 'प्रधानमन्त्री भारत भ्रमणमा')। "
    "'ref' मा सूचीको नम्बर (integer) राख। "
    "'image_query' मा त्यो समाचारको मुख्य व्यक्ति/स्थान/संस्थाको नाम अंग्रेजीमा २-४ शब्दमा लेख "
    "(जस्तै 'Balen Shah', 'Kathmandu'); सूचीमा नभएको नाम नबनाऊ। "
    "'video_query' मा त्यो समाचारसँग मिल्ने सामान्य दृश्यको अंग्रेजी खोजशब्द २-३ शब्दमा लेख "
    "(जस्तै 'parliament building', 'flood water', 'airport', 'cricket stadium', 'protest crowd')। "
)

SCHEMA = (
    'JSON मात्र फर्काऊ: {"title": "YouTube शीर्षक (नेपालीमा, ९० अक्षरभित्र)", '
    '"description": "२-३ वाक्यको विवरण", '
    '"items": [{"headline": "छोटो शीर्षक", "narration": "वाचन पाठ", "ref": 1, '
    '"image_query": "Balen Shah", "video_query": "parliament building"}]}. '
)


def make_prompt(listing):
    if MODE == "morning":
        task = ("तिमी 'हाम्रै खबर' नामक नेपाली समाचार च्यानलको सम्पादक हौ। तलका समाचारबाट आजका १० ओटा "
                "सबैभन्दा महत्त्वपूर्ण समाचार छान (नेपालको राजनीति र समाचार बढी, केही विश्व समाचार)। "
                "प्रत्येक items को narration करिब ५०-५५ शब्द हुनुपर्छ। "
                "वाचन सुनिँदा प्राकृतिक होस्: छोटा वाक्य, बोलचालको सरल समाचार शैली, अङ्कलाई शब्दमा लेख। ")
    else:
        task = ("तिमी 'हाम्रै खबर' नामक नेपाली समाचार च्यानलको विश्लेषक हौ। तलको सूचीमा धेरै स्रोतमा दोहोरिएको "
                "आजको सबैभन्दा चर्चित एउटा विषय छान। त्यसबारे ५-६ ओटा भागमा विश्लेषण लेख: के भयो, "
                "पृष्ठभूमि, को-को संलग्न, किन महत्त्वपूर्ण, अब के हुन सक्छ (अनुमान भए स्पष्ट 'सम्भावना' भन)। "
                "प्रत्येक narration करिब ७०-८० शब्द हुनुपर्छ। "
                "वाचन सुनिँदा प्राकृतिक होस्: छोटा वाक्य, बोलचालको सरल समाचार शैली। ")
    return task + RULES + SCHEMA + "\n\nसमाचार सूची:\n" + listing


# ---------------------------------------------------------------- आवाज
GEM_TTS_FAILS = 0
TTS_PREF = ["gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]


def gemini_tts(text, path):
    """Gemini को प्राकृतिक आवाज (wav)। सफल भए True।"""
    global GEM_TTS_FAILS
    if GEM_TTS_FAILS >= 2:
        return False
    models = [m for m in TTS_PREF if m in NAMES] or [n for n in NAMES if "tts" in n]
    prompt = text  # कुनै निर्देश नराख्ने, नत्र आवाजले त्यो पनि पढ्छ
    for m in models[:2]:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
            body = {"contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"responseModalities": ["AUDIO"],
                                         "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                                             "voiceName": GEMINI_VOICE}}}}}
            r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=300)
            r.raise_for_status()
            part = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
            pcm = base64.b64decode(part["data"])
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)
                w.writeframes(pcm)
            time.sleep(3)
            return True
        except Exception as ex:
            print("gemini tts fail:", m, str(ex)[:200])
    GEM_TTS_FAILS += 1
    return False


async def _edge(text, path):
    await edge_tts.Communicate(text, EDGE_VOICE).save(path)


def tts(text, base):
    """आवाज बनाउँछ र फाइलको path फर्काउँछ।"""
    wav = base + ".wav"
    if gemini_tts(text, wav):
        return wav
    mp3 = base + ".mp3"
    for _ in range(3):
        try:
            asyncio.run(_edge(text, mp3))
            if os.path.getsize(mp3) > 1000:
                print("edge tts used for", base)
                return mp3
        except Exception as ex:
            print("edge retry:", ex)
    raise SystemExit("आवाज बनाउन सकिएन (Gemini र Edge दुवै फेल)")


def duration(path):
    if path.endswith(".wav"):
        with wave.open(path) as w:
            return w.getnframes() / w.getframerate()
    return MP3(path).info.length


# ---------------------------------------------------------------- फोटो / भिडियो
def download(url, path):
    try:
        r = requests.get(url, headers=UA, timeout=40)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        if im.width < 600:
            return False
        im.save(path, quality=92)
        return True
    except Exception as ex:
        print("image fail:", str(ex)[:120])
        return False


def download_file(url, path, max_mb=90):
    try:
        with requests.get(url, headers=UA, timeout=60, stream=True) as r:
            r.raise_for_status()
            size = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    size += len(chunk)
                    if size > max_mb * (1 << 20):
                        print("file too big, skip")
                        return False
                    f.write(chunk)
        return os.path.getsize(path) > 50000
    except Exception as ex:
        print("download fail:", str(ex)[:120])
        return False


def pexels_video(q, idx):
    """Pexels (निःशुल्क, व्यावसायिक प्रयोग मिल्ने) को सामान्य भिडियो। PEXELS_API_KEY चाहिन्छ।"""
    key = os.environ.get("PEXELS_API_KEY")
    if not key or not q:
        return None
    try:
        r = requests.get("https://api.pexels.com/videos/search", headers={"Authorization": key},
                         params={"query": q, "per_page": 10, "orientation": "landscape"}, timeout=40)
        r.raise_for_status()
        for v in r.json().get("videos", []):
            if v.get("duration", 0) < 4:
                continue
            files = [f for f in v.get("video_files", [])
                     if f.get("file_type") == "video/mp4" and 1280 <= (f.get("width") or 0) <= 1920]
            if not files:
                continue
            f = sorted(files, key=lambda x: x["width"])[-1]
            path = f"{OUT}/raw_{idx}.mp4"
            if download_file(f["link"], path):
                return path, f'भिडियो: {v.get("user", {}).get("name", "")} / Pexels ({v.get("url", "")})'
    except Exception as ex:
        print("pexels fail:", str(ex)[:150])
    return None


def pixabay_video(q, idx):
    """Pixabay (निःशुल्क, व्यावसायिक प्रयोग मिल्ने) को सामान्य भिडियो। PIXABAY_API_KEY चाहिन्छ।"""
    key = os.environ.get("PIXABAY_API_KEY")
    if not key or not q:
        return None
    try:
        r = requests.get("https://pixabay.com/api/videos/", timeout=40, params={
            "key": key, "q": q[:100], "per_page": 12, "min_width": 1280, "safesearch": "true"})
        r.raise_for_status()
        for v in r.json().get("hits", []):
            if v.get("duration", 0) < 4:
                continue
            vids = v.get("videos", {})
            pick = None
            for k in ("medium", "large", "small"):
                f = vids.get(k) or {}
                if f.get("url") and 1280 <= (f.get("width") or 0) <= 1920:
                    pick = f
                    break
            if not pick:
                continue
            path = f"{OUT}/raw_{idx}.mp4"
            if download_file(pick["url"], path):
                return path, f'भिडियो: {v.get("user", "")} / Pixabay ({v.get("pageURL", "")})'
    except Exception as ex:
        print("pixabay fail:", str(ex)[:150])
    return None


def commons_video(q, idx):
    """Wikimedia Commons बाट खुला लाइसेन्सको भिडियो (कुञ्जी चाहिँदैन)।"""
    if not q:
        return None
    try:
        r = requests.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=40, params={
            "action": "query", "generator": "search", "gsrsearch": q + " filetype:video",
            "gsrnamespace": 6, "gsrlimit": 8, "prop": "imageinfo",
            "iiprop": "url|extmetadata|size|mime", "format": "json"})
        pages = r.json().get("query", {}).get("pages", {})
        for p in sorted(pages.values(), key=lambda x: x.get("index", 99)):
            ii = (p.get("imageinfo") or [{}])[0]
            if not str(ii.get("mime", "")).startswith("video/") or ii.get("size", 0) > 80 * (1 << 20):
                continue
            md = ii.get("extmetadata", {})
            lic = md.get("LicenseShortName", {}).get("value", "")
            if not (lic.startswith("CC") or "public domain" in lic.lower() or lic.lower().startswith("pd")):
                continue
            ext = ii["url"].rsplit(".", 1)[-1]
            path = f"{OUT}/raw_{idx}.{ext}"
            if download_file(ii["url"], path):
                artist = clean(md.get("Artist", {}).get("value", "")) or "Wikimedia Commons"
                return path, f'भिडियो: "{p.get("title", "")}" - {artist} ({lic}) via Wikimedia Commons'
    except Exception as ex:
        print("commons video fail:", str(ex)[:150])
    return None


def commons_search(q):
    """Wikimedia Commons बाट खुला लाइसेन्सको फोटो खोज्ने।"""
    try:
        r = requests.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=40, params={
            "action": "query", "generator": "search", "gsrsearch": q, "gsrnamespace": 6,
            "gsrlimit": 12, "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime",
            "iiurlwidth": 1600, "format": "json"})
        pages = r.json().get("query", {}).get("pages", {})
    except Exception as ex:
        print("commons fail:", str(ex)[:120])
        return None
    for p in sorted(pages.values(), key=lambda x: x.get("index", 99)):
        ii = (p.get("imageinfo") or [{}])[0]
        if ii.get("mime") not in ("image/jpeg", "image/png") or ii.get("width", 0) < 1000:
            continue
        title = p.get("title", "")
        if any(b in title.lower() for b in ("map", "logo", "symbol", "coat of arms", "diagram", "chart")):
            continue
        md = ii.get("extmetadata", {})
        lic = md.get("LicenseShortName", {}).get("value", "")
        if not (lic.startswith("CC") or "public domain" in lic.lower() or lic.lower().startswith("pd")):
            continue
        artist = clean(md.get("Artist", {}).get("value", "")) or "Wikimedia Commons"
        return (ii.get("thumburl") or ii["url"]), f'"{title}" - {artist} ({lic}) via Wikimedia Commons'
    return None


def get_media(idx, item, news):
    """प्राथमिकता: भिडियो -> फोटो -> (नभए खाली background)। (kind, path, credit) फर्काउँछ।"""
    vq = item.get("video_query", "")
    iq = item.get("image_query", "")
    r = pixabay_video(vq, idx) or pexels_video(vq, idx) or commons_video(iq, idx) or commons_video(vq, idx)
    if r:
        return "video", r[0], r[1]
    path = f"{OUT}/photo_{idx}.jpg"
    cands = []
    if ALLOW_ARTICLE_PHOTOS:
        try:
            n = news[int(item["ref"]) - 1]
            if n.get("image"):
                cands.append((n["image"], ""))
        except Exception:
            pass
    if iq:
        c = commons_search(iq)
        if c:
            cands.append(c)
    for url, credit in cands:
        if download(url, path):
            return "photo", path, credit
    return None, None, ""


# ---------------------------------------------------------------- चित्र
def font(size):
    p = os.environ.get("FONT_PATH")
    if not p:
        paths = glob.glob("/usr/share/fonts/**/NotoSansDevanagari-Bold.ttf", recursive=True)
        if not paths:
            raise SystemExit("Devanagari font भेटिएन")
        p = paths[0]
    try:
        return ImageFont.truetype(p, size, layout_engine=ImageFont.Layout.RAQM)
    except Exception:
        print("WARNING: RAQM छैन, अक्षर बिग्रन सक्छ")
        return ImageFont.truetype(p, size)


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


def fallback_bg(path):
    img = Image.new("RGB", (W, H), (11, 31, 92))
    logo = Image.open("logo.png").convert("RGBA").resize((520, 520))
    img.paste(logo, ((W - 520) // 2, (H - 520) // 2 - 60), logo)
    img.save(path)
    return path


def frame_png(path):
    """पारदर्शी तह; माथि बायाँ कुनामा सानो logo मात्र (च्यानलको नाम छैन)।"""
    fr = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    logo = Image.open("logo.png").convert("RGBA").resize((130, 130))
    shadow = Image.new("RGBA", (150, 150), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse((6, 10, 146, 146), fill=(0, 0, 0, 110))
    fr.alpha_composite(shadow, (30, 28))
    fr.alpha_composite(logo, (40, 36))
    fr.save(path)
    return path


BW, CW = 1400, 240


def chev(bh, i):
    cy, ch = bh // 2, int(bh * 0.30)
    x0 = 62 + i * 38
    return [(x0, cy - ch), (x0 + 30, cy), (x0, cy + ch)]


def bar_pngs(prefix, headline):
    """रातो समाचार पट्टी + ३ ओटा सेता chevron (highlight sweep को लागि)। (bar, [w0,w1,w2]) फर्काउँछ।"""
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    maxw = BW - CW - 90
    size, lines = 66, None
    while size >= 44:                       # एक पङ्क्तिमा अटाउन्जेल सानो बनाउने
        f = font(size)
        if tmp.textlength(headline, font=f) <= maxw:
            lines = [headline]
            break
        size -= 4
    if lines is None:                       # अझै नअटे दुई पङ्क्ति
        size = 50
        f = font(size)
        lines = wrap(tmp, headline, f, maxw)[:2]
    lh = int(size * 1.4)
    bh = max(96, lh * len(lines) + 34)

    bar = Image.new("RGBA", (BW, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(bar)
    for x in range(CW - 40, BW):
        t = (x - CW) / (BW - CW)
        d.line((x, 0, x, bh), fill=(int(215 - 45 * t), 0, int(10 + 5 * t), 255))
    d.polygon([(0, 0), (CW, 0), (CW - 42, bh), (0, bh)], fill=(240, 240, 240, 255))
    for i, col in enumerate([(220, 30, 30), (245, 130, 20), (250, 190, 20)]):
        d.line(chev(bh, i), fill=col + (255,), width=17, joint="curve")
    y = (bh - lh * len(lines)) // 2
    for ln in lines:
        tw = d.textlength(ln, font=f)
        d.text((CW + (BW - CW - tw) / 2 - 10, y), ln, font=f, fill="white")
        y += lh
    bar_path = f"{prefix}_bar.png"
    bar.save(bar_path)

    whites = []
    for i in range(3):
        w = Image.new("RGBA", (BW, bh), (0, 0, 0, 0))
        ImageDraw.Draw(w).line(chev(bh, i), fill=(255, 255, 255, 255), width=19, joint="curve")
        glow = w.filter(ImageFilter.GaussianBlur(6))
        w = Image.alpha_composite(glow, w)
        p = f"{prefix}_w{i}.png"
        w.save(p)
        whites.append(p)
    return bar_path, whites


SWEEP_W = W + 500


def sweep_png(path):
    """दृश्य बदलिँदा पर्दाभरि सर्ने रातो wipe (logo सहित)।"""
    img = Image.new("RGBA", (SWEEP_W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for x in range(SWEEP_W):
        t = x / SWEEP_W
        d.line((x, 0, x, H), fill=(int(205 - 60 * t), 0, int(12 + 10 * t), 255))
    mask = Image.new("L", (SWEEP_W, H), 0)
    ImageDraw.Draw(mask).polygon([(160, 0), (SWEEP_W, 0), (SWEEP_W - 160, H), (0, H)], fill=255)
    img.putalpha(mask)
    d = ImageDraw.Draw(img)
    d.polygon([(SWEEP_W - 330, 0), (SWEEP_W - 290, 0), (SWEEP_W - 450, H), (SWEEP_W - 490, H)],
              fill=(255, 255, 255, 235))
    logo = Image.open("logo.png").convert("RGBA").resize((420, 420))
    img.alpha_composite(logo, ((SWEEP_W - 420) // 2, (H - 420) // 2))
    img.save(path)
    return path


def thumbnail(path, title, photo):
    if photo:
        bg = Image.open(photo).convert("RGB")
        r = max(W / bg.width, H / bg.height)
        bg = bg.resize((int(bg.width * r), int(bg.height * r)))
        bg = bg.crop(((bg.width - W) // 2, (bg.height - H) // 2, (bg.width + W) // 2, (bg.height + H) // 2))
    else:
        bg = Image.open(fallback_bg(f"{OUT}/_bg.png")).convert("RGB")
    bg = Image.alpha_composite(bg.convert("RGBA"), Image.new("RGBA", (W, H), (5, 12, 40, 150)))
    d = ImageDraw.Draw(bg)
    logo = Image.open("logo.png").convert("RGBA").resize((170, 170))
    bg.paste(logo, (50, 40), logo)
    f = font(120)
    lines = wrap(d, title, f, W - 160)[:3]
    y = H - 100 - 160 * len(lines)
    for ln in lines:
        d.text((80, y), ln, font=f, fill="white", stroke_width=6, stroke_fill=(0, 0, 0))
        y += 160
    bg.convert("RGB").resize((1280, 720)).save(path, quality=88)


# ---------------------------------------------------------------- आवाज प्रभाव र सङ्गीत
def make_whoosh(path):
    run([FFMPEG, "-y", "-f", "lavfi", "-i",
         "anoisesrc=d=0.7:c=pink:a=0.6,highpass=f=500,lowpass=f=4500,"
         "afade=t=in:d=0.3,afade=t=out:st=0.35:d=0.35", "-ar", "44100", "-ac", "2", path])
    return path


def make_music(path):
    """repo मा music.mp3 छ भने त्यही; नभए आफैं समाचार-शैलीको सरल सङ्गीत बनाउने।"""
    if os.path.exists("music.mp3"):
        return "music.mp3"
    expr = ("0.20*sin(2*PI*110*t)*(0.6+0.4*sin(2*PI*2*t))"
            "+0.12*sin(2*PI*165*t)*(0.5+0.5*sin(2*PI*1*t))"
            "+0.09*sin(2*PI*220*t)*(0.5+0.5*sin(2*PI*0.5*t))"
            "+0.07*sin(2*PI*330*t)*(0.5+0.5*sin(2*PI*2*t+1.5))")
    run([FFMPEG, "-y", "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=44100:d=60:c=stereo",
         "-af", "lowpass=f=1800,aecho=0.8:0.6:60:0.3", path])
    return path


# ---------------------------------------------------------------- भिडियो
def make_clip(idx, kind, media, audio, dur, bar, whites, frame, sweep, whoosh, first, last):
    n = int(dur * 25) + 12
    if kind == "video":
        pre = ["-stream_loop", "-1", "-i", media]
        vf = ("scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
              "fps=25,setsar=1")
    else:
        pre = ["-i", media or fallback_bg(f"{OUT}/bg.png")]
        vf = ("scale=2112:1188:force_original_aspect_ratio=increase,crop=2112:1188,"
              f"zoompan=z='min(1+0.0004*on,1.1)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={n}:s=1920x1080:fps=25")
    cmd = [FFMPEG, "-y"] + pre + ["-i", audio]
    for p in [frame, bar] + whites + [sweep]:
        cmd += ["-loop", "1", "-framerate", "25", "-i", p]
    if not last:
        cmd += ["-i", whoosh]

    s0 = 0.15 if first else 0.5          # शीर्षक पट्टी स्लाइड सुरु हुने समय
    bx = f"if(lt(t,{s0}),-3000,if(lt(t,{s0 + 0.5}),-w+(260+w)*(t-{s0})/0.5,260))"
    out_start = dur - 0.45
    sx = (f"if(lt(t,0.45),-300+({W}+350)*t/0.45,"
          f"if(gt(t,{out_start:.2f}),-{SWEEP_W}+({SWEEP_W}-300)*(t-{out_start:.2f})/0.45,-5000))")
    if first:
        sx = f"if(gt(t,{out_start:.2f}),-{SWEEP_W}+({SWEEP_W}-300)*(t-{out_start:.2f})/0.45,-5000)"
    if last:
        sx = "if(lt(t,0.45),-300+(%d+350)*t/0.45,-5000)" % W
        if first:
            sx = "-5000"

    fc = (f"[0:v]{vf}[bg];"
          f"[bg][2:v]overlay=0:0:format=auto[v1];"
          f"[3:v][4:v]overlay=0:0:format=auto:enable='between(mod(t,1.8),0.15,0.45)'[b1];"
          f"[b1][5:v]overlay=0:0:format=auto:enable='between(mod(t,1.8),0.35,0.65)'[b2];"
          f"[b2][6:v]overlay=0:0:format=auto:enable='between(mod(t,1.8),0.55,0.85)'[b3];"
          f"[v1][b3]overlay=x='{bx}':y=H-h-45:format=auto[v2];"
          f"[v2][7:v]overlay=x='{sx}':y=0:format=auto[v];"
          f"[1:a]atempo={SPEED},apad[nar]")
    if last:
        amap = "[nar]"
    else:
        ms = max(0, int((dur - 0.6) * 1000))
        fc += (f";[8:a]adelay={ms}|{ms},volume=3.0[wh];"
               f"[nar][wh]amix=inputs=2:duration=first:normalize=0[a]")
        amap = "[a]"
    clip = f"{OUT}/clip{idx}.mp4"
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", amap,
            "-c:v", "libx264", "-preset", "veryfast", "-r", "25", "-c:a", "aac", "-b:a", "160k",
            "-ar", "44100", "-ac", "2", "-pix_fmt", "yuv420p", "-t", f"{dur:.2f}", clip]
    run(cmd)
    return os.path.basename(clip)


def concat(clips, final, music):
    with open(f"{OUT}/list.txt", "w") as f:
        for c in clips:
            f.write(f"file '{c}'\n")
    raw = f"{OUT}/_nomusic.mp4"
    run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", f"{OUT}/list.txt", "-c", "copy", raw])
    run([FFMPEG, "-y", "-i", raw, "-stream_loop", "-1", "-i", music, "-filter_complex",
         f"[1:a]volume={MUSIC_VOLUME}[m];[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
         "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", final])


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

    frame = frame_png(f"{OUT}/frame.png")
    sweep = sweep_png(f"{OUT}/sweep.png")
    whoosh = make_whoosh(f"{OUT}/whoosh.wav")
    music = make_music(f"{OUT}/music_bed.mp3")

    clips, credits, total, thumb_src = [], [], 0.0, None
    for i, it in enumerate(items, 1):
        kind, media, credit = get_media(i, it, news)
        if credit:
            credits.append(credit)
        if thumb_src is None and media:
            if kind == "photo":
                thumb_src = media
            else:
                fp = f"{OUT}/vf_{i}.jpg"
                try:
                    run([FFMPEG, "-y", "-ss", "1", "-i", media, "-frames:v", "1", fp])
                    thumb_src = fp
                except SystemExit:
                    pass
        audio = tts(it["narration"], f"{OUT}/a_{i}")
        dur = duration(audio) / SPEED + 0.6
        bar, whites = bar_pngs(f"{OUT}/h_{i}", it["headline"])
        clips.append(make_clip(i, kind, media, audio, dur, bar, whites, frame, sweep, whoosh,
                               first=(i == 1), last=(i == len(items))))
        total += dur
        print(f"clip {i} ok ({dur:.0f}s, media={kind})")

    print(f"कुल अवधि: {total / 60:.1f} मिनेट")
    final = f"{OUT}/final_{MODE}.mp4"
    concat(clips, final, music)

    thumb = f"{OUT}/thumb_{MODE}.jpg"
    thumbnail(thumb, data["title"], thumb_src)

    src_lines = []
    for it in items:
        try:
            n = news[int(it["ref"]) - 1]
            src_lines.append(f"- {n['source']}: {n['link']}")
        except Exception:
            pass
    desc = data["description"] + "\n\nस्रोतहरू:\n" + "\n".join(dict.fromkeys(src_lines))
    if credits:
        desc += "\n\nफोटो/भिडियो:\n" + "\n".join(dict.fromkeys(credits))
    desc += ("\n\nनोट: यो भिडियोको वाचनमा AI आवाज प्रयोग गरिएको छ। समाचार स्रोतका शीर्षकमा आधारित सारांश हो।\n"
             "हाम्रै खबर - सधै समाचार, सत्य समाचार")
    upload(final, thumb, {"title": data["title"], "description": desc})


if __name__ == "__main__":
    main()
