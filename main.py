def main():
    news = fetch_news(MAX_AGE_H)
    print(f"ताजा समाचार ({MAX_AGE_H} घण्टाभित्र): {len(news)}")
    if len(news) < 5:
        print("समाचार कम भए, सीमा १२ घण्टा गरिँदैछ")
        news = fetch_news(12)
    if len(news) < 5:
        raise SystemExit("पर्याप्त समाचार भेटिएन")
    news = news[:60]
    listing = "\n".join(f"{i + 1}. [{n['age']:.0f} घण्टा अघि] {n['title']} — {n['summary']}"
                        for i, n in enumerate(news))
    data = gemini(make_prompt(listing))
    cand = data["items"]
    fix_headlines(cand)

    global ASSETS
    ASSETS = scan_assets()

    # --- पहिले फोटो भएका १० समाचार छान्ने
    items, medias, seen_ref = [], [], set()
    for it in cand:
        if len(items) >= 10:
            break
        try:
            ref = int(it["ref"])
        except Exception:
            continue
        if ref in seen_ref or not 1 <= ref <= len(news):
            continue
        seen_ref.add(ref)
        t0 = time.time()
        kind, media, credit = get_media(len(items) + 1, it, news)
        if not media:
            print(f"छोडियो (आफ्नै फोटो छैन): {news[ref - 1]['title'][:50]}")
            continue
        print(f"चयन {len(items) + 1}: {len(media)} फोटो {time.time() - t0:.0f}s")
        items.append(it)
        medias.append((kind, media, credit))
    if len(items) < 3:
        raise SystemExit("फोटो भएका पर्याप्त समाचार भेटिएन")
    print(f"{len(items)} items, title: {data['title']}")

    layers = sweep_layers(f"{OUT}/sw")
    whoosh = make_whoosh(f"{OUT}/whoosh.wav")
    music, music_gain = make_music(f"{OUT}/music_bed.mp3", pick=(0 if MODE == "morning" else 1))

    t_v = time.time()
    voices = make_voices(items)
    print(f"आवाज तयार: {time.time() - t_v:.0f}s")

    clips, credits, total, thumb_imgs = [], [], 0.0, []
    for i, it in enumerate(items, 1):
        t0 = time.time()
        kind, media, credit = medias[i - 1]
        if credit:
            credits.append(credit)
        lab = media_label(kind, media, credit)
        frame = frame_png(f"{OUT}/frame_{i}.png", lab)
        if len(thumb_imgs) < 3 and media and it.get("type") != "accident":
            thumb_imgs.append(media[0])
        first, last = (i == 1), (i == len(items))
        lead = 0.0                       # आवाज हरेक clip को सुरुबाटै
        dur = duration(voices[i - 1]) + 0.6 + lead
        bar, whites = bar_pngs(f"{OUT}/h_{i}", it["headline"])
        args = (i, kind, media, voices[i - 1], dur, bar, whites, frame, layers, whoosh, first, last, lead)
        try:
            clips.append(make_clip(*args))
        except FFError:
            print(f"clip {i}: मुख्य तरिका फेल/अड्कियो, सरल तरिकाले पुनः बनाइँदैछ")
            clips.append(make_clip(*args, simple=True))
        total += dur
        print(f"clip {i} ok ({dur:.0f}s, {len(media)} फोटो, कुल {time.time() - t0:.0f}s)")

    print(f"कुल अवधि: {total / 60:.1f} मिनेट")
    final = f"{OUT}/final_{MODE}.mp4"
    concat(clips, final, music, music_gain)

    thumb = f"{OUT}/thumb_{MODE}.jpg"
    thumbnail(thumb, thumb_imgs)

    src_lines, used_links = [], []
    for it in items:
        n = news[int(it["ref"]) - 1]
        src_lines.append(f"- {n['source']}: {n['link']}")
        used_links.append(n["link"])
    desc = data["description"] + "\n\nस्रोतहरू:\n" + "\n".join(dict.fromkeys(src_lines))
    if credits:
        desc += "\n\nफोटो:\n" + "\n".join(dict.fromkeys(l for c in credits for l in c.split("\n") if l.strip()))
    desc += ("\n\nनोट: यो भिडियोको वाचनमा AI आवाज प्रयोग गरिएको छ। समाचार स्रोतका शीर्षकमा आधारित सारांश हो।\n"
             "हाम्रै खबर - सधै समाचार, सत्य समाचार")

    # ---- Facebook पोस्ट: पेज नाम + नेपाली मिति + १० हेडलाइन + ह्याशट्याग
    page = os.environ.get("FB_PAGE_NAME", "I Love Chitwan")
    bs = bs_date_str()
    fb_title = f"आजका प्रमुख समाचार | {bs}"
    fb_text = (f"📰 {page} | आजका प्रमुख समाचार\n📅 {bs}\n\n"
               + "\n".join(f"{deva(i)}. {it['headline']}" for i, it in enumerate(items, 1))
               + "\n\n#hamrochitwan #todaynews #todaynepalinews #nepali")

    json.dump({"title": data["title"], "description": desc, "fb_title": fb_title, "fb_text": fb_text},
              open(f"{OUT}/meta.json", "w", encoding="utf-8"), ensure_ascii=False)
    save_posted(used_links)
    upload(final, thumb, {"title": data["title"], "description": desc})
