# Ghost Studio

Make ads, pictures, songs and dubbed videos on your own computer, by typing or by talking to Ghost.
Free by default; an optional video-service key adds real moving video.

What it makes:

- **Video ads:** script → one picture per shot → camera moves → narrator → music → 1080p MP4.
- **Images only:** a set of pictures with the same person and place.
- **Music & songs:** full songs with vocals in Indian languages, covers of your own tune, replace a part, split stems.
- **Dubbing:** your video re-voiced in another language, keeping the background.

## The screen

- **One box.** Type an idea, or switch on **Exact words** and paste your dialogue. Then press Enter.
- **+ button.** Use it to add a reference ad to copy, up to 6 character or product photos, or background music. These keep the same faces and products in every shot. You can also drag files onto the box.
- **Final / Draft.** Draft makes a quick low-resolution preview so you can check the idea fast. **Make it final** then re-makes the same takes at full quality.
- **Small options under the box:** length, shape (9:16 Reels, 4:5 Feed, 1:1, 16:9 YouTube), voice and language.
- **Shots.** Click a shot to change its words, camera move, shake or move strength. Each camera tile shows its move when you point at it.
- **✨ Change.** On any picture, shot or song take, type what to change in plain words (“shorter hair”, “brown eyes”, “make it a man”, “blue saree”, “more upbeat”, “male voice”), or tap a suggestion. Ghost edits that same one and keeps everything else. Pick **All of them** to change every picture or shot. **↶ Undo** brings back the version from before. **New take** is still there when you want something completely different.
- **✨ Rewrite.** In ✎ Edit, every line, prompt and lyric has a Rewrite button (“shorter”, “more emotional”, “in Malayalam”). Check the result, then Save.
- **Talk to Ghost** (top bar). Tell Ghost what to do in your own words, in English, Hindi, Hinglish or Malayalam: “make the dog in picture 2 older”, “same as my Onam song but for Diwali”, “undo”. Every action and its result is shown, and big actions wait for your OK. How it is built: docs/ARCHITECTURE.md.
- **Live answers:** Talk to Ghost looks up today's news, weather and facts by itself (free) and shows its sources. 🧠 turns on deep thinking. Optional in ⚙: your own Gemini/OpenAI key for ChatGPT-level answers, and a Brave key for whole-web search.
- **Clock icon (top left)** opens all your ads. The **gear** shows what this computer is using.

## Free mode and the optional video service

- **Free (default, no key):** Ghost Studio writes the script, makes one picture per shot, records a narrator (Natural voices add-on, or the Mac's built-in voice), and brings every picture to life with your camera moves. The result is a "photo motion" ad.
  - **Pictures:** install **AI pictures** under ⚙ → Add-ons (needs an M-chip Mac with 16 GB or more). Otherwise it uses your own photos (+ button) or simple colour cards.
  - **Hindi narrator voice:** if you hear an English voice, add **Lekha** in System Settings → Accessibility → Spoken Content → System voice → Manage voices.
- **Video service (optional):** paste a **fal.ai** key under ⚙ to get real moving video where people speak (Veo 3.1 Lite, about US$0.05 per second). The key stays on that computer only, and the app stops at your monthly limit. Press **Remove key** to go back to free mode.

## Install (one line, Mac or Windows, no account)

Open **https://vishnusunilwork-lgtm.github.io/ghost-studio/** and copy the line for your computer.
- **Mac:** paste it in **Terminal** and press Enter.
- **Windows:** paste it in **PowerShell** and press Enter.

You get a **Ghost Studio** app icon on your Desktop. Click it and the app opens in your browser: no Terminal after the first install. It updates itself (**Update now** button), and you can stop it under ⚙ → **Stop Ghost Studio**.

## For the team

1. Install with the one line above (Mac or Windows). No account needed.
2. Click the **Ghost Studio** icon on the Desktop. The first start sets itself up in about 5 minutes.
3. Updates: an **Update now** bar appears in the app. Your ads, settings and what you are typing are kept.
4. Something went wrong? Press **Copy problem report** and send it to Vishnu.

## Add-ons (⚙ → Add-ons, install once)

| Add-on | What it does | Model (licence) |
|---|---|---|
| Script AI | Understands you in Talk to Ghost, writes scripts and prompts, translates, checks pictures | Qwen 3.5 / Qwen3-VL in Ollama (Apache 2.0), faster-whisper (MIT) |
| AI pictures | Photo-real pictures | FLUX.2 klein 4B (Apache 2.0) |
| Best realism | Most realistic people in Quality mode | Z-Image-Turbo (Apache 2.0) |
| Music & songs | Songs, covers, replace a part | ACE-Step 1.5 (MIT) |
| Natural voices | Human-sounding Malayalam, Hindi, Tamil, English… narration | Indic Parler-TTS (Apache 2.0) |
| Song stems | Vocals / drums / bass / rest; clean backgrounds for dubbing | Demucs (MIT) |

All of these allow commercial use. Voices and stems run in their own small Python so they can never break the picture models.

## Settings

`config.yaml` holds the brand, default language and models. Put your own changes in `config.local.yaml`: updates never touch it.
`engine: mock` runs a test mode with placeholder media.

## Licences — please read

- Every add-on model is Apache 2.0 or MIT (see the table above), so it is free for commercial use.
- Label AI-made videos, songs and voices as AI-generated.
- Always use fictional people. Never copy a real person's face, a competitor's logo, or make income, cure or guaranteed-result claims.

## Honest limits

- Free mode makes photo-motion ads (pictures + camera moves). Real moving video needs the optional video-service key.
- A Mac is much slower than cloud services: a picture takes 10–60 s, a song several minutes. The video-service key makes pictures and video take seconds.
- Dubbing uses a natural voice, not the original speaker's own voice. Lines that run long are sped up a little (at most 1.45×); shorter wording fits better.
- Local script models understand well but make more mistakes than ChatGPT-class models; Talk to Ghost shows every action and asks before big ones.

## Folders

- `projects/` holds one folder per ad, with every image, clip and the final MP4. Nothing is ever mixed between projects.
- `app/` holds the program.
