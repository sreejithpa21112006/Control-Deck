# Control Deck app

A small Android app that shows your Control Deck full screen, like a normal app.
No browser, no address bar, the screen stays on, and it remembers your laptop address.

It needs the new server.py (the one that came with this file) running on the laptop.

## Build the app without installing anything (GitHub does it for you)

1. Sign in at github.com (a free account is fine).
2. Click the + at the top right, then New repository. Name it controldeck-app, choose Private, click Create repository.
3. On the next page click "uploading an existing file".
4. Unzip this folder. Open it, select everything inside (app, .github, build.gradle, gradle.properties, settings.gradle) and drag it into the GitHub page.
   Check that the file list shows .github/workflows/build.yml. Some browsers skip folders that start with a dot.
   If it is missing: click Add file, then Create new file, type .github/workflows/build.yml as the name (the slashes make the folders), paste the contents of that file from this folder, and commit.
5. Click Commit changes.
6. Open the Actions tab. A run called "Build app" starts by itself. Wait 3 to 6 minutes for a green tick.
7. Click the finished run. At the bottom, under Artifacts, click ControlDeck-app. A zip downloads. Unzip it to get app-debug.apk.

## Install on the phone

1. Send app-debug.apk to the phone (USB, Google Drive, Telegram, email, anything).
2. Tap the file. If Android says it blocks installs from this source, tap Settings and allow it for that one app (Files, Chrome, Drive, whichever you used).
3. If Play Protect warns about an unknown app, tap Install anyway. The app is built from the code in this folder and is not on the Play Store.
4. Open Control Deck.

## First start

1. The app asks for the laptop address. Type what server.py prints, for example 192.168.29.168:5000 and tap Connect.
2. The deck opens full screen. The address is remembered.
3. To change the address later: Settings page of the deck, Laptop address, Change.

The first time, the deck asks for the PIN printed in the laptop window, as before.

## Notes

- The phone and laptop must be on the same Wi-Fi, and server.py must be running.
- If the laptop's address changes, the app shows the address screen again so you can type the new one.
- Back gesture does nothing on purpose. Use the home button to leave.
- If the build in step 6 fails (red cross), open the run, copy the red error text and send it back.

## Or build with Android Studio

Open this folder in Android Studio, let it sync, then Build > Build Bundle(s) / APK(s) > Build APK(s).
