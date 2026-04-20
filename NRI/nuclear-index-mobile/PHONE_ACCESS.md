# Getting the Nuclear Renaissance Index on your phone

Why it looks blank when you AirDrop/email the file: iOS's "preview" mode for
HTML attachments doesn't run JavaScript, and almost everything on this page
— charts, tables, filters — is rendered by JS. You need the file to open in
a **real browser** (Safari / Chrome / Firefox), not a preview pane.

Two reliable paths, ranked by ease:

## Path A (recommended): host it + Add to Home Screen

This gives you a tappable app icon and the site loads in ~1 second.

1. **Upload the HTML to a free static host.** Any of these work with no
   signup and no credit card:
   - [tiiny.host](https://tiiny.host/) — drag `Nuclear-Renaissance-Index.html`
     onto the page, pick a subdomain, done. Free tier is fine.
   - [Netlify Drop](https://app.netlify.com/drop) — drag the HTML, get a URL.
     (Requires a free account now, but signup is ~30 seconds with GitHub.)
   - [surge.sh](https://surge.sh/) — one command (`npx surge`), no account.
2. **Open the URL in Safari** on your iPhone (or Chrome on Android).
3. **Add to Home Screen:**
   - iOS: tap the Share button → scroll down → "Add to Home Screen" → Add.
   - Android Chrome: tap ⋮ menu → "Add to Home screen".

You'll get an icon (green atom on dark background, labeled "NRI") that opens
the site full-screen with no browser chrome — basically an app.

## Path B: save the file locally on your phone

Works but has edge cases. The `refresh.command` / `refresh.bat` workflow
doesn't apply — you can only refresh by re-downloading.

**iOS:**
1. On your computer, run `refresh.command` to get fresh baked prices.
2. AirDrop `Nuclear-Renaissance-Index.html` to your phone.
3. On the phone, tap **"Save to Files"** (important — don't tap "Open").
4. Open the **Files** app → find the HTML → **long-press → "Share" → "Save
   to Files"** into iCloud Drive (makes it sync).
5. In the Files app, tap the HTML to open. iOS will open it in a preview
   that *usually* runs JS. If it looks blank, tap the three-dot menu →
   "Open in Safari".
6. In Safari, use "Add to Home Screen" as in Path A.

**Android:**
1. Transfer the HTML to your phone (email to yourself, Google Drive, USB).
2. Open a file manager, tap the HTML → "Open with" → Chrome.
3. Add to Home Screen.

## Why Path A is worth the 2-minute setup

- **The page knows to be offline-friendly**: Chart.js is inlined into the
  HTML, so once loaded it runs without network.
- **Refresh workflow still works**: re-upload the new HTML to the same
  tiiny.host URL after running `refresh.command` and your phone will see
  fresh prices next time you open the home-screen icon.
- **No 1MB file transfers every time** you want to check prices.

## If you want truly zero-setup: self-host via iCloud Drive

1. Drop `Nuclear-Renaissance-Index.html` into `~/iCloud Drive/NRI/` on Mac.
2. On iPhone, open Files → iCloud Drive → NRI → tap the HTML.
3. It opens in the Files preview. If blank, use the Share button → "Copy
   to Safari" or "Open in Safari".

The `refresh.command` script on your Mac updates the file in-place, iCloud
syncs it to the phone in seconds, and you see fresh prices next open. No
hosting, no accounts. Downside: Files preview sometimes doesn't run JS —
you might have to "Open in Safari" every time.
