# How the AI Answer Evaluator Works — In Plain Language

This is a tool that grades handwritten exam papers automatically. A teacher feeds it a
scanned answer sheet, the correct answers, and the question paper. The tool reads the
student's handwriting, marks every question, and hands back a report with scores and
feedback — which the teacher can then check and correct.

This document explains the whole thing in everyday words. No prior technical knowledge is
needed. (A short glossary at the end connects each plain-English name to the real technical
term, in case you ever need to find it in the code.)

> **Plain-language edition.** For the detailed developer edition — the same system, with exact
> file and line references — see [ARCHITECTURE-technical.md](ARCHITECTURE-technical.md).

---

## 1. The big idea

Think of the tool as a **small office of specialists** working on an assembly line. A paper
comes in one end; a finished, graded report comes out the other. Along the way it passes
through a series of workers, and **each worker does exactly one job** — take the photos,
clean them up, read the handwriting, match answers to the key, grade drawings, and so on.
A **coordinator** hands the work from one specialist to the next in the right order.

Two simple habits make the whole thing reliable:

- **Everyone writes things down as they go.** Each paper gets its own folder, and every
  specialist drops its results into that folder as small note files. The next specialist
  reads those notes and adds its own. Nothing is kept "in someone's head" — if a step is
  slow or fails, the notes are still there and the office picks up where it left off. The
  website you use simply watches these notes and shows you the progress.

- **The machine never quietly guesses about marks.** Every score is either decided by a firm
  rule (like a multiple-choice answer being right or wrong) or judged by the AI against the
  teacher's answer key. Anything the tool is unsure about — messy handwriting, an answer
  that seems to belong to a different question, a possible trick — is **flagged for a human
  to look at**, never silently changed.

---

## 2. Before any grading starts: the teacher sets things up

The teacher fills in a short three-step form on the website.

**Step 1 — The question paper.** The tool reads the exam paper and pulls out each question,
its full text, and how many marks it's worth. It reads the paper **page by page, several
pages at once**, because that's fast and the marks for each question are printed right next
to it.

**Step 2 — The answer key (the correct answers).** The tool reads the marking scheme, but it
reads the **whole document in one go, not page by page**. There's a good reason: in a
marking scheme the marks often sit in a column down the right-hand side, and if you chop the
page into pieces you lose track of which marks belong to which answer. Reading it all at once
keeps every answer paired with its marks. (To save time, the tool remembers keys it has
already read, so re-uploading the same one is instant.)

**Safety checks before spending any money.** As soon as a file is uploaded, the tool does
quick sanity checks *before* asking the expensive AI to read it:

- Is this actually a document with readable text, or just a flat photo/scan with no text in
  it? (A pure scan is the number-one cause of failure, so it's caught early.)
- Are marks missing from lots of questions?
- Do the answer key and the question paper agree on the total marks? If not, it warns you.

**Who decides the marks?** Sometimes the key and the paper disagree about points. The teacher
chooses which one is the boss — the question paper or the answer key — and can even open a
**guided editor** to fix individual point values by hand. Whatever the teacher decides is
respected later when grading.

**Where should reports be saved?** The tool suggests a folder (organised by class and
subject) and the teacher confirms it.

Grading can't begin until all of this is done: the paper is in, the key is in, the checks
pass, the marks-authority is chosen, and the save folder is confirmed.

---

## 3. Grading one paper, step by step

Once setup is done, each answer sheet travels down the assembly line. Here are the workers,
in order.

### Step 1 — Take a picture of every page
The tool turns the uploaded PDF into a sharp image of each page (high resolution, so the
handwriting stays crisp). Pages are numbered so they stay in order.

**Uploaded the wrong sheet? Just upload the right one.** You do not need to reset anything or
re-enter the answer key — simply upload the correct sheet and evaluate again. The tool now **empties
that student's working folder first**, so nothing from the wrong sheet can survive. That matters more
than it sounds: the folder is named after the file, so re-uploading under the same name reuses it, and
before this fix a 5-page sheet replaced by a 2-page one left pages 3, 4 and 5 of the *wrong* sheet
behind — and the AI read all five, mixing two students' work into one result. It also threw away the
old marks, so a re-upload that failed part-way could no longer show you the previous student's
report by mistake.

Your answer key and question paper are kept somewhere else entirely, so they are never touched — the
new sheet is marked against exactly the same key. And the new report replaces the old one for that
student rather than piling up beside it.

**If the corrected file has a different name**, the tool cannot tell on its own that it replaces an
earlier one — a different file name looks like a different student. So Step 3 offers an optional
**"Replacing an earlier evaluation?"** dropdown listing everything marked so far. Pick the one this
sheet supersedes and it is tidied away for you: its working folder goes, and its report too if the new
one was saved under a different name.

The order matters, and it is deliberate: **the old evaluation is deleted only after the new one has
finished successfully.** If the new upload fails half way, nothing is removed and you still have the
original. The dropdown spells out in red exactly which evaluation will go, so there is no guessing.

**Already pressed Start Evaluation? You can still stop.** A **Cancel evaluation** button sits on the
progress screen, and the page-orientation review has a **"Wrong sheet — cancel"** button — so you can
back out at the two moments you are most likely to notice the mistake: when the pages first appear, and
while marking is running.

Cancelling genuinely stops the work rather than just hiding it. Marking is the part that costs money, so
the tool kills the job outright instead of letting it run to the end and throwing the answer away. The
wrong sheet is then deleted, you land back on the upload screen with the file box cleared, and you can
upload the right one straight away — the answer key and question paper are untouched, so there is
nothing to set up again.

### Step 2 — Clean up the pictures
Each page image is tidied so the AI can read it better: convert to grey, straighten a tilted
page, fix the camera angle, boost the contrast, and enlarge it. One deliberate choice here:
it keeps soft greys rather than forcing everything to pure black-and-white, because the
faint difference between strokes (for example, an underscore `_` versus a dash `-` in code)
matters and would be lost otherwise.

### Optional check — Is every page the right way up?
Before reading, the teacher can be shown every page exactly as it was scanned and asked to
rotate any that are sideways or upside down. This is entirely manual now — the tool doesn't
try to guess the rotation, because guessing was often wrong. If the teacher confirms with no
rotations, it's exactly the same as skipping this step. (This pause happens *before* the AI
reads anything, so it costs nothing.)

### Step 3 — The AI reads the handwriting
This is the heart of the tool. A powerful **AI that can look at images and read them**
transcribes each page into text, question by question. Several clever things happen here:

- It reads each page together with a peek at the previous page, so an answer that spills
  across a page break is stitched back together correctly.
- It labels where each answer starts and ends, then assembles the fragments into one answer
  per question.
- For **code and mathematics**, where a single mis-read symbol matters, it re-reads those
  spots and, only if two readings agree, lets a careful "referee" fix a symbol — but it is
  never allowed to change an actual word. So a student's word is never "corrected" into
  something else.
- It knows the real list of question numbers for this exam (from the key and paper), so if it
  thinks it sees a question that doesn't exist, it treats that as a likely mis-read and flags
  it instead of inventing a question.
- It then does a round of **untangling**: putting a run of answers that got clumped together
  back into separate questions, moving a stray piece back to the question it belongs to, and
  recovering an answer that accidentally got glued onto its neighbour. When it finds an answer
  hiding inside another question, it looks for the number the student themselves wrote — and it
  now recognises that number written **either way**, `Q17` or `Q.17`. Students almost always write
  the dot, and until recently the system only looked for the version without it, so it walked
  straight past answers it was meant to rescue.
- It now also recognises the other ways students head an answer: `Q-17`, `Q:17`, `Q No 17`,
  `Q.No.17`, and the **answer-side** forms `Ans 17)`, `Sol 17`, `A17.` — where the number is still the
  *question* number, not an answer count.
- Just as important, it knows what is **not** a label. A date (`12.5.2024`) used to be read as
  "question 12" — that is now fixed. Matrix entries like `A11 = -2`, geometry points, equation numbers,
  figure references, page numbers and marks notation are all explicitly excluded, and there is a shared
  list of these traps that every part of the system is tested against.
- **No page is ever thrown away.** If a page has writing on it but the system can't tell which
  question it belongs to, that text is kept to one side rather than discarded, and the run says so
  out loud: "page 2 produced no question number — check this page." Previously such a page vanished
  in silence, and every question on it was reported as unanswered.
- When several answers end up stuck inside one question, the system **keeps digging until nothing more
  comes out**, instead of pulling out the first one and moving on. It also re-checks its neighbours after
  each rescue, because an answer it just put back may itself be hiding the next one. It gives up as soon
  as a full sweep finds nothing new, and there is a hard limit on how much it will ask.
- The step that asks "does part of this answer belong to a different question?" is a bit unreliable — ask
  it twice about the same text and it can give different answers. So each question now gets **two goes**
  rather than one. Across your saved sheets this recovers **6 to 7 more answers, worth 23–27 marks**, and
  it never took an answer away.
- Anything the system rescues this way is **marked for the teacher to look at**, with a short note
  saying how it was recovered — a rescued answer went through a repair step instead of being read
  cleanly in place, so it deserves a second pair of eyes before the mark is trusted.

It also notes the student's name, roll number and date from the header, marks any spots that
were too messy to read for certain, and saves a plain-text copy of everything it read.

### Step 4 — Match each answer to the answer key
Now the tool lines up what the student wrote against the correct answers. Students label
their answers in all sorts of ways ("Q1", "Ans 1", "1.", "Answer 1") — the tool treats all
of these as **question 1** so they line up with the key no matter how they were written. It
also copies the **full question text from the question paper** onto each item (the marking
scheme leaves that blank on purpose), and it spots any "answer any one of these" choice
questions.

### Step 5 — Grade any diagrams or drawings
If the student drew something (a diagram, a graph, a labelled sketch), a separate specialist
handles it. It works in **two passes**: first it lists what's in the drawing (shapes, labels,
arrows) without looking at the answer, then it grades in detail while looking at the actual
image again to catch anything missed. To save time, **this runs in the background at the same
time as the written grading in Step 6**, and it raises a little "done" flag when it finishes
so the grader knows to fold in the diagram scores.

**Why this step decides how long a whole evaluation takes.** The written grader waits for that
"done" flag before it can finish, so however long the diagram work takes, the teacher waits too.
On two real sheets this step accounted for essentially *all* of the wall-clock time.

It was also where the biggest delay came from. Asked to describe a drawing "in extreme detail",
the AI occasionally gets stuck in a loop and repeats itself — on one page it produced **27× more
text than normal and ran for over ten minutes** on a single drawing. Nothing was broken or slow;
it simply would not stop, and the usual "give up after 90 seconds" guard can't catch that,
because that guard only trips when the AI goes *silent*, and this AI was talking the whole time.
The fix is a **word limit**: the description is now cut off at a sensible length, which turned a
10-minute page into about a minute and, as a bonus, rescued a diagram that had previously been
abandoned and left ungraded.

Two related safeguards went in alongside it: the diagram *grading* pass now also keeps whatever
it managed to finish if one drawing gets stuck (before, one stuck drawing threw away every
diagram already graded), and it now says so out loud when it can't read a drawing, instead of
quietly leaving it out — an ungraded diagram used to look exactly like one deliberately given
zero. Separately, the tidy "crop the picture out of the page" step for the report used to run
*ahead* of the grading work even though grading doesn't use it; it now runs alongside, saving
roughly half a minute per sheet.

**When an answer is read only halfway.** The AI's reading of a page is not perfectly repeatable: on one
sheet, the very same page image was read correctly most times but, roughly two times in five, a whole
block of the student's handwriting was simply skipped. That cost one answer 3.5 marks — and *nothing*
noticed, because every safety net the tool had was looking for answers that were **missing entirely**.
A half-captured answer looks perfectly normal: the question is there, it reads sensibly, it just stops
early.

The tool now uses something it already had: the answer key says which parts a question has. If the key
says a question has parts (a), (b) and (c) and only (a) and (b) were captured, that page is read again
and the missing part restored — and the question is flagged so a teacher can confirm it.

The catch worth knowing about is "choice" questions, where the key offers *(a) OR (b)* and the student
only has to answer one. Treating the unanswered alternative as "missing" would have raised five false
alarms on a single sheet. The tool compares the alternatives and only counts a part as required if
*every* option demands it — which brought that to one real alarm and no false ones. And a re-read is
only accepted if it doesn't make any other answer shorter, so this can never quietly cost a student
marks elsewhere.

### Step 5b — Double-check the point totals
Before grading, the tool makes sure the marks add up correctly. It merges multi-part
questions and "answer any one" choices so each question counts once, then **cross-checks
every question's marks against the question paper**. If the marking scheme accidentally
dropped some marks, it raises them to match the paper; if a whole question was missed, it
adds it back; if something looks inflated, it flags it for a human. The rule of thumb: it can
lift a wrong total up to the truth, but it never lowers a total that was already correct
(unless the teacher declared the paper the boss).

**When the choice information goes missing.** Many papers say "answer *either* question 22(a)
*or* 22(b)". The tool records those pairs when it reads the answer key, and counts each pair
once. If that step fails, the pairs are lost — and every alternative then gets *added* instead,
so the paper's total silently comes out too high (on one real key: **106 marks instead of 80**).
The number itself looks perfectly ordinary, which is what made it dangerous: nothing complained,
and if no question paper had been uploaded there was nothing left to compare against, so the
screen simply announced the wrong figure as "✓ Marks verified".

The tool now notices this. If the answer key records *no* choices at all, yet several questions
clearly have lettered alternatives, it says so plainly — an amber "**not verified**" line naming
the questions involved, instead of the green tick. It deliberately does **not** guess a corrected
total: once the choice information is gone, there is no way to know which alternative the paper
actually offered, so it reports the doubt and leaves the arithmetic to the teacher, who can fix
it in "Review & fix marks" or simply re-read the answer key.

### Step 6 — Grade everything and write the report
Finally, every question is scored:

- **Multiple-choice and other objective questions** are graded by **firm rules, with no AI at
  all**. This is fast and can't be fooled — if a student writes fake instructions in their
  answer hoping to trick the grader, it simply doesn't matter here.
- **Written answers** go to the AI, which grades them against the correct answer, and the
  **answer key always sets the maximum marks** (the AI can never award more than the question
  is worth). The student's answer is clearly walled off from the grading instructions, and the
  AI is told to watch for and report any attempt to sneak in commands.
- To keep costs down, there's a **"quick check, then careful check"** approach: a faster AI
  grades first, and only genuinely doubtful answers are re-graded by the slower, more expensive AI —
  ones where it says it isn't confident, where the answer looks off-topic, where someone may have
  tried to trick the grader, or where a real written answer got a zero.
- **The slower AI is no longer asked to re-check every part-marked answer**, and that turned out to
  make the marks *better*, not worse. We compared both against a **teacher's own marks** for a
  Computer Science paper. The quick AI landed **1.5 marks** from the teacher's total; sending the
  part-marked answers on to the slow AI moved it **further away**, to 2.5 — because the slow AI marks
  more harshly than this teacher does. It also used **68% more** of the paid AI capacity to get there.
  So this wasn't a case of paying more for better marking; the expensive route was simply stricter.
  Both answers where the two approaches disagreed ended up closer to the teacher under the new rule.
  Re-running each AI twice gave the same mark on 21 of 22 questions, so a quick mark is dependable
  rather than lucky. This is one paper marked by one teacher, so the old behaviour can be switched
  back on, and every re-check now records *why* it happened so more marked papers can settle it.
- **Marks only ever come in halves** — 0, 0.5, 1, 1.5, 2 and so on. Never an odd decimal like
  0.8 or 0.3. The AI is told the exact list of values it's allowed to use for each question,
  and every mark is checked again in the code before it reaches the report, so an odd value
  can't slip through even if the AI ignores the instruction. When a score lands between two
  allowed values it goes to the nearer one, and a dead heat is rounded **up**, in the
  student's favour. The same rule applies to marks a teacher types in by hand.
- **Partial credit actually works now.** The grader used to hand out 0 where a teacher would have
  given part marks: across the saved sheets, **19 out of every 100 attempted answers scored zero**,
  including some that showed a complete, correct method. Five things were wrong at once, and the two
  biggest were invisible:
  - The detailed marking guides for **maths and for code were never once used**. The tool chose a
    guide by looking for the words "maths" or "code" in the question's *type* — but a type is only
    ever something like "Short Answer" or "MCQ", which contain neither word. So every maths and
    computer-science answer was marked with the general essay guide, which knows nothing about
    giving marks for a correct formula, a correct step, or code with a small typo. It now picks the
    guide by **subject**, so those rules finally reach the marker.
  - Only the **first two pages** of each marking guide were being sent to the AI, and the guides are
    30 to 90 pages long. Everything about awarding part marks is further in; what fitted in those two
    pages was the introduction, which says to be "strict" and to "eliminate credit" for a vague
    answer. The marker was being handed the strictness and none of the rules. Each guide now starts
    with a short, explicit list of its marking rules, and that list is what gets sent.
  - The general guide also **contradicted itself** — "never deduct marks" in one rule and "deduct
    half a mark" in another; one section gave half marks for a thin-but-correct point while another
    gave it zero. Now they agree.
  - A **diagram score could wipe out correct written work**. One student wrote out a full, correct
    vector solution — the same answer as the key — and scored 0 out of 2, because the sketch beside
    it wasn't labelled (and the tool was in fact looking at the sketch from the *previous* question).
    The written answer and the drawing are two views of the same answer, so the student now gets the
    **better of the two**, and the report explains whichever one earned the mark.
  - An answer could be thrown out as **"off-topic"** just because it didn't match the question text —
    but the question text is itself read off a scan and can come out garbled, and when it does, every
    correct answer on the page looks off-topic. Three correct maths answers were zeroed this way. The
    **answer key** now has the deciding vote.

  On the saved sheets this roughly **tripled** the marks earned by answers that had been wrongly
  zeroed, while every genuinely misplaced answer stayed at zero. If a set of marks ever looks too
  generous, a single setting (`EVAL_GRADING_CALIBRATION=legacy`) puts the old behaviour back.
- For "answer any two of three" style questions, it keeps the **best** of the answered parts
  and doesn't count the rest against the student.
- It folds in the diagram scores, and **flags anything suspicious for review** — messy
  handwriting, an answer that seems misplaced, a possible trick, a point-value oddity.
- **Chemical structures keep their shape.** A drawn structure like but-1-ene is really a little
  picture made of letters, so it only reads correctly if every bond stays directly above its atom.
  The report used to chop one structure into three pieces — the rows of H's and bonds were mistaken
  for computer code and put in their own boxes, while the carbon chain was left as ordinary text in a
  different font. Now the whole structure is kept together in one fixed-width block, and the rows are
  nudged so each bond lines up with its atom (the AI's reading of the page tends to drift a couple of
  characters sideways). Ordinary maths working and single-line formulas are untouched.
- **"Illegible handwriting" now only ever means the writing was genuinely hard to read.** It used to
  double as a catch-all: whenever the second reading pass couldn't be lined up with the first — which,
  for maths, was nearly always — the answer was reported as illegible even when the writing was
  perfectly clear. On one maths paper that mislabelled 27 of 38 questions. When the two readings
  genuinely disagree about a symbol, you now get a separate, accurate note: *"Symbols may be misread"*.
  When the tool simply couldn't double-check something, it stays quiet, because that isn't a finding.
- **Every flag says WHY**, in two places. The top of the report opens with a panel grouping the
  flagged questions by reason — "No answer captured (11): Q1, Q2, Q3…", "Illegible handwriting (15):
  Q13, Q14…" — so you can see at a glance what kind of problem dominates the paper; click any
  question number to jump straight to it. Each question then repeats its own reasons on its row and
  spells them out in full under **"Why this needs review"** when you open it. The downloaded PDF
  carries the same summary and the same per-question explanations. A question the AI was happy with
  shows none of this.

Then it produces the report: a nicely formatted PDF (with proper mathematics and code
formatting, showing the *question* rather than echoing the answer, and diagram images you can
click to enlarge) plus the interactive web version. It also records exactly how much the AI
usage cost for this paper and how long each step took.

---

## 4. Grading a whole stack at once

You don't have to grade papers one at a time. You can upload **one big PDF containing many
students' sheets**, and the tool splits it up:

- It looks at the **top of each page** and asks the AI, "Is this the start of a new student's
  sheet?" — spotting each student's name/details header or bubble-sheet block. That's how it
  finds the boundaries between students (it doesn't rely on QR codes or blank separator
  pages).
- It shows the teacher the proposed split — who starts where, their names and subjects — and
  the teacher can adjust boundaries, rename students, merge or split before approving.
- Once approved, it runs the exact same assembly line for **each student**, using the same
  shared answer key and question paper.
- **Speed for big stacks:** by default it grades the sheets one after another. A setting
  (`BATCH_SHEET_CONCURRENCY`) lets it grade **a few sheets at the same time** to finish a large
  batch sooner. It isn't "N times faster" — the AI grading itself already runs flat‑out for a
  single sheet — but while one sheet is being graded, another's handwriting‑reading can be
  happening in parallel, which trims the total wait. Each sheet still runs as its own fully
  separate job, so grading a stack this way gives the **exact same marks** as grading them one
  by one; it just overlaps the waiting. Each parallel sheet is dialled back a little so the tool
  never asks the AI service for more than it would for a single sheet (keeping it within limits).
- The results appear as a **grid of student cards** — each showing the score and little
  badges (how many answers need a look, whether a trick was detected, how many you've
  reviewed). Clicking a card opens that student's full report.

**Seeing the student's actual handwriting.** Next to each answer the report can show a **photo of that
answer**, cut from the scanned page — and if an answer runs across two pages you get one picture per
page. The tool works out where each answer sits by asking the AI to point at it, then tidies the cut so
it lands in the blank space between lines and hugs the writing. If anything about that looks unreliable
it simply shows **the whole page instead** — so a picture is either right, or it's the full page; it is
never a misleading crop. These pictures appear in the **on-screen report only** (the downloadable PDF
is unchanged), and the whole feature can be switched off.

**Diagrams get the same treatment.** Where a student draws a figure — a ray diagram, a labelled
triangle, a graph — the report used to show the **whole page** it was drawn on. It now shows just the
drawing. On the papers we checked, a figure typically occupies about **4% of the page**, so this is the
difference between squinting at a full sheet and seeing the diagram itself.

Two things make it trustworthy. If the tool cannot confidently find the figure, it falls back to the
full page — exactly what it did before, so nothing is lost. And it deliberately **ignores pages that
have no drawing on them**: a long answer often spans several pages, and only one of them holds the
figure, so the others are left out instead of padding the report with whole pages. Importantly, this
only changes what you *see* — marking still reads the full page, so a bad crop can never change a score.

**How often does it get a tight picture? 93–97% of answers**, measured across three real papers. It used
to be less, and — more annoying — *unpredictable*: the same code gave 92% on one paper and 61% on
another. Two fixes closed that gap.

The first was luck. The AI occasionally garbles one number in its reply, and the tool used to throw away
the whole page because of it — six answers lost to a single bad moment. It now simply **asks again** (up
to twice), and keeps whatever good positions it did get. Re-testing the pages that had failed, they
succeeded 4 times out of 4, so this was pure bad luck rather than anything about those papers. Three
runs of the same paper now score identically.

The second was a real blind spot with **answers that run across pages**. When *two* long answers spilled
onto the same page, the tool only ever handled the first and quietly gave up on the second — that
happened on 1, 3 and 7 pages of the three papers we checked, and it was the cause of every remaining
failure. The tool now asks the AI where each continuing answer finishes, so a shared page is split
correctly between them. Every one of those "gave up" cases is now gone.

**Does it make marking slower? No — it's free.** The pictures are worked out *while the AI is busy
marking*, on a different AI model, so the two happen side by side instead of one after the other. On the
paper above the picture work took about **18 seconds** inside a **157-second** marking window, and the
total time came out **shorter** than adding the steps up — which is the proof they overlapped. Roughly a
third of pages need no AI call at all. What it does cost is about **1 paisa per paper** (~7% more) and
about **6 MB of pictures per paper**, so a 60-paper batch stores around 350 MB.

The one way it *could* cost time is if the picture work were still unfinished when marking ends, so the
tool waits **at most 90 seconds** for it and then simply publishes the report without pictures rather
than holding everything up. When several papers are marked at once, the picture work is also given
enough helpers to stay comfortably ahead — measured at **9 seconds instead of 17** after that change.

---

## 5. The teacher checks and corrects the results

Nothing is final until a human says so. The tool keeps **two copies** of each result:

- The **original AI grades**, saved once and never touched, so there's always a truthful
  record of what the machine decided.
- A **working copy** that's only created the moment the teacher makes their first change.
  (If the teacher changes nothing, there's no working copy at all — the result is exactly as
  graded.)

The reasons work on **reports you graded before this feature existed** too — they're worked out when
the report is opened, so nothing needs re-grading and no old file is changed.

On the report the teacher can:

- **Accept** a mark as-is.
- **Change a mark**, with an optional reason. The box only accepts halves (0, 0.5, 1, 1.5 …);
  anything else is snapped to the nearest allowed value as soon as you leave the box, so you
  always see the mark that will actually be saved.
- **Re-grade a single question** after fixing the AI's reading of it — and if the text didn't
  actually change, it skips the slow re-grade entirely and just confirms.

Accept and change-mark decisions are **staged and saved together when the teacher clicks
Submit** (with a warning if they try to leave with unsaved changes); re-grading a question
saves right away. Every time the teacher saves, the **PDF is rebuilt** to match, and any
marks the teacher overturned are recorded in a database as an audit trail. The "reviewed so
far" count on the student grid always stays in sync.

---

## 6. The AI, and how it's kept honest and affordable

- **One doorway to the AI.** Every step that needs the AI goes through a **single shared
  connection** to an online AI service (by default a service called OpenRouter, though a
  privately-run AI server also works). Pictures are sent to the AI safely encoded, and if a
  request hiccups it retries once.
- **The AI is no longer asked who the student is, when we already know.** One request used to exist
  purely to read the pupil's **name, class and roll number** off the top of the first page. But if
  the teacher already typed the student's name when uploading, that answer was thrown away anyway —
  so the request was sent for nothing. It is now **skipped entirely** in that case: no picture, no
  question, nothing about the child leaves the computer. When the name *isn't* known (bulk uploads,
  where the sheet's own header is the only way to tell whose paper is whose) the request still
  happens, because otherwise the reports could not be matched to pupils.
  *Honest limitation:* the name is **written on the paper**, and the pages themselves still go to
  the AI to be read. So this stops us *asking* who the student is; it doesn't erase the name from
  the photo. Blanking out the top strip of page one would — but it risks covering part of a real
  answer, so that stays a deliberate choice rather than something switched on quietly.
- **Student answers are only sent to companies that promise not to keep or learn from them.**
  OpenRouter passes each request on to one of several AI companies that actually run the model. Left
  alone, it is allowed to pick a company that **stores** what you send **and uses it to train** its
  future models — which is not acceptable for children's exam answers. The app now attaches two
  standing instructions to every request: *only use companies that will not train on this*, and
  *only use ones that do not keep a copy*. These are two different promises, so both are demanded.
  They are on automatically — nobody has to remember to switch them on, and if the setting is
  mistyped the app falls back to the **safer** option rather than the permissive one. Because these
  rules rule some companies out, a checker (`scripts/check_provider_privacy.py`) confirms the models
  are still available; all three were verified working under the strict policy.
- **Different AI "sizes" for different jobs.** Reading handwriting, parsing the key, and
  grading each use a capable large model; the diagram helpers use smaller, cheaper ones.
  Which model does which job is just a **setting** you can change without touching the code —
  so you can swap in a bigger or smaller model per step. (As currently set up, the biggest
  model reads handwriting and grades, while smaller ones handle diagrams.)
- **Honest cost tracking.** For every paper, the tool records the **real amount the AI
  service billed** (not a guess) for each step, and adds it up so you know exactly what a
  paper cost.
- **Safety limits.** Each step has a time limit so a stuck step can't hang the whole job — it
  just gives up gracefully and moves on. And the tool only does so many AI requests at once,
  to stay within sensible limits.

---

## 7. Where everything is stored

Almost everything lives as ordinary files on disk, not in a database.

- **Each paper gets its own folder** containing: the page images, the cleaned-up images, the
  AI's reading of the answers, the matched-up answer key, the diagram results, the
  point-value check, the original grades, the teacher's working copy, the student's details,
  the cost record, timing, and progress notes.
- **A shared "current setup" area** holds the most recently uploaded answer key and question
  paper (already read into a tidy form), the marks-authority choice, and the save-folder
  choice.
- **A remembered-keys cache** so re-reading the same answer key is instant.
- **A database (a digital filing cabinet)** is used only lightly: it stores the teacher's
  overturned marks (the audit trail) and an old, now-unused answer bank. The tool runs fine
  without it.

Finished reports are written to the folder the teacher chose (organised by class and
subject) and can be downloaded from the website.

---

## 8. Using the app (the website)

The website is a single page with a **three-step wizard** (question paper → answer key →
answer sheet) that unlocks each step as the previous one is done, then shows the results.

- A **live progress checklist** shows which step a paper is on ("Reading handwriting",
  "Analysing diagrams", "Grading & building report").
- The **same report display** is reused for a single student and for each student in a batch.
- The **orientation check** shows each page and lets you rotate it with simple Left / Flip /
  Right buttons; the preview matches exactly what the AI will read.
- The **guided marks editor** presents the point-value fixes as easy cards to click through.
- **Mathematics and code** are shown properly formatted, with a plain-text fallback if the
  fancy formatting can't load.

Behind the scenes, long jobs run in the background and the page simply checks in for updates —
so your browser never sits frozen waiting.

---

## 9. Running and hosting it

- **Self-contained package.** The whole app ships as a **container** — a sealed box with
  everything it needs — so it runs the same way on any machine. It serves the website through
  standard web-server software, and if given a storage disk it keeps all reports safe across
  restarts.
- **Sharing it over the internet.** A helper script can put the app online from a Mac through
  a secure **tunnel** (it supports a few different tunnel services). When it's exposed
  publicly it automatically turns on a **password prompt** and turns off developer debugging,
  so it's safe to share a link with a colleague.
- **Checking a new machine before you trust it.** One command —
  `python scripts/check_platform.py` — prints a plain pass/fail report: is the Python version one the
  libraries have ready-made builds for, is every library actually installed (and what breaks if one
  isn't), can the tool start and stop its helper programs, does it handle maths and chemistry symbols
  correctly, and can it really turn a PDF into cleaned-up page images. It is written to work even on a
  *broken* machine, so it explains the problem instead of crashing alongside it. Run this first
  whenever a computer misbehaves, and send the output.
- **Which computers it runs on.** Mac, Linux and **Windows**. The grading work is split into
  separate small programs that the main program starts and, if one hangs, stops. Both of those
  actions used to be written the Mac/Linux way only, so on Windows the small programs quietly
  did nothing at all — and stopping one crashed the whole run instead of tidying up. Windows
  also assumes a different alphabet for text unless told otherwise, which garbled the maths and
  chemistry symbols the tool works so hard to read correctly. All three now have a proper
  Windows equivalent, chosen automatically, with the Mac/Linux behaviour left exactly as it was.
  One caveat: the standard web-server software the container uses doesn't exist for Windows, so
  a Windows machine runs the app's own built-in server instead (the container is unaffected).

---

## 10. How it's tested

The tool comes with a large automated test suite — **358 checks** across roughly 40 files —
covering handwriting reading, page orientation, answer-untangling, point-value checking,
upload safety checks, grading, multiple-choice and choice questions, the teacher-review flow,
and cost tracking. These run without needing the internet or spending any money, so the
behaviour can be verified safely.

---

## 11. A few places where the old notes are out of date

While mapping the system, a handful of **stale comments** turned up. They don't affect how it
works, but they can mislead a reader:

- The little description files next to each specialist still mention an older AI ("Gemini");
  the tool actually uses the newer Qwen AI now.
- The orientation step's own description still says it suggests a rotation automatically; in
  reality that's fully manual now.
- A comment claims "235 tests" when there are really 358.
- A couple of setting files list example values that don't match the ones actually used.
- A file named as if it holds cropped diagrams (`diagram_crops.json`) actually holds full pages — it
  is really "which pages hold this question's diagram". The tight crops now live in a *separate*
  file, `diagram_display_crops.json`; the original name is still misleading.
- An old database-based answer look-up (and a few image helpers) are kept around but no longer
  used.

These are worth a quick cleanup so the notes match reality.

---

## 12. Glossary — plain word → technical term

For anyone who later needs to find these things in the code:

| In this document | The real name |
|---|---|
| The coordinator / assembly line | `scripts/full_evaluator.py` (the pipeline orchestrator) |
| A specialist worker (one job) | a "skill" run as a separate subprocess under `skills/` |
| Take a picture of each page | ingestion — rasterising the PDF to PNG images |
| Clean up the pictures | preprocessing (OpenCV: deskew, contrast, straighten) |
| The AI reads the handwriting | vision OCR with the Qwen3‑VL model (`run_ocr.py`) |
| Untangling split/mixed answers | segmentation‑repair layers |
| Match answers to the key | ground‑truth alignment |
| Double-check point totals | marks reconciliation against the question paper |
| Quick check then careful check | the grading "cascade" |
| A student trying to trick the grader | prompt injection |
| Note files in each paper's folder | JSON files under `output/<run_id>/` |
| The original grades vs the working copy | `review_state.json` vs `review_render.json` |
| One doorway to the AI | the shared `generate()` client (`scripts/llm_client.py`) |
| Which model does which job (a setting) | environment variables (e.g. `OCR_MODEL`, `EVAL_MODEL`) |
| The online AI service | OpenRouter (any OpenAI‑compatible endpoint) |
| The digital filing cabinet | a PostgreSQL database |
| Sealed box that runs anywhere | a Docker container |
| Secure internet link | a Tailscale / Cloudflare / ngrok tunnel |
| The website | the Flask web app (`evaluation_app/app.py`, `index.html`) |

**Saving every report for review (report collection).** During teacher testing, a small background helper
(`scripts/report_sync.py`) keeps a tidy, permanent copy of every finished report in one folder on the Mac —
`~/Evaluation Report Archive/` — as a zip per student plus a little searchable database (`index.sqlite3`) you
can open to check marks and flags. It is entirely local (nothing goes to the internet), runs itself every few
minutes, and tags each teacher's uploads by the "Tester / School" box on the upload screen. See
[REPORT-SYNC.md](REPORT-SYNC.md).

> This is the plain-language edition. Its companion, [ARCHITECTURE-technical.md](ARCHITECTURE-technical.md),
> describes the same system in full technical detail with exact file and line references.
