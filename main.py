"""हाम्रै खबर - daily Nepali news video bot (v2.2).

Usage:  python main.py morning     (10 मुख्य समाचार, ~4 min)
        python main.py afternoon   (आजको सबैभन्दा चर्चित विषय, ~3-4 min)

v2.2 मा के फेरियो:
  - पुराना समाचार हटाउने (समयको सीमा), पहिले पोस्ट भएको समाचार (posted.json) दोहोर्याउँदैन
  - शीर्षक छोटो बनाउने (Gemini + कडा सीमा)
  - फोटो: समाचारको आफ्नै फोटो मात्र (sidebar/related फोटो हटाइयो), एउटै फोटो दोहोरिँदैन
  - फोटो साभार (credit) देखिन्छ; out/meta.json मा title/description (Facebook को लागि)
"""
import os, sys, io, json, re, glob, wave, base64, shutil, subprocess, datetime, asyncio, time, calendar
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass
import requests, feedparser
from html import unescape
from urllib.parse import urljoin, urlparse
from mutagen.mp3 import MP3
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import edge_tts

try:
    import imageio_ffmpeg
    FFMPEG = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = "ffmpeg"

# ============================ सेटिङ ============================
ALLOW_ARTICLE_PHOTOS = True
GEMINI_VOICE = os.environ.get("GEMINI_VOICE", "Charon")
SPEED = 1.15
MUSIC_TARGET_DB = -40
VOICE_LUFS = -15
CLIP_TIMEOUT = 420
# ================================================================

MODE = sys.argv[1] if len(sys.argv) > 1 else "morning"
NPT = datetime.timezone(datetime.timedelta(hours=5, minutes=45))
NOW = datetime.datetime.now(NPT)
OUT = "out"
os.makedirs(OUT, exist_ok=True)
W, H = 1920, 1080

# ---- पुराना/दोहोरिएका समाचार रोक्ने
POSTED_FILE = "posted.json"
MAX_AGE_H = 20 if MODE == "morning" else 14      # यति घण्टाभन्दा पुराना समाचार लिइँदैन


def load_posted():
    try:
        return json.load(open(POSTED_FILE, encoding="utf-8"))
    except Exception:
        return []


def save_posted(keys):
    old = load_posted()
    merged = list(dict.fromkeys(old + [k for k in keys if k]))[-600:]
    json.dump(merged, open(POSTED_FILE, "w", encoding="utf-8"), ensure_ascii=False)


def age_hours(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return None if not t else (time.time() - calendar.timegm(t)) / 3600


BOT_CONTACT = os.environ.get("BOT_CONTACT", "https://www.youtube.com/@hamraikhabar")
UA = {"User-Agent": f"HamraiKhabarBot/1.0 ({BOT_CONTACT}; Nepali news video bot)"}
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0 Safari/537.36"}

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = None
NAMES = []
EDGE_VOICE = os.environ.get("TTS_VOICE", "ne-NP-SagarNeural")
PRIVACY = os.environ.get("UPLOAD_PRIVACY", "private")

FEEDS = [
    "https://www.onlinekhabar.com/feed",
    "https://www.setopati.com/feed",
    "https://feeds.bbci.co.uk/nepali/rss.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.cnn.com/rss/edition_world.rss",
    "https://news.google.com/rss/search?q=%E0%A4%A8%E0%A5%87%E0%A4%AA%E0%A4%BE%E0%A4%B2&hl=ne&gl=NP&ceid=NP:ne",
    "https://news.google.com/rss/headlines/section/topic/WORLD?hl=ne&gl=NP&ceid=NP:ne",
]

MONTHS = ["जनवरी", "फेब्रुअरी", "मार्च", "अप्रिल", "मे", "जुन",
          "जुलाई", "अगस्ट", "सेप्टेम्बर", "अक्टोबर", "नोभेम्बर", "डिसेम्बर"]


def deva(s):
    return str(s).translate(str.maketrans("0123456789", "०१२३४५६७८९"))


DATE_STR = deva(f"{NOW.day} {MONTHS[NOW.month - 1]} {NOW.year}")

BS_MONTHS = ["बैशाख", "जेठ", "असार", "श्रावण", "भदौ", "असोज", "कार्तिक", "मङ्सिर", "पौष", "माघ", "फागुन", "चैत"]
BS_2083 = [31, 31, 32, 31, 31, 31, 30, 29, 30, 29, 30, 30]


def bs_date_str():
    """आजको वि.सं. मिति, जस्तै '२०८३ मङ्सिर ७ गते'।"""
    d = NOW.date()
    try:
        import nepali_datetime
        n = nepali_datetime.date.from_datetime_date(d)
        y, m, dd = n.year, n.month, n.day
    except Exception:
        off = (d - datetime.date(2026, 4, 14)).days
        if not 0 <= off < sum(BS_2083):
            return DATE_STR
        y, m = 2083, 0
        while off >= BS_2083[m]:
            off -= BS_2083[m]
            m += 1
        m, dd = m + 1, off + 1
    return deva(f"{y} {BS_MONTHS[m - 1]} {dd} गते")


class FFError(Exception):
    pass


def run(cmd, timeout=900):
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print("COMMAND TIMEOUT:", " ".join(cmd[:6]), "...")
        raise FFError("ffmpeg timeout")
    if p.returncode != 0:
        print("COMMAND FAILED:", " ".join(cmd[:6]), "...")
        print(p.stderr[-1500:])
        raise FFError("ffmpeg failed")


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


ASSETS = (None, [], {})


def fetch_news(max_age=MAX_AGE_H):
    """ताजा (max_age घण्टाभित्रका), पहिले पोस्ट नभएका समाचार; नयाँ पहिले।"""
    items, seen, posted = [], set(), set(load_posted())
    for url in FEEDS:
        try:
            d = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
            src = clean(d.feed.get("title", url))
            for e in d.entries[:25]:
                title = clean(e.get("title", ""))
                a = age_hours(e)
                if not title or title in seen or a is None or a > max_age or a < -1:
                    continue
                if e.get("link") in posted:
                    continue
                seen.add(title)
                pub = clean((e.get("source") or {}).get("title", "")) or src
                items.append({"title": title, "summary": clean(e.get("summary", ""))[:300],
                              "source": pub, "link": e.get("link", ""),
                              "image": entry_image(e), "age": max(a, 0)})
            print(f"feed ok: {src} ({len(d.entries)})")
        except Exception as ex:
            print("feed failed:", url, ex)
    items.sort(key=lambda x: x["age"])
    direct = [n for n in items if "news.google.com" not in n["link"]]
    return direct if len(direct) >= 25 else items


# ---------------------------------------------------------------- Gemini (लेखन)
def pick_models():
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
    for rnd in range(3):
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
        time.sleep(30 * (rnd + 1))
    raise SystemExit(f"Gemini failed: {last}")


RULES = (
    "नियम: तलको सूचीमा भएका शीर्षक र सारांश मात्र आधार बनाऊ। कुनै नाम, संख्या, मिति वा तथ्य आफैं नथप। "
    "सूचीमा हरेक समाचारको सुरुमा कति घण्टा अघिको हो लेखिएको छ; सकेसम्म ताजा (कम घण्टा) समाचारलाई प्राथमिकता देऊ, "
    "पुरानो समाचारलाई 'आज' जस्तो गरी नलेख। "
    "पुष्टि नभएको वा एउटै स्रोतको कुरा 'भनिएको छ' भनेर लेख। कसैको पक्ष नलेऊ, तटस्थ र सरल नेपालीमा लेख। "
    "narration मा समाचार मात्र लेख: 'नमस्कार', 'यो समाचार', 'हेर्नुहोस्', 'सुन्नुहोस्', लाइक/सब्सक्राइब "
    "जस्ता कुनै अतिरिक्त कुरा नलेख; सिधै समाचारको मुख्य कुराबाट सुरु गर। "
    "headline अत्यन्त छोटो (अधिकतम ३५ अक्षर, ४-६ शब्द), पूरा अर्थ दिने, एक पङ्क्तिमा अटाउने र टुङ्गिएको हुनुपर्छ "
    "(जस्तै 'प्रधानमन्त्री भारत भ्रमणमा')। "
    "'ref' मा सूचीको नम्बर (integer) राख; प्रत्येक items को ref फरक-फरक हुनुपर्छ (विश्लेषणमा बाहेक)। "
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


def fix_headlines(items, limit=40):
    """लामा शीर्षक Gemini ले छोट्याउने; नसके शब्दको सीमामा काट्ने।"""
    long = [it for it in items if len(it.get("headline", "")) > limit]
    if long:
        try:
            r = gemini('तलका शीर्षक अर्थ नबिग्रने गरी ३५ अक्षरभित्र छोट्याऊ। JSON मात्र: {"h": ["..."]} उही क्रममा।\n'
                       + json.dumps([it["headline"] for it in long], ensure_ascii=False))
            for it, h in zip(long, r.get("h", [])):
                if h and len(h) < len(it["headline"]):
                    it["headline"] = h.strip()
        except BaseException as ex:
            print("headline fix fail:", ex)
    for it in items:
        h = it.get("headline", "")
        if len(h) > 48:
            it["headline"] = h[:48].rsplit(" ", 1)[0]


# ---------------------------------------------------------------- आवाज
TTS_PREF = ["gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]
GEMINI_TTS_DEAD = False


def gemini_tts(text, path):
    global GEMINI_TTS_DEAD
    if GEMINI_TTS_DEAD:
        return False
    models = [m for m in TTS_PREF if m in NAMES] or [n for n in NAMES if "tts" in n]
    tried, quota = 0, 0
    for m in models[:2]:
        tried += 1
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
            body = {"contents": [{"parts": [{"text": text}]}],
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
        except requests.HTTPError as ex:
            code = ex.response.status_code if ex.response is not None else 0
            if code == 429:
                quota += 1
            print("gemini tts fail:", m, code, str(ex)[:120])
        except Exception as ex:
            print("gemini tts fail:", m, str(ex)[:200])
    if tried and quota == tried:
        GEMINI_TTS_DEAD = True
        print("Gemini TTS quota सकियो (429): अब Gemini आवाज फेरि प्रयास गरिँदैन")
    return False


async def _edge(text, path):
    await edge_tts.Communicate(text, EDGE_VOICE).save(path)


def edge_tts_file(text, path):
    for _ in range(3):
        try:
            asyncio.run(_edge(text, path))
            if os.path.getsize(path) > 1000:
                return True
        except Exception as ex:
            print("edge retry:", ex)
            time.sleep(2)
    raise SystemExit("आवाज बनाउन सकिएन (Gemini र Edge दुवै फेल)")


def make_voices(items):
    engine = os.environ.get("TTS_ENGINE", "auto").lower()
    use_gemini = bool(GEMINI_KEY) and engine != "edge"
    raws = []
    if use_gemini:
        for i, it in enumerate(items, 1):
            path = f"{OUT}/raw_a_{i}.wav"
            if not gemini_tts(it["narration"], path):
                print(f"Gemini आवाज भाग {i} मा रोकियो; सबै Edge मा बनाइँदैछ")
                use_gemini = False
                break
            raws.append(path)
    if not use_gemini:
        raws = []
        for i, it in enumerate(items, 1):
            path = f"{OUT}/raw_a_{i}.mp3"
            edge_tts_file(it["narration"], path)
            raws.append(path)
    outs = []
    for i, raw in enumerate(raws, 1):
        out = f"{OUT}/voice_{i}.wav"
        run([FFMPEG, "-y", "-i", raw, "-af",
             f"atempo={SPEED},loudnorm=I={VOICE_LUFS}:TP=-1.5:LRA=7", "-ar", "44100", "-ac", "2", out])
        outs.append(out)
    return outs


def duration(path):
    with wave.open(path) as w:
        return w.getnframes() / w.getframerate()


# ---------------------------------------------------------------- Wikimedia
WM_GAP = 1.5
WM_LAST = 0.0
WM_429 = 0
WM_OFF = False


def is_wm(url):
    h = urlparse(url).netloc.lower()
    return h.endswith("wikimedia.org") or h.endswith("wikipedia.org")


def wm_wait():
    global WM_LAST
    gap = WM_GAP - (time.time() - WM_LAST)
    if gap > 0:
        time.sleep(gap)
    WM_LAST = time.time()


def wm_get(url, timeout=30, **kw):
    global WM_429, WM_OFF
    if WM_OFF:
        raise RuntimeError("wikimedia बन्द गरिएको (धेरै 429)")
    for attempt in range(3):
        wm_wait()
        r = requests.get(url, headers=UA, timeout=timeout, **kw)
        if r.status_code == 429:
            WM_429 += 1
            if WM_429 >= 4:
                WM_OFF = True
                print("Wikimedia मा धेरै 429: बाँकी समय Wikimedia प्रयोग गरिँदैन")
                raise RuntimeError("wikimedia 429")
            try:
                wait = min(15, int(r.headers.get("Retry-After", 5)))
            except Exception:
                wait = 5
            print(f"wikimedia 429, {wait}s पर्खने")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r
    raise RuntimeError("wikimedia 429")


# ---------------------------------------------------------------- फोटो / भिडियो
def download(url, path, minw=600):
    try:
        if is_wm(url):
            r = wm_get(url, timeout=25)
        else:
            r = requests.get(url, headers=BROWSER_UA, timeout=20)
            r.raise_for_status()
        ct = r.headers.get("content-type", "").lower()
        if ct and not (ct.startswith("image") or "octet" in ct):
            print("image fail: फोटो होइन:", ct[:40], url[:80])
            return False
        im = Image.open(io.BytesIO(r.content))
        im.load()
        im = im.convert("RGB")
        if im.width < minw:
            return False
        im.save(path, quality=92)
        return True
    except Exception as ex:
        print("image fail:", str(ex)[:120])
        return False


def download_file(url, path, max_mb=90):
    try:
        if is_wm(url):
            if WM_OFF:
                return False
            wm_wait()
        with requests.get(url, headers=UA if is_wm(url) else BROWSER_UA, timeout=60, stream=True) as r:
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
    if not q or WM_OFF:
        return None
    try:
        r = wm_get("https://commons.wikimedia.org/w/api.php", timeout=40, params={
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


_CS_CACHE = {}


def commons_search(q, newest=False):
    key = (q, newest)
    if key in _CS_CACHE:
        return _CS_CACHE[key]
    if WM_OFF:
        return None
    params = {"action": "query", "generator": "search", "gsrsearch": q, "gsrnamespace": 6,
              "gsrlimit": 15, "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime",
              "iiurlwidth": 1600, "format": "json"}
    if newest:
        params["gsrsort"] = "create_timestamp_desc"
    try:
        r = wm_get("https://commons.wikimedia.org/w/api.php", timeout=40, params=params)
        pages = r.json().get("query", {}).get("pages", {})
    except Exception as ex:
        print("commons fail:", str(ex)[:120])
        return None
    result = None
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
        result = ((ii.get("thumburl") or ii["url"]), f'"{title}" - {artist} ({lic}) via Wikimedia Commons')
        break
    _CS_CACHE[key] = result
    return result


def commons_photo(queries, idx):
    path = f"{OUT}/photo_{idx}.jpg"
    for q in queries:
        for newest in (True, False):
            if WM_OFF:
                return None
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


# ---- समाचारको आफ्नै पेजबाट फोटो/भिडियो
BAD_IMG = ("logo", "icon", "avatar", "sprite", "banner", "advert", "/ads", "placeholder", "favicon",
           "emoji", "pixel", "blank", "loader", "share", "facebook", "twitter", "qr", "default")

USED_URL = set()      # यसअघि प्रयोग भएका फोटो URL (दोहोरिन नदिन)
USED_HASH = []        # यसअघि प्रयोग भएका फोटोको hash


def _meta(html_txt, keys):
    out = []
    for tag in re.findall(r"<meta[^>]+>", html_txt, re.I):
        p = re.search(r'(?:property|name)=["\']([^"\']+)["\']', tag, re.I)
        c = re.search(r'content=["\']([^"\']+)["\']', tag, re.I)
        if p and c and p.group(1).lower() in keys:
            out.append(unescape(c.group(1)))
    return out


def article_page(n):
    link = n.get("link", "")
    if not link or "news.google.com" in link:
        return "", link
    try:
        r = requests.get(link, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        return r.text, r.url
    except Exception as ex:
        print("article fetch fail:", str(ex)[:100])
        return "", link


def _ahash(path):
    im = Image.open(path).convert("L").resize((8, 8))
    px = list(im.tobytes())
    avg = sum(px) / 64
    return [1 if p > avg else 0 for p in px]


DOMAIN_NAMES = {
    "reuters.com": "Reuters", "cnn.com": "CNN", "bbc.co.uk": "BBC", "bbc.com": "BBC",
    "aninews.in": "ANI", "ptinews.com": "PTI", "pib.gov.in": "PIB India", "pmindia.gov.in": "PMO India",
    "opmcm.gov.np": "PMO Nepal", "pmo.gov.np": "PMO Nepal", "rss.com.np": "राससस",
    "risingnepaldaily.com": "राससस", "nepalphotolibrary.com": "Nepal Photo Library",
    "onlinekhabar.com": "अनलाइनखबर", "setopati.com": "सेतोपाटी", "ratopati.com": "रातोपाटी",
    "kathmandupost.com": "The Kathmandu Post", "ekantipur.com": "कान्तिपुर",
}


def courtesy_name(host, fallback=""):
    host = (host or "").lower()
    for k, v in DOMAIN_NAMES.items():
        if host == k or host.endswith("." + k):
            return v
    return fallback or re.sub(r"^www\.", "", host)


def find_video_urls(page, base):
    urls = list(_meta(page, ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream")))
    urls += re.findall(r'<(?:video|source)[^>]+src=["\']([^"\']+)["\']', page, re.I)
    urls += re.findall(r'["\'](https?:[^"\'\s<>]+?\.(?:mp4|m3u8)[^"\'\s<>]*)["\']', page, re.I)
    out = []
    for u in urls:
        u = urljoin(base, unescape(u).replace("\\/", "/"))
        low = u.lower()
        if (".mp4" in low or ".m3u8" in low) and u not in out and "preview" not in low and "/ads/" not in low and "advert" not in low:
            out.append(u)
    return out[:6]


def grab_video(url, path):
    try:
        if ".m3u8" in url.lower():
            p = subprocess.run([FFMPEG, "-y", "-user_agent", "Mozilla/5.0", "-i", url, "-t", "45",
                                "-c", "copy", "-bsf:a", "aac_adtstoasc", path],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            ok = p.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 50000
        else:
            ok = download_file(url, path)
        return ok and audio_info(path)[0] >= 4
    except Exception as ex:
        print("video grab fail:", str(ex)[:100])
        return False


def article_media(n, idx, want_video=True, max_photos=3):
    """समाचारकै पेजबाट सम्बन्धित भिडियो (भए) वा फोटो। sidebar/related फोटो लिइँदैन
    (RSS को फोटो, og:image, twitter:image मात्र)।
    फर्काउँछ: ("video", [path], credit) | ("photo", [paths], credit) | None"""
    page, base = article_page(n)
    cands = [n["image"]] if n.get("image") else []
    name = courtesy_name(urlparse(base).netloc, source_label(n.get("source", "")))
    if page:
        if want_video:
            for v in find_video_urls(page, base):
                path = f"{OUT}/raw_{idx}.mp4"
                if grab_video(v, path):
                    return "video", [path], f"भिडियो साभार: {name}"
        cands += _meta(page, ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"))
    photos, tried = [], 0
    for u in cands:
        if len(photos) >= max_photos or tried >= 6:
            break
        u = urljoin(base, u)
        if not u.lower().startswith("http") or u in USED_URL or any(b in u.lower() for b in BAD_IMG):
            continue
        tried += 1
        path = f"{OUT}/photo_{idx}_{len(photos)}.jpg"
        if not download(u, path, minw=800):
            continue
        im = Image.open(path)
        if im.width / im.height < 1.15:
            continue
        h = _ahash(path)
        if any(sum(a != b for a, b in zip(h, o)) < 7 for o in USED_HASH):
            continue
        USED_HASH.append(h)
        USED_URL.add(u)
        photos.append(path)
    return ("photo", photos, f"फोटो साभार: {name}") if photos else None


def top_up_photos(photos, credits, iqs, vq, idx, want=2):
    k = len(photos)
    for q in iqs:
        if len(photos) >= want or WM_OFF:
            break
        c = commons_search(q, False)
        path = f"{OUT}/photo_{idx}_c{k}.jpg"
        if c and download(c[0], path, minw=1000) and Image.open(path).width / Image.open(path).height >= 1.15:
            photos.append(path)
            credits.append(c[1])
            k += 1
    if len(photos) < want and vq:
        p = pixabay_image(vq, idx)
        if p:
            new = f"{OUT}/photo_{idx}_p{k}.jpg"
            os.replace(p[0], new)
            photos.append(new)
            credits.append(p[1])
    return photos, credits


def get_media(idx, item, news):
    """(kind, [paths], credit) फर्काउँछ।"""
    typ = item.get("type", "general")
    vq = item.get("video_query", "")
    iqs = [q for q in (item.get("image_queries") or []) if q]
    if not iqs and item.get("image_query"):
        iqs = [item["image_query"]]
    try:
        n = news[int(item["ref"]) - 1]
    except Exception:
        n = None

    def stock():
        return pixabay_video(vq, idx) or pexels_video(vq, idx)

    if typ == "sensitive":
        p = pixabay_image(vq, idx)
        if p:
            return "photo", [p[0]], p[1]
        r = stock()
        return ("video", [r[0]], r[1]) if r else (None, [], "")
    if typ == "accident":
        r = stock()
        if r:
            return "video", [r[0]], r[1]
        p = pixabay_image(vq, idx)
        return ("photo", [p[0]], p[1]) if p else (None, [], "")

    if ALLOW_ARTICLE_PHOTOS and n:
        am = article_media(n, idx)
        if am and am[0] == "video":
            return am
        if am and am[0] == "photo":
            photos = list(am[1])
            cr = [am[2]] if am[2] else []
            if len(photos) < 2:
                photos, cr = top_up_photos(photos, cr, iqs, vq, idx)
            return "photo", photos, "\n".join(cr)
    if typ == "person_event":
        r = commons_video_any(iqs, idx)
        if r:
            return "video", [r[0]], r[1]
        p = commons_photo(iqs, idx)
        if p:
            ph, cr = top_up_photos([p[0]], [p[1]], iqs, vq, idx)
            return "photo", ph, "\n".join(cr)
        r = stock()
        if r:
            return "video", [r[0]], r[1]
    else:
        r = stock() or commons_video_any([vq] + iqs, idx)
        if r:
            return "video", [r[0]], r[1]
        p = commons_photo(iqs, idx)
        if p:
            ph, cr = top_up_photos([p[0]], [p[1]], iqs, vq, idx)
            return "photo", ph, "\n".join(cr)
    return None, [], ""


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
    env = os.environ.get("LOGO_FILE")
    if env and os.path.exists(env):
        return env
    pngs = sorted(f for f in os.listdir(".") if f.lower().endswith(".png"))
    print("repo मा भेटिएका png:", pngs)
    for f in pngs:
        if f.startswith("file_0000000029c88211b3f223523608709b"):
            return f
    for key in ("hamrai", "khabar", "hamr", "logo", "hk"):
        for f in pngs:
            if key in f.lower():
                return f
    return pngs[0] if pngs else None


LOGO_PATH = find_logo()
print("logo:", LOGO_PATH)
_LOGO_CACHE = {}


def raw_logo():
    if "raw" in _LOGO_CACHE:
        return _LOGO_CACHE["raw"].copy()
    if LOGO_PATH:
        im = Image.open(LOGO_PATH).convert("RGBA")
    else:
        f = font(150)
        im = Image.new("RGBA", (1100, 260), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((20, 30), "हाम्रै खबर", font=f, fill=(215, 0, 10, 255))
    if im.getchannel("A").getextrema()[0] == 255:
        for pt in ((0, 0), (im.width - 1, 0), (0, im.height - 1), (im.width - 1, im.height - 1)):
            try:
                ImageDraw.floodfill(im, pt, (0, 0, 0, 0), thresh=40)
            except Exception:
                pass
    box = im.getchannel("A").getbbox()
    if box:
        im = im.crop(box)
    _LOGO_CACHE["raw"] = im
    return im.copy()


def load_logo(h, maxw=None):
    im = raw_logo()
    r = h / im.height
    if maxw and im.width * r > maxw:
        r = maxw / im.width
    return im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))), Image.LANCZOS)


def logo_badge(h, maxw, padx=0, pady=0):
    return load_logo(h, maxw)


def fallback_bg(path):
    img = Image.new("RGBA", (W, H), (11, 31, 92, 255))
    b = logo_badge(220, 1100, 60, 30)
    img.alpha_composite(b, ((W - b.width) // 2, (H - b.height) // 2 - 40))
    img.convert("RGB").save(path)
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
    fr = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    b = logo_badge(78, 460, 24, 12)
    sh = Image.new("RGBA", (b.width + 40, b.height + 40), (0, 0, 0, 0))
    sh.paste((0, 0, 0, 110), (20, 24), b.getchannel("A"))
    sh = sh.filter(ImageFilter.GaussianBlur(7))
    fr.alpha_composite(sh, (30, 20))
    fr.alpha_composite(b, (40, 34))
    if label:
        tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        size = 30
        tw = draw_mixed(tmp, 0, 0, label, size, (0, 0, 0))
        pill = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        pd = ImageDraw.Draw(pill)
        pd.rounded_rectangle((W - 40 - tw - 36, 34, W - 40, 34 + size + 28), radius=14, fill=(0, 0, 0, 120))
        fr = Image.alpha_composite(fr, pill)
        draw_mixed(ImageDraw.Draw(fr), W - 40 - 18, 34 + 10, label, size, (255, 255, 255, 235))
    fr.save(path)
    return path


def video_label(credit):
    first = (credit or "").split("\n")[0]
    if first.startswith(("भिडियो साभार:", "फोटो साभार:")):
        return first
    s = credit_site(credit)
    return f"भिडियो साभार: {s}" if s else None


def media_label(kind, media, credit):
    lines = [l for l in (credit or "").split("\n") if l.strip()]
    sym = [l for l in lines if "Pixabay" in l or "Pexels" in l]
    if sym:
        if len(sym) >= len(media):
            return "साङ्केतिक भिडियो" if kind == "video" else "साङ्केतिक तस्बिर"
        return "केही साङ्केतिक तस्बिर"
    if kind == "video":
        return video_label(credit)
    first = lines[0] if lines else ""
    return first if first.startswith("फोटो साभार:") else None


BW, CW = 1400, 240
BAR_C0, BAR_C1 = (215, 0, 10), (170, 0, 15)
SW_C0, SW_C1 = (16, 42, 118), (6, 16, 52)


def chev(bh, i):
    cy, ch = bh // 2, int(bh * 0.30)
    x0 = 62 + i * 38
    return [(x0, cy - ch), (x0 + 30, cy), (x0, cy + ch)]


def bar_pngs(prefix, headline):
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    maxw = BW - CW - 90
    size, lines = 66, None
    while size >= 44:
        f = font(size)
        if tmp.textlength(headline, font=f) <= maxw:
            lines = [headline]
            break
        size -= 4
    if lines is None:
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


RW, AW, NW, SL = W + 900, 30, 16, 170
X0 = -(RW + AW + SL + 150)
SPAN = W + NW + 100 - X0
TT = 0.9
LAYERS = [("white", AW, RW, 0.0), ("red", RW, 0, 0.025), ("navy", NW, -NW, 0.06)]


def band_png(path, w, kind):
    img = Image.new("RGBA", (w + SL, H), (0, 0, 0, 0))
    mask = Image.new("L", (w + SL, H), 0)
    ImageDraw.Draw(mask).polygon([(SL, 0), (w + SL, 0), (w, H), (0, H)], fill=255)
    fill = Image.new("RGBA", (w + SL, H), (0, 0, 0, 255))
    d = ImageDraw.Draw(fill)
    if kind == "white":
        d.rectangle((0, 0, w + SL, H), fill=(215, 0, 10, 255))
    elif kind == "navy":
        d.rectangle((0, 0, w + SL, H), fill=(255, 255, 255, 235))
    else:
        for x in range(w + SL):
            t = x / (w + SL)
            d.line((x, 0, x, H), fill=tuple(int(SW_C0[k] + (SW_C1[k] - SW_C0[k]) * t) for k in range(3)) + (255,))
        d.polygon([(w - 60, 0), (w - 20, 0), (w - 20 - SL, H), (w - 60 - SL, H)], fill=(255, 255, 255, 40))
    img.paste(fill, (0, 0), mask)
    img.save(path)
    return path


def sweep_layers(prefix):
    return [band_png(f"{prefix}_{k}.png", w, k) for k, w, _, _ in LAYERS]


F_HOLD = 0.5
T_IN = 0.55
T_HOLD = 0.22
T_OUT = 0.55


def layer_x(ox, delay, D, first, last):
    q_in = f"clip((t-({D - T_IN:.3f})-{delay})/{T_IN},0,1)"
    q_out = f"clip((t-{T_HOLD}-{delay})/{T_OUT},0,1)"
    end_f = f"{F_HOLD}*(1-pow(1-{q_in},3))"
    start_f = f"({F_HOLD}+{1 - F_HOLD}*pow({q_out},3))"
    f = f"if(lt(t,{D / 2:.3f}),{'1' if first else start_f},{'0' if last else end_f})"
    return f"{ox}+({X0})+{SPAN}*{f}"


def _vgrad(w, h, c0, c1):
    g = Image.new("RGBA", (w, h))
    d = ImageDraw.Draw(g)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line((0, y, w, y), fill=tuple(int(c0[k] + (c1[k] - c0[k]) * t) for k in range(3)) + (255,))
    return g


def thumbnail(path, imgs):
    imgs = [p for p in imgs if p][:3]
    n, PH, s = len(imgs), 660, 90
    base = _vgrad(W, H, (10, 28, 120), (4, 10, 50))
    if n == 0:
        b = logo_badge(240, 1100)
        base.alpha_composite(b, ((W - b.width) // 2, 170))
    seams = [W * k // max(n, 1) for k in range(1, n)]
    for i, p in enumerate(imgs):
        a = 0 if i == 0 else seams[i - 1]
        b = W if i == n - 1 else seams[i]
        a_top, a_bot = (0, 0) if i == 0 else (a + s // 2, a - s // 2)
        b_top, b_bot = (W, W) if i == n - 1 else (b + s // 2, b - s // 2)
        x0, x1 = min(a_top, a_bot), max(b_top, b_bot)
        w = x1 - x0
        im = Image.open(p).convert("RGB")
        r = max(w / im.width, PH / im.height)
        im = im.resize((int(im.width * r) + 1, int(im.height * r) + 1))
        cx, cy = (im.width - w) // 2, int((im.height - PH) * 0.3)
        im = im.crop((cx, cy, cx + w, cy + PH))
        canvas = Image.new("RGB", (W, H))
        canvas.paste(im, (x0, 0))
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).polygon([(a_top, 0), (b_top, 0), (b_bot, PH), (a_bot, PH)], fill=255)
        base.paste(canvas, (0, 0), mask)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for x in seams:
        gd.line((x + s // 2, 0, x - s // 2, PH), fill=(255, 255, 255, 255), width=9)
    base = Image.alpha_composite(base, glow.filter(ImageFilter.GaussianBlur(8)))
    base = Image.alpha_composite(base, glow)
    top = Image.new("RGBA", (W, 230), (0, 0, 0, 0))
    td = ImageDraw.Draw(top)
    for y in range(230):
        td.line((0, y, W, y), fill=(0, 0, 0, int(120 * (1 - y / 230))))
    base.alpha_composite(top, (0, 0))
    lg = logo_badge(150, 640)
    sh = Image.new("RGBA", (lg.width + 60, lg.height + 60), (0, 0, 0, 0))
    sh.paste((0, 0, 0, 170), (30, 36), lg.getchannel("A"))
    sh = sh.filter(ImageFilter.GaussianBlur(9))
    base.alpha_composite(sh, (20, 14))
    base.alpha_composite(lg, (50, 44))

    line1, line2 = ("आजका प्रमुख", "समाचार") if MODE == "morning" else ("आजको चर्चित", "विषय")
    bx0, by0, bx1, by1 = 60, 545, W - 60, 715
    band = _vgrad(bx1 - bx0, by1 - by0, (240, 25, 30), (165, 0, 12))
    bm = Image.new("L", band.size, 0)
    ImageDraw.Draw(bm).rounded_rectangle((0, 0, band.width - 1, band.height - 1), radius=22, fill=255)
    base.paste(band, (bx0, by0), bm)
    d = ImageDraw.Draw(base)
    d.rounded_rectangle((bx0, by0, bx1, by1), radius=22, outline=(255, 255, 255, 200), width=4)
    f1 = font(140)
    d.text((W // 2 + 4, (by0 + by1) // 2 + 8), line1, font=f1, fill=(0, 0, 0, 150), anchor="mm")
    d.text((W // 2, (by0 + by1) // 2 + 4), line1, font=f1, fill="white", anchor="mm")
    f2 = font(205)
    d.text((W // 2, 832), line2, font=f2, fill=(255, 235, 20), anchor="mm", stroke_width=12, stroke_fill=(0, 0, 0))
    px0, py0, px1, py1 = 360, 950, W - 360, 1056
    pill = _vgrad(px1 - px0, py1 - py0, (255, 240, 60), (250, 195, 15))
    pm = Image.new("L", pill.size, 0)
    ImageDraw.Draw(pm).rounded_rectangle((0, 0, pill.width - 1, pill.height - 1), radius=26, fill=255)
    base.paste(pill, (px0, py0), pm)
    d = ImageDraw.Draw(base)
    for k in range(3):
        for left in (True, False):
            x = (px0 - 200 + k * 48) if left else (px1 + 40 + k * 48)
            d.polygon([(x + 34, py0), (x + 64, py0), (x + 30, py1), (x, py1)], fill=(215, 0, 10, 255))
    d.text((W // 2, (py0 + py1) // 2 + 4), bs_date_str(), font=font(80), fill=(10, 10, 10), anchor="mm")
    base.convert("RGB").resize((1280, 720), Image.LANCZOS).save(path, quality=90)


# ---------------------------------------------------------------- आवाज प्रभाव र सङ्गीत
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac")


def audio_info(path):
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
    trans, _, info = ASSETS
    if trans:
        g = gain_to(info[trans][1], -24.0)
        run([FFMPEG, "-y", "-i", trans, "-t", "1.6", "-af",
             f"volume={g:.1f}dB,afade=t=out:st=1.2:d=0.4", "-ar", "44100", "-ac", "2", path])
        return path
    run([FFMPEG, "-y", "-f", "lavfi", "-i",
         "anoisesrc=d=0.7:c=pink:a=0.6,highpass=f=500,lowpass=f=4500,"
         "afade=t=in:d=0.3,afade=t=out:st=0.35:d=0.35,volume=1.5", "-ar", "44100", "-ac", "2", path])
    return path


def make_music(path, style="news", pick=0):
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
    run([FFMPEG, "-y", "-f", "lavfi", "-i", f"aevalsrc='{expr}|{expr}':s=44100:d=60", "-af", af, path])
    return path, gain_to(audio_info(path)[1], MUSIC_TARGET_DB)


# ---------------------------------------------------------------- भिडियो
FG_SCALE = 0.78
SLIDE_FADE = 0.6
ZW, ZH = 2400, 1350


def photo_branch(j, k, mode, nf):
    cy = "y='(ih-ih/zoom)/2'"
    if mode == 0:
        zp = f"z='1+0.12*on/{nf}':x='iw/2-(iw/zoom/2)':{cy}"
    elif mode == 1:
        zp = f"z='1.12':x='(iw-iw/zoom)*on/{nf}':{cy}"
    elif mode == 2:
        zp = f"z='1.12-0.12*on/{nf}':x='iw/2-(iw/zoom/2)':{cy}"
    else:
        zp = f"z='1.12':x='(iw-iw/zoom)*(1-on/{nf})':{cy}"
    return (f"[{j}:v]scale={ZW}:{ZH}:force_original_aspect_ratio=increase,crop={ZW}:{ZH},"
            f"zoompan={zp}:d={nf}:s=1920x1080:fps=25,setsar=1,format=yuv420p[z{j}];")


def make_clip(idx, kind, media, audio, dur, bar, whites, frame, layers, whoosh, first, last, lead,
              simple=False):
    imgs = list(media or [])
    pre, fc = [], ""
    if simple:
        if kind == "video" and imgs:
            pre = ["-stream_loop", "-1", "-i", imgs[0]]
        else:
            src = imgs[0] if imgs else fallback_bg(f"{OUT}/bg.png")
            pre = ["-loop", "1", "-framerate", "25", "-i", src]
        m = 1
        fc = ("[0:v]fps=25,scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
              "setsar=1,format=yuv420p[bg];")
    elif kind == "video" and imgs:
        pre = ["-stream_loop", "-1", "-i", imgs[0]]
        m = 1
        fw = int(W * FG_SCALE) // 2 * 2
        fh = int(H * FG_SCALE) // 2 * 2
        fc = ("[0:v]fps=25,setsar=1,split=2[va][vb];"
              "[va]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
              "scale=480:270,boxblur=7:2,scale=1920:1080,eq=brightness=-0.10:saturation=1.1[vbg];"
              f"[vb]scale={fw}:{fh}:force_original_aspect_ratio=decrease,"
              "pad=iw+8:ih+8:4:4:color=white@0.9[vfg];"
              "[vbg][vfg]overlay=(W-w)/2:(H-h)/2-10,format=yuv420p[bg];")
    else:
        if not imgs:
            imgs = [fallback_bg(f"{OUT}/bg.png")]
        imgs = imgs[:3]
        k = len(imgs)
        m = k
        L = (dur + (k - 1) * SLIDE_FADE) / k
        nf = int(L * 25) + 2
        for j, p in enumerate(imgs):
            pre += ["-i", p]
            fc += photo_branch(j, k, (idx + j) % 4, nf)
        if k == 1:
            fc += "[z0]null[bg];"
        else:
            cur = "z0"
            for j in range(1, k):
                off = j * (L - SLIDE_FADE)
                out = "bg" if j == k - 1 else f"x{j}"
                fc += (f"[{cur}][z{j}]xfade=transition={'slideleft' if j % 2 else 'slideright'}:"
                       f"duration={SLIDE_FADE}:offset={off:.3f}[{out}];")
                cur = out
    iA, iF, iB = m, m + 1, m + 2
    iW0, iNavy, iRed, iWhite, iWh = m + 3, m + 6, m + 7, m + 8, m + 9

    cmd = [FFMPEG, "-y"] + pre + ["-i", audio]
    for p in [frame, bar] + whites + layers[::-1]:
        cmd += ["-loop", "1", "-framerate", "25", "-i", p]
    if not last:
        cmd += ["-i", whoosh]

    s0 = 0.15 if first else (T_HOLD + 0.45)
    bx = f"if(lt(t,{s0}),-3000,if(lt(t,{s0 + 0.5}),-w+(260+w)*(t-{s0})/0.5,260))"
    lx = {name: layer_x(ox, dl, dur, first, last) for name, _, ox, dl in LAYERS}
    en = f":enable='lt(t,1.0)+gte(t,{max(dur - T_IN - 0.2, 0):.3f})'"

    fc += (f"[bg][{iF}:v]overlay=0:0:format=auto[v1];"
           f"[{iB}:v][{iW0}:v]overlay=0:0:format=auto:enable='between(mod(t,1.8),0.15,0.45)'[b1];"
           f"[b1][{iW0 + 1}:v]overlay=0:0:format=auto:enable='between(mod(t,1.8),0.35,0.65)'[b2];"
           f"[b2][{iW0 + 2}:v]overlay=0:0:format=auto:enable='between(mod(t,1.8),0.55,0.85)'[b3];"
           f"[v1][b3]overlay=x='{bx}':y=H-h-45:format=auto[v2];"
           f"[v2][{iNavy}:v]overlay=x='{lx['navy']}':y=0:format=auto{en}[v3];"
           f"[v3][{iRed}:v]overlay=x='{lx['red']}':y=0:format=auto{en}[v4];"
           f"[v4][{iWhite}:v]overlay=x='{lx['white']}':y=0:format=auto{en}[v];")
    lead_ms = int(lead * 1000)
    fc += f"[{iA}:a]" + (f"adelay={lead_ms}|{lead_ms}," if lead_ms else "") + "apad[nar]"
    if last:
        amap = "[nar]"
    else:
        ms = max(0, int((dur - T_IN) * 1000))
        fc += (f";[{iWh}:a]adelay={ms}|{ms}[wh];"
               f"[nar][wh]amix=inputs=2:duration=first:normalize=0[a]")
        amap = "[a]"
    clip = f"{OUT}/clip{idx}.mp4"
    cmd += ["-filter_complex", fc, "-map", "[v]", "-map", amap,
            "-c:v", "libx264", "-preset", "veryfast", "-threads", "0", "-r", "25",
            "-c:a", "aac", "-b:a", "160k",
            "-ar", "44100", "-ac", "2", "-pix_fmt", "yuv420p", "-t", f"{dur:.2f}", clip]
    run(cmd, timeout=(300 if simple else CLIP_TIMEOUT))
    return os.path.basename(clip)


def concat(clips, final, music, gain_db):
    with open(f"{OUT}/list.txt", "w") as f:
        for c in clips:
            f.write(f"file '{c}'\n")
    raw = f"{OUT}/_nomusic.mp4"
    run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", f"{OUT}/list.txt", "-c", "copy", raw])
    run([FFMPEG, "-y", "-i", raw, "-stream_loop", "-1", "-i", music, "-filter_complex",
         f"[1:a]volume={gain_db:.1f}dB[m];[0:a][m]amix=inputs=2:duration=first:normalize=0,"
         f"loudnorm=I={VOICE_LUFS}:TP=-1.5:LRA=9[a]",
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
    news = fetch_news(MAX_AGE_H)
    print(f"ताजा समाचार ({MAX_AGE_H} घण्टाभित्र): {len(news)}")
    if len(news) < 10:
        print("ताजा समाचार कम भए, सीमा ४८ घण्टा गरिँदैछ")
        news = fetch_news(48)
    if len(news) < 10:
        raise SystemExit("पर्याप्त समाचार भेटिएन")
    news = news[:90]
    listing = "\n".join(f"{i + 1}. [{n['age']:.0f} घण्टा अघि] {n['title']} — {n['summary']}"
                        for i, n in enumerate(news))
    data = gemini(make_prompt(listing))
    items = data["items"]
    fix_headlines(items)
    print(f"{len(items)} items, title: {data['title']}")

    global ASSETS
    ASSETS = scan_assets()
    layers = sweep_layers(f"{OUT}/sw")
    whoosh = make_whoosh(f"{OUT}/whoosh.wav")
    music, music_gain = make_music(f"{OUT}/music_bed.mp3", pick=(0 if MODE == "morning" else 1))

    t_v = time.time()
    voices = make_voices(items)
    print(f"आवाज तयार: {time.time() - t_v:.0f}s")
    clips, credits, total, thumb_imgs = [], [], 0.0, []
    for i, it in enumerate(items, 1):
        t0 = time.time()
        kind, media, credit = get_media(i, it, news)
        print(f"भाग {i}: मिडिया {kind} ({len(media)}) {time.time() - t0:.0f}s, type={it.get('type')}")
        if credit:
            credits.append(credit)
        lab = media_label(kind, media, credit)
        frame = frame_png(f"{OUT}/frame_{i}.png", lab)
        if len(thumb_imgs) < 3 and media and it.get("type") not in ("sensitive", "accident"):
            if kind == "photo":
                thumb_imgs.append(media[0])
            else:
                fp = f"{OUT}/vf_{i}.jpg"
                try:
                    run([FFMPEG, "-y", "-ss", "1", "-i", media[0], "-frames:v", "1", fp], timeout=60)
                    thumb_imgs.append(fp)
                except Exception:
                    pass
        first, last = (i == 1), (i == len(items))
        lead = 0.0 if first else 0.3
        dur = duration(voices[i - 1]) + 0.6 + lead
        bar, whites = bar_pngs(f"{OUT}/h_{i}", it["headline"])
        args = (i, kind, media, voices[i - 1], dur, bar, whites, frame, layers, whoosh, first, last, lead)
        try:
            clips.append(make_clip(*args))
        except FFError:
            print(f"clip {i}: मुख्य तरिका फेल/अड्कियो, सरल तरिकाले पुनः बनाइँदैछ")
            clips.append(make_clip(*args, simple=True))
        total += dur
        print(f"clip {i} ok ({dur:.0f}s, media={kind}, {len(media)} वटा, कुल {time.time() - t0:.0f}s)")

    print(f"कुल अवधि: {total / 60:.1f} मिनेट")
    final = f"{OUT}/final_{MODE}.mp4"
    concat(clips, final, music, music_gain)

    thumb = f"{OUT}/thumb_{MODE}.jpg"
    thumbnail(thumb, thumb_imgs)

    src_lines, used_links = [], []
    for it in items:
        try:
            n = news[int(it["ref"]) - 1]
            src_lines.append(f"- {n['source']}: {n['link']}")
            used_links.append(n["link"])
        except Exception:
            pass
    desc = data["description"] + "\n\nस्रोतहरू:\n" + "\n".join(dict.fromkeys(src_lines))
    if credits:
        desc += "\n\nफोटो/भिडियो:\n" + "\n".join(dict.fromkeys(l for c in credits for l in c.split("\n") if l.strip()))
    if any("Pixabay" in c or "Pexels" in c for c in credits):
        desc += "\n\nनोट: Pixabay/Pexels बाट लिइएका तस्बिर र भिडियो साङ्केतिक (प्रतीकात्मक) हुन्।"
    desc += ("\n\nनोट: यो भिडियोको वाचनमा AI आवाज प्रयोग गरिएको छ। समाचार स्रोतका शीर्षकमा आधारित सारांश हो।\n"
             "हाम्रै खबर - सधै समाचार, सत्य समाचार")

    # Facebook को लागि title/description; र पोस्ट भएका समाचार सम्झने
    json.dump({"title": data["title"], "description": desc}, open(f"{OUT}/meta.json", "w", encoding="utf-8"),
              ensure_ascii=False)
    save_posted(used_links)
    upload(final, thumb, {"title": data["title"], "description": desc})


if __name__ == "__main__":
    main()
