# Trend tracker — GitHub Pages site

A single-page site (implemented from a Figma design) that explains what the
Trend Tracker agent does and how it works.

## Files
- `index.html` — the whole site (self-contained: HTML + CSS + inline SVG).
- `.nojekyll` — tells GitHub Pages to serve files as-is (no Jekyll processing).

## Deploy to GitHub Pages
1. Create a repo (or use an existing one) and push these files to it.
   - For a user/org site: repo named `<username>.github.io`, push to the default branch.
   - For a project site: any repo name works.
2. In the repo, go to **Settings → Pages**.
3. Under **Build and deployment → Source**, choose **Deploy from a branch**.
4. Select your branch (e.g. `main`) and the `/ (root)` folder, then **Save**.
5. Wait ~1 minute. Your page will be live at:
   - `https://<username>.github.io/` (user/org site), or
   - `https://<username>.github.io/<repo>/` (project site).

No build step is required — it's plain static HTML.

## Fonts
Fonts (Rethink Sans, Spectral, Fira Code) load from Google Fonts via a `<link>`
in `index.html`. They require an internet connection to render exactly as designed;
otherwise the page falls back to system serif/sans-serif fonts.
