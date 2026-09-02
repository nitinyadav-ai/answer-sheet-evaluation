# Deploying to Render (Docker + persistent disk + custom domain)

This app is a **stateful, long-running job processor**, not a request/response web app: a single
sheet takes minutes to grade, a batch takes hours, and every run writes page images, crops and JSON
under `output/<run_id>/`. That rules out serverless platforms (Vercel, Netlify Functions, Lambda) —
see [Why not Vercel](#appendix-a--why-not-vercel). Render runs the container as a normal long-lived
process with a real disk, which is what the pipeline needs.

`Dockerfile` and `docker-entrypoint.sh` already support this: the entrypoint detects a writable
`$DATA_DIR` and symlinks `output/`, `evaluation_app/uploads/` and the reports folders onto it, so
data survives restarts with **no code change**.

---

## 0. Pre-flight code changes (required — do these first)

Four changes. The first two are hard blockers; the rest matter because the app will be **public**.

### 0.1 Add a health endpoint that bypasses the password gate — BLOCKER

Render health-checks the service over HTTP. `_require_password_when_public` is a `before_request`
hook that guards **every** route, so an authenticated-only app answers Render's probe with `401`,
Render calls the deploy unhealthy, and you get a restart loop that looks like a crash.

In `evaluation_app/app.py`, inside `_require_password_when_public`, before the password check:

```python
    # Render/uptime probes must not be password-gated, or the platform marks the service unhealthy
    # and restarts it forever. /healthz exposes no data.
    if request.path == "/healthz":
        return
```

and add the route:

```python
@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200
```

### 0.2 Run ONE web worker — BLOCKER

`_REGRADE_JOBS = {}` (app.py:912) is a per-process dict. `/re-evaluate-question` stores a `job_id`
in it and the browser polls `/re-evaluate-status/<job_id>`. `docker-entrypoint.sh` starts gunicorn
with `--workers 2`, so there are **two dicts**; a poll routed to the other worker reports the job
missing. Locally you run `python app.py` (Flask dev server, single process), which is why this has
never bitten you.

Fix by configuration, not code — set in Render's env vars:

```
WEB_WORKERS=1
WEB_THREADS=8
```

The worker class is already `gthread`, so 8 threads still handle concurrent requests; you lose
nothing but the second process. (The alternative — moving job state to Redis or to disk — is more
work for no benefit at this scale. `_REGRADE_REVIEW_LOCKS` at app.py:914 has the same constraint.)

### 0.3 Move the Flask secret key out of source

`app.py:45` hardcodes `app.secret_key = "secret_key_for_session"`. It is committed, so anyone with
repo access can forge session cookies on a public deployment.

```python
app.secret_key = os.environ.get("APP_SECRET_KEY") or "secret_key_for_session"
```

Generate a value and set `APP_SECRET_KEY` in Render:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 0.4 Cap upload size

There is no `MAX_CONTENT_LENGTH`, so a public URL accepts an upload of any size until the paid disk
fills. After `app = Flask(__name__)`:

```python
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "64")) * 1024 * 1024
```

**Also consider:** `CORS(app)` (app.py:46) allows every origin. With Basic Auth in front the
practical risk is low, but for a public host restrict it to your own domain:
`CORS(app, origins=[os.environ.get("PUBLIC_ORIGIN", "*")])`.

---

## 1. Pick an instance size

Sizing is driven by RAM, not CPU. Each gunicorn worker loads OpenCV (**102 MB**) plus numpy
(**30 MB**), and page rasterisation/cropping holds full-resolution images in memory.

| Render plan | RAM | Verdict |
|---|---|---|
| Starter ($7/mo) | 512 MB | **Too small.** OOM during OCR/cropping. |
| **Standard (~$25/mo)** | 2 GB | **Minimum realistic choice.** 1 worker, `BATCH_SHEET_CONCURRENCY=1`. |
| Pro (~$85/mo) | 4 GB | Comfortable; allows `BATCH_SHEET_CONCURRENCY=2`. |

> Your `.env` sets `BATCH_SHEET_CONCURRENCY=3`, tuned for an M4 Mac with 16 GB. **Do not carry that
> value over** — on a 2 GB instance three parallel sheets will be OOM-killed mid-run. It buys little
> anyway: grading is capped by OpenRouter's ~370 tok/s aggregate throughput, not by local CPU.

Prices change — check Render's pricing page before committing.

---

## 2. Create the service

1. Sign in to <https://dashboard.render.com> → **New** → **Web Service**.
2. **Connect the repository.** Choose `SCS-Learning/AnswerSheetEvaluation`.
   Because it lives in an **organisation**, you must grant Render access to `SCS-Learning`, not just
   your personal account — on the GitHub authorisation screen pick the org and either grant all
   repositories or select this one. If the repo doesn't appear in Render's list, this is why.
3. Render detects the `Dockerfile` and selects the **Docker** runtime. Leave build/start commands
   empty — `ENTRYPOINT ["bash", "docker-entrypoint.sh"]` handles startup.
4. **Branch:** `main`. **Instance type:** Standard or larger (§1).
5. **Health Check Path:** `/healthz` (from §0.1).
6. Leave auto-deploy **on** so a push to `main` redeploys.

Do **not** click Deploy yet — add the disk and env vars first, or the first run will start without
storage and without an API key.

---

## 3. Attach the persistent disk

**Settings → Disks → Add Disk.**

| Field | Value |
|---|---|
| Name | `evaluator-data` |
| Mount path | `/data` |
| Size | **20 GB** to start |

The mount path must be exactly `/data`: `docker-entrypoint.sh` defaults `DATA_DIR=/data` and only
redirects storage when that path exists and is writable. Get it wrong and the app still boots, but
logs `no persistent storage — EPHEMERAL filesystem` and silently loses every report on restart.
**Check that log line on your first deploy.**

On sizing: your Mac currently holds **2.3 GB of `output/`** across ~20 runs — roughly 100 MB per
sheet including page images and crops. 20 GB is about 200 sheets. Render disks can be grown later
but never shrunk, so start modest.

---

## 4. Environment variables

**Settings → Environment**. Add these; mark `LLM_API_KEY` and `APP_AUTH_PASSWORD` as secret.

**Required**

| Key | Value | Notes |
|---|---|---|
| `LLM_API_KEY` | your OpenRouter key | **Secret.** Never commit it. |
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` | |
| `APP_AUTH_PASSWORD` | a long random password | **Secret.** Enables the gate; see §6. |
| `APP_AUTH_USERNAME` | e.g. `teacher` | Defaults to `teacher` if unset. |
| `APP_SECRET_KEY` | 64 hex chars (§0.3) | **Secret.** |
| `WEB_WORKERS` | `1` | **Required** — see §0.2. |
| `WEB_THREADS` | `8` | |
| `DATA_DIR` | `/data` | Matches the disk mount. |

**Models and tuning** — copy from your `.env`, but override the concurrency values:

| Key | Value |
|---|---|
| `OCR_MODEL` | `qwen/qwen3-vl-235b-a22b-instruct` |
| `EVAL_MODEL` | `qwen/qwen3-vl-235b-a22b-thinking` |
| `EVAL_REASONING_EFFORT` | `low` |
| `EVAL_MAX_TOKENS` | `12288` |
| `DIAGRAM_EVAL_MODEL` | `qwen/qwen3-vl-30b-a3b-thinking` |
| `EVAL_CASCADE` | `1` |
| `EVAL_GRADING_CALIBRATION` | `v2` |
| `BATCH_SHEET_CONCURRENCY` | **`1`** (not 3 — see §1) |
| `EVAL_MAX_CONCURRENCY` | `12` (down from 24) |
| `OCR_MAX_WORKERS` | `8` (down from 20) |
| `WEB_TIMEOUT` | `600` |

Do **not** set `DB_*` unless you actually use Postgres; the app runs without it.

> `.env` is gitignored and not in the image (`.dockerignore`), so Render's env vars are the only
> source of configuration. That is the correct arrangement — keep it that way.

---

## 5. Deploy and verify

Click **Create Web Service**. First build takes ~5–10 minutes (system packages + `pip install`).

Verify in this order — each step catches a different failure:

**1. Storage is persistent.** In **Logs**, confirm:
```
[entrypoint] persistent storage at /data — reports will survive restarts
```
If you instead see `no persistent storage`, the disk mount path is wrong (§3).

**2. Health.** `https://<service>.onrender.com/healthz` → `{"status": "ok"}` with no password prompt.

**3. Auth.** Open the root URL. You should get a browser password prompt, and a wrong password
should be refused.

**4. End-to-end.** Log in, upload a question paper, answer key and **one** answer sheet, and grade
it. This is the only test that exercises OCR, the LLM calls, cropping, PDF generation and the disk
together. Watch Logs for `[CALCULATION ERROR]`, OOM kills, or tracebacks.

**5. Persistence.** **Manual Deploy → Restart**, then confirm the run from step 4 is still listed
under previous evaluations. This is what proves the disk works — skip it and you may not discover
otherwise until you have real data to lose.

---

## 6. Custom domain

1. **Settings → Custom Domains → Add Custom Domain**, e.g. `evaluator.yourschool.com`.
2. Render shows a target host. At your DNS provider add:

   | Type | Name | Value |
   |---|---|---|
   | CNAME | `evaluator` | `<your-service>.onrender.com` |

   For an apex domain (`yourschool.com`) use an ALIAS/ANAME record, or the A records Render gives
   you — a plain CNAME at the apex is invalid DNS.
3. Wait for propagation (minutes to a few hours). Render issues a Let's Encrypt certificate
   automatically and redirects HTTP → HTTPS; no configuration needed.
4. Once live, set `PUBLIC_ORIGIN=https://evaluator.yourschool.com` if you applied the CORS
   restriction in §0.4.

---

## 7. Before you put student work on a public URL

This app processes **children's answer sheets** — names, roll numbers, handwriting. On a public
domain that deserves more than a shared password.

- **Basic Auth is a single shared credential.** No per-user accounts, no audit trail, no revocation
  short of changing the password for everyone. It is adequate for a handful of trusted teachers; it
  is not adequate for a wide rollout. `test123 / aigrader@123` from `.env.public` must **not** be
  reused here — generate something long and random.
- Basic Auth sends credentials on every request, so it is only safe over HTTPS. Render enforces
  HTTPS, so this is satisfied — but never expose the service over plain HTTP.
- **Answer keys are in the repository.** `evaluation_app/uploads/current_answer_key.json` and six
  sibling files are tracked and contain a real Class X Science key. Anyone with repo access can read
  them. Consider `git rm --cached` on that directory and adding it to `.gitignore` — they are
  regenerated on the next upload.
- **Retention.** Nothing deletes old runs; `output/` grows forever until the disk fills. Decide how
  long to keep student scans and prune deliberately.
- Check what your school or jurisdiction requires for storing student data with a third-party host
  and with an LLM provider (OpenRouter) — that is a policy question, not a technical one.

---

## 8. Operating notes

- **Cost.** Instance (~$25/mo Standard) + disk ($0.25/GB/mo ≈ $5 for 20 GB). **The OpenRouter API
  is billed separately and is the larger variable** — a full sheet costs roughly $0.30–0.50.
- **Do not enable "scale to zero"** for this service. Batch jobs run as in-process daemon threads;
  suspending the instance mid-batch orphans the work.
- **Never add `--max-requests` to gunicorn.** `docker-entrypoint.sh` already warns why: it would
  recycle a worker mid-job and orphan a running evaluation.
- **Deploys interrupt running jobs.** A push to `main` triggers a redeploy that replaces the
  container. Deploy when nothing is grading.
- **Logs are ephemeral.** Render retains a limited window; export anything you need for audit.
- The **launchd report-sync agent** and the **credit monitor** are macOS-only and do not move to
  Render. Reports land on the `/data` disk instead; download them or build a separate sync path.

---

## Appendix A — why not Vercel

Measured against this codebase, each of these alone is fatal:

| Constraint | Vercel | This project |
|---|---|---|
| Max execution | 60s Hobby / 300s Pro | One sheet ≈ minutes (a 56-answer replay took **424s**); batches run hours |
| Bundle size | 250 MB unzipped | `cv2` 102 MB + numpy 30 MB + PIL 11 MB + PyMuPDF, openai, fpdf2, PyPDF2, python-docx |
| Filesystem | Read-only except ephemeral `/tmp` | 43 write sites; `output/<run_id>/` holds images and crops |
| Process model | Stateless per invocation | `_REGRADE_JOBS` in memory; 10 threading uses; 5 subprocess calls; SIGKILL of process groups to cancel |
| System packages | none | `tesseract-ocr`, `libglib2.0-0`, `libgomp1`, DejaVu fonts |

The same reasoning rules out Netlify Functions and bare AWS Lambda. Any host that runs a **container
with a persistent volume** works: Render, Fly.io, Railway, or a plain VM (Oracle Cloud Always Free
is genuinely free and comfortably sized for this).
