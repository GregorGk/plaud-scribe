# plaud-scribe

Pulls recordings from your Plaud account, re-transcribes them with **ElevenLabs Scribe v2**
(detects and switches between languages *inside one file*, labels speakers), asks
**Claude** for a summary, and files everything in Google Drive. Runs unattended on this
host via a systemd timer, or by hand for a single recording.

```
Plaud REST API ──► Scribe v2 ──► turns + languages + speakers ──► md / txt ──► Drive
   (presigned audio URL handed to ElevenLabs; audio never lands here)  └► Claude ──► summary
```

Every recording becomes three files in `Plaud Transcripts/YYYY-MM/` on Drive:

| File | Contents |
| --- | --- |
| `2026-08-24_1403__weekly-sync.md` | Markdown transcript: front matter, `**[00:00:04] Speaker 1:**` lines, language tags where a turn departs from the main language |
| `2026-08-24_1403__weekly-sync.txt` | The same transcript as plain text, no markup |
| `2026-08-24_1403__weekly-sync.summary.md` | Claude's summary: overview, key points, decisions, action items, open questions |

Subtitles (`srt`, `vtt`) and the raw provider JSON are available too; see `[drive] formats`.

## Why it is built this way

* **Plaud publishes no personal API key.** The sanctioned path is the official
  `@plaud-ai/cli`, which does a browser OAuth2+PKCE login and stores tokens in
  `~/.plaud/tokens.json`. This app reuses that token and calls the same REST API the CLI
  calls. Token refresh is delegated back to the CLI (`plaud me`), with a direct call to
  the refresh endpoint as a fallback.
* **`language_code` is deliberately never set.** Scribe v2 handles the switching itself;
  pinning a language is what breaks mixed-language recordings.
* **Per-turn language tags are computed locally** with `lingua`, restricted to the
  languages you actually speak (`[language] expected`).
* **Raw transcripts and summaries are cached** under `~/.local/share/plaud-scribe/raw/`,
  so re-rendering after changing speaker names or formats costs nothing. A summary is only
  regenerated when the transcript changes or you ask for it.
* **A failed summary never loses the transcript.** The `.md` and `.txt` still upload; the
  summary is retried the next time the recording is rendered with `--upload`.

## Setup, step by step

The code is installed and the systemd units are in place. What remains is credentials.
Run `plaud-scribe doctor` after each step; it checks every one of them.

```bash
~/sec/plaud-scribe/.venv/bin/plaud-scribe doctor
```

Your config lives at `~/.config/plaud-scribe/config.toml` (already created from
`config.example.toml`, mode 600).

### 1. ElevenLabs API key (transcription)

1. Sign in at https://elevenlabs.io, open your profile menu, then **API Keys**.
2. **Create API Key**, and set the scopes as follows. The app calls exactly one endpoint,
   `speech_to_text.convert`, so anything else only widens the damage if the key leaks:
   * **Speech to Text → Access**
   * every other endpoint, and every row under Administration → **No Access**
3. Leave **Auto-disable if leaked** on. Under **Restrict by IP address** you can pin the
   key to this host's address, since nothing else uses it. Note that transcription then
   stops if the VPS address ever changes.
4. The **Usage Limits (Credits)** field is per billing period, which matches the
   `[limits] monthly_minutes` ceiling below. Set it as a backstop once you know your
   plan's credits-per-hour rate; see *Capping how much gets transcribed*.
5. Store the key with `scripts/set-key`, which reads it from stdin so it never appears
   on screen, in your shell history, or in a process argument list:

```bash
~/sec/plaud-scribe/scripts/set-key elevenlabs
```

   It prompts, and the key is not echoed as you paste. If your terminal will not paste
   into the VPS at all, pipe it from your local clipboard instead, run on your own machine:

```bash
pbpaste | ssh ubuntu@<this-host> '~/sec/plaud-scribe/scripts/set-key elevenlabs'
```

   The same script takes the Anthropic key: `scripts/set-key summary`.

Scribe v2 costs $0.22 per hour of audio (plus $0.05/hour if you fill in `keyterms`).

### 2. Anthropic API key (summaries)

1. Go to https://console.anthropic.com, **API Keys**, **Create Key**.
2. Put it under `[summary] api_key = "..."` in the same config file.

A one-hour recording is roughly 12k input tokens and under 1k output tokens with
`claude-opus-5`, so summaries cost a few cents each. `plaud-scribe status` shows spend.
Summaries are written in the language that dominates the recording; set
`[summary] language = "en"` (or any language) to fix it.

### 3. Sign in to Plaud

The OAuth callback always goes to **localhost:8199**, so tunnel that port from the
machine that has the browser, and stay in that session:

```bash
ssh -L 8199:localhost:8199 ubuntu@<this-host>
```

Then run the wrapper rather than `plaud login` directly:

```bash
~/sec/plaud-scribe/scripts/plaud-login
```

It prints the authorization URL; open it in your local browser and approve. You have two
minutes. The token lands in `~/.plaud/tokens.json` and refreshes itself from then on.

Plain `plaud login` does not work on a headless host. It only prints the URL if opening a
browser fails, and since `xdg-open` is installed here the open appears to succeed, so the
URL is never shown and the login sits until it times out. The wrapper puts a shim ahead
of `xdg-open` to capture the URL and print it.

### 4. Authorise Google Drive

The app uses the `drive.file` scope only: it can see nothing in your Drive except the
files it created itself.

1. https://console.cloud.google.com → create (or pick) a project.
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **APIs & Services → OAuth consent screen** → External, fill in the app name and your
   email → add scope `https://www.googleapis.com/auth/drive.file` → save.
4. **Publish the app** (status *In production*). This matters: a client left in *Testing*
   has its refresh tokens revoked after **7 days**, which silently kills the timer.
   `drive.file` is a non-sensitive scope, so publishing needs no Google review.
5. **Credentials → Create credentials → OAuth client ID** → type **Desktop app** →
   download the JSON and save it on this host as
   `~/.config/plaud-scribe/google_client.json` (`chmod 600`).
6. Tunnel port 8765 the same way as above, then run:

```bash
ssh -L 8765:localhost:8765 ubuntu@<this-host>
```

```bash
~/sec/plaud-scribe/.venv/bin/plaud-scribe auth google
```

### 5. First run, then the timer

```bash
~/sec/plaud-scribe/.venv/bin/plaud-scribe doctor
```

```bash
~/sec/plaud-scribe/.venv/bin/plaud-scribe sync --dry-run
```

```bash
~/sec/plaud-scribe/.venv/bin/plaud-scribe sync
```

The first `sync` processes the last 7 days. When you are happy with the output, start the
timer (every 30 minutes, catches up after a reboot):

```bash
systemctl --user enable --now plaud-scribe.timer
```

```bash
sudo loginctl enable-linger ubuntu
```

The `enable-linger` line lets the timer run when you are not logged in. Check on it with:

```bash
systemctl --user list-timers plaud-scribe.timer
```

```bash
journalctl --user -u plaud-scribe -n 50
```

## Use

```bash
plaud-scribe doctor                     # every credential and path, checked
plaud-scribe list --days 30             # what Plaud has, and what state it is in here
plaud-scribe sync --dry-run             # what would be processed
plaud-scribe sync                       # last 7 days (what the timer runs)
plaud-scribe sync --all                 # walk the full history
plaud-scribe transcribe <id>            # manual mode: one recording, by Plaud id
plaud-scribe transcribe <id> --force    # re-transcribe, ignoring the cache
plaud-scribe transcribe ./meeting.m4a   # any local audio file, for testing
plaud-scribe render <id>                # re-render from cache, no API cost
plaud-scribe render <id> --upload       # …and push the new version to Drive
plaud-scribe render <id> --resummarize  # ask Claude for a fresh summary
plaud-scribe status                     # what ran, what it cost this month
```

The venv binary is `~/sec/plaud-scribe/.venv/bin/plaud-scribe`; add that directory to
your `PATH` or alias it. Exit codes: `0` fine, `1` error, `2` Plaud auth needs attention.

Local copies of everything uploaded are kept under `~/.local/share/plaud-scribe/out/`.
Uploads are idempotent: a re-run updates the existing Drive file rather than creating a
second copy.

### Naming speakers

Diarization numbers speakers in order of first appearance, so *Speaker 1* is whoever
talks first, not necessarily you. Once you know who is who, map them in
`~/.config/plaud-scribe/config.toml`:

```toml
[speakers.aliases]            # applies to every recording
speaker_0 = "Anna"

[speakers.per_recording.rec_abc123]   # one recording only
speaker_1 = "Tomasz"
```

Then `plaud-scribe render <id> --upload`: free, no re-transcription. The summary is
regenerated because the transcript text changed.

Optionally set `[speakers] llm_naming = true` to let Claude pick up names people state
out loud (introductions, being addressed by name). Config always wins over it.

### Capping how much gets transcribed

`[limits]` caps the audio sent for transcription. `monthly_minutes` defaults to **2700
minutes, 45 hours, about $9.90 a month**, over the calendar month so it lines up with the
billing period the ElevenLabs key's own cap refreshes on. `daily_minutes` is a rolling
24-hour circuit breaker, off by default, there only to stop a runaway loop spending the
whole month in one afternoon. Whichever limit is tighter applies; `0` disables either.

A recording that would exceed what is left is **held back, not dropped**: it stays pending
and the next run picks it up once the window has moved on. `plaud-scribe status` shows
what is left under each limit, and `sync --ignore-limits` overrides for one run.
Re-rendering and re-uploading never consume budget, since no audio is sent.

### Adding a language

Append its ISO 639-1 code to `[language] expected`. Scribe transcribes every language
regardless; this list only controls which languages the per-turn tags may choose from.

## Cost

| Item | Rate |
| --- | --- |
| Scribe v2 transcription | $0.22 per audio hour ($0.27 with `keyterms`) |
| Claude summary (`claude-opus-5`) | a few cents per recording |
| Re-rendering | free |
| Default ceiling | 2700 audio minutes (45 hours) per calendar month, about $9.90 |

`plaud-scribe status` shows month-to-date spend for both.

## Tests

```bash
~/sec/plaud-scribe/.venv/bin/python -m pytest
```

Segmentation, language tagging, all renderers, the summary prompt, and the summary cache
run against a checked-in fixture and a fake Claude client. No network, no keys.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `exit 2`, "Not signed in to Plaud" | `plaud login` (tunnel port 8199 first) |
| Drive uploads fail after about a week | The OAuth consent screen is still in *Testing*. Publish it, then `plaud-scribe auth google` |
| "Plaud returned no audio URL" | The recording has not finished syncing from the device yet; it is retried by `sync --retry-failed` |
| `! summary skipped: ...` | Transcript uploaded, summary not. Fix the cause (usually the key), then `plaud-scribe render <id> --upload` |
| Speakers split or merged | Set `[elevenlabs] num_speakers`, or lower/raise `diarization_threshold`, then `transcribe <id> --force` |
| Wrong language tags on short turns | Raise `[language] min_chars` |
| `skip ... limit reached` | Expected: the audio budget is spent. It retries itself, or force it with `sync --ignore-limits` |
| Timer never fires when logged out | `sudo loginctl enable-linger ubuntu` |

## Swapping the transcription engine

`stt/base.py` defines the `SttProvider` protocol; everything downstream works on
provider-neutral `Transcript` objects. A second engine drops in as another `stt/*.py`
module without touching the pipeline.
