# Publishing DocToPDF — from preview to a turnkey download

The packaged `.app` (built by `setup.py` / `.github/workflows/release.yml`) is an
**unsigned** build that expects the user to supply their own OAuth client. Two
things turn it into a true one-click, works-for-anyone download.

## 1. Code signing + notarization (so Gatekeeper opens it cleanly)

Requires an **Apple Developer account** ($99/yr). Without this, users must
right-click → Open (or `xattr -dr com.apple.quarantine DocToPDF.app`).

1. In the Apple Developer portal, create a **"Developer ID Application"**
   certificate and install it in your login keychain.
2. Sign the bundle with the hardened runtime:
   ```bash
   codesign --deep --force --options runtime --timestamp \
     --sign "Developer ID Application: <Your Name> (<TEAMID>)" dist/DocToPDF.app
   ```
3. Store notary credentials once, then submit:
   ```bash
   xcrun notarytool store-credentials doctopdf-notary \
     --apple-id "<you@example.com>" --team-id "<TEAMID>" --password "<app-specific-password>"
   ditto -c -k --keepParent dist/DocToPDF.app dist/DocToPDF.zip
   xcrun notarytool submit dist/DocToPDF.zip --keychain-profile doctopdf-notary --wait
   ```
4. Staple the ticket so it validates offline, then re-zip for distribution:
   ```bash
   xcrun stapler staple dist/DocToPDF.app
   ditto -c -k --keepParent dist/DocToPDF.app dist/DocToPDF-macos.zip
   ```

**To automate in CI:** add the certificate (`.p12`, base64-encoded) and the
notarytool credentials as GitHub Actions secrets, import the cert into a
temporary keychain, and insert the sign → notarize → staple steps before the
upload step in `.github/workflows/release.yml`.

## 2. Google OAuth verification (so anyone can sign in)

Today the OAuth app is in **Testing** mode: only added test users can authorize,
and the app needs a local `client_secret.json`. To open it to everyone:

1. **Ship an OAuth client with the app.** Desktop ("installed app") OAuth clients
   are not confidential by design, so bundling a `client_secret.json` inside the
   `.app` (e.g. `Contents/Resources/`) — or fetching it at first run — is
   acceptable for this flow.
2. In **Google Cloud Console → OAuth consent screen**, move from *Testing* to
   *In production* (**Publish app**).
3. `drive.readonly` is a **sensitive/restricted** scope, so Google requires
   **verification**: a privacy-policy URL, an app homepage (your Vercel site
   works), a demo video, and — for restricted scopes — possibly a third-party
   **CASA security assessment**. Until verified, users see an "unverified app"
   warning they must click through.

## Alternative: keep "bring your own client_secret.json"

If you don't want to embed credentials or go through verification, keep the
current model — the user creates their own OAuth client (the README walkthrough).
Great for a developer audience; not turnkey for non-technical users.
