# Brand mark

The dashboard ships with no proprietary logo. Every screen renders
`app/icon.svg` — the project's own shield mark, which is always present.

To use the real Smart Shield logo instead:

1. Put the file in this folder, e.g. `public/branding/smart-shield-logo.png`.
2. Point the app at it, in `.env` or the environment of whatever runs
   `next build`:

   ```
   NEXT_PUBLIC_BRAND_LOGO_URL=/branding/smart-shield-logo.png
   ```

3. Rebuild. `NEXT_PUBLIC_*` values are inlined at build time, so a restart
   alone will not pick this up.

Any URL works, not only a file in this folder.

## Why it is a variable and not just a filename

`components/BrandLogo.tsx` used to request `/branding/smart-shield-logo.png`
unconditionally and fall back to the shield when that 404'd. The fallback
worked, so the logo looked right — but every page load in every browser
still fetched a file that was not there, and logged a console error for it.
A 404 that always happens is indistinguishable, in a log, from a 404 that
means something.

The fallback is still in place: if the URL above is set but the file is
missing or broken, the shield is shown rather than a broken image.
