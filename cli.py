import subprocess
import json
import os
import sys
import shutil

YT_DLP = shutil.which("yt-dlp") or "yt-dlp"
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

def run_cmd(cmd):
    """Run a shell command and return stdout, None on error."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"❌ Error running: {' '.join(cmd)}")
            print(result.stderr.strip())
            return None
        return result.stdout
    except FileNotFoundError:
        print(f"❌ Command not found: {cmd[0]}")
        sys.exit(1)

def safe_filename(name):
    """Remove illegal filename characters."""
    return "".join(c for c in name if c.isalnum() or c in " _-").strip()

def get_formats(url):
    """Get available video/audio formats for single video."""
    output = run_cmd([YT_DLP, "-J", "--no-playlist", url])
    if not output:
        return None, None, None

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        print("❌ Failed to parse yt-dlp JSON output.")
        return None, None, None

    title = data.get("title", "video")
    formats = data.get("formats", [])
    video_formats = []
    audio_formats = []

    for f in formats:
        if f.get("vcodec") != "none" and f.get("acodec") == "none":
            video_formats.append(f)
        elif f.get("acodec") != "none" and f.get("vcodec") == "none":
            audio_formats.append(f)

    return title, video_formats, audio_formats

def choose_video_format(video_formats):
    """Prompt user to choose a video format."""
    print("\n🎥 Available Video Formats:")
    for i, f in enumerate(video_formats):
        res = f.get("height", "audio")
        fmt_id = f["format_id"]
        ext = f["ext"]
        size = f.get("filesize", None)
        size_mb = f"{round(size / (1024*1024), 2)} MB" if size else "Unknown"
        print(f"{i+1}. {fmt_id} - {res}p - {ext} - {size_mb}")

    while True:
        try:
            choice = int(input("\nEnter choice number: ")) - 1
            if 0 <= choice < len(video_formats):
                return video_formats[choice]["format_id"]
            print("⚠ Invalid choice.")
        except ValueError:
            print("⚠ Please enter a number.")

def download_and_merge(url, video_fmt, audio_fmt, output_name):
    """Download video+audio separately, then merge."""
    video_file = "video_temp.mp4"
    audio_file = "audio_temp.m4a"

    try:
        print("📥 Downloading video...")
        if run_cmd([YT_DLP, "-f", video_fmt, "-o", video_file, url]) is None:
            return False

        print("📥 Downloading audio...")
        if run_cmd([YT_DLP, "-f", audio_fmt, "-o", audio_file, url]) is None:
            return False

        print("🔄 Merging...")
        if run_cmd([FFMPEG, "-y", "-i", video_file, "-i", audio_file, "-c", "copy", output_name]) is None:
            return False

        print(f"✅ Done! Saved as {output_name}")
        return True

    finally:
        # Cleanup temp files
        for f in [video_file, audio_file]:
            if os.path.exists(f):
                os.remove(f)

def download_playlist(url):
    """Download playlist in <=720p automatically."""
    print("📂 Fetching playlist info...")
    output = run_cmd([YT_DLP, "-J", "--flat-playlist", url])
    if not output:
        return

    try:
        data = json.loads(output)
        entries = data.get("entries", [])
    except json.JSONDecodeError:
        print("❌ Failed to parse playlist JSON.")
        return

    if not entries:
        print("❌ No videos found in playlist.")
        return

    print(f"✅ Found {len(entries)} videos.\n")

    for i, entry in enumerate(entries, 1):
        vid_id = entry.get("url")
       
        
      
        title = entry.get("title", f"video_{i}")
        safe_title_str = safe_filename(title)

        print(f"📹 Downloading {i}/{len(entries)} — {title}")
        cmd = [
            YT_DLP,
            "-f", "bv*[height<=720]+ba/b[height<=720]",
            "-o", f"{safe_title_str}.%(ext)s",

           vid_id
        ]

        if run_cmd(cmd) is None:
            print(f"⚠ Failed: {title}")
        else:
            print(f"✅ Finished: {title}\n")

if __name__ == "__main__":
    url = input("Enter YouTube URL: ").strip()
    is_playlist = input("Is this a playlist? (y/n): ").strip().lower() == "y"

    if is_playlist:
        download_playlist(url)
    else:
        title, videos, audios = get_formats(url)
        if not videos or not audios:
            print("❌ No formats found.")
            sys.exit(1)

        chosen_video_fmt = choose_video_format(videos)
        best_audio_fmt = audios[-1]["format_id"]  # best audio

        safe_title_str = safe_filename(title)
        download_and_merge(url, chosen_video_fmt, best_audio_fmt, f"{safe_title_str}.mp4")
