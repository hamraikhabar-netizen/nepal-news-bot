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
MUSIC_TARGET_DB = -30  # पृष्ठभूमि सङ्गीतको औसत आवाज (dB); -26 = चर्को, -34 = हल्का
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


ASSETS = (None, [], {})   # (transition, music list, info) - main() मा भरिन्छ


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
                pub = clean((e.get("source") or {}).get("title", "")) or src
                items.append({"title": title, "summary": clean(e.get("summary", ""))[:300],
                              "source": pub, "link": e.get("link", ""), "image": entry_image(e)})
            print(f"feed ok: {src} ({len(d.entries)})")
        except Exception as ex:
            print("feed failed:", url, ex)
    return items


# ---------------------------------------------------------------- Gemini (लेखन)
def pick_models():
    """चल्न सक्ने Gemini model हरूको क्रमबद्ध सूची (एउटा व्यस्त भए अर्कोमा जान)।"""
    global NAMES
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         headers={"x-goog-api-key": GEMINI_KEY}, params={"pageSize": 200}, timeout=60)
        r.raise_for_status()
        for m in r.json().get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                NAMES.append(m["name"].split("/")[-1])
    except Exception as ex:
        print("model list failed:", ex)
    order = []
    pref = os.environ.get("GEMINI_MODEL")
    if pref:
        order.append(pref)
    for p in ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-3-flash-preview",
              "gemini-2.5-flash", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-flash-latest",
              "gemini-2.5-flash-lite", "gemini-3.5-flash-lite", "gemini-4-hydrogen"]:
        if p in NAMES and p not in order:
            order.append(p)
    bad = ("tts", "live", "audio", "image", "embedding", "robotics", "computer", "omni", "transcribe")
    for n in sorted(NAMES):
        if ("flash" in n or "hydrogen" in n) and not any(x in n for x in bad) and n not in order:
            order.append(n)
    if not order:
        raise SystemExit("चल्ने Gemini model भेटिएन।")
    print("model order:", order[:6])
    return order


def gemini(prompt):
    global MODEL
    if MODEL is None:
        MODEL = pick_models()
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4,
                                 "maxOutputTokens": 16000}}
    last = None
    for rnd in range(3):                       # तीन चक्कर, हरेक model एक-एक पटक
        for model in MODEL[:6]:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            try:
                r = requests.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=240)
                r.raise_for_status()
                txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                data = json.loads(txt)
                print("gemini ok with:", model)
                return data
            except Exception as ex:
                last = ex
                print("gemini retry:", model, str(ex)[:120])
                time.sleep(5)
        time.sleep(30 * (rnd + 1))             # सबै व्यस्त भए केही बेर पर्खने
    raise SystemExit(f"Gemini failed: {last}")


RULES = (
    "नियम: तलको सूचीमा भएका शीर्षक र सारांश मात्र आधार बनाऊ। कुनै नाम, संख्या, मिति वा तथ्य आफैं नथप। "
    "पुष्टि नभएको वा एउटै स्रोतको कुरा 'भनिएको छ' भनेर लेख। कसैको पक्ष नलेऊ, तटस्थ र सरल नेपालीमा लेख। "
    "narration मा समाचार मात्र लेख: 'नमस्कार', 'यो समाचार', 'हेर्नुहोस्', 'सुन्नुहोस्', लाइक/सब्सक्राइब "
    "जस्ता कुनै अतिरिक्त कुरा नलेख; सिधै समाचारको मुख्य कुराबाट सुरु गर। "
    "headline अत्यन्त छोटो (४०-४५ अक्षरभित्र), पूरा अर्थ दिने, एक पङ्क्तिमा अटाउने र टुङ्गिएको हुनुपर्छ "
    "(जस्तै 'प्रधानमन्त्री भारत भ्रमणमा')। "
    "'ref' मा सूचीको नम्बर (integer) राख। "
    "'type' मा: 'person_event' (नाम चलेका व्यक्ति/नेता/सरकारी कार्यक्रम/भ्रमण), 'accident' (दुर्घटना, आगलागी, "
    "बाढीपहिरो, हत्या/अपराध, मृत्यु), 'sensitive' (बलात्कार/यौन हिंसा, बाल दुर्व्यवहार, आत्महत्या), "
    "वा 'general' मध्ये एउटा राख। "
    "'sensitive' प्रकारमा: पीडितको नाम, ठेगाना, स्कुल, परिवार वा चिनिने कुनै विवरण नलेख; घटनाको विवरण/तरिका नलेख; "
    "आरोपितलाई दोषी नभन ('आरोप', 'प्रहरीका अनुसार' भन); सूचीमा भएको अदालत/प्रहरीको कारबाही मात्र भन; "
    "आत्महत्याको तरिका कहिल्यै नलेख। video_query मा प्रतीकात्मक दृश्य मात्र राख "
    "(जस्तै 'justice scale', 'gavel court', 'police station', 'helping hands', 'stop violence', 'candle')। "
    "'image_queries' मा अंग्रेजीमा २-३ खोज वाक्यांशको सूची: पहिलो सबैभन्दा विशिष्ट (जस्तै 'Balen Shah India visit'), "
    "दोस्रो मुख्य व्यक्ति/स्थान मात्र (जस्तै 'Balen Shah'); सूचीमा नभएको नाम नबनाऊ। "
    "'accident' प्रकारमा: पीडितको नाम/अनुहार वा डरलाग्दो विवरण नलेख; घटनाको तथ्य मात्र सरल र संवेदनशील भाषामा लेख; "
    "video_query मा सुरक्षित सामान्य दृश्य मात्र राख (जस्तै 'ambulance', 'highway traffic', 'police car', "
    "'fire truck', 'hospital corridor', 'rain flood street')। "
    "'video_query' मा त्यो समाचारसँग मिल्ने सामान्य दृश्यको अंग्रेजी खोजशब्द २-३ शब्दमा लेख "
    "(जस्तै 'parliament building', 'flood water', 'airport', 'cricket stadium', 'protest crowd')। "
)

SCHEMA = (
    'JSON मात्र फर्काऊ: {"title": "YouTube शीर्षक (नेपालीमा, ९० अक्षरभित्र)", '
    '"description": "२-३ वाक्यको विवरण", '
    '"items": [{"headline": "छोटो शीर्षक", "narration": "वाचन पाठ", "ref": 1, '
    '"type": "person_event", "image_queries": ["Balen Shah India visit", "Balen Shah"], '
    '"video_query": "airport red carpet"}]}. '
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


def pixabay_image(q, idx):
    """Pixabay को निःशुल्क फोटो/चित्र (उही API key)। संवेदनशील समाचारमा प्रतीकात्मक दृश्यको लागि।"""
    key = os.environ.get("PIXABAY_API_KEY")
    if not key or not q:
        return None
    try:
        r = requests.get("https://pixabay.com/api/", timeout=40, params={
            "key": key, "q": q[:100], "image_type": "all", "orientation": "horizontal",
            "min_width": 1280, "safesearch": "true", "per_page": 10})
        r.raise_for_status()
        for v in r.json().get("hits", []):
            url = v.get("largeImageURL")
            path = f"{OUT}/photo_{idx}.jpg"
            if url and download(url, path):
                return path, f'फोटो: {v.get("user", "")} / Pixabay ({v.get("pageURL", "")})'
    except Exception as ex:
        print("pixabay image fail:", str(ex)[:150])
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


def commons_search(q, newest=False):
    """Wikimedia Commons बाट खुला लाइसेन्सको फोटो खोज्ने। newest=True भए नयाँ अपलोड पहिले।"""
    params = {"action": "query", "generator": "search", "gsrsearch": q, "gsrnamespace": 6,
              "gsrlimit": 15, "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime",
              "iiurlwidth": 1600, "format": "json"}
    if newest:
        params["gsrsort"] = "create_timestamp_desc"
    try:
        r = requests.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=40, params=params)
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


def commons_photo(queries, idx):
    path = f"{OUT}/photo_{idx}.jpg"
    for q in queries:
        for newest in (True, False):            # पहिले सबैभन्दा नयाँ, नभए सबैभन्दा मिल्ने
            c = commons_search(q, newest)
            if c and download(c[0], path):
                return path, c[1]
    return None


def commons_video_any(queries, idx):
    for q in queries:
        r = commons_video(q, idx)
        if r:
            return r
    return None


def get_media(idx, item, news):
    """समाचारको प्रकार अनुसार दृश्य छान्ने। (kind, path, credit) फर्काउँछ।
    person_event: Commons भिडियो -> Commons फोटो -> Pixabay/Pexels भिडियो
    general     : Pixabay/Pexels भिडियो -> Commons भिडियो -> Commons फोटो
    accident    : सुरक्षित सामान्य भिडियो (Pixabay/Pexels), नभए Pixabay फोटो; वास्तविक घटनाको फोटो/भिडियो प्रयोग गर्दैन
    sensitive   : प्रतीकात्मक Pixabay चित्र (न्यायको तराजु, हथौडा आदि) मात्र; पीडित/घटनाको कुनै दृश्य होइन
    """
    typ = item.get("type", "general")
    vq = item.get("video_query", "")
    iqs = [q for q in (item.get("image_queries") or []) if q]
    if not iqs and item.get("image_query"):
        iqs = [item["image_query"]]

    def stock():
        return pixabay_video(vq, idx) or pexels_video(vq, idx)

    r = None
    if typ == "sensitive":      # बलात्कार/यौन हिंसा/बाल दुर्व्यवहार/आत्महत्या: प्रतीकात्मक सुरक्षित चित्र मात्र
        p = pixabay_image(vq, idx)
        if p:
            return "photo", p[0], p[1]
        r = stock()
        return ("video", r[0], r[1]) if r else (None, None, "")
    if typ == "accident":
        r = stock()
        if r:
            return "video", r[0], r[1]
        p = pixabay_image(vq, idx)
        return ("photo", p[0], p[1]) if p else (None, None, "")
    if typ == "person_event":
        r = commons_video_any(iqs, idx)
        if r:
            return "video", r[0], r[1]
        p = commons_photo(iqs, idx)
        if p:
            return "photo", p[0], p[1]
        r = stock()
        if r:
            return "video", r[0], r[1]
    else:
        r = stock() or commons_video_any([vq] + iqs, idx)
        if r:
            return "video", r[0], r[1]
        p = commons_photo(iqs, idx)
        if p:
            return "photo", p[0], p[1]
    if ALLOW_ARTICLE_PHOTOS:
        try:
            n = news[int(item["ref"]) - 1]
            if n.get("image") and download(n["image"], f"{OUT}/photo_{idx}.jpg"):
                return "photo", f"{OUT}/photo_{idx}.jpg", ""
        except Exception:
            pass
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


def find_logo():
    """repo मा रहेको logo खोज्ने: logo.png -> नाममा logo/khabar/hamr भएको png -> कुनै पनि png।"""
    pngs = [f for f in os.listdir(".") if f.lower().endswith(".png")]
    for pat in ("logo.png",):
        if pat in pngs:
            return pat
    for key in ("logo", "khabar", "hamr", "hk"):
        for f in sorted(pngs):
            if key in f.lower():
                return f
    return sorted(pngs)[0] if pngs else None


LOGO_PATH = find_logo()
print("logo:", LOGO_PATH)


def load_logo(size):
    """logo लाई अनुपात नबिगारी size x size भित्र अटाउने गरी तयार पार्ने।"""
    im = Image.open(LOGO_PATH).convert("RGBA")
    im.thumbnail((size * 2, size), Image.LANCZOS)
    return im


def fallback_bg(path):
    img = Image.new("RGB", (W, H), (11, 31, 92))
    logo = load_logo(520)
    img.paste(logo, ((W - logo.width) // 2, (H - logo.height) // 2 - 60), logo)
    img.save(path)
    return path


SRC_LABELS = {
    "online khabar": "अनलाइनखबर", "onlinekhabar": "अनलाइनखबर", "अनलाइनखबर": "अनलाइनखबर",
    "setopati": "सेतोपाटी", "ratopati": "रातोपाटी", "bbc": "बीबीसी नेपाली",
    "ekantipur": "कान्तिपुर", "kantipur": "कान्तिपुर", "kathmandu post": "The Kathmandu Post",
    "himalayan": "The Himalayan Times", "annapurna": "अन्नपूर्ण पोस्ट",
    "rastriya samachar": "राससस", "rss": "राससस", "nepal samacharpatra": "नेपाल समाचारपत्र",
}


def source_label(src):
    low = (src or "").lower()
    for k, v in SRC_LABELS.items():
        if k in low:
            return v
    return re.sub(r"\s*[-–|]\s*Google News.*$", "", src or "").strip('" ') or "Google News"


def credit_site(credit):
    for k in ("Pixabay", "Pexels", "Wikimedia Commons"):
        if k in (credit or ""):
            return k
    return ""


def latin_font(size):
    p = os.environ.get("LATIN_FONT_PATH")
    if not p:
        for pat in ("/usr/share/fonts/**/NotoSans-Regular.ttf", "/usr/share/fonts/**/DejaVuSans.ttf"):
            g = glob.glob(pat, recursive=True)
            if g:
                p = g[0]
                break
    return ImageFont.truetype(p, size) if p else ImageFont.load_default()


def draw_mixed(d, right, y, text, size, fill):
    """नेपाली र अंग्रेजी अक्षर मिसिएको सानो पाठ दायाँ छेउबाट मिलाएर लेख्ने।"""
    runs = re.findall(r"[A-Za-z0-9 .,&'\-/:@]+|[^A-Za-z0-9 .,&'\-/:@]+", text)
    parts, total = [], 0
    for r in runs:
        f = latin_font(size) if re.match(r"[A-Za-z0-9 .,&'\-/:@]", r[0]) else font(size)
        w = d.textlength(r, font=f)
        parts.append((r, f, w))
        total += w
    x = right - total
    for r, f, w in parts:
        d.text((x, y), r, font=f, fill=fill)
        x += w
    return total


def frame_png(path, label=None):
    """पारदर्शी तह; माथि बायाँ logo, र (भए) माथि दायाँ सानो स्रोत/क्रेडिट पाठ।"""
    fr = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    logo = load_logo(130)
    sh = Image.new("RGBA", (logo.width + 40, logo.height + 40), (0, 0, 0, 0))
    sh.paste((0, 0, 0, 120), (20, 26), logo.getchannel("A"))
    sh = sh.filter(ImageFilter.GaussianBlur(7))
    fr.alpha_composite(sh, (20, 16))
    fr.alpha_composite(logo, (40, 36))
    if label:
        tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        size = 30
        tw = draw_mixed(tmp, 0, 0, label, size, (0, 0, 0))   # चौडाइ नाप्न मात्र
        pill = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        pd = ImageDraw.Draw(pill)
        pd.rounded_rectangle((W - 40 - tw - 36, 34, W - 40, 34 + size + 28), radius=14, fill=(0, 0, 0, 120))
        fr = Image.alpha_composite(fr, pill)
        draw_mixed(ImageDraw.Draw(fr), W - 40 - 18, 34 + 10, label, size, (255, 255, 255, 235))
    fr.save(path)
    return path


BW, CW = 1400, 240
BAR_C0, BAR_C1 = (215, 0, 10), (170, 0, 15)      # शीर्षक पट्टीको रङ (बायाँ -> दायाँ)
SW_C0, SW_C1 = (205, 0, 12), (145, 0, 22)        # wipe को रङ


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
        d.line((x, 0, x, bh), fill=tuple(int(BAR_C0[k] + (BAR_C1[k] - BAR_C0[k]) * t) for k in range(3)) + (255,))
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


RW, AW, NW, SL = W + 900, 90, 110, 170        # लाल पट्टी, सेतो धार, नीलो धार, तिरछोपन
X0 = -(RW + AW + SL + 150)
SPAN = W + NW + 100 - X0
TT = 0.9                                          # एक transition को कुल समय
LAYERS = [("white", AW, RW, 0.0), ("red", RW, 0, 0.025), ("navy", NW, -NW, 0.06)]


def band_png(path, w, kind):
    img = Image.new("RGBA", (w + SL, H), (0, 0, 0, 0))
    mask = Image.new("L", (w + SL, H), 0)
    ImageDraw.Draw(mask).polygon([(SL, 0), (w + SL, 0), (w, H), (0, H)], fill=255)
    fill = Image.new("RGBA", (w + SL, H), (0, 0, 0, 255))
    d = ImageDraw.Draw(fill)
    if kind == "white":
        d.rectangle((0, 0, w + SL, H), fill=(255, 255, 255, 255))
    elif kind == "navy":
        d.rectangle((0, 0, w + SL, H), fill=(11, 31, 92, 255))
    else:
        for x in range(w + SL):
            t = x / (w + SL)
            d.line((x, 0, x, H), fill=tuple(int(SW_C0[k] + (SW_C1[k] - SW_C0[k]) * t) for k in range(3)) + (255,))
        # हल्का चमक (gloss) धार
        d.polygon([(w - 60, 0), (w - 20, 0), (w - 20 - SL, H), (w - 60 - SL, H)], fill=(255, 255, 255, 40))
    img.paste(fill, (0, 0), mask)
    img.save(path)
    return path


def sweep_layers(prefix):
    """व्यावसायिक तीन-तह wipe (सेतो धार, रातो पट्टी, नीलो धार) - logo बिना।"""
    return [band_png(f"{prefix}_{k}.png", w, k) for k, w, _, _ in LAYERS]


def _S(arg):
    p = f"clip(({arg})/{TT},0,1)"
    return f"{p}*{p}*(3-2*{p})"                   # smoothstep: सुरुमा ढिलो, बीचमा छिटो, अन्त्यमा ढिलो


def layer_x(ox, delay, D, first, last):
    start = "1" if first else _S(f"t+0.45-{delay}")
    end = "0" if last else _S(f"t-({D - 0.45:.3f})-{delay}")
    return f"{ox}+({X0})+{SPAN}*if(lt(t,{D / 2:.3f}),{start},{end})"


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
    logo = load_logo(170)
    bg.paste(logo, (50, 40), logo)
    f = font(120)
    lines = wrap(d, title, f, W - 160)[:3]
    y = H - 100 - 160 * len(lines)
    for ln in lines:
        d.text((80, y), ln, font=f, fill="white", stroke_width=6, stroke_fill=(0, 0, 0))
        y += 160
    bg.convert("RGB").resize((1280, 720)).save(path, quality=88)


# ---------------------------------------------------------------- आवाज प्रभाव र सङ्गीत
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac")


def audio_info(path):
    """(अवधि सेकेन्ड, औसत आवाज dB)। ffmpeg को आउटपुटबाट पढ्ने।"""
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", path, "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    txt = p.stderr
    dur, mean = 0.0, -30.0
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", txt)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"mean_volume: (-?[\d.]+) dB", txt)
    if m:
        mean = float(m.group(1))
    return dur, mean


def scan_assets():
    """repo मा राखिएका आवाज फाइल: सबैभन्दा छोटो (<=10s) = transition sound, लामा = news music।"""
    files = [f for f in sorted(os.listdir(".")) if f.lower().endswith(AUDIO_EXT)]
    info = {f: audio_info(f) for f in files}
    trans, music = None, []
    for f, (d, _) in info.items():
        low = f.lower()
        if any(k in low for k in ("transition", "whoosh", "swoosh", "effect")):
            trans = f
    if trans is None:
        short = [f for f, (d, _) in info.items() if 0 < d <= 10]
        if short:
            trans = min(short, key=lambda f: info[f][0])
    music = [f for f, (d, _) in info.items() if f != trans and d > 10]
    print("आवाज फाइलहरू:", {f: f"{info[f][0]:.0f}s" for f in files}, "| transition:", trans, "| music:", music)
    return trans, music, info


def gain_to(mean_db, target):
    return max(-25.0, min(25.0, target - mean_db))


def make_whoosh(path):
    """तपाईंको transition sound (भए), नभए आफैं बनाएको whoosh। आवाज बराबर बनाइन्छ।"""
    trans, _, info = ASSETS
    if trans:
        g = gain_to(info[trans][1], -16.0)
        run([FFMPEG, "-y", "-i", trans, "-t", "1.6", "-af",
             f"volume={g:.1f}dB,afade=t=out:st=1.2:d=0.4", "-ar", "44100", "-ac", "2", path])
        return path
    run([FFMPEG, "-y", "-f", "lavfi", "-i",
         "anoisesrc=d=0.7:c=pink:a=0.6,highpass=f=500,lowpass=f=4500,"
         "afade=t=in:d=0.3,afade=t=out:st=0.35:d=0.35,volume=3", "-ar", "44100", "-ac", "2", path])
    return path


def make_music(path, style="news", pick=0):
    """तपाईंको news music (भए) मध्ये एउटा; नभए आफैं बनाएको सरल सङ्गीत। (path, gain_dB) फर्काउँछ।"""
    _, music, info = ASSETS
    if music:
        f = music[pick % len(music)]
        print("music:", f)
        return f, gain_to(info[f][1], MUSIC_TARGET_DB)
    if style == "calm":
        expr = ("0.16*sin(2*PI*196*t)*(0.7+0.3*sin(2*PI*0.20*t))"
                "+0.12*sin(2*PI*246.94*t)*(0.7+0.3*sin(2*PI*0.25*t+1))"
                "+0.10*sin(2*PI*293.66*t)*(0.7+0.3*sin(2*PI*0.15*t+2))"
                "+0.05*sin(2*PI*392*t)*(0.5+0.5*sin(2*PI*0.10*t))")
        af = "lowpass=f=1200,aecho=0.8:0.7:120:0.35"
    else:
        expr = ("0.20*sin(2*PI*110*t)*(0.6+0.4*sin(2*PI*2*t))"
                "+0.12*sin(2*PI*165*t)*(0.5+0.5*sin(2*PI*1*t))"
                "+0.09*sin(2*PI*220*t)*(0.5+0.5*sin(2*PI*0.5*t))"
                "+0.07*sin(2*PI*330*t)*(0.5+0.5*sin(2*PI*2*t+1.5))")
        af = "lowpass=f=1800,aecho=0.8:0.6:60:0.3"
    run([FFMPEG, "-y", "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=44100:d=60:c=stereo", "-af", af, path])
    return path, gain_to(audio_info(path)[1], MUSIC_TARGET_DB)


# ---------------------------------------------------------------- भिडियो
def make_clip(idx, kind, media, audio, dur, bar, whites, frame, layers, whoosh, first, last):
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
    for p in [frame, bar] + whites + layers[::-1]:       # layers: नीलो, रातो, सेतो (तल देखि माथि)
        cmd += ["-loop", "1", "-framerate", "25", "-i", p]
    if not last:
        cmd += ["-i", whoosh]

    s0 = 0.15 if first else 0.55          # शीर्षक पट्टी स्लाइड सुरु हुने समय (transition पछि)
    bx = f"if(lt(t,{s0}),-3000,if(lt(t,{s0 + 0.5}),-w+(260+w)*(t-{s0})/0.5,260))"

    # layers = [white, red, navy]; इनपुट क्रम 7=navy, 8=red, 9=white
    lx = {name: layer_x(ox, dl, dur, first, last) for name, _, ox, dl in LAYERS}
    fc = (f"[0:v]{vf}[bg];"
          f"[bg][2:v]overlay=0:0:format=auto[v1];"
          f"[3:v][4:v]overlay=0:0:format=auto:enable='between(mod(t,1.8),0.15,0.45)'[b1];"
          f"[b1][5:v]overlay=0:0:format=auto:enable='between(mod(t,1.8),0.35,0.65)'[b2];"
          f"[b2][6:v]overlay=0:0:format=auto:enable='between(mod(t,1.8),0.55,0.85)'[b3];"
          f"[v1][b3]overlay=x='{bx}':y=H-h-45:format=auto[v2];"
          f"[v2][7:v]overlay=x='{lx['navy']}':y=0:format=auto[v3];"
          f"[v3][8:v]overlay=x='{lx['red']}':y=0:format=auto[v4];"
          f"[v4][9:v]overlay=x='{lx['white']}':y=0:format=auto[v];"
          f"[1:a]atempo={SPEED},apad[nar]")
    if last:
        amap = "[nar]"
    else:
        ms = max(0, int((dur - 0.7) * 1000))
        fc += (f";[10:a]adelay={ms}|{ms}[wh];"
               f"[nar][wh]amix=inputs=2:duration=first:normalize=0[a]")
        amap = "[a]"
    clip = f"{OUT}/clip{idx}.mp4"
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", amap,
            "-c:v", "libx264", "-preset", "veryfast", "-r", "25", "-c:a", "aac", "-b:a", "160k",
            "-ar", "44100", "-ac", "2", "-pix_fmt", "yuv420p", "-t", f"{dur:.2f}", clip]
    run(cmd)
    return os.path.basename(clip)


def concat(clips, final, music, gain_db):
    with open(f"{OUT}/list.txt", "w") as f:
        for c in clips:
            f.write(f"file '{c}'\n")
    raw = f"{OUT}/_nomusic.mp4"
    run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", f"{OUT}/list.txt", "-c", "copy", raw])
    run([FFMPEG, "-y", "-i", raw, "-stream_loop", "-1", "-i", music, "-filter_complex",
         f"[1:a]volume={gain_db:.1f}dB[m];[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
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
                    "tags": meta.get("tags", ["नेपाल", "समाचार", "हाम्रै खबर", "Nepal news", "Hamrai Khabar"]),
                    "categoryId": meta.get("category", "25"), "defaultLanguage": "ne"},
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

    global ASSETS
    ASSETS = scan_assets()
    layers = sweep_layers(f"{OUT}/sw")
    whoosh = make_whoosh(f"{OUT}/whoosh.wav")
    music, music_gain = make_music(f"{OUT}/music_bed.mp3", pick=(0 if MODE == "morning" else 1))

    clips, credits, total, thumb_src = [], [], 0.0, None
    for i, it in enumerate(items, 1):
        kind, media, credit = get_media(i, it, news)
        if credit:
            credits.append(credit)
        try:
            lab = "स्रोत: " + source_label(news[int(it["ref"]) - 1]["source"])
        except Exception:
            lab = ""
        site = credit_site(credit)
        if site:
            lab += (" | " if lab else "") + "दृश्य: " + site
        frame = frame_png(f"{OUT}/frame_{i}.png", lab or None)
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
        clips.append(make_clip(i, kind, media, audio, dur, bar, whites, frame, layers, whoosh,
                               first=(i == 1), last=(i == len(items))))
        total += dur
        print(f"clip {i} ok ({dur:.0f}s, media={kind})")

    print(f"कुल अवधि: {total / 60:.1f} मिनेट")
    final = f"{OUT}/final_{MODE}.mp4"
    concat(clips, final, music, music_gain)

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
