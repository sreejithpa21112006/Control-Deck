# Control Deck

Control Deck turns a phone into a wireless remote for a Windows laptop or desktop. A single Python script runs on the computer and serves a black-and-white touch interface over the local network. The phone opens it in a browser, or in the optional Android app, and gets a trackpad, joystick, media and volume controls, customizable shortcut buttons, quick-launch tiles and power controls.

It is built for an old phone left on a desk or couch: no accounts, no cloud service, no internet connection required. All traffic stays on your own network.

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Running the server](#running-the-server)
- [Connecting the phone](#connecting-the-phone)
- [Android app](#android-app)
- [Starting automatically with Windows](#starting-automatically-with-windows)
- [Using the deck](#using-the-deck)
- [Settings reference](#settings-reference)
- [Configuration and files](#configuration-and-files)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Platform support](#platform-support)

## Features

**Controls**
- Rotary volume knob. Drag around the dial to set the laptop volume from 0 to 100. Tap the centre to play or pause.
- Previous and next track buttons, a mute button, and three volume presets (60, 80 and Max by default, editable in Settings).
- Now-playing card showing the current title and artist, with an animated indicator while audio plays.
- Sound output switcher. Choose which playback device the laptop uses (for example wired headphones or a Bluetooth speaker) from a drop-down. Microphones and hands-free devices are not listed.
- The knob follows the real laptop volume and mute state, so changes made on the laptop appear on the phone.

**Shortcuts**
- Layouts of custom buttons. Each layout holds up to 6 pages of 9 buttons, and you can keep up to 8 layouts.
- A button can send a keyboard shortcut, open a website, or run a command.
- Layouts can be linked to programs. When a linked program is in the foreground on the laptop, its layout appears on the phone automatically (for example a layout for `code.exe`).
- Editing requires the edit PIN.

**Trackpad**
- Touchpad mode: one finger moves the cursor, a tap clicks, two fingers scroll, and a three-finger swipe triggers window gestures (Task View, show desktop, switch window).
- Joystick mode: a floating joystick that appears wherever your thumb lands. Push to move the cursor, with speed increasing the further you push. Size is adjustable.
- Remote mode: a plus-shaped arrow pad with video controls (see below).
- Left and right click buttons, a scroll strip, a Hold button for drag operations and a Double button for double-clicks.
- Optional phone keyboard that opens automatically when a text box is focused on the laptop.

**Video remote (Remote mode)**
- Skip back 10 seconds and forward 10 seconds, play or pause, mute, captions, full screen, slower and faster playback, in addition to the arrow keys, Escape, Tab, Back and Space.
- Holding the skip and Back buttons repeats them.
- These send standard video player keys, so they work on YouTube. Other players differ in what they support (see [Using the deck](#using-the-deck)).

**More**
- Quick launch: up to 8 tiles that open a website, program, folder or file on the laptop with one tap.
- Laptop power controls: lock, switch the screen off, sleep, restart and shut down. Sleep, restart and shut down ask for confirmation, and restart and shut down include a 15-second countdown with a Stop button.

**Android app (optional)**
- A full-screen wrapper with no browser bars, which keeps the screen on and remembers the laptop address.
- Phone volume buttons control the laptop volume while the app is open.
- Lock-screen and notification media controls (previous, play or pause, next) for the laptop's playback.

**Battery and performance**
- The server does almost no work when no phone is connected.
- The busiest background checks (active window and text-box detection) pause automatically when the phone screen is off or the page is hidden.

## How it works

The server is a single file, `server.py`, built on Flask. It serves the web interface and keeps a WebSocket open to the phone for low-latency pointer, keyboard and status messages. On Windows it uses the operating system's input, audio and media APIs. The interface is plain HTML, CSS and JavaScript embedded in the script, so there is no build step and no external assets.

The phone and the computer must be on the same local network. The server listens on port 5000 on all network interfaces.

## Requirements

- Windows 10 or 11 for the complete feature set. The server starts on macOS and Linux, but several features are Windows-only (see [Platform support](#platform-support)).
- Python 3.9 or newer.
- A phone with a modern browser (Chrome is recommended) on the same Wi-Fi network as the computer.
- To build the Android app: a free GitHub account, or Android Studio.

## Installation

1. Download or clone the repository.

   ```
   git clone https://github.com/sreejithpa21112006/Control-Deck.git
   cd Control-Deck
   ```

2. Install the dependencies.

   ```
   pip install -r requirements.txt
   ```

   The requirements file installs the core packages on every system, plus the Windows-only packages automatically on Windows.

   | Package | Purpose | Required |
   |---|---|---|
   | flask | Web server | Yes |
   | flask-sock | WebSocket link to the phone (trackpad, keyboard, live status) | Yes |
   | pyautogui | Mouse and keyboard control | Yes |
   | uiautomation | Detects text boxes so the phone keyboard can open automatically | Windows, optional |
   | pycaw, comtypes | Volume knob state and sound output switching | Windows, optional |
   | winrt-* packages | Now-playing information from any media player | Windows, optional |

   Without the optional packages the server still runs, and prints a note at startup naming each feature that is switched off and the command that enables it.

## Running the server

```
python server.py
```

The console prints two things you need:

```
Control Deck running. Open on your phone:  http://192.168.1.20:5000
Edit PIN for custom buttons and typing: 123456
```

The address is the computer's address on your network. The PIN is a six-digit code generated on first run and stored in `deck_config.json`.

The first time Windows runs the server, it may show a firewall prompt. Allow access on private networks, otherwise the phone cannot connect.

## Connecting the phone

1. Make sure the phone is on the same Wi-Fi network as the computer (not a guest network).
2. Open the printed address in the phone's browser.
3. Enter the PIN when asked. The phone remembers it.

If the phone only supports the 2.4 GHz Wi-Fi band, that is sufficient. The deck sends very small messages. On a dual-band router the 2.4 GHz and 5 GHz bands are normally on the same network, so the two devices can still reach each other.

### Full-screen use without the Android app

Chrome shows its address bar for plain `http` sites. To install the deck as a full-screen web app without building the Android app:

1. On the phone, open `chrome://flags` in Chrome.
2. Search for "Insecure origins treated as secure" and enable it.
3. In its text box enter the server address, for example `http://192.168.1.20:5000`.
4. Tap Relaunch, open the address again, then use the Chrome menu and choose Install app.

The Android app described next is the more reliable option.

## Android app

The `app` folder contains a small Android project that wraps the deck in a full-screen WebView. It requires Android 7.0 (API 24) or newer. The app is not on the Play Store; you build it yourself. There are two ways.

### Option 1: build with GitHub Actions (no installation required)

1. Fork this repository, or push a copy of it to your own GitHub account.
2. Open the Actions tab. The "Build app" workflow runs on every push. You can also start it manually with "Run workflow".
3. When the run finishes, open it and download the `ControlDeck-app` artifact. Unzip it to get `app-debug.apk`.

### Option 2: build with Android Studio

1. Open the repository folder in Android Studio and let Gradle sync.
2. Choose Build, then Build Bundle(s) / APK(s), then Build APK(s).

### Installing the APK

1. Copy `app-debug.apk` to the phone.
2. Open the file. If Android blocks the install, allow installs from that source when prompted.
3. If Play Protect shows a warning, choose Install anyway. The APK is debug-signed and built from the code in this repository.
4. On first launch, enter the address the server printed (for example `192.168.1.20:5000`). The app remembers it. To change it later, use Settings, then Laptop address.
5. On Android 13 or newer, allow notifications when asked. This is only needed for the lock-screen media controls.

### App behaviour

- The phone volume buttons change the laptop volume in 2 percent steps while the app is open. This can be switched off in Settings.
- The lock-screen controls show the laptop's current title and artist with previous, play or pause, and next buttons. This can be switched off in Settings.
- The back gesture is disabled on purpose. Use the home button to leave the app.
- Changes to the web interface or `server.py` do not require rebuilding the app. Only changes inside the `app` folder do.

## Starting automatically with Windows

Run once, from the folder that contains `server.py`:

```
python server.py --install-startup
```

This creates `ControlDeck.vbs` in your Startup folder. From the next sign-in, the server starts in the background with no console window and no taskbar entry, using `pythonw.exe`. Output is written to `deck.log` next to `server.py`.

| Task | How |
|---|---|
| Remove automatic start | `python server.py --remove-startup` |
| Stop the server now | End `pythonw.exe` in Task Manager |
| Start it now without restarting | Double-click `ControlDeck.vbs` in the Startup folder (open it with Win+R, then `shell:startup`) |
| After moving the project folder | Run `--install-startup` again from the new location |

If you previously used a batch file or shortcut to start the server, remove it so that two copies do not compete for port 5000.

## Using the deck

The deck has five pages. Swipe between them or use the tabs at the bottom.

### Controls

Turn the knob to set the volume. Tap the centre to play or pause. The buttons beside the knob skip tracks. Below it are Mute and the three volume presets. The sound output drop-down at the bottom switches the laptop's playback device. The now-playing card appears when a supported player is active.

### Shortcuts

Shows the buttons of the current layout. Tap a button to run it. Tap the layout name to switch layouts, or open Settings, then Layouts, then Manage to add, edit or delete them. Tap an empty slot to create a button, or hold an existing button to edit or delete it. Saving needs the PIN. Each button is one of:

| Type | Example | Result |
|---|---|---|
| Keyboard shortcut | `ctrl+shift+t` | Sends the key combination |
| Website | `https://example.com` | Opens it in the default browser |
| Command | `calc` | Runs the command on the computer |

To tie a layout to programs, list their executable names (for example `code.exe`) in the layout settings. The layout then appears automatically while one of those programs is in the foreground. This requires Windows.

### Trackpad

Three modes, switched with the control at the top of the page:

- Touchpad: move, tap to click, two-finger scroll, three-finger swipes for Task View (up), show desktop (down) and switching windows (left and right).
- Joystick: touch anywhere in the zone and push in a direction. The cursor speed follows how far you push. The click buttons sit alongside.
- Remote: the arrow pad and video controls.

The Hold button keeps the left button pressed so that you can drag and then release. The Double button sends a double-click. Typing opens the phone keyboard to send text to the laptop; this needs the PIN.

### Remote mode and video keys

| Button | Key sent | Effect on YouTube |
|---|---|---|
| -10s | J | Back 10 seconds |
| +10s | L | Forward 10 seconds |
| Play | K | Play or pause |
| Mute | M | Mute |
| Slower, Faster | Shift+, and Shift+. | Playback speed down or up |
| CC | C | Captions |
| Full | F | Full screen |
| Esc, Tab, Back, Space | Same keys | Standard behaviour |

The arrow pad sends the arrow keys. On YouTube, left and right seek 5 seconds, and up and down change the player volume. Other services differ: for example Netflix and Prime Video seek 10 seconds with the arrow keys. Keys go to whichever window is active on the laptop, so the video must be in front. Not every player supports every key.

### More

Quick launch tiles: tap "Add tile", enter a name (up to 14 characters) and a website or program, and save with the PIN. A website can be written as `youtube.com`. A program can be a name such as `chrome`, `notepad` or `code`, or a full path to a program, file or folder. Hold a tile to edit or delete it.

Laptop controls: Lock and Screen off act immediately. Sleep, Restart and Shut down ask for confirmation. Restart and Shut down start a 15-second countdown that you can stop. Sleep and Shut down disconnect the phone; wake the computer with its power button. Screen off ends when you move the mouse.

## Settings reference

| Setting | Description |
|---|---|
| Cursor speed | Pointer speed for touchpad and joystick |
| Joystick size | Small, medium or large touch zone |
| Natural scrolling | Content follows your fingers, or the reverse |
| Open in text boxes | Opens the phone keyboard when a text box is focused on the laptop (needs `uiautomation`) |
| Layouts | Which shortcut layout is shown, and its auto-switching |
| Laptop address (app only) | Change the server address stored in the app |
| Phone volume buttons (app only) | Use the phone volume buttons for laptop volume |
| Lock-screen controls (app only) | Media controls on the lock screen and notifications |
| Full screen, Full screen on first tap | Browser full-screen behaviour |
| Keep screen on | Stops the phone from sleeping while the page is open |
| Volume presets | The three values under the knob, from 0 to 100 (100 is shown as Max) |
| Edit PIN | The PIN stored on this phone |

## Configuration and files

| File | Purpose |
|---|---|
| `deck_config.json` | Created next to `server.py`. Stores the PIN, layouts, buttons and quick-launch tiles. Do not publish it. |
| `deck.log` | Created when the server runs without a console window. |

To reset the PIN, stop the server, delete the `pin` entry from `deck_config.json` (or the whole file to reset everything), and start the server again. A new PIN is printed.

## Security

Control Deck gives whoever can use it control of the computer. Read this section before using it on any shared network.

- Use it only on networks you trust, such as your home network. The server uses plain HTTP and the connection is not encrypted.
- The server listens on all network interfaces on port 5000. Do not expose that port to the internet, and do not forward it on your router.
- The six-digit PIN is required for the live trackpad, keyboard, typing, sound output switching, quick launch, power controls and all editing. After five wrong attempts the connection is closed.
- The media buttons and the saved shortcut buttons do not require the PIN to run. Anyone on the same network who can reach the server could trigger media keys or run buttons you have already saved. Do not save a button that runs a command you would not want triggered by someone else.
- Custom command buttons and program tiles run with the same permissions as the user who started the server.
- Keep `deck_config.json` private. It contains the PIN.

If you need stronger protection, run the server only when you need it (and not at start-up), keep it on a separate trusted network, or place it behind a VPN.

## Troubleshooting

**The phone cannot open the address.** Check that both devices are on the same network and not a guest network, that the server is running, and that Windows Firewall allows Python on private networks. Try opening the address in a browser on the computer itself first.

**The address changed.** Home routers can give the computer a new address. Reserve a fixed address for the computer in the router settings, then use that address in the browser or app.

**The trackpad, keyboard or now-playing card does nothing.** Install `flask-sock` and restart. The console prints a note for each feature that is switched off.

**The cursor stutters.** Move the phone closer to the router. On crowded 2.4 GHz networks, set the router's channel to 1, 6 or 11.

**The sound output list is empty or missing.** Install `pycaw` and `comtypes`, then restart. Only devices that are currently active appear.

**Now playing only shows some players.** Install the `winrt-*` packages listed in `requirements.txt`. Without them, only a few players are detected.

**Phone volume buttons or lock-screen controls do nothing.** Make sure you installed the latest APK and that the matching settings are on. On Android 13 or newer, notifications must be allowed for the app.

**Two copies of the server start.** Remove any older start-up batch file or shortcut, then run `python server.py --install-startup` once.

**The build fails on GitHub.** Open the failed run, copy the red error text, and open an issue.

## Project layout

```
server.py            The server and the complete web interface
requirements.txt     Python dependencies
app/                 Android app source (Java, no external libraries)
build.gradle         Android build configuration
settings.gradle
gradle.properties
.github/workflows/   GitHub Actions workflow that builds the APK
```

## Platform support

| Feature | Windows | macOS and Linux |
|---|---|---|
| Trackpad, joystick, remote, typing | Yes | Yes (through pyautogui) |
| Media keys and shortcuts | Yes | Yes |
| Volume knob state and sound output switching | Yes | No |
| Now-playing information | Yes | No |
| Layouts that switch by program | Yes | No |
| Phone keyboard on text-box focus | Yes | No |
| Lock, sleep, screen off, restart, shut down | Yes | No |
| Start with the computer, no window | Yes | No |
