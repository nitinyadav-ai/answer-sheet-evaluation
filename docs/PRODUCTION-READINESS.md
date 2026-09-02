# Production readiness for multi-school use

Assessment of the system against the goal of **multiple schools grading real exams concurrently, at
minimum cost**, under these constraints:

1. OpenRouter for all inference — no self-hosted or custom model deployments.
2. Do **not** store answer sheets, answer keys, question papers, or student PII.
3. Persist **only** student reports, class-wise reports and teacher remarks.
4. Teachers can view and edit reports online at any time — accept/reject marks, re-evaluate answers.
5. Verify OpenRouter / provider data retention; flag leakage risks and mitigations.
6. School-level data isolation (multi-tenancy).
7. Concurrent multi-school processing, scaled at minimum cost.

Every figure below is measured from this repo's own cost ledgers and run output, or read live from
the OpenRouter API. `file:line` references throughout so each claim can be checked.

**Headline.** The grading engine is sound. Three defects will corrupt results the moment a second
school runs concurrently (Part 1). Requirement 2 conflicts with requirements 3–4 and has to be
restated to be satisfiable (Part 2) — but once restated, it is achievable and **removes 84% of
stored bytes**. The binding scale constraint is that your grading model is served by only **two**
providers, one of them currently degraded; the fix for that is also the fix for cost (Part 5).

---

## Part 1 — Three blockers before a second school touches this

Each one silently corrupts real students' results.

### 1.1 Global singleton state — WRONG MARKS

```python
ANSWER_KEY_PATH = os.path.join(UPLOAD_FOLDER, "current_answer_key.json")          # app.py:83
QUESTION_PAPER_PATH = os.path.join(UPLOAD_FOLDER, "current_question_paper.json")  # app.py:86
```

One answer key for the entire application (seven such singletons exist). School A starts grading
Physics; ninety seconds later School B uploads their Biology key, overwriting the file; School A's
running evaluation grades Physics answers against Biology expectations — **silently**, with a
normal-looking PDF. Requirement 2 happens to fix this: keys become per-run and in-memory only
(Part 2).

### 1.2 `run_id` is the uploaded filename — DATA LOSS

```python
run_id = os.path.splitext(filename)[0]     # app.py:1573
run_id = Path(input_file).stem             # full_evaluator.py:1837
_reset_run_dir(output_base)                # full_evaluator.py:1843 — wipes the folder
```

Two schools uploading `Class10_Maths.pdf` share a `run_id`, and the second run **deletes the first
school's data**. Fix: `run_id = uuid4()`, scoped by `school_id`; the filename becomes a display
label only. Never derive identity from user-supplied text.

### 1.3 No tenancy — every school sees every other school

`tenant`, `school_id`, `org_id`, `user_id` and `account` return **nothing** across `app.py`.
`tester_id` is a free-text label in `review_state.json`, not an authorisation boundary. Today
`/previous-evaluations` lists every run from every school, any authenticated user can open any
`/student-report/<run_id>`, and one shared Basic Auth password (app.py:54-67) serves everyone with
no per-user identity, no revocation and no audit of who changed which mark. See Part 4.

---

## Part 2 — Requirement 2 conflicts with 3 and 4; here is the resolution

**The conflict.** A persisted `review_state.json` today contains:

| Field | What it actually is |
|---|---|
| `student_name`, `student_details` | **Student PII** |
| `Student Wrote` | The student's answer — i.e. *the answer sheet*, transcribed |
| `Correct Answer` | *The answer key*, per question |
| `Question` | *The question paper*, per question |
| `Formatted` → embedded `data:image` | **55 base64 images of handwriting** found in stored reports |

And `--regrade-one` (evaluate.py:2394-2399) loads `db_answers.json` — the answer key — as a separate
file.

So "don't store answer sheets, keys or papers" and "persist reports that teachers can re-evaluate"
are the same bytes described two ways. A report a teacher can meaningfully review **must** show what
the student wrote and what was expected; a re-evaluation **must** have the expected answer.

**The resolution** — distinguish *source documents* from *derived per-question text*:

| Category | Policy | Achievable? |
|---|---|---|
| Uploaded PDFs/scans of answer sheets, keys, papers | **Never persisted.** Processed in a temp dir, deleted when the run ends. | Yes |
| Page images, preprocessed pages, OCR intermediates, crops | **Deleted at run end.** | Yes |
| Student name, roll number, header details | **Never persisted.** Replaced by a pseudonymous `student_ref`; the school keeps the mapping. | Yes |
| Handwriting crops embedded in reports | **Dropped** (see trade-off below) | Yes |
| Per-question `Question`, `Student Wrote`, `Correct Answer`, marks, justification, remarks | **Persisted** — this *is* the report | Required by reqs 3–4 |

**Re-evaluation without storing the answer key.** The report already carries `Question`,
`Correct Answer` and `Maximum Marks` per question. Reconstruct the grader's `db_answers` from the
report itself instead of from a stored key file:

```python
db_data = {"question": res["Question"], "answer": res["Correct Answer"],
           "marks": res["Maximum Marks"], "type": res.get("Type", ""),
           "subject": state.get("exam_subject", "")}
```

`evaluate_single` needs nothing else. This satisfies requirement 4 fully while the uploaded key
document is discarded at run end — requirement 2. It also deletes a whole class of bug, since the
report becomes self-contained.

**The trade-off you must decide.** Dropping handwriting crops means teachers review **OCR text
only** and can no longer visually check the transcription against the original. The review loop
exists precisely because OCR can be wrong — so this weakens it. Three options:

| Option | Effect |
|---|---|
| Drop crops entirely | Strictest privacy; teachers cannot verify OCR against the page |
| **Keep crops for a short window (e.g. 30 days), then delete** | Review works during the marking period; long-term store is text-only. **Recommended.** |
| Keep crops for the report's life | Best review experience; you are storing handwriting indefinitely |

**Measured effect of discarding sources** (31 archived runs):

| | Per sheet | Share |
|---|---|---|
| Discarded (scans, crops, OCR intermediates) | 66.7 MB | **84.3%** |
| Kept (report JSON + PDF) | 12.4 MB | 15.7% |
| Kept, text-only (crops dropped) | ~0.3 MB | — |

---

## Part 3 — OpenRouter data retention (requirement 5)

### What is true today

- **OpenRouter does not log prompts or completions by default.** It stores request *metadata* —
  timestamp, model, token counts, latency — for billing and operations. Prompt logging is opt-in.
- **Zero Data Retention (ZDR)** routes only to endpoints whose provider does not store data at rest.
  Providers under ZDR also cannot train on the data.
- **`data_collection: "deny"`** is a *separate* control: it excludes providers that would use data
  for training. Default is `"allow"`, which permits providers that store data non-transiently **and
  may train on it**.
- The two are **not** the same, and a provider can have one without the other. For student data you
  want **both**.
- Nuance worth knowing: OpenRouter treats **in-memory caching as not "retention"**, so
  implicit-caching endpoints remain eligible under ZDR.

### ✅ IMPLEMENTED (2026-07-29)

Previously the pipeline sent no `provider` privacy directive, so every request ran under
OpenRouter's default `data_collection: "allow"` — a provider that retains **and trains on** prompts
was eligible to receive children's exam answers. That was the largest live exposure in the system.

`llm_client._provider_directive()` now emits both guarantees, **on by default**:

```python
prov["data_collection"] = "deny"   # LLM_PROVIDER_DATA_COLLECTION (default "deny")
prov["zdr"] = True                 # LLM_PROVIDER_ZDR             (default 1)
```

Design points that matter:

- **Privacy is the default, not an opt-in.** A deploy that sets no `LLM_PROVIDER_*` variable at all
  is still private. Previously the whole directive was dropped unless a *performance* knob was set,
  so a privacy-only configuration would have been silently discarded.
- **An invalid value falls back to `deny`, never to omission.** Omitting the field returns the
  decision to OpenRouter's permissive default, so a one-character typo
  (`LLM_PROVIDER_DATA_COLLECTION=denny`) would have shipped student answers to a training provider
  while the config still read as though privacy were on. *This bug existed in the first version of
  this fix and was caught by mutation testing.*
- **A blank value means "unset", not "false"** — clearing a variable in a hosting dashboard leaves an
  empty string, which must not read as disabled.
- The directive survives `generate()`'s error-retry path, which drops optional knobs; a retried call
  cannot silently downgrade privacy.

Also set both at account level (openrouter.ai/settings/privacy) as defence in depth.

**Verified against the live API** — `scripts/check_provider_privacy.py` probes every configured
model under the active policy:

| Model | Under `zdr` + `data_collection: deny` |
|---|---|
| `qwen3-vl-235b-a22b-thinking` (grading) | **OK** |
| `qwen3-vl-235b-a22b-instruct` (cascade/OCR/key parser) | **OK** |
| `qwen3-vl-30b-a3b-thinking` (diagrams) | **OK** |

The risk that ZDR would strand the two-provider grading model **did not materialise**. Re-run that
script after any `LLM_PROVIDER_*` change and in CI before deploying — the pool can change under you,
and OpenRouter's failure mode is a hard error rather than a silent privacy downgrade (the correct
direction, but still an outage if it happens mid-cycle).

Coverage: 29 tests in `tests/test_llm_provider_pin.py`, 7/8 mutations caught.

Additional mitigations, independent of routing:

- **Stop asking for the student's identity — ✅ IMPLEMENTED (2026-07-29).** `HEADER_PROMPT` was the
  only request in the pipeline whose *purpose* was extracting a child's identity: it asks the model
  to read Name, Class, Roll No and Date off the front page. When the teacher has already supplied
  the name, `_resolve_student_name` discards the OCR'd one anyway — so that call was pure exposure
  and a wasted request. It is now **skipped entirely** in that case: no image, no prompt, nothing
  leaves the machine (verified: 0 requests issued, 0 tokens billed). `OCR_EXTRACT_STUDENT_PII=0`
  disables it for every run, for deployments that take names from a manifest or filename.
  Default stays **on** so batch grading — where the sheet's own header is how 200 uploaded scans get
  identified — keeps working; the privacy win comes from skipping it when the answer is already
  known, which costs nothing. 19 tests, **8/8 mutations caught**.

  | Teacher-supplied name | `OCR_EXTRACT_STUDENT_PII` | Identity call |
  |---|---|---|
  | given (e.g. "Riya Sharma") | any | **skipped** |
  | placeholder ("Student") or blank | `1` (default) | made |
  | placeholder or blank | `0` | **skipped** |

- **What this does NOT fix — state it plainly.** The student's name is *written on the sheet*. The
  answer-OCR call sends page images, and page 1's pixels contain the header. Skipping the header
  call stops the system **asking** for the identity; it does not remove it from the image. Only
  redaction of the header band would, and that can clip a real answer, so it is left as a
  deliberate, separate decision rather than a silent default. For those pixels the protection is the
  routing policy above (no retention, no training) — which is exactly why both layers exist.
- **Keep prompt logging off** on the OpenRouter account.
- **Separate API keys per environment** so a leaked dev key cannot read production usage; rate limits
  are per-key, which also helps Part 5.
- **Record the routing policy per run.** `run_meta.json` already stamps grade-time models and flags —
  extend it with the privacy directive so you can prove to a school what policy applied to their data.

---

## Part 4 — School-level isolation (requirement 6)

Isolation must hold at four layers; anything less is isolation in name only.

| Layer | Requirement |
|---|---|
| **Identity** | `schools`, `users` (belonging to one school), roles (teacher/admin). Replaces the shared Basic Auth password. |
| **Data model** | `school_id` on every run, report and remark. **Every query filtered by the session's school** — enforced centrally, not per-route, so a new endpoint cannot forget. With Postgres, Row-Level Security makes this structural. |
| **Storage** | Object keys prefixed `{school_id}/{run_id}/…`; signed, expiring URLs. Never expose a raw path — today `/student-report/<run_id>` is guessable and unscoped. |
| **Audit** | Every mark change recorded with user, timestamp, before/after. `Machine Marks` and `Pre-edit AI Marks` exist per question, but there is no queryable trail — which is exactly what a school asks for when a grade is contested. |

Test isolation the way an attacker would: authenticate as School A and request School B's `run_id`
directly. That test belongs in the suite, permanently.

---

## Part 5 — Concurrency and scale on OpenRouter (requirement 7)

### The binding constraint, measured live

Read from the OpenRouter API for your two configured models:

**`qwen3-vl-235b-a22b-thinking`** — the grading model:

| Provider | 1-day uptime | Status | $/M output |
|---|---|---|---|
| Alibaba | **84.1%** | **-2 (deranked)** | $4.00 |
| Novita | 96.0% | 0 | $3.95 |

**Two providers, one deranked at 84% uptime.** This is both the ~370 tok/s ceiling *and* a single
point of failure on your critical path.

**`qwen3-vl-235b-a22b-instruct`** — the cascade's fast model:

| Provider | 1-day uptime | Status | $/M output |
|---|---|---|---|
| DeepInfra | 97.2% | 0 | **$0.88** |
| Alibaba | 99.7% | 0 | $1.04 |
| Novita | 98.0% | 0 | $1.50 |
| Venice | 97.6% | 0 | $1.90 |
| Parasail | 99.2% | 0 | $1.90 |

**Five providers, all healthy, output 4.5× cheaper.**

### What follows

Shifting grading from the thinking model to the instruct model improves **cost, throughput and
reliability simultaneously**. `EVAL_CASCADE` already does exactly this and already cut output from
90k to 63.8k tokens per sheet (−29%). **Tuning its escalation criteria is the highest-leverage
change available to you**, and it needs no new infrastructure.

The measured escalation triggers today (`_cascade_should_escalate`, evaluate.py) are: partial credit,
low/blank confidence, off-topic or injection flag, non-finite mark, or a substantive zero. "Partial
credit → always escalate" is the expensive one — and after the calibration work, partial credit is
now the *common* outcome rather than the exception. Re-measure it: if the instruct model agrees with
the thinking model on most partial-credit cases, that trigger can be narrowed and the saving is
large. Do this as a replay experiment against the archived corpus, not in production.

### Where the money actually goes

From 46 real cost records across 13 runs:

| Stage | Calls | Input tok | Output tok | Cost | Share |
|---|---|---|---|---|---|
| **grading** | 13 | 575,052 | 776,660 | $2.129 | **69%** |
| **ocr** | 13 | 2,932,017 | 83,548 | $0.789 | **26%** |
| answer_crop | 4 | 207,836 | 1,891 | $0.056 | 2% |
| diagram_grading | 7 | 34,053 | 28,693 | $0.049 | 2% |
| diagram_features | 7 | 87,325 | 22,680 | $0.023 | <1% |
| diagram_crop | 2 | 91,368 | 1,188 | $0.023 | <1% |

**Real cost per sheet: median $0.239, mean $0.236, range $0.137–$0.351.** (An earlier estimate of
$0.30–0.50 was too high.)

OCR is 26% of spend and is **input**-dominated — 2.9M input tokens, because pages are images.
Fewer/smaller page images is the lever there; note the earlier measurement that downscaling below
~1400px made the model interpolate, so this has a floor.

### ✅ Cascade escalation narrowed (2026-07-29)

Validated against a teacher's own per-question marks — Computer Science, 22 LLM-graded questions,
teacher subtotal 48.5, through the real `grade_cascade`:

| policy | total | MAE | escalations | output tokens |
|---|---|---|---|---|
| **narrowed (now default)** | **47.0** | **0.43** | **2** | **19,038** |
| old (escalate on all partial credit) | 46.0 | 0.48 | 11 (9 purely partial) | 58,591 |

Partial credit no longer escalates on its own. It was firing on ~40% of answers after the calibration
work, and escalating moved marks **away** from the teacher — so the expensive path was also the less
accurate one. Low confidence, off-topic, injection, non-finite marks and substantive zeros still
escalate. `EVAL_CASCADE_ESCALATE_PARTIAL=1` restores the old rule.

Every escalated result now carries `Escalated Because` and the overridden `Fast Marks`, so the trigger
mix and tier agreement can be read off production runs rather than re-run as a bespoke experiment.

**Evidence limit:** one sheet, one marker. 2-3 more marked sheets (ideally one Science) would settle
whether the thinking tier earns its 17.5x cost premium at all.

### Levers, ranked by measured value

| Lever | Effect | Verdict |
|---|---|---|
| **Narrow cascade escalation** | ✅ **DONE** — measured **−68% output tokens** and marks *closer* to the teacher | **Best.** Cheaper, faster and more accurate at once |
| Reduce OCR image tokens | Up to ~26% of spend, floor at ~1400px | Worth measuring |
| Prompt caching (rubric prefix) | ~**5.5%** of total — output dominates at 81% | Marginal; do it, but expect little |
| OpenRouter batch discount | **Does not exist** (unlike OpenAI's 50% batch tier) | Unavailable |
| More app instances / bigger machines | **Zero** — the ceiling is external | Do not spend here |

### Concurrency architecture

Because throughput is capped **outside** your infrastructure, the cheapest correct design is small:

- **One queue, fair-share across schools.** Round-robin per school so one large school cannot starve
  the rest. Persisted job state so a deploy or crash resumes rather than orphans (today's in-process
  daemon threads lose the work).
- **A small worker pool sized to saturate the API, not the CPU.** `EVAL_MAX_CONCURRENCY=24` already
  saturates the ceiling; going wider buys nothing and risks 429s.
- **Do not scale out app instances for throughput.** It cannot help. Scale out only for
  availability.
- **Rate limits are per-key.** Multiple keys may raise *request* limits, but will not exceed a
  provider's token capacity. Test before designing around it.
- **Set a realistic SLA.** At the current ceiling one school's 2,500-sheet cycle is ~5 days of
  continuous grading. Sell a turnaround you can meet, with staggered per-school slots.

---

## Part 6 — Cost breakdown for complete deployment

### Fixed infrastructure (monthly)

| Component | Spec | Cost |
|---|---|---|
| Render web service | Standard, 2 GB RAM | $25 |
| Render worker service | Standard (queue consumer) | $25 |
| Render Postgres | Basic (runs, reports, users, audit) | ~$20 |
| Render Redis (queue) | Starter | ~$10 |
| Object storage | Cloudflare R2, no egress fees | ~$1–7 |
| Error tracking | Sentry free tier | $0 |
| **Total fixed** | | **~$81–87/month** |

Storage is small **because sources are discarded** (Part 2): text-only reports are ~0.3 MB/sheet, so
150,000 sheets/year ≈ 45 GB ≈ $0.70/month. Keeping crops for 30 days adds a modest rolling
allowance. This is the direct financial payoff of requirement 2.

### Variable cost — the real driver

At the **measured $0.236/sheet**:

| Scale | Sheets/cycle | API cost/cycle | 3 cycles/year |
|---|---|---|---|
| 1 school (500 students × 5 subjects) | 2,500 | **$590** | $1,770 |
| 5 schools | 12,500 | **$2,950** | $8,850 |
| 20 schools | 50,000 | **$11,800** | $35,400 |

With a successful cascade re-tune (assume a third of grading output moves from $4.00/M to $0.88/M,
grading being 69% of spend) the per-sheet cost falls to roughly **$0.18**, saving ~$2,900/year at
5 schools and ~$11,600/year at 20.

### Total, 20 schools, 3 cycles/year

| | Annual |
|---|---|
| Infrastructure | ~$1,040 |
| API (at $0.236/sheet) | ~$35,400 |
| API (after cascade re-tune, ~$0.18) | ~$27,000 |
| **Total** | **~$28,000–36,400** |

**≈ $0.19–0.24 per sheet all-in.** Infrastructure is under 3% of it — which is why the optimisation
effort belongs in token usage, not in servers.

Excluded: staff time, support, legal/DPA review, and the compliance work in Part 7. Render and
OpenRouter prices change — re-check before committing.

---

## Part 7 — Children's data

Requirement 2, properly implemented, removes most of the exposure: no scans, no keys, no papers, no
names. What remains still needs handling.

- **Reports still contain a child's work** — their answers, their marks — under a pseudonymous ref.
  The school holds the mapping, so it is still personal data in their hands.
- **A written agreement with each school** covering what you store, for how long, where, and who can
  access it. Schools ask for this in procurement.
- **State your OpenRouter position** — with ZDR and `data_collection: deny` enabled and PII stripped
  before the call, that is a strong, defensible answer. Without them it is not.
- **Retention with real deletion**, plus per-school export and deletion on request.
- **Encryption at rest** for reports; **TLS** everywhere (Render provides it).
- **Jurisdiction** — India's DPDP Act, GDPR, FERPA, depending on where the schools are. This needs
  someone qualified; the legal lead time is usually longer than the engineering.

---

## Part 8 — Roadmap

**Phase 0 — correctness (before a second school)**
Per-run keys held in memory (§1.1, satisfies req 2) · UUID `run_id` (§1.2) · schools/users/roles with
`school_id` scoping (§1.3, §4) · **`zdr` + `data_collection: deny` (§3 — do this first, it is two
lines and it is the largest live exposure)** · `/healthz` exempt from auth · `WEB_WORKERS=1` ·
`app.secret_key` from env · upload size cap.

**Phase 1 — data minimisation + durability**
Discard sources at run end (§2) · strip PII before OCR · rebuild `db_answers` from the report so
re-evaluation needs no stored key (§2) · durable queue with persisted job state · Postgres + object
storage · backups **with a rehearsed restore** · error tracking and spend alerts.

**Phase 2 — cost and scale**
Replay experiment to narrow cascade escalation (§5 — the single biggest lever) · OCR image-token
measurement · fair-share queue · per-school spend caps (today one runaway batch is unbounded, and the
only spend monitor is a launchd agent on your Mac that does not exist in production).

**Phase 3 — trust**
Audit trail for mark changes · per-school export/deletion · status page and incident process.

Realistically Phase 0 is a substantial retrofit and Phase 1 is larger. Until they land, the safe
position is **one school at a time, manually sequenced**.

---

## What is already strong

- Grading quality is **measured, not asserted** — replay harness, held-out controls, mutation-tested
  rules, 1001 tests.
- Marks are provably legal (half-step quantization at every write site).
- The teacher stays in the loop, with reasons on every flag and machine marks preserved for audit.
- Failures degrade rather than crash: retry-and-salvage, sentinels, full-page fallback over a wrong
  crop.
- The container is host-agnostic and already supports a persistent volume.
- **`llm_client` already has provider-routing plumbing**, so the privacy fix in §3 is small.

---

## Sources

- [OpenRouter — Zero Data Retention](https://openrouter.ai/docs/guides/features/zdr)
- [OpenRouter — Provider Routing (`data_collection`)](https://openrouter.ai/docs/features/provider-routing)
- [OpenRouter — Prompt Caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching)
- [OpenRouter — Prompt caching & sticky routing](https://openrouter.ai/blog/tutorials/prompt-caching-sticky-routing/)
- [OpenRouter Data Retention Policy Across Providers](https://anarlog.so/blog/openrouter-data-retention-policy/)
- [OpenRouter Rate Limits Explained](https://www.datastudios.org/post/openrouter-rate-limits-explained-request-caps-free-model-limits-provider-quotas-scaling-issues)
- [OpenRouter Privacy and Data Routing](https://www.datastudios.org/post/openrouter-privacy-and-data-routing-provider-policies-key-handling-and-deployment-choices-explain)
- Live provider/pricing data: `https://openrouter.ai/api/v1/models/{model}/endpoints`
- Cost and storage figures: this repo's `output/*/api_costs.jsonl` (46 records, 13 runs) and
  `output/*` (31 runs)
