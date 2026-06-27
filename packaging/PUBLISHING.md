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

## 2. Google OAuth — embed the client + verify (so anyone can sign in)

`drive.readonly` is a Google **restricted** scope. The app already ships an
embedded OAuth client (see "Embedding the client" below), so end users do zero
Google setup. The remaining work is publishing + verification. Key facts
(verified against Google docs, June 2026):

| Publishing status | Users | Friction | Google review |
| --- | --- | --- | --- |
| Testing | ≤100 (added by hand) | **re-auth every 7 days** | none |
| In production, **unverified** | ≤100 total (permanent cap) | one-time "unverified app" warning | none |
| In production, **verified** | unlimited | none | brand verification **+ CASA** |

### Embedding the client (already wired)
- `config._resolve_client_secret_path()` finds a `client_secret.json` bundled at
  `Contents/Resources/` of the `.app` (or one you drop in the app-support dir).
- `setup.py` bundles `client_secret.json` if it's present at build time.
- The release CI writes it from a repo secret. **Add it once:** GitHub →
  repo *Settings → Secrets and variables → Actions → New repository secret*,
  name **`GOOGLE_CLIENT_SECRET_JSON`**, value = the full contents of your Desktop
  OAuth client's `client_secret.json`. Then cut a tag — the build embeds it.
  (Desktop client secrets are non-confidential by Google's own definition, so
  shipping it in the app is expected.)

### Verification checklist (for unlimited public users)
1. **OAuth consent screen → Branding:** app name, logo, **app homepage**
   (`https://doctopdf-pi.vercel.app`), **privacy policy URL**
   (`https://doctopdf-pi.vercel.app/privacy` — already built), and **authorized
   domains**. ⚠️ Authorized domains must be ones you can verify ownership of in
   Search Console; a `*.vercel.app` subdomain generally won't qualify — you'll
   likely need a **custom domain** (point it at the Vercel project, then set
   `site.url` + the consent-screen URLs to it).
2. **Scopes:** add `drive.readonly`, and write the **limited-use / why-you-need-it
   justification** (this app exports + diffs the user's chosen Docs; least scope
   that works).
3. **Demo video:** an unlisted YouTube link showing the OAuth grant and the app
   actually using the restricted scope (watching + exporting a Doc).
4. **Publish app** (Testing → In production) and **submit for verification**.
   Brand verification takes ~2–3 business days.
5. **CASA security assessment** (required for restricted scopes accessible via a
   server, and as a general gate for restricted-scope production): Google charges
   nothing, but you engage and pay an App Defense Alliance assessor directly.
   Target assurance level **AL2**; on passing you get a Letter of Validation.
   **Must be renewed every 12 months.** No official price; third-party assessors
   publicly quote roughly **$500–$4,500/yr** depending on tier.

### Don't need unlimited users?
Two no-review options, both using the same embedded client:
- **Production, unverified** — up to 100 users total (permanent lifetime cap),
  one-time warning, **no weekly re-auth**. Best quick path.
- **Verification exemptions** — personal use (only you / a few people you know) and
  internal Workspace-org use are exempt from verification entirely.

### Or keep "bring your own client_secret.json"
Don't set `GOOGLE_CLIENT_SECRET_JSON`, and each user creates their own OAuth
client (the README walkthrough). No verification, no embedded secret — developer-
oriented, not turnkey for non-technical users.
