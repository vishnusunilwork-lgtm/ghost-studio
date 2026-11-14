# Ghost Studio (AI Film Studio Local)

Make a vertical 9:16 ad in **one click**, entirely on your own computer.
No accounts, no API keys, no cloud, nothing to pay per video.

Write an idea, or paste your dialogue, or drop in a reference ad, then press **Make my ad**. The app does the rest:

1. **Reads the reference.** Finds the scene cuts, looks at the key frames and writes down what is said.
2. **Writes the script and every prompt.** This covers the image prompt and one video prompt per clip.
3. **Makes the first-frame image.** It makes one image per person, so faces stay the same.
4. **Makes each video clip.** People speak and their lips match the words.
5. **Records a narrator,** if you chose narrator voice.
6. **Edits and exports.** It adds a camera move to each clip (push in, pull out, pan, tilt and handheld shake), mixes in music and exports a 1080x1920 MP4 at full quality.

After that you can change any clip's line, camera move or shake. Press **Make this clip again** for a new take, or **Re-export** to apply the changes.

## The screen

- **One box.** Type an idea, or switch on **Exact words** and paste your dialogue. Then press Enter.
- **+ button.** Use it to add a reference ad to copy, up to 6 character or product photos, or background music. These keep the same faces and products in every shot. You can also drag files onto the box.
- **Final / Draft.** Draft makes a quick low-resolution preview so you can check the idea fast. **Make it final** then re-makes the same takes at full quality.
- **Small options under the box:** length, shape (9:16 Reels, 4:5 Feed, 1:1, 16:9 YouTube), voice and language.
- **Shots.** Click a shot to change its words, camera move, shake or move strength. Each camera tile shows its move when you point at it.
- **Clock icon (top left)** opens all your ads. The **gear** shows what this computer is using.

## For the team (Mac)

1. Unzip **Get Ghost Studio.zip** from Vishnu and move the **Ghost Studio** folder anywhere, for example into Documents.
2. Double-click **Start Ghost Studio**. If your Mac says it can't be opened: right-click it, choose **Open**, then click **Open** again.
3. **First time only:**
   - A code appears in the window. Press Enter and your browser opens.
   - Paste the code and click **Authorize**.
   - The app then downloads and installs itself, which takes about 5 minutes.
4. Every later start checks for updates automatically.
5. **Something went wrong?** Press **Copy problem report** (on the ad, or under the gear) and send it to Vishnu.
6. **Your own settings** go in a file called `config.local.yaml`. Updates never touch that file, your ads, or the installed parts.

## What you need

| | Minimum | Good |
|---|---|---|
| Graphics card | NVIDIA, 16 GB memory (RTX 4080 / 4070 Ti Super) | NVIDIA, 24–32 GB (RTX 4090 / 5090) |
| Computer memory | 32 GB | 64 GB |
| Free disk space | 120 GB | 200 GB (SSD) |
| System | Windows 10/11 or Linux | |

A Mac **cannot** make the real videos, because these video models need an NVIDIA graphics card. On a Mac the app runs in **test mode**: you can try every button, but it makes placeholder clips.

## Install (one time)

- **Windows:** right-click `install.ps1` and choose **Run with PowerShell**.
- **Linux/Mac:** run `./install.sh`.

The first time you make an ad, the app downloads the image and video models. That is tens of GB and can take an hour; after that it starts quickly.

## Start

- **Windows:** double-click `run.bat`.
- **Linux/Mac:** run `./run.sh`.

The browser opens at http://localhost:7860. Only this computer can see it.

## How long it takes

On an RTX 4090, a 30-second ad usually takes **15–40 minutes**, and most of that time goes into the video clips. You can leave the app running; the progress bar shows each step.

## Settings

Open `config.yaml` in Notepad to change any of these:

- the brand
- the default language
- the voice
- the models
- the quality

Then restart the app.

- `engine: auto` uses real models when an NVIDIA GPU is found. `engine: mock` always uses test mode.
- `export_crf: 16` is visually lossless. A lower number gives higher quality and a bigger file.

## Licences — please read

- **Script models** (Qwen 2.5 via Ollama), **image model** (FLUX.2 klein 4B), **voice** (Kokoro) and **speech-to-text** (Whisper) are all Apache 2.0 or MIT, so they are free for commercial use.
- **Video model (LTX-2 by Lightricks):**
  - It is free for companies under **US$10 million** yearly revenue, counting group companies.
  - Bigger companies must sign a commercial agreement with Lightricks before using it for ads.
  - The licence also requires AI-generated content to be labelled.
  - It forbids impersonating real people without consent.
- Always use fictional people. Never copy a real person's face, a competitor's logo, or make income, cure or guaranteed-result claims.

## Honest limits

- Quality is good but **below Google Veo or Kling**. Expect to re-make some clips.
- Hindi speech from the video model is decent. Other Indian languages may need narrator mode.
- The NVIDIA steps could not be tested on a GPU before shipping, so the first real run may need small fixes.

## Folders

- `projects/` holds one folder per ad, with every image, clip and the final MP4. Nothing is ever mixed between projects.
- `app/` holds the program.
