# Permanent free link with Tailscale Funnel

Goal: a **permanent** public URL — `https://ai-answer-evaluator.<your-tailnet>.ts.net` — that stays the
same every time, free, no credit card. It's live whenever this Mac is on and `./run-public.sh` is running.

You do the one-time setup below **once**. After that, it's just `./run-public.sh` forever.

> Your login is already set (in `.env.public`): username **test123**, password **aigrader@123**.
> `.env.public` is gitignored, so the password never goes to GitHub.

---

## One-time setup (~5 minutes)

### 1. Install Tailscale
Pick either:
- **Download:** <https://tailscale.com/download/mac> → open the downloaded app, OR
- **Terminal:** `brew install --cask tailscale`  (it will ask for your Mac password)

On first launch, if macOS pops up about a **system/network extension**, approve it in
**System Settings → Privacy & Security**.

### 2. Log in (free, no card)
Click the **Tailscale icon in the menu bar** (top-right of your screen) → **Log in…** → sign up with
Google / GitHub / email. The **Personal** plan is free and needs no card.

### 3. Name this Mac `ai-answer-evaluator`
This name becomes the first part of your URL.
- Open the admin console: <https://login.tailscale.com/admin/machines>
- Find this Mac in the list → **⋯** menu → **Edit machine name…** → set it to `ai-answer-evaluator` → save.

*(CLI alternative: `/Applications/Tailscale.app/Contents/MacOS/Tailscale set --hostname=ai-answer-evaluator`)*

### 4. Turn on HTTPS
- Open <https://login.tailscale.com/admin/dns>
- Make sure **MagicDNS** is **ON**.
- Click **Enable HTTPS** and confirm. (Funnel needs this for the padlock/https certificate.)

### 5. Turn on Funnel
- Open <https://login.tailscale.com/admin/acls>
- Add this block inside the policy (top level of the JSON), then **Save**:
  ```json
  "nodeAttrs": [
    { "target": ["autogroup:member"], "attr": ["funnel"] }
  ]
  ```
  (If a `nodeAttrs` block already exists, just add the `{ "target": ["autogroup:member"], "attr": ["funnel"] }`
  entry to it.)

---

## Go live

```bash
cd "/Users/nidhishchettri/Desktop/Answer_Evaluator_OpenClaw Test OpenSource"
./run-public.sh
```

It prints your **permanent** link and your login:

```
  LOGIN for the public link:
     username: test123
     password: aigrader@123

  https://ai-answer-evaluator.<your-tailnet>.ts.net
```

Share **that URL + the login**. Press **Ctrl-C** in the Terminal to take it offline. The link is the
**same every time** — bookmark it. (The `<your-tailnet>` middle part is assigned by Tailscale; you can
see/rename it on the DNS admin page, but it never changes on its own.)

---

## Notes & troubleshooting

- **The Mac must be on, awake, and online, with `./run-public.sh` running** for the link to work. The
  script keeps the Mac awake automatically while it runs; keep it plugged in and don't shut it down.
- **"Tailscale isn't logged in"** when you run the script → finish steps 1–2 above.
- **First `funnel` run asks to confirm / says Funnel isn't enabled** → finish steps 4–5, then retry.
- **Change the login** anytime by editing `APP_AUTH_USERNAME` / `APP_AUTH_PASSWORD` in `.env.public`.
- **Switch back to a throwaway link** (no account) by setting `TUNNEL='cloudflare'` in `.env.public`.
- Funnel is free but exposes the app to the internet — that's exactly why the password gate is on.
  A stronger password than `aigrader@123` is a good idea if you share the link widely.
