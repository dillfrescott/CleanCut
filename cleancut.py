import os, sys, re, subprocess, tempfile, shutil, json
from pathlib import Path
from tqdm import tqdm
sys.stderr = open(os.devnull, 'w')
BAD_WORD_PATTERN = re.compile(
    r"""
    \b(
        god[\s\-]*damn(?:it|ed|ing|er|s)? |
        god[\s\-]*dam(?:mit|nit|ed|ing|n)? |
        jesus[\s\-]*christ |
        jesus |
        christ
    )\b
    """,
    flags=re.IGNORECASE | re.VERBOSE
)
REPLACEMENT_WORD = "[REDACTED]"

def ask_yes_no(prompt):
    while True:
        c = input(f"{prompt} (y/n): ").strip().lower()
        if c in ("y", "n"):
            return c == "y"

def get_stream_ids(p: Path):
    r = subprocess.run(["ffprobe","-v","quiet","-print_format","json","-show_streams",str(p)],capture_output=True,text=True,encoding="utf-8")
    try:
        streams = json.loads(r.stdout)["streams"]
    except (json.JSONDecodeError, KeyError):
        return [], {}, {}
    vids, auds, subs = [], {}, {}
    for s in streams:
        st = s.get("codec_type")
        sid = int(s["index"])
        lang = s.get("tags", {}).get("language", "und")
        title = s.get("tags", {}).get("title")
        info = {"id": sid, "title": title}
        if st == "video":
            vids.append(sid)
        elif st == "audio":
            auds.setdefault(lang, []).append(info)
        elif st == "subtitle":
            subs.setdefault(lang, []).append(info)
    return vids, auds, subs

def modify_subs(f: Path):
    t = f.read_text(encoding="utf-8")
    m = list(BAD_WORD_PATTERN.finditer(t))
    if m:
        for x in m:
            print(f"📝 Subtitle: Found '{x.group(0)}' → replaced with {REPLACEMENT_WORD}")
        f.write_text(BAD_WORD_PATTERN.sub(REPLACEMENT_WORD, t), encoding="utf-8")
    return len(m)

def whisper_censor_replace_english(mkv_path: Path, eng_info: dict):
    eng_stream_id = eng_info['id']
    eng_title = eng_info['title'] or "English Audio"
    with tempfile.TemporaryDirectory() as tmp:
        raw_wav = Path(tmp) / "eng_raw.wav"
        cens_wav = Path(tmp) / "eng_censored.wav"
        out_mkv = mkv_path.with_name(mkv_path.stem + "_censored.mkv")
        subprocess.run(["ffmpeg","-y","-i",str(mkv_path),"-map",f"0:{eng_stream_id}","-vn","-acodec","pcm_s16le",str(raw_wav)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        dur_result = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(raw_wav)],capture_output=True,text=True)
        try:
            total_duration = float(dur_result.stdout.strip())
        except Exception:
            total_duration = 0
        try:
            from faster_whisper import WhisperModel
            import torch
            from tqdm import tqdm
            mutes=[]
            with tqdm(total=total_duration, unit="s", desc="Scanning audio", leave=False, file=sys.stdout) as bar:
                dev = "cuda" if torch.cuda.is_available() else "cpu"
                tqdm.write(f"🔊 Using Faster-Whisper large-v3 on {dev}...")
                model = WhisperModel("large-v3", device=dev, compute_type="float16" if dev=="cuda" else "int8")
                segs,_ = model.transcribe(str(raw_wav), word_timestamps=True)
                last = 0
                for s in segs:
                    if s.end > last:
                        bar.update(max(0, s.end - last))
                        last = s.end
                    for w in s.words:
                        if BAD_WORD_PATTERN.search(w.word):
                            tqdm.write(f"🔊 Audio: Found '{w.word}' from {w.start:.2f}s to {w.end:.2f}s → silenced.")
                            mutes.append((w.start, w.end))
        except Exception as e:
            print(f"⚠️ Whisper failed: {type(e).__name__}: {e}")
            return
        if not mutes:
            print(f"{mkv_path.name}: No bad words detected in English audio.")
            return
        filt=[f"volume=enable='between(t,{s},{e})':volume=0" for s,e in mutes]
        subprocess.run(["ffmpeg","-y","-i",str(raw_wav),"-af",",".join(filt),str(cens_wav)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        
        _, auds, _ = get_stream_ids(mkv_path)
        num_audio_streams = sum(len(v) for v in auds.values())
        new_audio_index = num_audio_streams - 1
        
        cmd=["ffmpeg","-y","-i",str(mkv_path),"-i",str(cens_wav),
             "-map","0","-map",f"-0:{eng_stream_id}","-map","1:a:0",
             "-c","copy",
             f"-metadata:s:a:{new_audio_index}","language=eng",
             f"-metadata:s:a:{new_audio_index}",f"title={eng_title}",
             str(out_mkv)]
        subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        shutil.move(out_mkv,mkv_path)
        print(f"{mkv_path.name}: Silenced {len(mutes)} bad word(s) in audio.")

def process_mkv(p: Path, o):
    v,a,s=get_stream_ids(p)
    print(f"\n🎧 Detected audio streams in {p.name}:")
    for k,vlist in a.items():
        print(f"  {k}: {[i['id'] for i in vlist]}")
    jpn=a.get("jpn",[])
    eng=a.get("eng",[]) or a.get("und",[])
    engsubs=s.get("eng",[]) or s.get("und",[])
    with tempfile.TemporaryDirectory() as tmp:
        if o["clean_subs"] and engsubs:
            subfile=Path(tmp)/"eng.ass"
            subprocess.run(["ffmpeg","-y","-i",str(p),"-map",f"0:{engsubs[0]['id']}","-c:s","ass",str(subfile)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            c=modify_subs(subfile)
            print(f"{p.name}: Replaced {c} subtitle bad words.")
        tmpout=p.with_suffix(".tmp.mkv")
        cmd=["ffmpeg","-y","-i",str(p)]
        if o["clean_subs"] and engsubs:
            cmd+=["-i",str(subfile)]
        cmd+=["-map","0:v?"]
        audio_index=0
        if o["remove_eng_audio"]:
            for aud_info in jpn:
                cmd+=["-map",f"0:{aud_info['id']}",f"-metadata:s:a:{audio_index}","language=jpn"]
                title = aud_info['title'] or "Japanese Audio"
                cmd+=[f"-metadata:s:a:{audio_index}",f"title={title}"]
                audio_index+=1
        else:
            for lang, aud_infos in a.items():
                for aud_info in aud_infos:
                    cmd+=["-map",f"0:{aud_info['id']}"]
                    lang_tag = lang if lang != 'und' else 'eng'
                    cmd+=[f"-metadata:s:a:{audio_index}", f"language={lang_tag}"]
                    title = aud_info['title']
                    if not title:
                        if lang in ('eng', 'und'): title = 'English Audio'
                        elif lang == 'jpn': title = 'Japanese Audio'
                    if title:
                        cmd+=[f"-metadata:s:a:{audio_index}", f"title={title}"]
                    audio_index+=1
        subtitle_index = 0
        if o["remove_non_eng_subs"]:
            if engsubs:
                cmd+=["-map","1:0" if o["clean_subs"] else f"0:{engsubs[0]['id']}"]
                cmd+=[f"-metadata:s:s:{subtitle_index}","language=eng"]
                title = engsubs[0]['title'] or "English"
                cmd+=[f"-metadata:s:s:{subtitle_index}",f"title={title}"]
                subtitle_index+=1
        else:
            if o["clean_subs"] and engsubs:
                cmd+=["-map","1:0"]
                cmd+=[f"-metadata:s:s:{subtitle_index}","language=eng"]
                title = engsubs[0]['title'] or "English"
                cmd+=[f"-metadata:s:s:{subtitle_index}",f"title={title}"]
                subtitle_index+=1
                for lang, sub_infos in s.items():
                    if lang in ('eng', 'und'): continue
                    for sub_info in sub_infos:
                        cmd+=["-map",f"0:{sub_info['id']}"]
                        cmd+=[f"-metadata:s:s:{subtitle_index}",f"language={lang}"]
                        if sub_info['title']:
                            cmd+=[f"-metadata:s:s:{subtitle_index}",f"title={sub_info['title']}"]
                        subtitle_index+=1
            else:
                for lang, sub_infos in s.items():
                    for sub_info in sub_infos:
                        cmd+=["-map",f"0:{sub_info['id']}"]
                        cmd+=[f"-metadata:s:s:{subtitle_index}",f"language={lang}"]
                        if sub_info['title']:
                            cmd+=[f"-metadata:s:s:{subtitle_index}",f"title={sub_info['title']}"]
                        subtitle_index+=1
        cmd+=["-c","copy","-map_metadata","0",str(tmpout)]
        subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        shutil.move(tmpout,p)
        if o["whisper_censor"] and eng and not o["remove_eng_audio"]:
            _, a_post, _ = get_stream_ids(p)
            eng_post = a_post.get("eng", []) or a_post.get("und", [])
            if eng_post:
                whisper_censor_replace_english(p, eng_post[0])

def process_all(d="."):
    o={"clean_subs":ask_yes_no("Remove blasphemous words from subtitles?"),"remove_eng_audio":ask_yes_no("Remove English audio and keep only Japanese?"),"remove_non_eng_subs":ask_yes_no("Remove non-English subtitles?"),"whisper_censor":ask_yes_no("Use Whisper large-v3 to detect and silence bad words in English audio?")}
    f=list(Path(d).glob("*.mkv"))
    if not f:
        print(f"No .mkv files found in '{d}'.")
        return
    for m in tqdm(f,desc="Processing MKV files",unit="file", file=sys.stdout):
        try:
            process_mkv(m,o)
        except Exception as e:
            tqdm.write(f"Error processing {m.name}: {e}")

if __name__=="__main__":
    target_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    if not os.path.isdir(target_dir):
        print(f"Usage: python {sys.argv[0]} [directory]")
        print(f"Error: Directory '{target_dir}' not found.")
        sys.exit(1)
    process_all(target_dir)
