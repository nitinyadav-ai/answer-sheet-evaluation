# Deploying to Hugging Face (free, no credit card)

This runs the **whole app** as a **Docker Space** on Hugging Face — the only genuinely free,
card-free *hosted* option that has enough memory (16 GB) to run this pipeline.

> ### Read this first
> A **free** Space has **temporary storage**. Uploaded sheets and generated reports are **wiped**
> whenever the Space restarts, rebuilds, or goes to sleep. Treat it as **"grade → download the PDF
> right away"**, not as a place that keeps your data. See **[Disadvantages](#disadvantages)** at the
> bottom before you rely on it.

---

## What's already in the repo for this

| File | Purpose |
|------|---------|
| `Dockerfile` | Builds the image (Tesseract + OpenCV/PyMuPDF libs + Python deps), runs as HF's uid 1000, listens on port 7860. |
| `docker-entrypoint.sh` | Starts gunicorn. Auto-uses a persistent disk at `/data` **if** one exists, otherwise runs on the free ephemeral disk. |
| `requirements.txt` | Pinned Python dependencies. |
| `.dockerignore` | Keeps secrets (`.env`) and local junk out of the image. |
| `README.md` front-matter | The `sdk: docker` + `app_port: 7860` block that tells HF how to build and route. |

---

## Step 1 — Make a Hugging Face account (free, no card)

Go to <https://huggingface.co/join> and sign up with email + password (or Google/GitHub). No card is asked.

## Step 2 — Create the Space

1. Top-right **+** → **New Space** (or <https://huggingface.co/new-space>).
2. **Space name**: e.g. `answer-evaluator`.
3. **License**: your choice (e.g. `mit`).
4. **Select the Space SDK**: **Docker** → **Blank**.
5. **Hardware**: leave **CPU basic — FREE** (2 vCPU, 16 GB).
6. **Visibility**: **Private** ← important — you'll be uploading student data. (Private Spaces are free.)
7. **Create Space**. HF gives you an empty git repo at
   `https://huggingface.co/spaces/<username>/answer-evaluator`.

## Step 3 — Put the code in the Space (clean copy — no secret history)

We push a **fresh copy**, not this project's existing git history, so the old `.env` that once lived
in the history never reaches Hugging Face. In a terminal (replace `<username>`):

```bash
# 1. Clone the EMPTY Space repo into a new folder
cd ~
git clone https://huggingface.co/spaces/<username>/answer-evaluator hf-space
cd hf-space

# 2. Copy the project files in (rsync skips secrets + heavy/local junk)
rsync -a --delete \
  --exclude '.git' --exclude '.env' --exclude '.env.*' \
  --exclude 'output' --exclude 'evaluation_app/uploads' \
  --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.DS_Store' \
  "/Users/nidhishchettri/Desktop/Answer_Evaluator_OpenClaw Test OpenSource/"  ./

# 3. Commit and push
git add .
git commit -m "Deploy AI Answer Evaluator"
git push
```

> When git asks for a password, paste a Hugging Face **Access Token**, not your account password:
> **Settings → Access Tokens → New token**, role **Write**.

The moment you push, HF starts **building** the Docker image. Open the Space's **Logs** tab to watch —
the first build takes a few minutes (it installs Tesseract + the Python deps).

## Step 4 — Add your keys as Space secrets

The app reads everything from environment variables — no `.env` file is needed in the container.
In the Space → **Settings** → **Variables and secrets**:

**Secret** (hidden — click *New secret*):

- `LLM_API_KEY` — your OpenRouter key.

**Variables** (plain — click *New variable*). Copy each **value** from your local `.env`:

- `LLM_BASE_URL`  (e.g. `https://openrouter.ai/api/v1`)
- `LLM_JSON_MODE`
- `OCR_MODEL`, `SEPARATOR_MODEL`, `KEY_PARSER_MODEL`
- `EVAL_MODEL`, `EVAL_REASONING_EFFORT`, `EVAL_MAX_TOKENS`
- `DIAGRAM_EVAL_MODEL`, `DIAGRAM_EVAL_REASONING_EFFORT`, `DIAGRAM_EVAL_MAX_TOKENS`
- `DIAGRAM_FEATURES_MODEL`, `MAX_DIAGRAM_PAGES_PER_Q`, `OCR_VERIFY_CODE`

> The `DB_*` variables are **not needed** — the app no longer requires a database to run.
> After adding or changing secrets, use **Settings → Factory reboot** so they load.

## Step 5 — Open it

The Space's **App** tab shows the running web UI at
`https://<username>-answer-evaluator.hf.space` (visible only to you while it's Private).
Upload a question paper, answer key, and a student sheet exactly like you do locally — then
**download each report immediately** (see the storage warning above).

## Updating the app later

Re-run the `rsync` + `git add/commit/push` from Step 3 in your `hf-space` folder. Every push
triggers a fresh rebuild.

---

## Making data survive restarts (optional, needs a card)

If you outgrow the "download immediately" workflow, add HF **persistent storage**
(Space → **Settings → Persistent storage**, a few dollars/month for 20 GB, mounted at `/data`).
`docker-entrypoint.sh` auto-detects `/data` and starts saving reports there permanently — no code
change. This upgrade **does** require a payment method.

---

## Disadvantages

Honest trade-offs of hosting this on a free Hugging Face Space:

1. **Storage is temporary.** The single biggest one. On the free tier the filesystem resets on every
   restart, rebuild, or sleep — uploaded sheets and finished reports disappear. Download reports the
   moment they're generated. (Fixable only with paid persistent storage.)
2. **It sleeps when idle.** A free Space pauses after ~48 h with no visitors and cold-starts on the
   next visit — and waking it counts as a restart, so it also wipes the temporary storage. It is not a
   guaranteed always-on server.
3. **You're uploading student data to a third-party cloud.** Even in a Private Space, student answer
   sheets (personal data) leave your machine and sit on Hugging Face's US servers. Check this is
   acceptable for your school before uploading real student work.
4. **Shared, CPU-only hardware.** Free = 2 shared vCPUs. Grading is already minutes per sheet; a large
   batch will be slow, and heavy moments may be throttled. (You don't need a GPU — the AI runs on
   OpenRouter — but the image/OCR orchestration is CPU-bound.)
5. **Your OpenRouter key lives on a third party.** You paste it into HF's secret store, so you're
   trusting HF with it. Mitigate by setting a spend limit on OpenRouter and rotating the key if needed.
6. **Rebuilds take a few minutes.** Every code change reinstalls system + Python deps before the Space
   comes back.
7. **It's not a "real" server.** No persistent shell, fixed resource caps, and the platform is built
   for ML demos rather than general web apps — fine for this, but support/limits reflect that.

**If these become dealbreakers** (especially the storage + privacy points), the alternative is a small
always-on machine you control — your own computer left running behind a Cloudflare tunnel, or a cheap
cloud VM — where storage is permanent and the data stays where you put it.
