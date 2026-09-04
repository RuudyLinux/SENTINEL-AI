# Smart Shield logo goes here

Save the supplied **Smart Shield — Gujarat Police Innovation Challenge 2026**
logo image as:

```
frontend/public/branding/smart-shield-logo.png
```

(PNG or SVG both work — if you use SVG, also update the one `.png` reference
in `frontend/app/login/page.tsx` and `frontend/components/Sidebar.tsx` to
`.svg`.)

Nothing else needs to change — both the login page and the sidebar already
reference this exact path (`/branding/smart-shield-logo.png`) and will pick
it up automatically once the file exists. Until then, they fall back to the
project's existing shield mark (`app/icon.svg`) so nothing looks broken.

Keep the file's original aspect ratio — the containers around it use
`object-contain`, so the image is never stretched or cropped, only ever
scaled down to fit.
