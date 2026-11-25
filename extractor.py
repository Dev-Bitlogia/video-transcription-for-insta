"""
Video Script Extractor - TEST VERSION (OpenAI version)
1 Reel URL in -> spoken script + visual content + viral breakdown out.

Stack: Apify (get video) -> ffmpeg (audio + frames)
     -> AssemblyAI (spoken script) -> OpenAI GPT-4o (visuals)
     -> OpenAI GPT-4o-mini (viral analysis)

Keys are read ONLY from .env, never hardcoded.
Cache: results/<shortcode>.json - same Reel is never processed twice.
"""
import os
import re
import json
import hashlib
import time
import base64
import shutil
import subprocess
import requests
from pathlib import Path

BASE_DIR = Path(__file__).parent
RESULTS_DIR = BASE_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# Bump this when the analysis changes, so old cached results auto re-run.
# v5 = song in Spoken script shows only "Song", no lyrics. No manual delete needed.
CACHE_VERSION = 17  # v17 = download via _ytdlp_run (android/tv/default) + Apify YouTube downloader fallback; v16 meta fallback worked, download was still plain yt-dlp

# ---------- helpers ----------

def detect_platform(url: str) -> str:
    """Detect the platform from the URL first, before any scraping.
    Returns 'instagram', 'tiktok', 'facebook', or raises ValueError."""
    u = (url or "").strip().lower()
    if "instagram.com" in u or "instagr.am" in u:
        return "instagram"
    if "tiktok.com" in u or "vm.tiktok.com" in u or "vt.tiktok.com" in u:
        return "tiktok"
    if "facebook.com" in u or "fb.watch" in u or "fb.com" in u:
        return "facebook"
    if "youtube.com" in u or "youtu.be" in u or "youtube-nocookie.com" in u:
        return "youtube"
    raise ValueError(
        "Unsupported platform. Paste a public video link from Instagram, TikTok, Facebook, or YouTube. "
        "Example Instagram: https://www.instagram.com/reel/ABC123/  "
        "TikTok: https://www.tiktok.com/@user/video/1234567890123456789  "
        "Facebook: https://www.facebook.com/reel/123456789012345  "
        "YouTube: https://www.youtube.com/watch?v=ABC12345678"
    )


def extract_shortcode(url: str) -> str:
    """Backwards-compatible name. Now returns the video id for any platform.
    Instagram: instagram.com/reel/ABC123/ -> ABC123
    TikTok: tiktok.com/@user/video/123... -> 123...
    Facebook: facebook.com/reel/123... or /watch/?v=123... -> 123..."""
    return extract_video_id(url)


def extract_video_id(url: str) -> str:
    platform = detect_platform(url)
    u = url.strip().rstrip("/")
    if platform == "instagram":
        m = re.search(r"instagram\.com/(?:reel|p|tv)/([A-Za-z0-9_-]+)", u)
        if not m:
            raise ValueError("Invalid Instagram Reel URL. Copy the link from Share -> Copy link. Example: https://www.instagram.com/reel/ABC123/")
        return m.group(1)
    if platform == "tiktok":
        m = re.search(r"/video/(\d+)", u)
        if m:
            return m.group(1)
        # Short / share links have no id in the URL - use a hash so the cache file is stable.
        return "tiktok-" + hashlib.md5(u.encode()).hexdigest()[:12]
    if platform == "youtube":
        m = re.search(r"[?&]v=([A-Za-z0-9_-]{6,})", u) or re.search(r"youtu\.be/([A-Za-z0-9_-]{6,})", u) or re.search(r"/shorts/([A-Za-z0-9_-]{6,})", u) or re.search(r"/embed/([A-Za-z0-9_-]{6,})", u)
        if m:
            return "yt-" + m.group(1)
        return "yt-" + hashlib.md5(u.encode()).hexdigest()[:12]
    if platform == "facebook":
        m = re.search(r"/reel/(\d+)", u) or re.search(r"[?&]v=(\d+)", u) or re.search(r"/videos/(\d+)", u) or re.search(r"/watch/[^/]*/(\d+)", u)
        if m:
            return "fb-" + m.group(1)
        return "fb-" + hashlib.md5(u.encode()).hexdigest()[:12]
    raise ValueError("Unsupported platform.")


def build_srt(words):
    """words: [{text, start, end}] in ms -> SRT string"""
    def fmt(ms):
        s = int(ms // 1000)
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        return f"{h:02d}:{m:02d}:{s:02d},{int(ms % 1000):03d}"
    lines, buf, start = [], [], None
    # group words into ~8-word lines
    for i, w in enumerate(words or []):
        if start is None:
            start = w.get("start", 0)
        buf.append(w.get("text", ""))
        if len(buf) >= 8 or i == len(words) - 1:
            end = w.get("end", w.get("start", 0))
            lines.append(f"{len(lines)+1}\n{fmt(start)} --> {fmt(end)}\n{' '.join(buf)}\n")
            buf, start = [], None
    return "\n".join(lines)


def _clean_json_text(text: str) -> dict:
    """OpenAI sometimes wraps JSON in ```json ... ``` - strip it."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


# ---------- Step 1: Apify (multi-platform) ----------

def _pick(item: dict, *keys):
    """Return the first non-empty value from item, trying nested dicts too."""
    for k in keys:
        if not isinstance(item, dict):
            continue
        v = item.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _pick_nested(item: dict, *paths):
    for path in paths:
        cur = item
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, "", [], {}):
            return cur
    return None


def _apify_run(apify_token: str, actor: str, payload: dict, timeout: int = 180):
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={apify_token}"
    r = requests.post(url, json=payload, timeout=timeout)
    if "text/html" in r.headers.get("content-type", "") or r.text.lstrip().startswith("<!DOCTYPE html"):
        raise RuntimeError("APIFY_LOGIN_BLOCKED: The platform returned a login page, not video data. Wait 10 min and try again, or try a different video.")
    r.raise_for_status()
    data = r.json()
    if not data:
        raise RuntimeError("APIFY_NO_ITEMS: Apify returned 0 items. The video may be private, deleted, or login-walled.")
    item = data[0]
    if isinstance(item, dict) and item.get("error"):
        raise RuntimeError(f"APIFY_ERROR: {item.get('error')} - {item.get('errorDescription', '')}")
    return item


def _normalise_meta(item: dict, platform: str, video_url: str) -> dict:
    """Map the different actors' field names to one common set.
    TikTok and Facebook actors use different names from Instagram, so try many."""
    caption = _pick(item, "caption", "text", "desc", "description", "title") or ""
    if isinstance(caption, dict):
        caption = caption.get("text", "") or ""
    owner = (
        _pick(item, "ownerUsername", "username", "author", "channelName", "channel")
        or _pick_nested(item, "authorMeta.name", "author.username", "authorMeta.nickName", "owner.username")
        or ""
    )
    if isinstance(owner, dict):
        owner = owner.get("username") or owner.get("name") or ""
    music = (
        _pick(item, "musicInfo")
        or _pick_nested(item, "musicMeta.musicName", "musicMeta.musicAuthor", "music.title")
        or ""
    )
    if isinstance(music, dict):
        music = music.get("title") or music.get("musicName") or ""
    return {
        "shortcode": _pick(item, "shortCode", "id", "videoId") or "",
        "url": _pick(item, "url", "webVideoUrl", "postPage", "link") or "",
        "caption": caption,
        "hashtags": _pick(item, "hashtags") or [],
        "timestamp": _pick(item, "timestamp", "createTime", "createdAt") or "",
        "likes": _pick(item, "likesCount", "diggCount", "likes", "likeCount", "reactions", "reactionCount", "totalReactions", "reactionsCount"),
        "comments": _pick(item, "commentsCount", "commentCount", "comments", "totalComments", "comment_count"),
        "views": _pick(item, "videoPlayCount", "videoViewCount", "playCount", "views", "viewCount", "plays", "play_count"),
        "duration": _pick(item, "videoDuration", "duration") or _pick_nested(item, "videoMeta.duration", "video.duration"),
        "music": music,
        "videoUrl": video_url,
        "owner": owner,
        "platform": platform,
    }


def _find_video_url(item: dict, platform: str):
    """Try every field name the TikTok / Facebook / Instagram actors use for the MP4."""
    candidates = [
        _pick(item, "videoUrl", "video_url", "downloadAddr", "playUrl", "webVideoUrl"),
        _pick_nested(item, "video.playAddr", "video.downloadAddr", "videoMeta.videoUrl", "video.url", "video.play_url", "media.videoUrl", "videoUrl"),
    ]
    for c in candidates:
        if c and isinstance(c, str) and c.startswith("http"):
            return c
    # Some actors return a list of media URLs
    for k in ("mediaUrls", "videoUrls", "urls"):
        v = item.get(k) if isinstance(item, dict) else None
        if isinstance(v, list):
            for x in v:
                if isinstance(x, str) and x.startswith("http") and (".mp4" in x or "video" in x):
                    return x
    return None


def _parse_count(text):
    """'19K' -> 19000, '1.2M' -> 1200000, '442' -> 442. Used for Facebook title fallback."""
    if not text:
        return None
    m = re.search(r"([0-9][0-9,.]*)\s*([KM])\b", str(text), re.I)
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except Exception:
        return None
    if m.group(2).upper() == "K":
        n *= 1000
    elif m.group(2).upper() == "M":
        n *= 1000000
    return int(n)


def _ytdlp_run(args, timeout=120):
    """Run yt-dlp, trying android/ios/mweb client first, then default.
    YouTube in 2026 needs a JS runtime (deno) for the web client, and GitHub
    runners have none by default. The android client does not need JS.
    Returns (returncode, stdout, stderr) of the first successful run, or the last run."""
    tries = [
        ["--extractor-args", "youtube:player_client=android,ios,mweb"],
        ["--extractor-args", "youtube:player_client=tv"],
        [],
    ]
    last = (1, "", "")
    for extra in tries:
        r = subprocess.run(["yt-dlp"] + extra + args, capture_output=True, text=True, timeout=timeout)
        last = (r.returncode, r.stdout, r.stderr)
        if r.returncode == 0:
            return last
    return last

def youtube_get_meta(video_url: str) -> dict:
    """YouTube / Facebook: no Apify. Use yt-dlp to get metadata only.
    Returns meta with videoUrl = the YouTube page URL itself, and
    download_via = "yt-dlp", so download_video knows to use yt-dlp."""
    if shutil.which("yt-dlp") is None:
        raise RuntimeError("YTDLP_MISSING: yt-dlp is not installed. It is in requirements.txt, run pip install -r requirements.txt, or in GitHub Actions it is installed from requirements.")
    _rc, _out, _err = _ytdlp_run(["--dump-json", "--no-playlist", video_url], timeout=120)
    if _rc != 0:
        # FIX 2026-11-17 v15: YouTube on GitHub says "Sign in to confirm you're not a bot".
        # Fall back to Apify streamers/youtube-scraper for metadata (views/likes/comments/title).
        # It runs on Apify with residential proxies, so it is not blocked like GitHub's IP.
        # Download still uses yt-dlp afterwards; if that also fails, the video is unavailable.
        print(f"yt-dlp YouTube meta failed ({_err[-200:]}), trying Apify youtube-scraper", flush=True)
        try:
            import os as _os
            _token = _os.environ.get("APIFY_TOKEN", "")
            if not _token:
                raise RuntimeError("no APIFY_TOKEN for fallback")
            item = _apify_run(_token, "streamers~youtube-scraper", {
                "startUrls": [{"url": video_url}],
                "maxResults": 1,
                "maxResultsShorts": 1,
                "maxResultStreams": 0,
            }, timeout=300)
            meta = _normalise_meta(item, "youtube", video_url)
            meta["platform"] = "youtube"
            meta["download_via"] = "yt-dlp"
            meta["videoUrl"] = video_url
            return meta
        except Exception as _e:
            raise RuntimeError(f"YTDLP_ERROR: yt-dlp could not read this YouTube video, and Apify fallback also failed ({_e}). It may be private, deleted, age-restricted, or YouTube bot-blocked. yt-dlp said: {_err[-800:]}")
    try:
        info = json.loads(_out)
    except Exception:
        raise RuntimeError("YTDLP_ERROR: yt-dlp returned no JSON metadata for this YouTube video.")
    _likes = info.get("like_count")
    if _likes is None:
        # Facebook yt-dlp gives like_count=None, but the title often says "19K reactions".
        # Use that as likes, never invent a number. If title has no reactions, likes stays None -> PDF shows "-".
        _m = re.search(r"([0-9][0-9,.]*\s*[KM])\s+reactions", info.get("title") or "", re.I)
        if _m:
            _likes = _parse_count(_m.group(1))
    return {
        "shortcode": info.get("id") or "",
        "url": info.get("webpage_url") or video_url,
        "caption": info.get("description") or "",
        "hashtags": info.get("tags") or [],
        "timestamp": str(info.get("upload_date") or ""),
        "likes": _likes,
        "comments": info.get("comment_count"),
        "views": info.get("view_count"),
        "duration": info.get("duration"),
        "music": "",
        "videoUrl": video_url,
        "owner": info.get("uploader") or info.get("channel") or "",
        "platform": detect_platform(video_url),
        "download_via": "yt-dlp",
    }


def apify_get_reel(apify_token: str, reel_url: str, timeout: int = 180) -> dict:
    """Kept name for compatibility. Detects platform, then calls that platform's actor."""
    platform = detect_platform(reel_url)
    if platform == "youtube":
        return youtube_get_meta(reel_url)
    if platform == "facebook":
        # FIX 2026-11-17 v13: Apify apify~facebook-reels-scraper gave 400 because we sent
        # resultsLimit, which is NOT a valid input for this actor. Correct input is
        # startUrls:[{url}], maxItems:1, maxReelsPerProfile:1 - see Apify docs.
        # Apify is used for METADATA ONLY (likes/comments/views/caption/owner).
        # Download is still via yt-dlp, same as TikTok, because Apify videoUrl is unreliable.
        # If Apify fails or gives no likes/comments, fall back to yt-dlp meta (title reactions as likes).
        try:
            item = _apify_run(apify_token, "apify~facebook-reels-scraper", {
                "startUrls": [{"url": reel_url}],
                "maxItems": 1,
                "maxReelsPerProfile": 1,
            })
            meta = _normalise_meta(item, "facebook", reel_url)
            meta["platform"] = "facebook"
            meta["download_via"] = "yt-dlp"
            meta["videoUrl"] = reel_url
            # If Apify gave no likes, fall back to yt-dlp title reactions, same as v13 fallback
            if meta.get("likes") in (None, "", 0):
                try:
                    _yt = youtube_get_meta(reel_url)
                    if _yt.get("likes"):
                        meta["likes"] = _yt["likes"]
                    if meta.get("views") in (None, "", 0) and _yt.get("views"):
                        meta["views"] = _yt["views"]
                except Exception:
                    pass
            return meta
        except Exception as e:
            print(f"Facebook Apify metadata failed ({e}), falling back to yt-dlp meta", flush=True)
            meta = youtube_get_meta(reel_url)
            meta["platform"] = "facebook"
            return meta
    if platform == "instagram":
        item = _apify_run(apify_token, "apify~instagram-scraper", {
            "directUrls": [reel_url], "resultsType": "reels", "resultsLimit": 1,
        }, timeout)
    elif platform == "tiktok":
        # FIX 2026-11-17: Apify TikTok videoUrl is often not a real MP4 file.
        # ffmpeg failed on it with exit 183. So keep Apify for metadata only,
        # and download the video with yt-dlp, same as YouTube.
        # clockworks/tiktok-scraper: postURLs for a single video.
        # shouldDownloadVideos=True stores the file in Apify KV, the dataset item
        # still carries the direct video URL in most runs - _find_video_url tries all names.
        item = _apify_run(apify_token, "clockworks~tiktok-scraper", {
            "postURLs": [reel_url], "resultsPerPage": 1, "shouldDownloadVideos": False,
        }, timeout)
    elif platform == "facebook":
        # Facebook Reels scraper: public Reels / videos only. Private or login-walled
        # videos return no items - that is a platform limit, not an extractor bug.
        item = _apify_run(apify_token, "apify~facebook-reels-scraper", {
            "startUrls": [reel_url], "resultsLimit": 1,
        }, timeout)
    else:
        raise ValueError("Unsupported platform.")
    video_url = _find_video_url(item, platform)
    if not video_url:
        raise RuntimeError(
            f"APIFY_NO_VIDEO_URL ({platform}): Apify found the video but gave no direct MP4 link. "
            "TikTok/Facebook sometimes hide the direct link - try a different video, or a public Reel. "
            f"Raw keys were: {sorted(item.keys()) if isinstance(item, dict) else type(item)}"
        )
    meta = _normalise_meta(item, platform, video_url)
    meta["platform"] = platform
    if platform == "tiktok":
        meta["download_via"] = "yt-dlp"
        meta["videoUrl"] = reel_url
    return meta


# ---------- Step 2: download + ffmpeg ----------


def _apify_youtube_download(apify_token: str, video_url: str, dest: Path) -> bool:
    """YouTube download via Apify, only when yt-dlp is bot-blocked on GitHub.
    Tries two downloader actors with a few input variants, picks downloadUrl,
    downloads it with requests. Returns True on success, False if all fail.
    Cost: marielise actor ~$0.02/min at 720p, epctex similar, inside Apify free credit.
    Input field names are not all documented, so try the common variants."""
    variants = [
        ("marielise.dev~youtube-video-downloader", {"videoUrl": video_url, "format": "default", "quality": "720"}),
        ("marielise.dev~youtube-video-downloader", {"url": video_url, "format": "default", "quality": "720"}),
        ("epctex~youtube-video-downloader", {"videoUrl": video_url, "quality": "720p"}),
        ("epctex~youtube-video-downloader", {"url": video_url, "quality": "720p"}),
        ("epctex~youtube-video-downloader", {"youtubeUrl": video_url, "quality": "720p"}),
    ]
    for actor, payload in variants:
        try:
            item = _apify_run(apify_token, actor, payload, timeout=300)
            dl = _pick(item, "downloadUrl", "downloadURL", "download_url", "url", "fileUrl")
            if not dl or not isinstance(dl, str) or not dl.startswith("http"):
                print(f"Apify {actor} gave no downloadUrl, trying next", flush=True)
                continue
            print(f"Apify {actor} gave downloadUrl, downloading file", flush=True)
            headers = {"User-Agent": "Mozilla/5.0", "Authorization": f"Bearer {apify_token}"}
            with requests.get(dl, headers=headers, stream=True, timeout=300) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
            if dest.exists() and dest.stat().st_size >= 10000:
                return True
        except Exception as e:
            print(f"Apify {actor} download failed: {e}", flush=True)
            continue
    return False

def download_video(video_url: str, dest: Path, download_via: str = "", apify_token: str = ""):
    if download_via == "yt-dlp":
        # YouTube/TikTok/Facebook: download MP4 with yt-dlp, best mp4 under 720p.
        # _ytdlp_run tries android, then tv, then default - plain yt-dlp alone was bot-blocked.
        _rc, _out, _err = _ytdlp_run(["-f", "best[ext=mp4][height<=720]/best[ext=mp4]/best", "--no-playlist", "-o", str(dest), video_url], timeout=600)
        if _rc == 0 and dest.exists() and dest.stat().st_size >= 10000:
            return
        print(f"yt-dlp download failed ({_err[-300:]}), trying Apify downloader", flush=True)
        if "youtube.com" in video_url or "youtu.be" in video_url:
            if apify_token and _apify_youtube_download(apify_token, video_url, dest):
                return
        raise RuntimeError(f"YTDLP_DOWNLOAD_ERROR: yt-dlp could not download this YouTube video. {_err[-800:]}")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    with requests.get(video_url, headers=headers, stream=True, timeout=90) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    if dest.stat().st_size < 10000:
        raise RuntimeError("DOWNLOAD_TOO_SMALL: Downloaded video is under 10KB, link probably expired.")


def extract_audio_and_frames(video_path: Path, workdir: Path):
    """Returns (audio_path, frames_list). Frames: 1 per 2 sec, max 15, 640px wide."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("FFMPEG_MISSING: ffmpeg is not installed or not in PATH. Install it, then run again.")
    audio_path = workdir / "audio.mp3"
    frames_dir = workdir / "frames"
    frames_dir.mkdir(exist_ok=True)
    # audio: mono 16kHz mp3
    subprocess.run(["ffmpeg", "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", str(audio_path)],
                   check=True, capture_output=True, text=True, timeout=120)
    # frames: 1 per 2 sec, scale 640 wide, max 15 frames
    subprocess.run(["ffmpeg", "-y", "-i", str(video_path), "-vf", "fps=1/2,scale=640:-1", str(frames_dir / "frame_%03d.jpg")],
                   check=True, capture_output=True, text=True, timeout=120)
    frames = sorted(frames_dir.glob("*.jpg"))[:15]
    return audio_path, frames


# ---------- Step 3A: AssemblyAI ----------

def assemblyai_transcribe(api_key: str, audio_path: Path, timeout_sec: int = 300) -> dict:
    headers = {"authorization": api_key}
    # 1. upload
    with open(audio_path, "rb") as f:
        up = requests.post("https://api.assemblyai.com/v2/upload", headers=headers, data=f, timeout=120)
    up.raise_for_status()
    audio_url = up.json()["upload_url"]
    # 2. create transcript job
    # FIX 2026-11-13: AssemblyAI deprecated singular "speech_model" on async API.
    # It now returns 400. Use plural "speech_models" list instead.
    # Docs: Pass a non-empty list, e.g. ["universal-3-5-pro", "universal-2"].
    job = requests.post("https://api.assemblyai.com/v2/transcript", headers={**headers, "content-type": "application/json"},
                        json={
                            "audio_url": audio_url,
                            "speech_models": ["universal-3-5-pro", "universal-2"],
                            "language_detection": True,
                            "speaker_labels": True,
                            "punctuate": True,
                            "format_text": True,
                        }, timeout=60)
    if job.status_code >= 400:
        raise RuntimeError(f"ASSEMBLYAI_400: {job.status_code} - {job.text[:500]}")
    tid = job.json()["id"]
    # 3. poll
    start = time.time()
    while time.time() - start < timeout_sec:
        time.sleep(5)
        res = requests.get(f"https://api.assemblyai.com/v2/transcript/{tid}", headers=headers, timeout=30)
        res.raise_for_status()
        data = res.json()
        if data["status"] == "completed":
            return {
                "text": data.get("text") or "",
                "words": data.get("words") or [],
                "utterances": data.get("utterances") or [],
                "language": data.get("language_code"),
                "confidence": data.get("confidence"),
            }
        if data["status"] == "error":
            raise RuntimeError(f"ASSEMBLYAI_ERROR: {data.get('error')}")
    raise RuntimeError("ASSEMBLYAI_TIMEOUT: Transcription took longer than 5 minutes.")


# ---------- Step 3B: OpenAI GPT-4o visual ----------

def openai_visual_analysis(openai_key: str, frames, transcript_text: str) -> dict:
    from openai import OpenAI
    client = OpenAI(api_key=openai_key)
    if not frames:
        return {"on_screen_text": [], "visual_hook_0_3s": "No frames extracted", "scenes": [], "emotion": "", "camera_and_pace": "", "objects_and_setting": ""}
    content = [{
        "type": "text",
        "text": (
            "You are analysing frames from ONE video, in time order. "
            "The spoken transcript is: " + (transcript_text[:2000] or "(no speech)") + "\n\n"
            "Return JSON ONLY, no other text, with exactly these keys:\n"
            "{\n"
            '  "on_screen_text": [{"text": "...", "time": "00:00-00:03"}],\n'
            '  "visual_hook_0_3s": "what is seen in the first 3 seconds",\n'
            '  "scenes": [{"start": "00:00", "end": "00:05", "what_happens": "...", "who_is_in_frame": "...", "emotion": "..."}],\n'
            '  "emotion": "overall emotion, e.g. excited / calm / funny",\n'
            '  "camera_and_pace": "static or moving, how many cuts, fast or slow",\n'
            '  "objects_and_setting": "objects, background, location"\n'
            "}\n"
            "RULES: If there is NO text visible on screen, return on_screen_text as an empty list []. "
            "Do NOT invent text. Do NOT guess words that are not clearly visible. "
            "If a frame is blurry, say so in that scene, do not guess."
        )
    }]
    for fp in frames:
        b64 = base64.b64encode(fp.read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"}})
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": content}],
        temperature=0.2,
        max_tokens=1500,
        response_format={"type": "json_object"},
    )
    return _clean_json_text(resp.choices[0].message.content)


# ---------- Step 4: OpenAI GPT-4o-mini viral analysis ----------

def openai_viral_analysis(openai_key: str, meta: dict, transcript: dict, visual: dict) -> dict:
    from openai import OpenAI
    client = OpenAI(api_key=openai_key)
    # FIX 2026-11-13: viral analysis was generic ("relatable content", "popular hashtags").
    # Now every reason must quote exact evidence from THIS Reel only.
    # FIX 2026-11-17 v12: verdict is decided BEFORE the model writes, so it fills the correct side.
    # Previously the model wrote why_not for a 221k-view video, then Python cleared it, leaving no why_viral.
    try:
        _v = int(meta.get("views") or 0)
    except Exception:
        _v = 0
    _is_viral = _v >= 5000
    _verdict_line = f"This video IS VIRAL with {_v} views. Fill why_viral with 3 reasons, leave why_not_reaching as []." if _is_viral else f"This video is NOT VIRAL, only {_v} views. Fill why_not_reaching with 3 reasons and what_to_fix with 3 fixes, leave why_viral as []."
    prompt = (
        "You are a viral content analyst. Analyse ONLY the facts below. Do not invent facts.\n\n"
        f"VERDICT DECIDED: {_verdict_line}\n"
        f"PLATFORM: {meta.get('platform', 'instagram')}  METRICS: views={meta.get('views')} likes={meta.get('likes')} comments={meta.get('comments')} duration={meta.get('duration')}\n"
        f"CAPTION: {meta.get('caption','')[:1000]}\n"
        f"MUSIC: {meta.get('music')}\n"
        f"SPOKEN SCRIPT: {transcript.get('text','')[:4000]}\n"
        f"VISUAL: {json.dumps(visual)[:3000]}\n\n"
        "STRICT RULES - follow them all:\n"
        "1. Every item in why_viral MUST quote exact evidence from this Reel: a specific spoken phrase in quotes, a specific visual from VISUAL, a specific caption line or hashtag, or a specific metric number. If you cannot quote evidence, do not write that reason.\n"
        "2. FORBIDDEN generic phrases - never write them alone: 'relatable content', 'engaging question', 'popular hashtags', 'daily life', 'self-expression', 'visually engaging hook', 'personal content'. Use the exact words instead.\n"
        "3. hook_spoken MUST be the exact first spoken line from SPOKEN SCRIPT, word for word. hook_visual MUST be the exact visual_hook_0_3s from VISUAL. hook_text MUST be the exact first on-screen text, or empty if none.\n"
        "4. trigger_words MUST be exact words/phrases that actually appear in SPOKEN SCRIPT or CAPTION. Copy them exactly, do not invent synonyms.\n"
        "5. TWO-SIDED VERDICT - do only one side:\n"
        "   - IF views is 5000 or more: viral_verdict = 'Viral - X views'. Fill why_viral with 3 reasons, each with exact evidence. Leave why_not_reaching as [].\n"
        "   - IF views is under 5000: viral_verdict = 'Not viral - only X views'. Leave why_viral as []. Fill why_not_reaching with 3 specific reasons why this Reel did NOT get reach, each with exact evidence from THIS Reel. Look for: first 3 sec visual has no face/person, spoken hook is a song lyric not a direct hook, caption question is generic, hashtags are niche community tags only, no clear CTA in the video, engagement rate is low (calculate likes/views). Do NOT write generic 'low quality' or 'bad content' - name the exact hook, visual, line, or hashtag that caused it.\n"
        "6. replicable_formula: ONLY if viral. If NOT viral, replicable_formula MUST be empty string \"\". Never write a formula for a non-viral Reel.\n"
        "7. If viral, replicable_formula = how to copy what worked, step-by-step using THIS Reel's exact hook, exact visual, and exact closing line. If NOT viral, what_to_fix = 3 specific fixes for THIS Reel, each naming the exact line/visual/hashtag to change and what to change it to.\n"
        "8. If there is no speech, say hook_spoken is empty. If SPOKEN SCRIPT is exactly 'Song', that means music only, no spoken words: set hook_spoken to empty, and in why_not_reaching say there is no spoken hook, only music. If there is no on-screen text, say hook_text is empty. If music is missing, say so, do not guess a song.\n\n"
        "Return JSON ONLY with exactly these keys:\n"
        "{\n"
        '  "viral_verdict": "Viral - X views" OR "Not viral - only X views",\n'
        '  "hook_spoken": "exact first spoken line, or empty",\n'
        '  "hook_visual": "exact first visual, or empty",\n'
        '  "hook_text": "exact first on-screen text, or empty",\n'
        '  "structure": [{"part": "Hook", "time": "00:00-00:03", "content": "exact hook from this Reel"}, {"part": "Body", "time": "...", "content": "exact body from this Reel"}, {"part": "CTA", "time": "...", "content": "exact closing line/question from this Reel"}],\n'
        '  "trigger_words": ["exact word from transcript/caption", "exact word from transcript/caption"],\n'
        '  "why_viral": ["only if viral: reason with exact quote", "only if viral: reason with exact visual/caption evidence", "only if viral: reason with exact metric evidence"],\n'
        '  "why_not_reaching": ["only if NOT viral: specific reason with exact evidence", "only if NOT viral: specific reason with exact evidence", "only if NOT viral: specific reason with exact evidence"],\n'
        '  "replicable_formula": "if viral: Step 1: copy this exact hook ... Step 2: copy this exact visual ... Step 3: end with this exact line ...",\n'
        '  "what_to_fix": ["only if NOT viral: change this exact line/visual to ...", "only if NOT viral: change ...", "only if NOT viral: change ..."],\n'
        '  "tone": "e.g. direct, funny, educational"\n'
        "}\n"
    )
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=1500,
        response_format={"type": "json_object"},
    )
    return _clean_json_text(resp.choices[0].message.content)


# ---------- Main runner ----------

def run_extraction(video_url: str, apify_token: str, assemblyai_key: str, openai_key: str, progress=None, force: bool = False, reel_url: str = None):
    """video_url is the public video link (Instagram / TikTok / Facebook). reel_url is an old alias."""
    if reel_url is not None:
        video_url = reel_url
    """progress(status_text) is called after each step, for the UI.
    force=True ignores cache and re-processes. Old cache auto re-runs when CACHE_VERSION changes."""
    def log(msg):
        if progress:
            progress(msg)

    reel_url = video_url
    platform = detect_platform(reel_url)
    shortcode = extract_video_id(reel_url)
    cached = RESULTS_DIR / f"{shortcode}.json"
    if cached.exists() and not force:
        try:
            old = json.loads(cached.read_text(encoding="utf-8"))
            # Auto-delete old cache: if version is old, re-process automatically.
            if old.get("cache_version") == CACHE_VERSION:
                return old
        except Exception:
            pass  # corrupt cache -> re-process below

    workdir = BASE_DIR / "cache" / shortcode
    workdir.mkdir(parents=True, exist_ok=True)

    log("Step 1/5: Apify - getting Reel video + metrics...")
    meta = apify_get_reel(apify_token, reel_url)
    meta["shortcode"] = shortcode
    meta["reel_url"] = reel_url
    meta["platform"] = platform

    log("Step 2/5: Downloading video + extracting audio and frames...")
    video_path = workdir / "video.mp4"
    download_video(meta["videoUrl"], video_path, download_via=meta.get("download_via", ""), apify_token=apify_token)
    audio_path, frames = extract_audio_and_frames(video_path, workdir)

    log("Step 3/5: AssemblyAI - transcribing spoken script...")
    transcript = assemblyai_transcribe(assemblyai_key, audio_path)
    # NEW 2026-11-13: if the audio is only a song, do NOT write the lyrics.
    # Spoken script shows only "Song". Lyrics are kept in JSON as "lyrics" for record, not shown.
    is_song = False
    original_text = transcript.get("text") or ""
    if original_text.strip():
        try:
            from openai import OpenAI
            _c = OpenAI(api_key=openai_key)
            _r = _c.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "Is the following text sung song lyrics, or spoken dialogue? Answer with only one word: SONG or SPEECH. Song lyrics rhyme, repeat, or are sung. Spoken dialogue is normal talking.\n\nTEXT: " + original_text[:2000]}],
                temperature=0.0,
                max_tokens=10,
            )
            is_song = _r.choices[0].message.content.strip().upper().startswith("SONG")
        except Exception:
            is_song = False
    transcript["is_song"] = is_song
    if is_song:
        transcript["lyrics"] = original_text
        transcript["text"] = "Song"
        srt_text = "Song"
    else:
        srt_text = build_srt(transcript.get("words") or [])

    log("Step 4/5: OpenAI GPT-4o - analysing visuals + on-screen text...")
    visual = openai_visual_analysis(openai_key, frames, transcript.get("text") or "")

    log("Step 5/5: OpenAI GPT-4o-mini - viral breakdown...")
    viral = openai_viral_analysis(openai_key, meta, transcript, visual)
    # FIX 2026-11-17: model sometimes said "Not viral" for 221,727 views.
    # Verdict is decided HERE in Python from the real view count, never by the model alone.
    # 5000+ views = Viral, under 5000 = Not viral. The two sides are never mixed.
    try:
        _views = int(meta.get("views") or 0)
    except Exception:
        _views = 0
    if _views >= 5000:
        viral["viral_verdict"] = f"Viral - {_views} views"
        viral["why_not_reaching"] = []
        viral["what_to_fix"] = []
    else:
        viral["viral_verdict"] = f"Not viral - only {_views} views"
        viral["why_viral"] = []
        viral["replicable_formula"] = ""

    result = {
        "cache_version": CACHE_VERSION,
        "shortcode": shortcode,
        "platform": platform,
        "reel_url": reel_url,
        "meta": meta,
        "transcript": {"text": transcript.get("text") or "", "is_song": is_song, "lyrics": transcript.get("lyrics") or "", "language": transcript.get("language"), "words": [] if is_song else (transcript.get("words") or []), "utterances": transcript.get("utterances") or []},
        "srt": srt_text,
        "visual": visual,
        "viral": viral,
        "cost_estimate_usd": 0.12,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    cached.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (RESULTS_DIR / f"{shortcode}.txt").write_text(result["transcript"]["text"], encoding="utf-8")
    (RESULTS_DIR / f"{shortcode}.srt").write_text(srt_text, encoding="utf-8")

    # cleanup temp video/audio/frames, keep results
    try:
        shutil.rmtree(workdir)
    except Exception:
        pass
    log("Done.")
    return result
