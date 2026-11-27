---
name: video-transcript
description: Create a plain-text transcript (speech-to-text) from a YouTube video link or from a video/audio file on disk, in the original spoken language with no translation. Use this skill whenever the user asks for a transcript, captions, subtitles, "dialogs/dialogue as text", "video to text", "speech to text", or wants the spoken words of a YouTube URL or an .mp4/.mkv/.mov/.webm/.mp3/.wav/.m4a file, even if they never say the word "transcript".
compatibility: Needs internet access, Python 3.9+, ffmpeg, yt-dlp and faster-whisper (or openai-whisper). Works in Claude Code, Cowork, or on the user's own machine; the claude.ai sandbox has no network.
---

# Video transcript

Turns a YouTube link or a local media file into a text file containing what is spoken. The text stays in the spoken language. Nothing is translated unless the user separately asks for it afterwards.

## How it works

`scripts/transcribe.py` does two things, in this order:

1. **YouTube link:** download the video's own captions (manual ones if they exist, otherwise YouTube's automatic ones). This takes seconds and needs no heavy model.
2. **Fallback, or any local file:** extract the audio with ffmpeg and run Whisper speech recognition locally. Slower, but works for any file and is usually more accurate than automatic captions.

Whisper is always run with `task="transcribe"`, which keeps the original language. This matters because Whisper can also translate to English, and the user explicitly does not want that.

## Workflow

1. **Check the environment first.**
   ```bash
   python scripts/transcribe.py --check
   ```
   It reports which of ffmpeg, yt-dlp, faster-whisper and network access are available. If something is missing, install it (`pip install yt-dlp faster-whisper`, and ffmpeg via the system package manager).
   If there is no network and the packages can't be installed, stop and tell the user plainly what the skill needs and that it runs in Claude Code, Cowork, or on their own computer. Offer two alternatives: they run the command below themselves, or they paste YouTube's "Show transcript" text and Claude cleans it up. Do not try to fake a transcript.

2. **Run the script.**
   ```bash
   # YouTube link (captions first, Whisper if there are none)
   python scripts/transcribe.py "https://www.youtube.com/watch?v=VIDEO_ID" --out-dir /mnt/user-data/outputs

   # File on disk
   python scripts/transcribe.py "/path/to/interview.mp4" --out-dir /mnt/user-data/outputs
   ```

3. **Deliver the result.** The script prints the path of the `.txt` file it wrote. Give the user that file, and show a short excerpt (first 10 to 15 lines) so they can check the language and quality at a glance.

## Options worth knowing

| Option | Use it when |
|---|---|
| `--lang de` | The spoken language is known. Skipping auto-detection is more reliable, especially for short or noisy clips or mixed-language audio. Use the ISO code of the language actually spoken. |
| `--engine whisper` | The user wants the best accuracy, or YouTube's automatic captions are poor. Forces speech recognition even for a URL. |
| `--engine captions` | The user only wants a quick result from a YouTube link and no model download. |
| `--model small` (default) | Good balance on CPU. Use `medium` or `large-v3` for accuracy on hard audio if the machine has a GPU or the user is happy to wait; `base` or `tiny` for speed. |
| `--timestamps` | The user wants `[mm:ss]` before each line. |
| `--srt` | The user also wants a subtitle file. |
| `--device cuda` / `cpu` | Override the automatic choice. |

If the user didn't say which language is spoken and the result looks wrong (garbled text, wrong script), rerun with `--lang` set explicitly rather than guessing.

## Dialogue and speakers

The output is one line per spoken segment. YouTube captions sometimes mark a change of speaker with `>>`, and the script starts a new line there, so some dialogue structure survives. Whisper itself does not identify speakers.

If the user wants lines labelled by speaker (`A:` / `B:`), say that automatic speaker labelling is not part of this skill and that any labels Claude adds by reading the text are guesses. Offer to add them as a best-effort pass only if they accept that. Never present guessed labels as fact.

## Troubleshooting

- **YouTube returns 403, "Sign in to confirm you're not a bot", or age-restricted errors:** try `pip install -U yt-dlp` first. If it persists, the user can export a cookies file from their browser and the script accepts `--cookies cookies.txt`.
- **"No captions found":** expected for many videos. The script falls back to Whisper automatically.
- **Whisper is very slow:** use a smaller `--model`, or run on a machine with a GPU.
- **Output language looks wrong:** pass `--lang`.
- **Private, members-only, or DRM-protected videos:** not supported. Ask the user for a file they have a copy of.

## Scope

Use this for videos the user has the right to work with: their own recordings, meetings, lectures, interviews, or public videos for personal study or accessibility. Don't use it to bypass access controls.
