# CBSE-Level Rubric for Evaluating Code-Based Answers
## For AI Answer Sheet Evaluator (ExamLens)

<!-- GRADER-DIRECTIVES:BEGIN -->
GRADING DIRECTIVES — CODE / PROGRAMMING / SQL

CBSE decomposes every code answer into 1/2-mark components. LOGIC ALWAYS OUTWEIGHS SYNTAX.

1. DECOMPOSE into components and award each independently. Standard CBSE splits:
   * function header 1/2 + opening the file correctly 1/2 + read/iterate logic 1/2-1 +
     the condition 1/2 + the computation 1/2 + the output/return 1/2.
   * SQL: correct statement/clause structure 1/2 + correct columns and table 1/2 +
     correct condition / join / grouping 1/2-1.
   Note: no marks for a function header that was already given in the question.
2. LOGIC EARNS ITS MARKS DESPITE SYNTAX SLIPS. A missing colon, wrong indentation, a misspelled
   keyword or method (retrun, apend), missing quotes, print without parentheses, [] instead of ()
   — each costs AT MOST 1/2 mark, and the TOTAL syntax penalty for one answer is CAPPED AT 25% of
   that question's marks. Indentation costs at most 1/2 in total, because OCR distorts it.
3. ANY CORRECT ALTERNATIVE IMPLEMENTATION earns full marks — a different loop, a different library
   call, a different but valid algorithm. Single and double quotes are identical. A missing
   semicolon in SQL, extra whitespace, and line breaks inside a query cost nothing.
4. NO CUMULATIVE PENALTY: one error is charged once, and later code that is correct relative to it
   still earns its marks.
5. PARTIALLY CORRECT OUTPUT earns partial marks. For "predict the output" questions award per
   correct line or value; a wrong or missing separator character costs at most 1/2. Minor
   whitespace differences in expected output are not penalised.
6. CHOOSING THE RIGHT ALTERNATIVE EARNS CREDIT. Where the key offers OR-alternatives, identifying
   and attempting the correct one is itself worth marks; grade the attempt against that alternative
   only. Getting some of a required tuple/list/string right earns the share for the parts that match.
7. NEVER ZERO an answer containing recognisably correct logic for part of the task. RESERVE ZERO
   for a blank answer, or code that solves an entirely different problem. Before reporting 0 on an
   answer that contains real code or output, re-check rules 2, 4 and 5 — a zero claims nothing in
   it was right.
<!-- GRADER-DIRECTIVES:END -->

---

## Document Purpose

This rubric governs how the AI evaluator handles **any question requiring the student to write, complete, debug, or trace code**. It is grounded in the actual CBSE Class 10/12 Computer Science (083) and Informatics Practices (065) marking schemes, which decompose every programming answer into fine-grained ½-mark components covering function headers, file operations, logic, loops, conditions, and output formatting.

The rubric is designed for **Python** (the language used in current CBSE board exams) but the evaluation principles extend to any programming language.

---

## Section 1 — Core Principles of Code Evaluation

### 1.1 CBSE's Official Code Evaluation Rules

These rules are extracted directly from CBSE marking scheme instructions:

1. **"All answers/codes are suggestive; any other alternative correct answers to be accepted."** — If a student's code produces the correct result using a different approach than the Answer Key, it gets full marks.

2. **String content is accepted within single quotes `' '` or double quotes `" "`.** — Never penalize a student for using `'hello'` vs `"hello"`.

3. **Step-wise marking applies.** Each component of the code (function header, file opening, reading/writing logic, condition checking, output) carries independent marks.

4. **Logic earns marks even with minor syntax errors.** If the student's code demonstrates correct algorithmic thinking but has a small syntax mistake (missing colon, wrong indentation in OCR, misspelled function name), the logic marks are still awarded.

5. **No marks deducted for cumulative effect of an error.** An error in one part of the code is penalized once. If subsequent code is logically correct relative to the error, those parts still earn marks.

### 1.2 The Three Pillars of Code Evaluation

Every code answer is assessed on three dimensions:

```
┌────────────────────────────────────────────────────────────┐
│              THE THREE PILLARS                             │
│                                                            │
│  PILLAR 1: LOGIC & ALGORITHM          (50-60% of marks)   │
│  Is the approach correct? Does the algorithm solve the     │
│  problem? Are the right data structures used? Is the       │
│  control flow (loops, conditions) appropriate?             │
│                                                            │
│  PILLAR 2: SYNTAX & LANGUAGE          (20-30% of marks)   │
│  Is the code syntactically valid? Are Python keywords,     │
│  functions, and methods used correctly? Is indentation     │
│  correct?                                                  │
│                                                            │
│  PILLAR 3: COMPLETENESS & OUTPUT      (10-20% of marks)   │
│  Does the code address all parts of the question? Is the   │
│  output format correct? Are edge cases handled (if         │
│  required by the question)?                                │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

**Critical Rule**: Logic ALWAYS outweighs syntax. A student who demonstrates perfect algorithmic thinking with 2 minor syntax errors scores significantly higher than a student who writes syntactically perfect code with fundamentally flawed logic.

---

## Section 2 — Code Question Categories in CBSE

### 2.1 Question Type Taxonomy

| Question Type | Typical Marks | What the Student Must Do | Key Evaluation Focus |
|---|---|---|---|
| **Write a function/program** | 2–5 marks | Write complete code from scratch | Logic + Syntax + Completeness |
| **Find and fix errors** | 2 marks | Identify and correct syntax/logic errors in given code | Error identification accuracy |
| **Predict the output** | 2–3 marks | Trace code execution and write the exact output | Trace accuracy + Output formatting |
| **Fill in the blanks** | 1–2 marks | Complete missing statements in partially written code | Contextual correctness |
| **Stack operations** | 3–4 marks | Implement push/pop/display using Python lists | Algorithm correctness + Edge cases |
| **File handling** | 3–5 marks | Read/write/process text, binary, or CSV files | File mode + Read/Write logic + Close |
| **SQL queries** | 1–4 marks | Write SQL SELECT/INSERT/UPDATE/DELETE statements | Query correctness + Syntax |
| **Python-SQL connectivity** | 4–5 marks | Write Python code to connect to and query MySQL | Connection + Cursor + Query + Fetch |

---

## Section 3 — Step-Marking Templates for Code Questions

### 3.1 "Write a Function/Program" Questions (2–5 Marks)

This is the most common code question type. CBSE decomposes the marking as follows:

**Template for a 3-Mark Function (e.g., File Handling)**:

Based on the actual CBSE marking scheme (Q29 from 2024-25 sample paper):

| Step | Component | Marks |
|---|---|---|
| S1 | Correct function header (`def function_name():` or with parameters) | ½ |
| S2 | Correctly opening the file (right filename, right mode) | ½ |
| S3 | Correctly reading from the file (`.read()`, `.readlines()`, etc.) | ½ |
| S4 | Correctly processing the data (splitting, iterating, filtering) | ½ |
| S5 | Correctly implementing the core logic (condition check, computation) | 1 |
| **Total** | | **3** |

**Template for a 4-Mark Program (e.g., SQL Connectivity)**:

Based on Q35 from CBSE 2024-25 marking scheme:

| Step | Component | Marks |
|---|---|---|
| S1 | Correctly importing the connector module | ½ |
| S2 | Correctly creating the connection object (host, user, password, database) | ½ |
| S3 | Correctly creating the cursor object | ½ |
| S4 | Correctly taking user input (if required) | ½ |
| S5 | Correctly constructing the SQL query | ½ |
| S6 | Correctly executing the query with commit (for INSERT/UPDATE/DELETE) | ½ |
| S7 | Correctly executing a SELECT query (if required) | ½ |
| S8 | Correctly displaying/fetching results | ½ |
| **Total** | | **4** |

**Template for a 3-Mark Stack Operations Question**:

Based on Q30 from CBSE 2024-25 marking scheme:

| Step | Component | Marks |
|---|---|---|
| S1 | Push function — correct use of `.append()` | 1 |
| S2 | Pop function — correct use of `.pop()` with underflow check | 1 |
| S3 | Display/Peek function — correct access with empty check | 1 |
| **Total** | | **3** |

Note: "No marks for any function header as it was a part of the question" — when the function signature is given in the question, the student gets no marks for merely repeating it.

**Template for a 5-Mark Binary File Program**:

Based on Q36 from CBSE 2024-25 marking scheme:

| Step | Component | Marks |
|---|---|---|
| S1 | Correct `import pickle` statement | ½ |
| S2 | Correct user input collection | ½ |
| S3 | Opening file in correct binary mode + using `pickle.dump()` | 1 |
| S4 | Opening file in read binary mode + using `pickle.load()` | 1 |
| S5 | Correct condition checking and data processing | 1 |
| S6 | Correct display of filtered/processed data | 1 |
| **Total** | | **5** |

### 3.2 "Predict the Output" Questions (2–3 Marks)

| Step | Component | Marks |
|---|---|---|
| Each correct line of output | Exact character-for-character match | 1 per line (typically) |
| Special character accuracy | Getting separators like `@`, `#`, `$` correct | ½ deducted per missing separator |

**Rules**:
- Each line of output is typically worth 1 mark.
- Missing or wrong separator characters (like `#`, `@`, `$`, `%`) → deduct ½ mark.
- Extra spaces that don't appear in the expected output → deduct ½ mark (but be lenient with trailing spaces from OCR).
- Output must match **exactly** including spacing, case, and special characters.

### 3.3 "Find and Fix Errors" Questions (2 Marks)

| Step | Component | Marks |
|---|---|---|
| Each correctly identified and fixed error | Must show both the error and the correction | ½ per error |

Typical question has 4 errors → ½ mark each = 2 marks total.

**Rules**:
- Student must **underline** or clearly indicate each correction. Since OCR cannot capture underlining, the AI should look for the corrected code and identify where changes were made by comparing against the original erroneous code.
- If the student correctly rewrites the code but doesn't explicitly mark the corrections, still award marks if the corrections are identifiable by comparison.
- Identifying the error without fixing it → 0 marks for that error.
- Fixing the error correctly but identifying the wrong line → still award marks (the fix is what matters).

### 3.4 "Fill in the Blanks" in Code (1–2 Marks)

| Step | Component | Marks |
|---|---|---|
| Each correct blank filled | Exact or functionally equivalent code | ½ to 1 per blank |

**Rules**:
- Accept any functionally equivalent code that produces the same behavior.
- For file mode blanks: `'r'`, `"r"`, and `r` (without quotes in some contexts) are all acceptable.
- For function names: Accept common aliases (e.g., `csv.writer()` and `csv.writer(f)` both acceptable if contextually correct).

### 3.5 SQL Query Questions (1–4 Marks)

| Step | Component | Marks |
|---|---|---|
| Each correct SQL query | Must be syntactically valid and produce correct result | 1 per query |

**Rules**:
- Keywords can be in ANY case: `SELECT`, `select`, `Select` — all accepted.
- Table and column names must match the given schema (but case-insensitive in MySQL context).
- Extra spaces, line breaks within the query → no penalty.
- Missing semicolon at end → no penalty (CBSE does not mandate it).
- `NATURAL JOIN` vs explicit `WHERE table1.col = table2.col` → both accepted.
- `BETWEEN x AND y` vs `>= x AND <= y` → both accepted.
- Column aliases (`AS`) → optional unless specifically asked.

---

## Section 4 — Logic Evaluation Framework

### 4.1 What Counts as "Correct Logic"

The AI evaluator must determine whether the student's code demonstrates correct **algorithmic thinking**, independent of syntax. This means:

```
CORRECT LOGIC means ALL of the following are true:
  1. The right APPROACH is used for the problem
     (e.g., using a loop to iterate through a list, not a single if-statement)
  
  2. The right DATA STRUCTURES are used
     (e.g., using a list for stack operations, not a string)
  
  3. The CONTROL FLOW is correct
     (e.g., loop runs the right number of times, conditions check the right thing)
  
  4. The ALGORITHM produces the correct output for the given/expected input
     (e.g., the filter condition matches what the question asks)
  
  5. EDGE CASES are handled if the question mentions them
     (e.g., underflow check for stack pop, empty file check)
```

### 4.2 Logic Error Categories

| Error Type | Severity | Mark Impact | Example |
|---|---|---|---|
| **Fundamental algorithm error** | CRITICAL | Lose all logic marks | Using bubble sort when binary search is asked |
| **Wrong condition** | MAJOR | Lose the condition mark | `if x > 5` instead of `if x >= 5` |
| **Off-by-one error** | MODERATE | Lose ½ mark | `range(1, n)` instead of `range(1, n+1)` |
| **Missing edge case** | MINOR | Lose ½ mark | No underflow check in stack pop |
| **Wrong operator** | MODERATE | Lose ½–1 mark | `+` instead of `*` in calculation |
| **Wrong return value** | MODERATE | Lose ½–1 mark | Returns `None` instead of the computed value |
| **Infinite loop** | MAJOR | Lose the loop mark | While condition never becomes False |

### 4.3 Evaluating Alternative Approaches

CBSE explicitly states: **"All answers/codes are suggestive; any other alternative correct answers to be accepted."**

The AI must verify whether an alternative approach:
1. **Produces the correct output** for the expected input(s).
2. **Handles the required cases** mentioned in the question.
3. **Uses valid Python syntax and constructs**.

If all three are true → **full marks**, regardless of how different it looks from the Answer Key.

**Common valid alternatives the AI must accept**:

| Answer Key Uses | Student Uses | Accept? |
|---|---|---|
| `for` loop | `while` loop | YES (if logic is correct) |
| `.read()` then `.split()` | `.readlines()` then loop | YES |
| `if not stack:` | `if len(stack) == 0:` | YES |
| `stack.append(x)` | `stack = stack + [x]` | YES (if logically equivalent) |
| `open('file.txt', 'r')` | `open("file.txt")` | YES (default mode is 'r') |
| List comprehension | Explicit loop with append | YES |
| f-string `f"{var}"` | `.format()` or `str(var)` | YES |
| `with open() as f:` | `f = open(); ... f.close()` | YES |
| `pickle.dump(data, f)` | Manual serialization | YES (if it works correctly) |
| `cursor.fetchall()` | `cursor.fetchone()` in a loop | YES (if all rows are retrieved) |

---

## Section 5 — Syntax Evaluation Framework

### 5.1 What Counts as a Syntax Error

In Python, syntax errors are errors that prevent the code from running. The AI must distinguish between:

**HARD SYNTAX ERRORS** (code cannot run at all):
- Missing colon after `def`, `if`, `for`, `while`, `else`, `elif`, `try`, `except`
- Unmatched parentheses, brackets, or braces
- Invalid assignment (e.g., `5 = x`)
- Using a reserved keyword as a variable name
- Missing `import` for required module

**SOFT SYNTAX ISSUES** (code runs but may not behave as intended):
- Wrong indentation level (but intention is clear from context)
- Missing `return` statement (function returns `None`)
- Using `=` instead of `==` in a condition (assignment vs comparison)
- Missing `self` parameter in class methods

### 5.2 Syntax Error Penalty Matrix

| Error Type | Penalty | Rationale |
|---|---|---|
| Missing colon (`:`) after function def/if/for/while | ½ mark deduction | Minor — intent is clear |
| Wrong indentation (but logic block is clear) | ½ mark deduction maximum for entire answer | OCR frequently distorts indentation |
| Misspelled Python keyword (e.g., `retrun` for `return`) | ½ mark deduction | Minor — intent is clear |
| Misspelled function/method name (e.g., `apend` for `append`) | ½ mark deduction | Minor — intent is clear |
| Wrong parentheses type (`[]` vs `()` for function call) | ½ mark deduction | Minor — intent is clear |
| Missing `import` for a required module | ½ mark deduction | Unless the question states "assume imported" |
| Using `print` without parentheses (Python 2 style) | ½ mark deduction | Accept if logic is correct |
| Capital letter errors in keywords (`Print` vs `print`) | ½ mark deduction | Common handwriting OCR issue |
| Missing quotes around string literals | ½ mark deduction | If the string content is identifiable |
| Using `;` instead of `:` | No penalty | OCR frequently confuses `;` and `:` |

### 5.3 Maximum Syntax Penalty Rule

**CRITICAL**: The total marks deducted for syntax errors in a single answer MUST NOT exceed **25% of the total marks** for that question.

Rationale: CBSE prioritizes logic over syntax. A student who demonstrates perfect algorithmic thinking with 3-4 minor syntax errors should not lose more than a quarter of the marks.

| Question Marks | Maximum Syntax Penalty |
|---|---|
| 2 marks | ½ mark maximum |
| 3 marks | 1 mark maximum |
| 4 marks | 1 mark maximum |
| 5 marks | 1.5 marks maximum |

---

## Section 6 — OCR Tolerance for Code

### 6.1 Code-Specific OCR Distortions

Handwritten code is especially prone to OCR errors. The AI must apply these interpretation rules:

| What Student Likely Wrote | OCR Might Produce | Interpretation Rule |
|---|---|---|
| `:` (colon) | `;` or `.` or `:` | In Python control flow context, always interpret as `:` |
| `=` (assignment) | `=`, `-`, `~` | If followed by a value/expression, interpret as `=` |
| `==` (comparison) | `==`, `= =`, `--` | In condition context (`if`/`while`), interpret as `==` |
| `!=` | `!=`, `! =`, `1=` | In condition context, interpret as `!=` |
| `()` (parentheses) | `()`, `{}`, `[]` (sometimes confused) | Interpret based on context (function call vs dict vs list) |
| `_` (underscore) | `_`, `-`, `—` | In variable names, interpret as `_` |
| `#` (comment) | `#`, `≠`, number sign | At start of line or after code, interpret as comment marker |
| Indentation (4 spaces/tab) | Varies wildly | See Section 6.2 |
| `0` (zero) vs `O` (letter O) | Context-dependent | In numeric context → `0`. In variable name → `O` |
| `1` (one) vs `l` (lowercase L) | Context-dependent | In numeric context → `1`. In variable name → `l` |
| `"` vs `'` | Often confused | Both are valid in Python — accept either |
| `\n` (newline escape) | `\n`, `In`, `\N` | In string/print context, interpret as `\n` |

### 6.2 Indentation Handling

Indentation is **semantic** in Python (it defines code blocks), but OCR of handwritten code almost never preserves indentation correctly. The AI must:

1. **Attempt to reconstruct logical indentation** from the code structure:
   - Lines after `def`, `if`, `for`, `while`, `with`, `try`, `else`, `elif`, `except` are indented.
   - Lines after `return`, `break`, `continue` return to the previous indentation level.

2. **Apply the "benefit of doubt" rule**: If the code's logic is clear from the control flow keywords even though OCR indentation is wrong, evaluate based on the **intended** indentation.

3. **Deduct at most ½ mark** for indentation issues in the entire answer, regardless of how many lines are mis-indented, because this is predominantly an OCR problem, not a student error.

### 6.3 Comment Handling

- Comments (`# ...`) in the student's code should be **ignored during evaluation**. They do not earn marks and should not lose marks.
- If OCR garbles a comment, ignore it entirely.
- If a student writes a pseudo-code comment explaining their logic but doesn't write the actual code → award partial logic marks (up to ½ the logic marks) if the algorithm described is correct.

---

## Section 7 — Category-Specific Evaluation Rules

### 7.1 File Handling Programs

**Text File Operations** (most common in CBSE):

| Component | Marks | What to Check |
|---|---|---|
| Function definition with correct parameters | ½ | `def func():` or `def func(filename):` |
| Opening file in correct mode | ½ | `'r'` for reading, `'w'` for writing, `'a'` for appending |
| Reading correctly | ½ | `.read()`, `.readline()`, or `.readlines()` — any valid method |
| Processing/splitting text | ½ | `.split()`, `.split('\n')`, or iterating through lines |
| Core logic (filtering, counting, etc.) | 1 | The condition/computation the question asks for |
| Correct output/display | ½ | `print()` with correct content |
| Closing the file (or using `with`) | 0 | **Not separately marked in CBSE** — no deduction for missing `.close()` if `with` is used, and vice versa |

**Binary File Operations**:

| Component | Marks | What to Check |
|---|---|---|
| `import pickle` | ½ | Must be present (either at top or inside function) |
| Opening in correct binary mode | ½ | `'rb'` for reading, `'wb'` for writing, `'ab'` for appending |
| Correct use of `pickle.dump()` | ½ | For writing data |
| Correct use of `pickle.load()` | ½ | For reading data — must be in a loop with `EOFError` handling |
| `EOFError` exception handling | ½ | `try/except EOFError` — essential for binary file reading |
| Core processing logic | 1 | Whatever filtering/updating the question requires |

**CSV File Operations**:

| Component | Marks | What to Check |
|---|---|---|
| `import csv` | ½ | Note: CBSE says "Ignore import csv as it may be considered part of the complete program" — but award ½ if present, don't deduct if absent |
| Opening file correctly (with `newline=''`) | ½ | `newline=''` parameter is important but may be missed |
| Creating reader/writer object | ½ | `csv.reader(f)` or `csv.writer(f)` |
| Skipping header row (if needed) | 0 | CBSE says "Ignore `next(records, None)` as the file may or may not have the Header Row" — no marks awarded or deducted |
| Core processing logic | 1 | Filtering, counting, displaying as asked |

### 7.2 Stack Implementation Programs

| Component | Marks | What to Check |
|---|---|---|
| **Push operation** | 1 | Uses `.append()` to add element to list. Must add to the END of the list (LIFO). |
| **Pop operation** | 1 | Uses `.pop()` to remove and return last element. MUST include underflow check (`if not stack:` or `if len(stack) == 0:` → print "Underflow"). |
| **Display/Peek operation** | 1 | Shows top element (`stack[-1]`) or all elements. MUST include empty stack check. |

**Common errors to watch for**:
- Using `.pop(0)` instead of `.pop()` → This is a QUEUE, not a STACK → lose the pop mark.
- Using `.insert(0, x)` instead of `.append(x)` → This is wrong for LIFO → lose the push mark.
- Missing underflow/empty check → lose ½ mark from the relevant function.

### 7.3 Python-MySQL Connectivity Programs

| Component | Marks | What to Check |
|---|---|---|
| `import mysql.connector` (or `as mycon`) | ½ | Any valid import alias is accepted |
| Connection object creation | ½ | `mycon.connect(host=..., user=..., passwd=..., database=...)` — all 4 parameters needed |
| Cursor object creation | ½ | `cursor = connection.cursor()` |
| User input (if required) | ½ | Correct `input()` with appropriate type conversion |
| Query construction | ½ | Must be a valid SQL query string with correct variable insertion |
| Query execution | ½ | `cursor.execute(query)` |
| `commit()` for INSERT/UPDATE/DELETE | ½ | `connection.commit()` — **mandatory for write operations** |
| Fetching and displaying results (for SELECT) | ½ | `cursor.fetchall()` or `cursor.fetchone()` in a loop with `print()` |

**Security note**: CBSE does not penalize for using string formatting instead of parameterized queries. Both `f"INSERT INTO table VALUES ({var})"` and `"INSERT INTO table VALUES (%s)"` with parameters are accepted.

### 7.4 SQL Query Writing

For each SQL query (typically 1 mark each):

| Criterion | Check |
|---|---|
| Correct SQL command | `SELECT` / `INSERT` / `UPDATE` / `DELETE` / `ALTER` as appropriate |
| Correct table name(s) | Must match the given schema |
| Correct column names | Must match the given schema |
| Correct `WHERE` condition (if needed) | Logic must produce the right filter |
| Correct aggregate function (if needed) | `SUM()`, `COUNT()`, `AVG()`, `MAX()`, `MIN()` |
| Correct `GROUP BY` / `HAVING` / `ORDER BY` (if needed) | Must be in correct position and reference correct columns |
| Correct `JOIN` condition (if joining tables) | `NATURAL JOIN` or explicit `ON`/`WHERE` condition |

**Accepted SQL variations**:
- Semicolon at end: optional (no penalty if missing).
- Keyword case: any case accepted (`SELECT` = `select` = `Select`).
- `*` vs listing all columns: both accepted for "display all details."
- `IS NULL` vs `= NULL`: technically `IS NULL` is correct SQL; `= NULL` is incorrect but if the logic intent is clear, deduct only ½ mark.

---

## Section 8 — Output Format for Code Questions

```
========================================
Question Number: Q<number>
Question Type: <Write Code / Predict Output / Fix Errors / Fill Blanks / SQL Query>
Maximum Marks: <N>
========================================

CODE DECOMPOSITION (from Answer Key):
  S1 (<marks>): <Component description>
  S2 (<marks>): <Component description>
  ...

STEP-BY-STEP EVALUATION:

  S1: <component description>
    Student's Code: "<Exact OCR text for this component>"
    Assessment: <CORRECT / PARTIALLY CORRECT / INCORRECT / NOT ATTEMPTED>
    Logic: <SOUND / FLAWED — brief description>
    Syntax: <VALID / MINOR ERROR(s) — list them>
    Marks: <x> / <max>
    Note: <OCR interpretation, alternative approach detected, etc.>

  S2: <component description>
    Student's Code: "<Exact OCR text>"
    Assessment: <...>
    Logic: <...>
    Syntax: <...>
    Marks: <x> / <max>
    Note: <...>

  (Continue for all steps)

ALTERNATIVE APPROACH DETECTED: <If the student used a different but valid approach,
  describe it and confirm it produces correct output. If none, write "None.">

SYNTAX ERROR SUMMARY:
  Total syntax errors found: <N>
  Errors: <List each error briefly>
  Total syntax penalty applied: <X> mark(s)
  (Capped at 25% of maximum marks per the maximum syntax penalty rule)

OCR FLAGS: <List any code-specific OCR artifacts interpreted.>

MARKS AWARDED: <X> / <N>

JUSTIFICATION:
<3-6 sentence detailed explanation covering:
  1. Which code components were correctly implemented.
  2. Where logic or syntax errors occurred.
  3. Whether alternative approaches were accepted.
  4. How OCR artifacts were handled.
  5. How the syntax penalty cap was applied (if relevant).>
========================================
```

---

## Section 9 — Worked Examples

### Example 1: File Handling Function (3 Marks)

**Question**: Write a function `show()` to read the file "Email.txt" and display all words containing `@cmail`.

**Answer Key**:
```python
def show():
    f = open("Email.txt", 'r')
    data = f.read()
    words = data.split()
    for word in words:
        if '@cmail' in word:
            print(word, end=' ')
    f.close()
```

**Marking Scheme**:
- S1 (½): Correct function header
- S2 (½): Correctly opening the file
- S3 (½): Correctly reading from the file
- S4 (½): Splitting the text into words
- S5 (1): Correctly displaying the desired words

**Student's OCR Answer**:
```
def show()
  f = open("Email.txt")
  data = f.read()
  words = data.split()
  for word in words;
    if '@cmail' in word;
      print(word)
  f.close()
```

**Evaluation**:

```
========================================
Question Number: Q29
Question Type: Write Code (File Handling)
Maximum Marks: 3
========================================

CODE DECOMPOSITION (from Answer Key):
  S1 (½): Correct function header — def show():
  S2 (½): Opening file in read mode — open("Email.txt", 'r')
  S3 (½): Reading file content — f.read()
  S4 (½): Splitting into words — data.split()
  S5 (1): Filtering and displaying words containing '@cmail'

STEP-BY-STEP EVALUATION:

  S1: Function header
    Student's Code: "def show()"
    Assessment: PARTIALLY CORRECT
    Logic: SOUND — correct function name and structure
    Syntax: MINOR ERROR — missing colon after `def show()`
    Marks: ½ / ½
    Note: Missing colon is a minor syntax error. The ½ mark syntax
          penalty is tracked but applied globally, not per-step for
          function headers. Logic is correct, so the component mark
          is awarded.

  S2: Opening file
    Student's Code: "f = open("Email.txt")"
    Assessment: CORRECT
    Logic: SOUND
    Syntax: VALID — default mode is 'r', so omitting it is correct
    Marks: ½ / ½
    Note: Omitting 'r' mode is valid Python — open() defaults to read mode.

  S3: Reading file content
    Student's Code: "data = f.read()"
    Assessment: CORRECT
    Logic: SOUND
    Syntax: VALID
    Marks: ½ / ½

  S4: Splitting into words
    Student's Code: "words = data.split()"
    Assessment: CORRECT
    Logic: SOUND
    Syntax: VALID
    Marks: ½ / ½

  S5: Filtering and displaying
    Student's Code: "for word in words;\n  if '@cmail' in word;\n    print(word)"
    Assessment: PARTIALLY CORRECT
    Logic: SOUND — correct iteration, correct condition, correct print
    Syntax: MINOR ERRORS — semicolons instead of colons after `for` and `if`
    Marks: 1 / 1
    Note: OCR likely misread colons as semicolons — a very common artifact.
          The logic for filtering words containing '@cmail' is correct.
          Full logic marks awarded. The student also omitted `end=' '`
          in print(), but this only affects output formatting (words on
          separate lines vs same line) — not the core logic. No deduction
          for missing end parameter unless the question explicitly requires
          specific output formatting.

ALTERNATIVE APPROACH DETECTED: None — student followed the standard approach.

SYNTAX ERROR SUMMARY:
  Total syntax errors found: 3
  Errors: (1) Missing colon after def show(), (2) semicolon instead of colon
           after for, (3) semicolon instead of colon after if
  Total syntax penalty applied: ½ mark
  (3 errors but capped at maximum 1 mark for a 3-mark question.
   Applied ½ mark since errors (2) and (3) are likely OCR artifacts
   — colons misread as semicolons.)

OCR FLAGS: Semicolons (;) in positions where colons (:) are expected —
  interpreted as OCR misreading of colons. Indentation inconsistent —
  likely OCR artifact, interpreted based on code structure.

MARKS AWARDED: 2.5 / 3

JUSTIFICATION:
The student's code demonstrates correct logic across all components:
correct function definition, file opening (default read mode is valid),
reading, splitting, and filtering words containing '@cmail'. Three syntax
errors were found: a missing colon after the function header, and
semicolons where colons should appear in the for-loop and if-statement.
The semicolons are very likely OCR artifacts (handwritten colons frequently
misread as semicolons). A ½ mark syntax penalty is applied. The missing
`end=' '` in print() is not penalized as the core filtering logic is correct
and output format was not explicitly specified in the question.
========================================
```

### Example 2: Stack Operations (3 Marks)

**Question**: Write the following functions for a stack named `BooksStack`:
(i) `push_book(BooksStack, new_book)` — push a book
(ii) `pop_book(BooksStack)` — pop and return, print "Underflow" if empty
(iii) `peep(BooksStack)` — display top element, print "None" if empty

**Student's OCR Answer**:
```
def push_book(BooksStack, new_book):
  BooksStack.append(new_book)

def pop_book(BooksStack):
  if BooksStack == []:
    print("Underflow")
  else:
    return BooksStack.pop()

def peep(BooksStack):
  if len(BooksStack) > 0:
    print(BooksStack[-1])
  else:
    print("None")
```

**Evaluation**:

```
========================================
Question Number: Q30
Question Type: Write Code (Stack Operations)
Maximum Marks: 3
========================================

CODE DECOMPOSITION (from Answer Key):
  S1 (1): Push function — correct use of .append()
  S2 (1): Pop function — correct .pop() with underflow check
  S3 (1): Peep function — correct top element access with empty check

STEP-BY-STEP EVALUATION:

  S1: Push function
    Student's Code: "def push_book(BooksStack, new_book):\n  BooksStack.append(new_book)"
    Assessment: CORRECT
    Logic: SOUND — appends to end of list (LIFO push)
    Syntax: VALID
    Marks: 1 / 1
    Note: No marks for function header (given in question). Full mark
          for correct .append() usage.

  S2: Pop function
    Student's Code: "def pop_book(BooksStack):\n  if BooksStack == []:\n    print(\"Underflow\")\n  else:\n    return BooksStack.pop()"
    Assessment: CORRECT
    Logic: SOUND — checks for empty stack, prints underflow message,
           pops and returns last element
    Syntax: VALID
    Marks: 1 / 1
    Note: Student used `BooksStack == []` instead of `not BooksStack`.
          This is a valid alternative — both correctly check for an empty list.

  S3: Peep function
    Student's Code: "def peep(BooksStack):\n  if len(BooksStack) > 0:\n    print(BooksStack[-1])\n  else:\n    print(\"None\")"
    Assessment: CORRECT
    Logic: SOUND — checks if stack has elements, prints top element
    Syntax: VALID
    Marks: 1 / 1
    Note: Student used `len(BooksStack) > 0` instead of `not BooksStack`.
          Valid alternative. Also correctly uses `[-1]` to access top.

ALTERNATIVE APPROACH DETECTED: Student used `BooksStack == []` and
  `len(BooksStack) > 0` for empty checks instead of `not BooksStack` and
  `not BooksStack`. Both are functionally equivalent and accepted.

SYNTAX ERROR SUMMARY:
  Total syntax errors found: 0
  Total syntax penalty applied: 0 marks

OCR FLAGS: No issues.

MARKS AWARDED: 3 / 3

JUSTIFICATION:
All three stack operations are correctly implemented. Push uses .append()
correctly. Pop includes proper underflow check and returns the popped
element. Peep correctly accesses the top element with BooksStack[-1] and
handles the empty stack case. The student used alternative but equally
valid methods for checking empty stacks (== [] and len() > 0 instead
of `not stack`). Full marks awarded.
========================================
```

### Example 3: Predict the Output (3 Marks)

**Question**: Find the output:
```python
def Alter(M, N=50):
    M = M + N
    N = M - N
    print(M, "@", N)
    return M

R = 25
S = 10
R = Alter(R, S)
print(R, "$", S)
```

**Answer Key**:
```
35 @ 25
35 $ 10
```

**Student's OCR Answer**: "35 @ 25\n35 S 10"

**Evaluation**:

```
========================================
Question Number: Q31
Question Type: Predict Output
Maximum Marks: 3
========================================

STEP-BY-STEP EVALUATION:

  Line 1 of output:
    Expected: "35 @ 25"
    Student wrote: "35 @ 25"
    Assessment: CORRECT
    Marks: 1 / 1

  Line 2 of output:
    Expected: "35 $ 10"
    Student wrote: "35 S 10"
    Assessment: PARTIALLY CORRECT
    Marks: ½ / 1
    Note: Student wrote "S" instead of "$". This could be an OCR
          artifact ($ misread as S) or a student error. Since "$" and
          "S" are visually similar in handwriting, benefit of doubt is
          applied. However, the separator character is part of the output
          accuracy requirement. ½ mark deducted for incorrect separator.

  Spacing/formatting:
    Assessment: CORRECT — line breaks match expected output

OCR FLAGS: "$" possibly misread as "S" — visually similar in handwriting.

MARKS AWARDED: 2.5 / 3

JUSTIFICATION:
The student correctly traced the code execution, getting both computed
values (35, 25, 35, 10) correct. Line 1 output matches exactly. In Line 2,
the separator "$" appears as "S" — likely an OCR artifact given the visual
similarity between handwritten "$" and "S". Per CBSE marking scheme,
½ mark is deducted for incorrect separator characters. The numerical
values in both lines are correct, demonstrating sound understanding of
parameter passing and variable scope.
========================================
```

---

## Section 10 — Evaluator Checklist for Code Questions

Before finalizing any code evaluation, verify:

- [ ] The code has been decomposed into CBSE-standard ½-mark components.
- [ ] Each component has been evaluated independently (step-wise marking).
- [ ] **Logic has been evaluated separately from syntax** — logic marks are not lost for syntax errors.
- [ ] The **maximum syntax penalty cap** (25% of total marks) has been applied.
- [ ] **Alternative correct approaches** have been considered and accepted.
- [ ] **OCR artifacts** in code (especially colons, indentation, parentheses) have been interpreted charitably.
- [ ] For SQL queries: keyword case, semicolons, and equivalent expressions are accepted.
- [ ] For file handling: both `with` and explicit `open/close` are accepted.
- [ ] For stack operations: alternative empty-check methods are accepted.
- [ ] The justification clearly explains every mark awarded or withheld.
- [ ] The total marks do not exceed the maximum.

---

*End of Code Evaluation Rubric*
