# Handoff: auto-edited 4K Reels from iPhone clips (run on the Mac M4 Pro)

From the Windows PC Claude Code session "Reel videos", 2026-10-20.

## Goal
The owner wants Instagram Reels made from their own recorded clips. Claude picks the
clips itself, decides the order, and cuts to a song. Output must stay at the
**original recording resolution (4K)**, not 1080x1920.

## Why the Mac
- The originals live in the Mac's **Photos library (iCloud Photos)**.
- The M4 Pro media engines encode and decode HEVC/H.264/ProRes in hardware, so
  ffmpeg with `hevc_videotoolbox` / `h264_videotoolbox` makes 4K renders fast.
- The Windows PC (Ryzen 5 2600X, GT 610 with no hardware encoder) can only render
  4K on the CPU, which is slow.

## How to get the clips (agreed approach)
- No direct iCloud access, and never sign in with the Apple ID.
- Use **osxphotos** (`pip install osxphotos` or `uv tool install osxphotos`) to read
  the local Photos library: list albums and videos (date, GPS, resolution, HDR),
  then export the **unmodified originals**, including `--download-missing` for
  videos that are only in iCloud ("Optimize Mac Storage").
- The owner must grant Photos / Full Disk Access to the terminal or Claude app once
  (System Settings → Privacy & Security).
- Typical request from the owner: "make a reel from album X with this song".

## Pipeline
1. **Inventory:** list the album's clips with duration, resolution, fps,
   codec/HDR, GPS and time.
2. **See the clips:** sample frames from each clip (e.g. 1 per second plus scene
   changes) and look at them. Score sharpness, shake, exposure and how interesting
   the subject is. Group clips by location from GPS and by time.
3. **Analyse the song** (Claude can't listen, so measure it): tempo, beat grid,
   energy curve, build-ups and drops (librosa). Ask the owner for any lyric or
   moment cues.
4. **Edit plan:** cuts land on beats; short punchy clips go in high-energy parts and
   wide or slow shots in calm parts; tell a location or time story; speed ramps
   where useful. Make 2–3 versions.
5. **Drafts:** fast low-res previews for review. Expect 1–2 feedback rounds.
6. **Master:** render once at the original resolution and frame rate.
   - Clips recorded vertical 4K (2160x3840): full 4K vertical master.
   - Clips recorded horizontal 4K: a 9:16 crop gives about 1215x2160. Tell the owner,
     or keep a horizontal 4K master instead.
   - HDR (HLG / Dolby Vision): keep HDR, or tone-map everything to SDR. Never mix
     HDR and SDR in one Reel.
7. **Exports:** a 4K master, plus an Instagram upload version. Export a silent
   version too. Copyrighted songs burned into the video may get muted on Instagram;
   the safer route is adding the same song inside Instagram, where the cuts still
   line up with its beats.

## Owner preferences
- Short, plain-language replies.
- Instagram downsizes to about 1080 wide for playback, but the owner still wants
  the 4K master (Instagram's compression looks better from a cleaner source, and
  the master works for other platforms).
- Nothing gets posted on the owner's behalf. Hand back the files only.

## Related work already done (PC)
`G:\fyers_data_pipeline\content\insta_reels\` holds a generated quant-trading Reel
(`quant_vs_buy_sell_reel.mp4`) and its script `make_reel.py`. That is a separate
project; the owner will come back to it.
