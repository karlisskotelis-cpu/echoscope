# EchoScope

Free audio radar for games on Windows. It listens to whatever plays on your default playback device (WASAPI loopback) and draws a radar: angle is left/right, radius is loudness, the white arrow marks the newest cue. It starts listening the moment it opens, and Overlay mode turns the radar into a click-through, always-on-top layer that floats over borderless-windowed games.

Nothing is recorded or uploaded. It only listens to the audio you already hear and does not touch the game. Single portable EXE, no installer, no account, MIT licence.

**Download:** grab `EchoScope-1.0-windows.zip` from the [Releases page](../../releases), unzip, run `EchoScope.exe`. Windows SmartScreen will warn that the app is unsigned the first time: click "More info", then "Run anyway". The whole source is in this repository if you would rather build it yourself (see below).

## Install (recommended)

```bash
powershell -ExecutionPolicy Bypass -File native\install.ps1
```

This copies `dist\EchoScope.exe` to `%LOCALAPPDATA%\Programs\EchoScope\` and adds **Start Menu** and **Desktop** shortcuts, so pressing the Windows key and typing "EchoScope" finds it. No admin rights needed. `-NoDesktop` skips the desktop shortcut; `-Uninstall` removes everything again. Running it after a rebuild updates the installed copy and keeps the version it replaced in `previous\` (Start Menu: "EchoScope (previous version)").

Keeping and restoring versions:

```bash
powershell -ExecutionPolicy Bypass -File native\install.ps1 -Snapshot stable
```

saves the currently installed EXE and its settings as `versions\stable\` with its own Start Menu entry "EchoScope (stable)". `install.ps1 -Revert` puts the previous version back; `install.ps1 -Revert -Snapshot stable` puts a named snapshot back.

Or just run `dist\EchoScope.exe` from anywhere: it is a single portable file. Two files appear next to it on first use:

| File | Purpose |
| --- | --- |
| `settings.json` | Every setting plus the window position and size. Delete it to start fresh. |
| `errorlog.txt` | Start/stop lines, the loopback device that was opened, and any errors. |

## First run

A welcome panel explains the basics. **Enter** closes it, **Space** closes it and starts the demo scene, **D** closes it for good.

1. Play a game (or any sound). The HUD line shows which device it is listening to. No game handy? Press **D** for the demo scene (an engine drifting, a walker circling, an occasional shot).
2. Drag the panel anywhere, resize from its edges. Hotkeys work while the panel has focus; the Ctrl+Shift variants work from any app.
3. Press **O** for Overlay: the panel vanishes, only the radar stays, and clicks pass straight through to the game. To get the panel back: **Ctrl+Shift+O**, or left-click the EchoScope icon in the system tray, or click its taskbar button and press **Esc**.

Borderless windowed mode works best. Exclusive fullscreen covers every overlay, this one included.

## Controls

| Key | Action |
| --- | --- |
| Tab | **Tuning panel**: live sliders for Sensitivity, Stereo width, Centre suppression, Smoothing, Cues per frame, Noise gate, Radius boost, Trail life, Line width, Opacity, Boost, Fade steady sounds, Colour by pitch and Event markers. Up/Down picks a row, Left/Right adjusts, the radar keeps running next to it |
| , . | **Sensitivity** down / up, 1 to 10 (default 5). One dial that makes cues bigger and more numerous or smaller and fewer; shown as "SENS n" in the HUD line |
| 1 2 3 4 5 | Petals / Dots / Comet / Solid Fan / Single Line |
| B | Boost (visual-only push of cues toward the rim) |
| [ ] | Trail / line life shorter or longer (0.1 to 2 s) |
| - = | Opacity down or up (20 to 100 %) |
| P | Pin on top (always-on-top window) |
| O | Overlay (click-through, no panel) |
| R | Restart the audio stream |
| D | Demo scene on / off |
| S / A / C | Smoothing level (Off, Low, Medium, High, Max) / fade steady sounds on-off / cues per frame (1, 2, 3, 5, 10) |
| G / M / T | Rim and guides / L-R level meters / next colour theme |
| ? or F1 | Help (two pages, Space turns the page) |
| Esc | Close panels, leave overlay |
| Ctrl+Q | Quit (Alt+F4 also works) |
| Ctrl+Shift + 1-5, B, O, P, R, [ ], - =, , . | The same actions as **global hotkeys**, from any app, game included (Ctrl+Shift+, and Ctrl+Shift+. change Sensitivity). Switch them off in the menu if a game needs those keys. |
| Right-click, or the tray icon | The menu, drawn inside the radar in the same style as the HUD: a short list whose groups show their current value: Tuning panel, Mode, Sensitivity, Stereo width, Detection (smoothing, cues per frame, centre suppression, noise gate, radius boost, trail life, input gain, Boost, fade steady sounds), Look (colour theme, line width, opacity, window size, colour by pitch, event markers, guides, meters), Audio (restart, demo, capture only the game, full-circle options), Game profiles, Window (pin, overlay, global hotkeys, start in overlay, start with Windows), Help (controls, welcome, open settings.json / errorlog.txt, reset), Quit. Hover or arrow keys select, click / Enter / Right opens, Left / Backspace or the header goes back, Esc or a click outside closes. Toggles are switches, choices are radio lists. The Tab tuning panel also takes clicks on its sliders. |

## Reading the scope

- **Colour by pitch** (on by default): warm amber = low bands (engines, rumble), the theme colour = middle, a light tint = high bands (footsteps, metallic hits). Turn it off in the Tab panel to get a single colour.
- **Event markers** (on by default): a sound that jumps up suddenly leaves a dot just outside the rim at its direction, with a short ping ring, fading over 4 seconds. Useful for a short sound you did not catch live.
- **Full-circle radar**: if the audio reaching EchoScope has 4, 6 or 8 channels, the scope becomes a full circle with FWD at the top and BACK at the bottom and direction comes from the speaker channels. "Full circle always" (Tab panel or menu) draws the circle even with stereo input, where only the front half can be used because stereo carries no front/back information.

  Getting real front/back with a stereo headset: the headset itself only needs to play stereo, but games must *output* 7.1. Turn on **Windows Sonic for Headphones** (or DTS / Dolby) under Settings > System > Sound > your headset > Spatial audio. Windows then presents a virtual 7.1 device to games, and with "Capture only the game" switched on EchoScope asks the per-process loopback for all 8 channels, so the rear channels reach the radar. Set the game's own audio output to 7.1 / surround if it has such an option.

## Game profiles, auto-hide, per-app capture

- **Game profiles** (right-click > Game profiles): with the game running, alt-tab to the radar and choose "Save current tuning as profile for game.exe". From then on, whenever that game is in front, its tuning is applied (and any change you make while it is active is stored in the profile); when another app comes to the front the base tuning returns. The active profile's name shows in the HUD line. "Switch profiles with the game in front" turns the automatic switching off.
- **Auto-hide**: shows the radar only while a profiled game (or EchoScope itself) is in front. Off by default.
- **Capture only the game**: uses Windows' per-process loopback to listen to the last app that was in front (and its child processes) instead of the whole playback device, so voice chat, music or a browser never show on the radar. The HUD line then reads "game.exe only". If the game exits, capture falls back to the device automatically; if Windows refuses the per-process capture, the reason goes to `errorlog.txt` and the device is used. Needs Windows 10 2004 or later.
- **Window size**: Small / Medium / Large presets in the menu; you can still resize from the edges.

Tray icon: left-click shows the panel (and leaves overlay), double-click toggles overlay, right-click opens the menu.

Audio device changes are handled automatically: if the Windows default playback device changes or disappears, the stream is reopened on the new device within a couple of seconds. Press **R** if you ever want to force it.

## Tuning

Start with **Sensitivity** (keys `,` and `.`, or the first row of the Tab panel). 5 is neutral; every step up adds 3 dB of input gain and lowers the noise gate by 0.03, every step down does the reverse, so one dial changes both how big and how many cues you see. Fine-tune the rest in the Tab panel while the game plays.

**Stereo width** (1 to 10, default 7, Tab panel or menu) sets how big a left/right level difference counts as fully sideways: at 7 a 6.5 dB difference already reads 90 degrees, at 1 it takes 15.5 dB (close to the web version's pan law), at 10 just 2 dB. Raise it if sounds right beside you still show near the middle; lower it if everything sticks to the sides.

**Centre suppression** (0 to 10, default 5, Tab panel) shrinks sound that is identical in both ears: your own engine, music beds, narration. Each band is split into what both ears share and what differs; the shared part is scaled down by the setting, and the direction is decided by a vote of the bins that carry off-centre sound, so a centred hum no longer drags side sounds toward the middle. 0 switches it off, 10 removes centred sound completely.

Three controls keep the scope calm (in the Tab panel and the right-click menu, with keys S, C and A):

- **Smoothing** (default Medium): cue angles and levels follow the audio through a short time constant instead of jumping every frame, and the white arrow sticks to its band until another one is clearly louder.
- **Cues per frame** (default 3): only the loudest bands draw a cue each frame. 10 shows every band like the web version; 1 shows a single direction.
- **Fade steady sounds** (default off): each band's level is compared with a slow 2-second background, and a sound that has been steady for a couple of seconds is shown at half level, so anything new or changing stands out. It stays off by default because a game with a constant engine or music bed otherwise looks almost empty.

Other hints:

- Jumpy or all over the place? Raise Smoothing, lower Cues per frame, or raise the noise gate.
- Scope nearly empty although the meters move? Turn "Fade steady sounds" off, lower the noise gate, or raise Sensitivity.
- Looks streaky? Lower Life. Cues vanish too fast? Raise Life.
- Footsteps look tiny? Turn Boost on. Scope sticks to the rim? Turn Boost off or raise the noise gate.
- Everything looks small? Raise Radius boost or Input gain in the right-click menu.
- No movement? The game must play through the Windows default playback device (the one shown in the HUD line). Spatial-audio or virtual devices in between can hide the stereo image.

## Build from source

Requirements: Windows 10/11 64-bit, Python 3.11 to 3.13 (3.12 recommended), and the packages in `requirements.txt` (numpy, pycairo, pyaudiowpatch, pyinstaller). No compiler is needed; all of them ship as wheels.

If Python is missing, install it with one of:

```bash
winget install --id Python.Python.3.12 --exact --scope user
```

or download it from https://www.python.org/downloads/windows/ and tick "Add python.exe to PATH".

Then build:

```bash
powershell -ExecutionPolicy Bypass -File native\build.ps1
```

`build.ps1` installs the requirements, renders `echoscope.ico` with `make_icon.py`, and runs PyInstaller (`--onefile --windowed`). The result is `native\dist\EchoScope.exe`; `build\` is PyInstaller's scratch folder and can be deleted. Run `install.ps1` afterwards to update the installed copy and shortcuts.

To run without building (handy while editing):

```bash
python native\echoscope.py
```

Add `--stats path\to\stats.jsonl` to either the script or the EXE to append one JSON line per second with the audio state, device, FPS, meters, cue counts, tray and hotkey status. That is how the build was verified.

## How it works

- **Capture**: `pyaudiowpatch` opens the WASAPI loopback endpoint of the default playback device (float32, the device's own sample rate and channel count; 4/6/8-channel devices keep every channel for the full-circle mode, anything else uses the first two). A watchdog polls the default endpoint id through `IMMDeviceEnumerator` once a second and reopens the stream on change, and retries with back-off if the stream dies. "Capture only the game" uses `ActivateAudioInterfaceAsync` on the `VAD\Process_Loopback` virtual device with the target process id (raw COM through ctypes, including the completion-handler object), then `IAudioClient` / `IAudioCaptureClient` in event-driven shared loopback mode.
- **Analysis**: the same maths as the web version's `analyse()`. The last 2048 samples per channel go through a Blackman window and an FFT (numpy), magnitudes are smoothed like an `AnalyserNode` (0.4), ten log-spaced bands between 70 Hz and 9 kHz each get a level (-72 to -22 dB mapped to 0..1) and an angle from the left/right level difference in dB, scaled by the Stereo width setting (the web version inverts a constant-power pan law instead, which reads too central with game audio). On top of that: optionally half of a slow per-band background is subtracted (fade steady sounds), the gate and radius boost apply, each band's angle and level are low-pass filtered (smoothing), only the loudest N bands become cues, and the dominant band (white arrow, Comet trail, Single Line pointer) only changes when another band is at least 15 % louder.
- **Demo scene**: generated in numpy at 48 kHz and pushed into the same analysis window, so it exercises exactly the live path. Noise beds are shaped once with an FFT filter; the engine tone is a band-limited harmonic sum; panning uses the constant-power law like `StereoPannerNode`.
- **Rendering**: pycairo draws the frame into a 32-bit DIB section that is pushed to the screen with `UpdateLayeredWindow` (per-pixel alpha; the Opacity setting is the blend function's constant alpha). The window is a frameless `WS_POPUP` with `WS_EX_LAYERED | WS_EX_TOPMOST`; Overlay adds `WS_EX_TRANSPARENT` and returns `HTTRANSPARENT` from hit-testing, so every click goes to the window underneath. Tray icon via `Shell_NotifyIcon`, global hotkeys via `RegisterHotKey`, start-with-Windows via the HKCU `Run` key. Everything is ctypes; no pywin32 or GUI toolkit.
- **Frame pacing**: a message loop with `MsgWaitForMultipleObjects` targets 60 FPS (about 1 ms accuracy via `timeBeginPeriod`); a `WM_TIMER` keeps the radar moving while Windows runs a modal loop (drag, resize, menu).

## Troubleshooting

- **"Retrying: ..." in the HUD**: no WASAPI playback device could be opened. Check that a playback device is enabled in Windows sound settings; the app keeps retrying every few seconds. Details are in `errorlog.txt`.
- **Hotkeys do nothing**: the plain keys need the panel to have keyboard focus; the Ctrl+Shift ones work anywhere as long as "Global hotkeys" is ticked in the menu.
- **A Ctrl+Shift hotkey does nothing**: another app already registered it; `errorlog.txt` lists which ones were taken.
- **Antivirus flags the EXE**: PyInstaller one-file executables are sometimes flagged by heuristics. Build it yourself from the source in this folder if you prefer.
