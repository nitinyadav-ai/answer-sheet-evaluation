# CBSE-Level Rubric for Evaluating Mathematical Questions
## For AI Answer Sheet Evaluator (ExamLens)

<!-- GRADER-DIRECTIVES:BEGIN -->
GRADING DIRECTIVES — MATHEMATICS / NUMERICALS / PROOFS / DERIVATIONS

CBSE marks the PROCESS, not only the final answer. Award marks step by step, additively.

1. DECOMPOSE the solution into steps and give each a share of the marks. Standard CBSE splits:
   * 1 mark  — correct final answer only. (No partial credit at 1 mark.)
   * 2 marks — formula/method 1/2 + correct substitution 1/2 + final answer 1.
   * 3 marks — formula/theorem 1 + substitution 1/2 + intermediate working 1/2 + final answer 1.
   * 4-5 marks — setup 1/2-1 + formula 1 + working 1-2 + final answer 1 + units/conclusion 1/2.
   * Prove / show / derive — correct starting point 1/2 + intermediate transformations + reaching
     the required result 1/2.
2. AWARD EVERY STEP THE STUDENT GETS RIGHT, even when the final answer is wrong. A correct formula
   earns its mark. Correct substitution earns its mark. A correct method earns its marks. An
   answer that reaches the wrong number by a sound method is NOT a zero. Working that is partly
   correct MUST score above zero — award the marks for the steps that are right and withhold only
   the marks for the steps that are wrong.
3. CARRY-FORWARD ERROR — "penalize the error once, not twice". If the student slips at one step
   and then uses their own incorrect value CORRECTLY and CONSISTENTLY afterwards, award 0 for the
   step containing the slip and FULL marks for every later step whose method is right relative to
   that value. A single arithmetic slip must never cascade into a zero.
4. ALTERNATIVE METHODS are fully valid. Any mathematically sound route to the result earns full
   marks even when it differs from the key.
5. EQUIVALENT FORMS ARE EQUAL: 182/3 pi = 182pi/3; 0.5 = 1/2; sqrt2/2 = 1/sqrt2; decimals vs
   fractions; simplified vs unsimplified. A reversed vector convention (PQ vs QP, giving the exact
   negative with the same magnitude) is a direction convention, not a wrong answer — award the
   method marks and note the convention.
6. NEVER DEDUCT for untidy working, a missing unit on an intermediate line, or skipped obvious
   algebra. Marks come from what IS present.
7. THE SETUP EARNS ITS OWN MARK. Correctly writing down the given values, stating what is asked,
   naming the events/variables, or drawing the required figure earns the setup mark EVEN IF the
   formula chosen next is the wrong one. Do not let a later error erase an earlier correct step.
8. MATCHING THE KEY'S METHOD EARNS THAT METHOD'S MARKS. If the student's working follows the same
   route as the Expected Answer, award the marks for the steps they reach, even when a variable,
   a sign, or an intermediate value differs from the key. A difference of that kind costs the ONE
   step it occurs in — never the whole answer.
9. RESERVE ZERO for a blank answer, or working that shares no correct step with any valid method.
   Before you report 0 on a question where the student has shown working, check rules 2, 3, 7 and 8
   again: a zero means they got NOTHING right, which is rare when working is present.
<!-- GRADER-DIRECTIVES:END -->

---

## Document Purpose

This rubric governs how the AI evaluator handles **any question containing mathematical equations, formulas, numerical calculations, proofs, derivations, or graphical/geometric reasoning**. It applies across subjects — Mathematics, Physics numericals, Chemistry numericals, Economics calculations, Accountancy computations, and any other paper where mathematical working appears.

The rubric is grounded in actual CBSE evaluation practices: stepwise marking, carry-forward error handling, half-mark granularity, and the principle that **process earns marks, not just the final answer**.

---

## Section 1 — The Fundamental Principle of Mathematical Evaluation

### 1.1 Process Over Product

In CBSE board evaluation, mathematical questions are scored based on the **solving process**, not just the final answer. A student who shows the correct method but makes an arithmetic slip in the last step loses only the final-answer mark. A student who writes only the correct final answer without any working (in questions worth 2+ marks) earns only the final-answer mark.

This means: **the journey is worth more than the destination.**

### 1.2 The Step-Marking Pipeline

Every mathematical solution follows a pipeline. The AI evaluator must decompose every solution into these stages and score each stage independently:

```
┌──────────────────────────────────────────────────────────────────┐
│                    THE STEP-MARKING PIPELINE                     │
│                                                                  │
│  STAGE 1: GIVEN DATA / SETUP                                    │
│  Writing down given information, identifying what is asked,      │
│  drawing a figure (if geometric), assigning variables.           │
│  Marks: 0 to ½ mark (varies by question)                        │
│                                                                  │
│  STAGE 2: FORMULA / METHOD IDENTIFICATION                        │
│  Writing the correct formula, theorem, identity, or equation     │
│  that applies to this problem.                                   │
│  Marks: ½ to 1 mark                                             │
│                                                                  │
│  STAGE 3: SUBSTITUTION                                           │
│  Correctly substituting the given values into the formula.       │
│  Marks: ½ to 1 mark                                             │
│                                                                  │
│  STAGE 4: INTERMEDIATE CALCULATION / SIMPLIFICATION              │
│  Working through algebraic manipulation, arithmetic, or          │
│  logical steps to move from substitution toward the answer.      │
│  Marks: ½ to 2 marks (depending on question complexity)          │
│                                                                  │
│  STAGE 5: FINAL ANSWER                                           │
│  The concluding numerical value, expression, or statement        │
│  with appropriate units (if applicable).                         │
│  Marks: ½ to 1 mark                                             │
│                                                                  │
│  STAGE 6: UNITS / CONCLUDING STATEMENT                           │
│  Correct units, proper notation, "therefore" statement,          │
│  or interpretation of the result.                                │
│  Marks: 0 to ½ mark                                             │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

Not every question will use all 6 stages. A 1-mark question might only have Stage 2 + Stage 5. A 5-mark derivation will use all 6 stages with multiple sub-steps within Stage 4.

---

## Section 2 — Mark Distribution Templates by Question Weight

### 2.1 — One-Mark Questions (1M)

**Expected**: Direct answer. Formula application or single-step calculation.

| Component | Marks |
|---|---|
| Correct final answer | 1 |

**Rules**:
- Full 1 mark ONLY for correct final answer.
- No partial credit — it is either right or wrong.
- If the student writes only the formula but not the answer → 0 marks.
- If the student writes the correct answer without any working → 1 mark (full credit — working is not expected for 1M questions).

---

### 2.2 — Two-Mark Questions (2M)

**Expected**: Formula + application OR two logical steps.

**Template A — Numerical/Calculation Type**:

| Step | Component | Marks |
|---|---|---|
| S1 | Correct formula / method identification | ½ |
| S2 | Correct substitution of values | ½ |
| S3 | Correct final answer (with units if applicable) | 1 |

**Template B — Prove/Show/Derive Type**:

| Step | Component | Marks |
|---|---|---|
| S1 | Correct starting point (LHS or known identity) | ½ |
| S2 | Correct intermediate transformation | 1 |
| S3 | Reaching the required result (RHS / QED) | ½ |

**Template C — Find + Justify Type**:

| Step | Component | Marks |
|---|---|---|
| S1 | Method / approach | 1 |
| S2 | Correct answer with justification | 1 |

**Rules for 2M**:
- If only the correct formula is written with no further work → ½ mark maximum.
- If formula is correct, substitution is correct, but calculation error in final step → 1 mark (formula ½ + substitution ½, final answer 0).
- If the student skips the formula but writes correct substitution and answer → 1.5 marks (deduct ½ for missing formula).
- Correct final answer with no working shown → 1 mark only (the final-answer mark).

---

### 2.3 — Three-Mark Questions (3M)

**Expected**: Multi-step solution with clear working.

**Template A — Numerical/Calculation Type**:

| Step | Component | Marks |
|---|---|---|
| S1 | Correct formula / theorem stated | 1 |
| S2 | Correct substitution of values | ½ |
| S3 | Correct intermediate calculation | ½ |
| S4 | Correct final answer with units | 1 |

**Template B — Prove/Derive Type**:

| Step | Component | Marks |
|---|---|---|
| S1 | Starting expression / setup | ½ |
| S2 | Key transformation step 1 | 1 |
| S3 | Key transformation step 2 | 1 |
| S4 | Reaching the required conclusion | ½ |

**Template C — Word Problem Type**:

| Step | Component | Marks |
|---|---|---|
| S1 | Formulating the equation from the problem | 1 |
| S2 | Solving the equation (showing steps) | 1 |
| S3 | Final answer with interpretation | 1 |

**Rules for 3M**:
- Formula alone without any work → ½ to 1 mark maximum.
- All steps correct but final arithmetic error → 2 to 2.5 marks (lose only the final answer mark).
- Correct final answer with no working → 1 mark only.

---

### 2.4 — Four-Mark Questions (4M)

**Expected**: Comprehensive solution with multiple stages.

**Template A — Numerical/Calculation Type**:

| Step | Component | Marks |
|---|---|---|
| S1 | Identifying the correct formula / setting up the problem | 1 |
| S2 | Substitution of given values | ½ |
| S3 | Intermediate calculations / simplification (may have 2-3 sub-steps) | 1.5 |
| S4 | Final answer with correct units | 1 |

**Template B — Prove/Derive/Show Type**:

| Step | Component | Marks |
|---|---|---|
| S1 | Starting point / known result stated | ½ |
| S2 | First key logical step | 1 |
| S3 | Second key logical step | 1 |
| S4 | Third key logical step / intermediate result | 1 |
| S5 | Final conclusion / QED | ½ |

**Template C — Graph + Calculation Type**:

| Step | Component | Marks |
|---|---|---|
| S1 | Correct equation / calculation setup | 1 |
| S2 | Solving / finding coordinates or values | 1 |
| S3 | Correct graph with labeled axes | 1 |
| S4 | Identifying solution from graph / interpretation | 1 |

**Rules for 4M**:
- Formula only → ½ to 1 mark maximum.
- All steps correct, final arithmetic error → 3 to 3.5 marks.
- Correct final answer, no working → 1 mark only.

---

### 2.5 — Five-Mark Questions (5M)

**Expected**: Full-length solution with detailed working.

**Template A — Numerical/Calculation Type**:

| Step | Component | Marks |
|---|---|---|
| S1 | Given data organized / figure drawn (if needed) | ½ |
| S2 | Correct formula / approach identified | 1 |
| S3 | Substitution of values | ½ |
| S4 | Intermediate calculation step 1 | 1 |
| S5 | Intermediate calculation step 2 | 1 |
| S6 | Final answer with units and concluding statement | 1 |

**Template B — Multi-Part Question (a + b type)**:

Decompose into sub-parts and apply the appropriate template to each sub-part independently. Total of sub-part marks = 5.

**Template C — Derivation / Proof Type**:

| Step | Component | Marks |
|---|---|---|
| S1 | Starting point / assumptions stated | ½ |
| S2 | Key step 1 | 1 |
| S3 | Key step 2 | 1 |
| S4 | Key step 3 | 1 |
| S5 | Key step 4 / arriving at intermediate result | 1 |
| S6 | Final result with proper notation | ½ |

---

## Section 3 — The Carry-Forward Error (CFE) Rule

This is the single most important fairness rule in CBSE mathematical evaluation.

### 3.1 Definition

A **Carry-Forward Error (CFE)** occurs when:
1. The student makes a mistake in an earlier step (e.g., arithmetic error, sign error, copying error).
2. The student then uses the **incorrect intermediate value** in all subsequent steps **correctly and consistently**.

### 3.2 How to Handle CFE

```
IF the student:
  - Made an error in Step N (earning 0 for that step)
  - BUT used the wrong value from Step N consistently in Steps N+1, N+2, ...
  - AND the method / logic in Steps N+1, N+2, ... is CORRECT

THEN:
  - Award 0 marks for Step N (where the error occurred)
  - Award FULL marks for Steps N+1, N+2, ... (because the logic is correct
    relative to their incorrect intermediate value)
  - The student loses marks ONLY for the step where the actual error was made

THIS IS CALLED: "Penalize the error once, not twice."
```

### 3.3 CFE Examples

**Example 1 — Arithmetic CFE in a 3-mark question**:

Answer Key Step 2: 3x + 6 = 15 → 3x = 9
Student writes: 3x + 6 = 15 → 3x = 11 (arithmetic error: 15-6=11 instead of 9)

Answer Key Step 3: x = 9/3 = 3
Student writes: x = 11/3 = 3.67 (consistent with THEIR incorrect 3x = 11)

**Marking**: Step 2 → 0 marks (error here). Step 3 → FULL marks (division is correct relative to their value). The student loses marks only once.

**Example 2 — Sign Error CFE in a 4-mark question**:

Step 1: Student correctly writes the quadratic formula. ✓ (1 mark)
Step 2: Student substitutes but writes b² = -16 instead of +16 (sign error). ✗ (0 marks)
Step 3: Student correctly computes √(value) using their -16. (½ mark — method is correct)
Step 4: Student gets wrong final answer because of propagated error. (0 marks for wrong answer, BUT if the arithmetic following from their wrong discriminant is correct, award the step mark)

### 3.4 When CFE Does NOT Apply

- If the student makes a **fresh, independent error** in a later step (not caused by the earlier error), that step also loses marks independently.
- If the student **changes method** midway and the new method is also wrong, CFE does not bridge across method changes.
- If the student makes **conceptual errors** (e.g., using addition instead of multiplication), these are method errors, not arithmetic errors — they lose the method mark, not just the calculation mark.

---

## Section 4 — OCR Tolerance Rules for Mathematical Content

OCR of handwritten mathematical content is significantly harder than OCR of text. The AI evaluator must apply special tolerance rules.

### 4.1 Common OCR Distortions in Mathematics

| What the Student Wrote | What OCR Might Produce | How to Interpret |
|---|---|---|
| x² | x2, x^2, x², x2 | All mean x-squared |
| √x | √x, sqrt(x), vx, Vx | All mean square root of x |
| ∫ | ∫, S, ſ, f | Context: if followed by dx, it is an integral sign |
| Σ | Σ, E, sigma, ∑ | Context: if followed by i=1 to n, it is summation |
| π | π, pi, n (misread), TT | Context-dependent — if in geometry/trig, likely pi |
| θ | θ, 0 (zero), O, Q | Context: if in trigonometry, it is theta |
| ≤, ≥ | <=, >=, <, >, ≤, ≥ | Accept all notations for inequality |
| ∞ | ∞, inf, 8 (misread), oo | Context: if in limits or integration bounds, it is infinity |
| dy/dx | dy/dx, dy / dx, d y/d x | All mean the derivative |
| ½ | 1/2, 0.5, ½ | All equivalent |
| × (multiplication) | x (letter), *, ×, · | Context: if between two numbers, it is multiplication |
| − (minus) | -, – , — , _ | All mean subtraction or negative |
| = | =, ==, ⇒ (sometimes) | Accept = in all forms |
| log₁₀ | log10, log 10, log₁₀ | All mean logarithm base 10 |
| sin²θ | sin^2θ, sin2θ, (sinθ)² | All mean sine-squared of theta |
| ₙCᵣ | nCr, C(n,r), ⁿCᵣ | All mean combination |

### 4.2 OCR Tolerance Decision Framework

```
STEP 1: Read the raw OCR output for the mathematical expression.

STEP 2: Identify the CONTEXT (which subject, which topic, what type of problem).

STEP 3: Apply contextual interpretation:
  - If the expression is ambiguous, choose the interpretation that is
    MATHEMATICALLY VALID in the context of the question.
  - If two interpretations are both valid, choose the one that GIVES
    THE STUDENT MORE MARKS (benefit of doubt).

STEP 4: If the expression is completely garbled and no reasonable
  mathematical interpretation is possible:
  - Flag: "OCR artifact — mathematical expression illegible.
    Manual review recommended."
  - Award 0 for that specific step only. Do not penalize other steps.

STEP 5: NEVER penalize a student for:
  - Missing superscript/subscript formatting (x2 = x²)
  - Missing multiplication signs (2x = 2×x)
  - Using / instead of ÷ or a fraction bar
  - Using * instead of ×
  - Informal notation that is unambiguous in context
```

### 4.3 Equation Equivalence Rules

The AI must recognize that these are all equivalent ways of expressing the same mathematical idea:

**Fractions**: `3/4` = `¾` = `0.75` (unless the question specifically demands a particular form)

**Quadratic Formula**: `x = (-b ± √(b²-4ac)) / 2a` = `x = (-b ± √(b² - 4ac)) / (2a)` = `x = -(b) ± √(b^2 - 4ac) / 2a`

**Trigonometric**: `sin²θ + cos²θ = 1` = `(sinθ)² + (cosθ)² = 1` = `sin^2(theta) + cos^2(theta) = 1`

**Logarithmic**: `log x` = `log₁₀ x` (when base is standard in context) = `lg x`

**Derivatives**: `dy/dx` = `y'` = `f'(x)` = `Dy` (all acceptable notations)

**Integrals**: `∫f(x)dx` — the constant of integration `+ C` must be present for indefinite integrals (½ mark deduction if missing, per CBSE practice)

---

## Section 5 — Category-Specific Evaluation Rules

### 5.1 Algebra Questions

**Equation Solving (Linear, Quadratic, Simultaneous)**:
- Writing the standard form of the equation → ½ mark
- Identifying the correct method (factoring, quadratic formula, elimination, substitution) → ½ mark
- Executing the method correctly → 1–2 marks (depending on total)
- Final answer(s) → ½–1 mark
- Verification (if asked) → ½–1 mark

**Special Rules**:
- For quadratic equations: BOTH roots must be found for full marks. Finding only one root → lose ½ mark from the final answer step.
- If the question asks to "solve" and the student factors correctly but doesn't state x = ... → lose ½ mark.
- Accept answers in any equivalent form: x = 3/2 = 1.5 = 1½.

### 5.2 Calculus Questions (Class 11–12)

**Differentiation**:
- Identifying the correct rule (chain rule, product rule, quotient rule) → ½ mark
- Correct application of the rule → 1–2 marks
- Simplification to final form → ½–1 mark

**Integration**:
- Identifying the correct method (substitution, by parts, partial fractions) → ½ mark
- Correct substitution / setup → ½ mark
- Executing the integration → 1–2 marks
- Final answer → ½ mark
- **Constant of integration (+C)**: Missing +C in indefinite integrals → deduct ½ mark. This is a CBSE-standard deduction.
- Limits of integration applied correctly (for definite integrals) → ½ mark

**Differential Equations**:
- Identifying the type (separable, linear, homogeneous) → ½ mark
- Correct separation / integrating factor → 1 mark
- Integration of both sides → 1–2 marks
- Applying initial conditions (if given) → ½–1 mark
- Final answer → ½ mark

### 5.3 Geometry & Mensuration Questions

**Area/Volume/Surface Area**:
- Correct formula identified → ½–1 mark
- Substitution of dimensions → ½ mark
- Calculation → ½–1 mark
- Final answer with correct units (cm², m³, etc.) → ½–1 mark
- **Units are critical**: Missing units → deduct ½ mark. Wrong units (e.g., cm² instead of cm³) → deduct ½ mark.

**Coordinate Geometry**:
- Correct formula (distance/section/slope/area) → ½ mark
- Substitution of coordinates → ½ mark
- Calculation → 1–2 marks
- Final answer → ½ mark

**Proofs (Congruence, Similarity, Circle Theorems)**:
- Identifying the correct theorem/property → ½ mark
- Logical chain of reasoning (each key step) → 1 mark each
- Concluding statement ("Hence proved" / QED) → ½ mark
- **Diagram**: If the question asks "with diagram" or a diagram is part of the expected answer → 1 mark for a labeled diagram. Unlabeled diagram → ½ mark. No diagram → 0 for diagram mark, but full marks for proof are still possible.

### 5.4 Statistics & Probability Questions

**Statistics (Mean, Median, Mode, SD)**:
- Correct formula → ½ mark
- Correct table construction (if cumulative frequency, etc.) → 1 mark
- Correct computation → 1–2 marks
- Final answer → ½–1 mark

**Probability**:
- Identifying sample space / total outcomes → ½ mark
- Identifying favorable outcomes → ½ mark
- Correct formula application (P = favorable/total, or Bayes', or binomial) → ½ mark
- Calculation → ½–1 mark
- Final answer (must be between 0 and 1, or expressed as fraction/percentage) → ½ mark

### 5.5 Trigonometry Questions

**Proving Identities**:
- Starting from LHS (or RHS) → ½ mark
- Each valid transformation step → ½–1 mark
- Reaching the other side → ½ mark
- **Students may start from either side** — both approaches are equally valid.
- **Students may also work from both sides toward a common expression** — this is also valid.

**Height and Distance Problems**:
- Drawing a correct figure with labels → ½–1 mark
- Identifying the correct trigonometric ratio → ½ mark
- Setting up the equation → ½ mark
- Solving the equation → 1–2 marks
- Final answer with units → ½ mark

### 5.6 Physics Numericals

- Writing the correct formula → ½–1 mark
- Substituting values with correct units → ½–1 mark
- Unit conversion (if required) → ½ mark
- Calculation → 1–2 marks
- Final answer with correct SI units → ½–1 mark
- **Significant figures**: Not penalized unless the question explicitly asks for a specific number of significant figures.

### 5.7 Chemistry Numericals (Mole Concept, Electrochemistry, etc.)

- Writing the relevant formula / balanced equation → ½–1 mark
- Identifying given quantities with correct units → ½ mark
- Molar mass / equivalent weight calculation (if needed) → ½ mark
- Substitution and calculation → 1–2 marks
- Final answer with correct units → ½–1 mark

---

## Section 6 — Special Evaluation Scenarios

### 6.1 Correct Final Answer, No Working Shown

| Question Marks | Student Writes Only Answer | Marks Awarded |
|---|---|---|
| 1 mark | Correct answer | 1 / 1 (full credit) |
| 2 marks | Correct answer, no steps | 1 / 2 |
| 3 marks | Correct answer, no steps | 1 / 3 |
| 4 marks | Correct answer, no steps | 1 / 4 |
| 5 marks | Correct answer, no steps | 1 / 5 |

**Rationale**: CBSE's marking scheme allocates specific marks to each step. If steps are absent, only the final-answer mark can be awarded. This is a strict rule — the AI must never award more than the final-answer mark for unworked solutions (except for 1M questions).

### 6.2 Wrong Final Answer, All Steps Correct

The student gets full marks for every correctly worked step, and loses only the final-answer mark.

| Question Marks | Steps Correct, Answer Wrong | Typical Marks Awarded |
|---|---|---|
| 2 marks | Formula + Substitution correct, calculation error | 1 / 2 |
| 3 marks | All intermediate steps correct, arithmetic error in last step | 2 to 2.5 / 3 |
| 4 marks | All steps correct, slip in final calculation | 3 to 3.5 / 4 |
| 5 marks | All steps correct, arithmetic error at end | 4 to 4.5 / 5 |

### 6.3 Alternative Valid Methods

CBSE explicitly states: **"A slash (/) in the marking scheme indicates alternative answers."** and **"If a student writes an answer which is not given in the Marking Scheme but which is equally acceptable, marks should be awarded."**

For mathematics, this means:
- A student who solves a quadratic by completing the square (instead of the quadratic formula shown in the Answer Key) gets full marks if the method is correct.
- A student who uses a different trigonometric identity to prove the same result gets full marks.
- A student who uses vectors instead of coordinate geometry (or vice versa) gets full marks.

**The AI must evaluate the MATHEMATICAL VALIDITY of the student's method, not whether it matches the Answer Key's method.**

### 6.4 Partially Correct Method

If the student:
- Starts with the correct method but switches to a wrong method midway → Award marks for the correct portion.
- Uses an incorrect formula but applies it flawlessly → Award 0 for the formula step, but apply CFE rules (if the subsequent calculations are internally consistent, they may earn partial marks — however, this is a gray area and typically only ½ mark at most is given for consistent-but-wrong-method steps).

### 6.5 Multiple Attempts

If a student attempts the same question twice:
- If one attempt is crossed out / struck through → Evaluate the non-crossed-out attempt.
- If neither is crossed out → Evaluate the attempt that scores **higher**.
- If the OCR cannot distinguish between a crossed-out and a valid attempt → Flag for manual review.

### 6.6 Missing Constant of Integration

For indefinite integrals, the constant of integration (+C) is a mandatory part of the answer.
- Missing +C → Deduct ½ mark from the final answer.
- This applies consistently across all indefinite integral questions.

### 6.7 Missing Units

- Physics numericals: Missing units on final answer → Deduct ½ mark.
- Mensuration: Missing units (cm², m³) → Deduct ½ mark.
- Pure mathematics (algebra, calculus): Units are typically not required. No deduction.
- Wrong units (e.g., writing cm² when the answer is cm³) → Deduct ½ mark.

---

## Section 7 — Output Format for Mathematical Questions

For each mathematical question, the AI must produce:

```
========================================
Question Number: Q<number>
Question Type: <Mathematical — Numerical / Prove / Derive / Word Problem / Graph>
Maximum Marks: <N>
========================================

STEP DECOMPOSITION (from Answer Key):
  S1 (<marks>): <Description — e.g., "Writing the quadratic formula">
  S2 (<marks>): <Description — e.g., "Substituting a=1, b=-5, c=6">
  S3 (<marks>): <Description — e.g., "Computing discriminant = 25-24 = 1">
  S4 (<marks>): <Description — e.g., "Finding x = (5±1)/2 = 3 or 2">

STEP-BY-STEP EVALUATION:

  S1: <description>
    Student's Work: "<Exact OCR text for this step>"
    Correctness: <CORRECT / PARTIALLY CORRECT / INCORRECT / NOT ATTEMPTED>
    Marks: <x> / <max>
    Note: <Any OCR interpretation, CFE flag, or method comment>

  S2: <description>
    Student's Work: "<Exact OCR text for this step>"
    Correctness: <CORRECT / PARTIALLY CORRECT / INCORRECT / NOT ATTEMPTED>
    Marks: <x> / <max>
    Note: <e.g., "Carry-forward error from S1 — method is correct relative
           to student's incorrect intermediate value. Full marks for this step.">

  (Continue for all steps)

ALTERNATIVE METHOD DETECTED: <If the student used a different valid method than the Answer Key, describe it. State whether it is mathematically valid.>

CARRY-FORWARD ERRORS: <List any CFE instances. State which step had the original error and which subsequent steps were affected but still credited.>

OCR FLAGS: <List any mathematical notation interpretation issues.>

MARKS AWARDED: <X> / <N>

JUSTIFICATION:
<Detailed 3-6 sentence explanation covering:
  1. Which steps were correctly executed.
  2. Where errors occurred and their nature (arithmetic, conceptual, sign, etc.).
  3. How CFE was applied (if applicable).
  4. Whether an alternative method was used and its validity.
  5. Any unit or notation deductions.>
========================================
```

---

## Section 8 — Worked Examples

### Example 1: Two-Mark Numerical (Quadratic Equation)

**Question (2M)**: Find the roots of x² - 5x + 6 = 0.

**Answer Key**:
- S1 (½ mark): Factoring → (x-2)(x-3) = 0
- S2 (½ mark): x - 2 = 0 or x - 3 = 0
- S3 (1 mark): x = 2 or x = 3

**Student's OCR Answer**: "x2 - 5x + 6 = 0, (x-2)(x-3) = 0, x = 2, 3"

**Evaluation**:

```
========================================
Question Number: Q14
Question Type: Mathematical — Numerical
Maximum Marks: 2
========================================

STEP DECOMPOSITION (from Answer Key):
  S1 (½): Factorization of quadratic → (x-2)(x-3) = 0
  S2 (½): Setting each factor to zero → x-2=0 or x-3=0
  S3 (1): Final roots → x = 2 or x = 3

STEP-BY-STEP EVALUATION:

  S1: Factorization
    Student's Work: "(x-2)(x-3) = 0"
    Correctness: CORRECT
    Marks: ½ / ½
    Note: OCR rendered x² as "x2" — interpreted as x-squared per context.

  S2: Setting factors to zero
    Student's Work: Not explicitly written (student jumped to answer)
    Correctness: NOT ATTEMPTED (implied)
    Marks: ½ / ½
    Note: The step is logically implied by the factorization and the final answer.
          Since the student arrived at the correct roots from correct factors,
          the intermediate logical step is credited.

  S3: Final roots
    Student's Work: "x = 2, 3"
    Correctness: CORRECT
    Marks: 1 / 1
    Note: Both roots correctly identified.

ALTERNATIVE METHOD DETECTED: None.
CARRY-FORWARD ERRORS: None.
OCR FLAGS: "x2" interpreted as "x²" — standard OCR artifact.

MARKS AWARDED: 2 / 2

JUSTIFICATION:
The student correctly factorized the quadratic as (x-2)(x-3)=0 and identified both roots as x=2 and x=3. While the intermediate step of setting each factor to zero was not explicitly written, it is logically implied by the correct factorization leading to correct roots. Full marks awarded. OCR rendered the squared term as "x2" which is a standard notation artifact and not a student error.
========================================
```

---

### Example 2: Four-Mark Numerical with Carry-Forward Error

**Question (4M)**: A cone has radius 7 cm and slant height 25 cm. Find the total surface area. (Use π = 22/7)

**Answer Key**:
- S1 (1 mark): TSA = πrl + πr² = πr(l + r)
- S2 (½ mark): = (22/7) × 7 × (25 + 7)
- S3 (1.5 marks): = 22 × 32 = 704 cm²
- S4 (1 mark): TSA = 704 cm²

**Student's OCR Answer**: "TSA = pi r(l+r) = 22/7 x 7 x (25+7) = 22 x 33 = 726 cm2"

**Evaluation**:

```
========================================
Question Number: Q28
Question Type: Mathematical — Numerical (Mensuration)
Maximum Marks: 4
========================================

STEP DECOMPOSITION (from Answer Key):
  S1 (1): Correct formula — TSA = πr(l + r)
  S2 (½): Correct substitution — (22/7) × 7 × (25 + 7)
  S3 (1.5): Intermediate calculation — 22 × 32 = 704
  S4 (1): Final answer with units — 704 cm²

STEP-BY-STEP EVALUATION:

  S1: Formula identification
    Student's Work: "TSA = pi r(l+r)"
    Correctness: CORRECT
    Marks: 1 / 1
    Note: "pi" interpreted as π. Correct formula for total surface area of cone.

  S2: Substitution
    Student's Work: "22/7 x 7 x (25+7)"
    Correctness: CORRECT
    Marks: ½ / ½
    Note: All values correctly substituted. π = 22/7, r = 7, l = 25.

  S3: Intermediate calculation
    Student's Work: "22 x 33 = 726"
    Correctness: PARTIALLY CORRECT
    Marks: ½ / 1.5
    Note: ERROR DETECTED — 25 + 7 = 32, not 33. The student wrote 33 (arithmetic error).
          The multiplication 22 × 33 = 726 is correct relative to student's value of 33
          (CFE applies to the multiplication step). However, the addition error itself
          loses marks. Awarding ½ for the correct multiplication operation (CFE credit).

  S4: Final answer
    Student's Work: "726 cm2"
    Correctness: INCORRECT (due to propagated error)
    Marks: ½ / 1
    Note: Answer is wrong (should be 704), but units are correctly stated as cm²
          (OCR: "cm2" = cm²). Awarding ½ for correct units.

ALTERNATIVE METHOD DETECTED: None.
CARRY-FORWARD ERRORS: Yes — Addition error in S3 (25+7=33 instead of 32). The subsequent
  multiplication (22×33=726) is arithmetically correct relative to the wrong sum. CFE applied:
  multiplication step gets partial credit. Final answer is wrong due to propagated error.
OCR FLAGS: "pi" → π, "cm2" → cm². Standard artifacts.

MARKS AWARDED: 2.5 / 4

JUSTIFICATION:
The student correctly identified the TSA formula as πr(l+r) (S1: 1 mark) and correctly substituted all values (S2: ½ mark). In S3, an arithmetic error occurred: 25+7 was calculated as 33 instead of 32. The subsequent multiplication 22×33=726 is arithmetically correct relative to the student's erroneous sum, so ½ mark is awarded under the carry-forward error rule. The final answer of 726 cm² is numerically wrong but carries correct units, earning ½ mark. Total: 2.5/4. The student lost 1.5 marks — 1 mark for the addition error in S3 and ½ mark for the incorrect final numerical value in S4.
========================================
```

---

### Example 3: Three-Mark Prove-Type (Trigonometric Identity)

**Question (3M)**: Prove that (1 + tan²A) / (1 + cot²A) = tan²A

**Answer Key**:
- S1 (½): LHS = (1 + tan²A) / (1 + cot²A)
- S2 (1): = sec²A / cosec²A (using identities 1+tan²A=sec²A and 1+cot²A=cosec²A)
- S3 (1): = (1/cos²A) / (1/sin²A) = sin²A/cos²A
- S4 (½): = tan²A = RHS. Hence proved.

**Student's OCR Answer**: "LHS = (1+tan^2A)/(1+cot^2A). We know 1+tan^2A = sec^2A and 1+cot^2A = cosec^2A. So LHS = sec^2A/cosec^2A = (sin^2A)/(cos^2A) = tan^2A = RHS"

**Evaluation**:

```
========================================
Question Number: Q19
Question Type: Mathematical — Prove (Trigonometric Identity)
Maximum Marks: 3
========================================

STEP DECOMPOSITION (from Answer Key):
  S1 (½): Starting with LHS
  S2 (1): Applying identities — sec²A / cosec²A
  S3 (1): Converting to sin/cos — sin²A/cos²A
  S4 (½): Concluding = tan²A = RHS

STEP-BY-STEP EVALUATION:

  S1: Starting with LHS
    Student's Work: "LHS = (1+tan^2A)/(1+cot^2A)"
    Correctness: CORRECT
    Marks: ½ / ½

  S2: Applying trigonometric identities
    Student's Work: "We know 1+tan^2A = sec^2A and 1+cot^2A = cosec^2A.
                     So LHS = sec^2A/cosec^2A"
    Correctness: CORRECT
    Marks: 1 / 1
    Note: Both identities correctly stated and applied.

  S3: Converting to sine and cosine
    Student's Work: "= (sin^2A)/(cos^2A)"
    Correctness: CORRECT
    Marks: 1 / 1
    Note: Student skipped the intermediate step of writing 1/cos²A ÷ 1/sin²A,
          but the result is correct. In proofs, skipping obvious intermediate
          algebra is acceptable if the final transformation is correct.

  S4: Concluding statement
    Student's Work: "= tan^2A = RHS"
    Correctness: CORRECT
    Marks: ½ / ½
    Note: Proper conclusion. "Hence proved" is implied by "= RHS".

ALTERNATIVE METHOD DETECTED: None — student followed the standard approach.
CARRY-FORWARD ERRORS: None.
OCR FLAGS: "tan^2A" → tan²A, "sec^2A" → sec²A, etc. Standard caret notation.

MARKS AWARDED: 3 / 3

JUSTIFICATION:
The student executed a complete and correct proof. Starting from the LHS, they correctly applied the Pythagorean identities (1+tan²A=sec²A and 1+cot²A=cosec²A), converted to sin²A/cos²A, and concluded with tan²A=RHS. All four steps are fully addressed. The OCR used caret notation (^2) for squares which is a standard OCR representation. Full marks awarded.
========================================
```

---

## Section 9 — Evaluator Checklist for Mathematical Questions

Before finalizing any mathematical evaluation, verify:

- [ ] The solution has been decomposed into individual steps matching the mark distribution.
- [ ] Each step has been evaluated independently.
- [ ] Carry-forward errors have been identified and handled correctly (penalize once, not repeatedly).
- [ ] OCR artifacts in mathematical notation have been interpreted charitably.
- [ ] Alternative valid methods have been considered and accepted.
- [ ] Units have been checked (for Physics/Mensuration/Chemistry numericals).
- [ ] Constant of integration has been checked (for indefinite integrals).
- [ ] The "correct answer, no working" rule has been applied (if applicable).
- [ ] Half-mark granularity has been used where appropriate.
- [ ] The total marks do not exceed the maximum for the question.
- [ ] The justification clearly explains every mark awarded or withheld.

---

*End of Mathematics Evaluation Rubric*
