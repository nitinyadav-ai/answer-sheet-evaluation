# """Per-page parallel parsing for question papers + answer keys.

# The single-call parsers (one LLM call over the whole document's text) are SLOW on the flagship 235B
# model (~180s for a 3-page key) and 235B sometimes returns an empty {} for a whole question paper. This
# module splits a PDF into pages and extracts the questions/answers from each page CONCURRENTLY -- many
# small, fast, reliable calls -- then runs ONE global pass to detect the things that need the whole-paper
# view (metadata + internal choices). Net: the accurate 235B model becomes fast enough to use, and each
# call is a small, easy task.

# Shared by extract_json_from_key.py and extract_json_from_question_paper.py. Gated by
# PARSER_PARALLEL_PAGES (default on); falls back to the caller's single-call path for docx / 1-page PDFs /
# when disabled, so nothing regresses.
# """
# import os
# import re
# import json
# import concurrent.futures

# from llm_client import generate, strip_reasoning


# def parallel_enabled():
#     return os.environ.get("PARSER_PARALLEL_PAGES", "1").strip().lower() not in ("0", "false", "no", "off")


# def pdf_page_texts(path):
#     """List of per-page extracted text (one string per PDF page). Text layer only (same as the
#     single-call extractor); a scanned/no-text page yields ''."""
#     import PyPDF2
#     pages = []
#     with open(path, "rb") as f:
#         reader = PyPDF2.PdfReader(f)
#         for pg in reader.pages:
#             try:
#                 pages.append(pg.extract_text() or "")
#             except Exception:
#                 pages.append("")
#     return pages


# def _sanitize_json_escapes(s):
#     """Double any backslash that is not a valid JSON escape, so LaTeX-heavy math (\\frac, \\sqrt) parses
#     instead of raising 'Invalid \\escape'. Mirrors extract_json_from_key._sanitize_json_escapes."""
#     out, i, n = [], 0, len(s)
#     while i < n:
#         c = s[i]
#         if c != "\\":
#             out.append(c); i += 1; continue
#         nxt = s[i + 1] if i + 1 < n else ""
#         if nxt in '"\\/':
#             out.append("\\" + nxt); i += 2
#         elif nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", s[i + 2:i + 6] or ""):
#             out.append(s[i:i + 6]); i += 6
#         elif nxt == "n":
#             out.append("\\n"); i += 2
#         elif nxt in "tfbr" and not (i + 2 < n and s[i + 2].isalpha()):
#             out.append("\\" + nxt); i += 2
#         else:
#             out.append("\\\\"); i += 1
#     return "".join(out)


# def tolerant_json(text):
#     """Parse model JSON, tolerating <think> blocks, unescaped LaTeX backslashes, and leading/trailing
#     noise. Returns {} on total failure (a page with no parseable questions is not fatal)."""
#     content = strip_reasoning((text or "").strip())
#     for candidate in (content, _sanitize_json_escapes(content)):
#         try:
#             return json.loads(candidate)
#         except json.JSONDecodeError:
#             m = re.search(r"(\{.*\})", candidate, re.DOTALL)
#             if m:
#                 try:
#                     return json.loads(m.group(1))
#                 except json.JSONDecodeError:
#                     continue
#     return {}


# def _questions_of(obj):
#     if isinstance(obj, dict) and isinstance(obj.get("questions"), dict):
#         return obj["questions"]
#     return obj if isinstance(obj, dict) else {}


# def extract_pages_parallel(page_texts, prompt_template, model, max_tokens, max_workers=None):
#     """Extract questions from every page CONCURRENTLY. `prompt_template` contains a `{page_text}`
#     placeholder and must ask for {"questions": {...}} for THAT page only. Returns
#     (merged_questions_dict, total_in_tokens, total_out_tokens). Pages with no questions contribute
#     nothing; a failed page is skipped (never aborts the whole parse)."""
#     workers = int(max_workers or os.environ.get("PARSER_MAX_WORKERS", "8"))
#     non_empty = [(i, t) for i, t in enumerate(page_texts) if (t or "").strip()]

#     def one_page(idx, text):
#         prompt = prompt_template.replace("{page_text}", text)
#         try:
#             out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
#                                          max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#             return _questions_of(tolerant_json(out)), int(i_tok or 0), int(o_tok or 0)
#         except Exception:
#             return {}, 0, 0

#     per_page = [None] * len(non_empty)
#     with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(non_empty) or 1)) as ex:
#         futs = {ex.submit(one_page, idx, text): pos for pos, (idx, text) in enumerate(non_empty)}
#         for fut in concurrent.futures.as_completed(futs):
#             per_page[futs[fut]] = fut.result()

#     merged, in_tok, out_tok = {}, 0, 0
#     for res in per_page:                                # keep page order for stable cross-page merge
#         if not res:
#             continue
#         qs, i_tok, o_tok = res
#         in_tok += i_tok; out_tok += o_tok
#         if isinstance(qs, dict):
#             _merge_into(merged, qs)
#     return merged, in_tok, out_tok


# def _merge_into(acc, page_qs):
#     """Merge one page's questions into the accumulator. A question id that appears on two pages (a
#     long answer that spilled across a page break) is COMBINED: text/answer concatenated, marks kept as
#     the MAX seen (never summed -- so a continuation can't double the marks)."""
#     for qid, v in page_qs.items():
#         if not isinstance(v, dict):
#             continue
#         key = str(qid).strip()
#         if not key:
#             continue
#         if key not in acc:
#             acc[key] = dict(v)
#             acc[key].setdefault("question_id", key)
#             continue
#         prev = acc[key]
#         for fld in ("question", "answer"):
#             a, b = str(prev.get(fld, "") or ""), str(v.get(fld, "") or "")
#             if b and b not in a:
#                 prev[fld] = (a + ("\n" if a else "") + b).strip()
#         try:
#             prev["marks"] = max(float(prev.get("marks", 0) or 0), float(v.get("marks", 0) or 0))
#         except (TypeError, ValueError):
#             pass


# def global_metadata_pass(full_text, prompt_template, model, max_tokens):
#     """One call over the WHOLE paper to detect what needs global context: metadata (class, subject) +
#     internal choices (choice_groups / inline_choice_ids). `prompt_template` has a `{full_text}`
#     placeholder. Returns (metadata_dict, in_tok, out_tok); {} on failure (choices just won't be
#     applied, which the downstream structural detector + reconciler still catch)."""
#     prompt = prompt_template.replace("{full_text}", full_text)
#     try:
#         out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
#                                      max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#         data = tolerant_json(out)
#         meta = data.get("metadata", data) if isinstance(data, dict) else {}
#         if not isinstance(meta, dict):
#             meta = {}
#         return meta, int(i_tok or 0), int(o_tok or 0)
#     except Exception:
#         return {}, 0, 0


# """Per-page parallel parsing for question papers + answer keys.

# The single-call parsers (one LLM call over the whole document's text) are SLOW on the flagship 235B
# model (~180s for a 3-page key) and 235B sometimes returns an empty {} for a whole question paper. This
# module splits a PDF into pages and extracts the questions/answers from each page CONCURRENTLY -- many
# small, fast, reliable calls -- then runs ONE global pass to detect the things that need the whole-paper
# view (metadata + internal choices). Net: the accurate 235B model becomes fast enough to use, and each
# call is a small, easy task.

# Shared by extract_json_from_key.py and extract_json_from_question_paper.py. Gated by
# PARSER_PARALLEL_PAGES (default on); falls back to the caller's single-call path for docx / 1-page PDFs /
# when disabled, so nothing regresses.
# """
# import os
# import re
# import json
# import concurrent.futures

# from llm_client import generate, strip_reasoning


# def parallel_enabled():
#     return os.environ.get("PARSER_PARALLEL_PAGES", "1").strip().lower() not in ("0", "false", "no", "off")


# def pdf_page_texts(path):
#     """List of per-page extracted text (one string per PDF page). Text layer only (same as the
#     single-call extractor); a scanned/no-text page yields ''."""
#     import PyPDF2
#     pages = []
#     with open(path, "rb") as f:
#         reader = PyPDF2.PdfReader(f)
#         for pg in reader.pages:
#             try:
#                 pages.append(pg.extract_text() or "")
#             except Exception:
#                 pages.append("")
#     return pages


# def _sanitize_json_escapes(s):
#     """Double any backslash that is not a valid JSON escape, so LaTeX-heavy math (\\frac, \\sqrt) parses
#     instead of raising 'Invalid \\escape'. Mirrors extract_json_from_key._sanitize_json_escapes."""
#     out, i, n = [], 0, len(s)
#     while i < n:
#         c = s[i]
#         if c != "\\":
#             out.append(c); i += 1; continue
#         nxt = s[i + 1] if i + 1 < n else ""
#         if nxt in '"\\/':
#             out.append("\\" + nxt); i += 2
#         elif nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", s[i + 2:i + 6] or ""):
#             out.append(s[i:i + 6]); i += 6
#         elif nxt == "n":
#             out.append("\\n"); i += 2
#         elif nxt in "tfbr" and not (i + 2 < n and s[i + 2].isalpha()):
#             out.append("\\" + nxt); i += 2
#         else:
#             out.append("\\\\"); i += 1
#     return "".join(out)


# def tolerant_json(text):
#     """Parse model JSON, tolerating <think> blocks, unescaped LaTeX backslashes, and leading/trailing
#     noise. Returns {} on total failure (a page with no parseable questions is not fatal)."""
#     content = strip_reasoning((text or "").strip())
#     for candidate in (content, _sanitize_json_escapes(content)):
#         try:
#             return json.loads(candidate)
#         except json.JSONDecodeError:
#             m = re.search(r"(\{.*\})", candidate, re.DOTALL)
#             if m:
#                 try:
#                     return json.loads(m.group(1))
#                 except json.JSONDecodeError:
#                     continue
#     return {}


# def _questions_of(obj):
#     if isinstance(obj, dict) and isinstance(obj.get("questions"), dict):
#         return obj["questions"]
#     return obj if isinstance(obj, dict) else {}


# def extract_pages_parallel(page_texts, prompt_template, model, max_tokens, max_workers=None):
#     """Extract questions from every page CONCURRENTLY. `prompt_template` contains a `{page_text}`
#     placeholder and must ask for {"questions": {...}} for THAT page only. Returns
#     (merged_questions_dict, total_in_tokens, total_out_tokens). Pages with no questions contribute
#     nothing; a failed page is skipped (never aborts the whole parse)."""
#     workers = int(max_workers or os.environ.get("PARSER_MAX_WORKERS", "8"))
#     non_empty = [(i, t) for i, t in enumerate(page_texts) if (t or "").strip()]

#     def one_page(idx, text):
#         prompt = prompt_template.replace("{page_text}", text)
#         try:
#             out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
#                                          max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#             return _questions_of(tolerant_json(out)), int(i_tok or 0), int(o_tok or 0)
#         except Exception:
#             return {}, 0, 0

#     per_page = [None] * len(non_empty)
#     with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(non_empty) or 1)) as ex:
#         futs = {ex.submit(one_page, idx, text): pos for pos, (idx, text) in enumerate(non_empty)}
#         for fut in concurrent.futures.as_completed(futs):
#             per_page[futs[fut]] = fut.result()

#     merged, in_tok, out_tok = {}, 0, 0
#     for res in per_page:                                # keep page order for stable cross-page merge
#         if not res:
#             continue
#         qs, i_tok, o_tok = res
#         in_tok += i_tok; out_tok += o_tok
#         if isinstance(qs, dict):
#             _merge_into(merged, qs)
#     return merged, in_tok, out_tok


# def _merge_into(acc, page_qs):
#     """Merge one page's questions into the accumulator. A question id that appears on two pages (a
#     long answer that spilled across a page break) is COMBINED: text/answer concatenated, marks kept as
#     the MAX seen (never summed -- so a continuation can't double the marks)."""
#     for qid, v in page_qs.items():
#         if not isinstance(v, dict):
#             continue
#         key = str(qid).strip()
#         if not key:
#             continue
#         if key not in acc:
#             acc[key] = dict(v)
#             acc[key].setdefault("question_id", key)
#             continue
#         prev = acc[key]
#         for fld in ("question", "answer"):
#             a, b = str(prev.get(fld, "") or ""), str(v.get(fld, "") or "")
#             if b and b not in a:
#                 prev[fld] = (a + ("\n" if a else "") + b).strip()
#         try:
#             prev["marks"] = max(float(prev.get("marks", 0) or 0), float(v.get("marks", 0) or 0))
#         except (TypeError, ValueError):
#             pass


# def global_metadata_pass(full_text, prompt_template, model, max_tokens):
#     """One call over the WHOLE paper to detect what needs global context: metadata (class, subject) +
#     internal choices (choice_groups / inline_choice_ids). `prompt_template` has a `{full_text}`
#     placeholder. Returns (metadata_dict, in_tok, out_tok); {} on failure (choices just won't be
#     applied, which the downstream structural detector + reconciler still catch)."""
#     prompt = prompt_template.replace("{full_text}", full_text)
#     try:
#         out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
#                                      max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#         data = tolerant_json(out)
#         meta = data.get("metadata", data) if isinstance(data, dict) else {}
#         if not isinstance(meta, dict):
#             meta = {}
#         return meta, int(i_tok or 0), int(o_tok or 0)
#     except Exception:
#         return {}, 0, 0


# # ---------------------------------------------------------------------------------------------------
# # SECTION-MARKS MAP -- a global, document-wide pass that reads ONLY this paper's own section headers
# # ("SECTION A -- MCQ (20 x 1 = 20)", "Section D consists of 4 questions ... Each question carries
# # 5 Marks", ...) and turns them into an authoritative {question-number-range -> per-question marks}
# # table. Nothing here is templated to any known board/pattern: every paper's section boundaries and
# # marks are read fresh from THAT paper's own printed headers, because different papers number their
# # sections completely differently (Class X Maths Section A is Q1-20 @ 1 mark; a CS paper's Section A
# # is Q1-21 @ 1 mark; a Science paper's Section B is Q21-26 @ 2 marks -- there is no fixed layout).
# #
# # Why this exists: the PER-PAGE parallel parser (extract_pages_parallel) has no visibility beyond one
# # page, so a question whose own page doesn't repeat its section's header (most commonly a page-break
# # case, or a question dominated by an embedded data table/figure that visually crowds out the header
# # context) can end up with the WRONG per-question marks, or 0. This pass sees the section headers
# # WHEREVER they appear across the whole document in one shot, so it cannot be fooled by a single
# # page's missing local context -- it becomes the tie-breaker the per-page pass is reconciled against.
# # ---------------------------------------------------------------------------------------------------
# SECTION_MARKS_PROMPT = """You are given the FULL text of an exam question paper. Find EVERY section
# header that states how many questions are in it and how many marks each one carries -- for example
# "SECTION A -- MCQ (20 x 1 = 20)", "Section B consists of 7 questions (22 to 28). Each question
# carries 2 Marks", "SECTION D (4 x 5 = 20 Marks)", "Section E -- Case Studies (3 x 4 = 12)", or any
# similar wording this specific paper uses.

# For EACH such section, work out the QUESTION NUMBER RANGE it covers (its first and last question
# number) and the MARKS PER QUESTION in that section -- using ONLY what is printed or stated in THIS
# paper. Do NOT assume any standard, typical, or previously-seen layout: different papers number their
# sections completely differently, so the range and marks must come from what is actually written here.

# - If a section states an explicit question range ("Q21 to Q25", "Questions 22 to 28", "36-38"), use
#   exactly that range.
# - If a section does not state its range explicitly, infer it from which question numbers are
#   physically listed under that header, up to (but not including) the next section header.
# - For a section made of multi-part / case-study questions (each WHOLE question is worth one total
#   even though its own sub-parts carry different sub-marks that add up to that total, e.g. "(i) 1 mark
#   (ii) 1 mark (iii) 2 marks" summing to 4), report the section's per-QUESTION total (4), not a
#   sub-part's marks.
# - If the paper has NO section structure at all (marks are printed individually next to every single
#   question, with no grouping headers), return an empty list -- do not invent sections.

# Return ONLY this JSON shape:
# {{"sections": [{{"from": <first question number as an integer>, "to": <last question number as an
# integer>, "marks": <marks per question in this section, as a number>, "label": "<the section name or
# heading exactly as printed>"}}, ...]}}
# ordered by "from". Return {{"sections": []}} if you cannot find this structure.

# FULL QUESTION PAPER TEXT:
# {full_text}

# Return ONLY the raw JSON. No markdown."""


# def extract_section_marks_map(full_text, model, max_tokens=None):
#     """One document-wide call that turns THIS paper's own section headers into an authoritative
#     [{"from", "to", "marks", "label"}, ...] list, sorted by "from". Purely descriptive of what this
#     specific paper prints -- no template, no assumed board/pattern, so it produces a different map for
#     every paper. Returns ([], 0, 0) on failure or when the paper has no such structure, so a caller can
#     always fall back to whatever the per-page/single-call parse already produced."""
#     max_tokens = max_tokens or int(os.environ.get("QP_SECTION_MARKS_MAX_TOKENS", "2048"))
#     prompt = SECTION_MARKS_PROMPT.replace("{full_text}", full_text)
#     try:
#         out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
#                                      max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#         data = tolerant_json(out)
#         secs = data.get("sections") if isinstance(data, dict) else None
#         if not isinstance(secs, list):
#             return [], int(i_tok or 0), int(o_tok or 0)
#         clean = []
#         for s in secs:
#             if not isinstance(s, dict):
#                 continue
#             try:
#                 lo = int(s.get("from"))
#                 hi = int(s.get("to"))
#                 mk = float(s.get("marks"))
#             except (TypeError, ValueError):
#                 continue
#             if lo > 0 and hi >= lo and mk > 0:
#                 clean.append({"from": lo, "to": hi, "marks": mk, "label": str(s.get("label") or "")})
#         clean.sort(key=lambda r: r["from"])
#         return clean, int(i_tok or 0), int(o_tok or 0)
#     except Exception:
#         return [], 0, 0







# """Per-page parallel parsing for question papers + answer keys.

# The single-call parsers (one LLM call over the whole document's text) are SLOW on the flagship 235B
# model (~180s for a 3-page key) and 235B sometimes returns an empty {} for a whole question paper. This
# module splits a PDF into pages and extracts the questions/answers from each page CONCURRENTLY -- many
# small, fast, reliable calls -- then runs ONE global pass to detect the things that need the whole-paper
# view (metadata + internal choices). Net: the accurate 235B model becomes fast enough to use, and each
# call is a small, easy task.

# Shared by extract_json_from_key.py and extract_json_from_question_paper.py. Gated by
# PARSER_PARALLEL_PAGES (default on); falls back to the caller's single-call path for docx / 1-page PDFs /
# when disabled, so nothing regresses.
# """
# import os
# import re
# import json
# import concurrent.futures

# from llm_client import generate, strip_reasoning


# def parallel_enabled():
#     return os.environ.get("PARSER_PARALLEL_PAGES", "1").strip().lower() not in ("0", "false", "no", "off")


# def pdf_page_texts(path):
#     """List of per-page extracted text (one string per PDF page). Text layer only (same as the
#     single-call extractor); a scanned/no-text page yields ''."""
#     import PyPDF2
#     pages = []
#     with open(path, "rb") as f:
#         reader = PyPDF2.PdfReader(f)
#         for pg in reader.pages:
#             try:
#                 pages.append(pg.extract_text() or "")
#             except Exception:
#                 pages.append("")
#     return pages


# def _sanitize_json_escapes(s):
#     """Double any backslash that is not a valid JSON escape, so LaTeX-heavy math (\\frac, \\sqrt) parses
#     instead of raising 'Invalid \\escape'. Mirrors extract_json_from_key._sanitize_json_escapes."""
#     out, i, n = [], 0, len(s)
#     while i < n:
#         c = s[i]
#         if c != "\\":
#             out.append(c); i += 1; continue
#         nxt = s[i + 1] if i + 1 < n else ""
#         if nxt in '"\\/':
#             out.append("\\" + nxt); i += 2
#         elif nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", s[i + 2:i + 6] or ""):
#             out.append(s[i:i + 6]); i += 6
#         elif nxt == "n":
#             out.append("\\n"); i += 2
#         elif nxt in "tfbr" and not (i + 2 < n and s[i + 2].isalpha()):
#             out.append("\\" + nxt); i += 2
#         else:
#             out.append("\\\\"); i += 1
#     return "".join(out)


# def tolerant_json(text):
#     """Parse model JSON, tolerating <think> blocks, unescaped LaTeX backslashes, and leading/trailing
#     noise. Returns {} on total failure (a page with no parseable questions is not fatal)."""
#     content = strip_reasoning((text or "").strip())
#     for candidate in (content, _sanitize_json_escapes(content)):
#         try:
#             return json.loads(candidate)
#         except json.JSONDecodeError:
#             m = re.search(r"(\{.*\})", candidate, re.DOTALL)
#             if m:
#                 try:
#                     return json.loads(m.group(1))
#                 except json.JSONDecodeError:
#                     continue
#     return {}


# def _questions_of(obj):
#     if isinstance(obj, dict) and isinstance(obj.get("questions"), dict):
#         return obj["questions"]
#     return obj if isinstance(obj, dict) else {}


# def extract_pages_parallel(page_texts, prompt_template, model, max_tokens, max_workers=None,
#                            extra_context=""):
#     """Extract questions from every page CONCURRENTLY. `prompt_template` contains a `{page_text}`
#     placeholder and must ask for {"questions": {...}} for THAT page only. `extra_context` (optional)
#     is spliced in via a `{extra_context}` placeholder in the template -- used to give every page the
#     SAME document-wide context (e.g. a section-numbering map) that a single page's own text cannot
#     supply; templates without that placeholder are unaffected (the .replace is then simply a no-op).
#     Returns (merged_questions_dict, total_in_tokens, total_out_tokens). Pages with no questions
#     contribute nothing; a failed page is skipped (never aborts the whole parse)."""
#     workers = int(max_workers or os.environ.get("PARSER_MAX_WORKERS", "8"))
#     non_empty = [(i, t) for i, t in enumerate(page_texts) if (t or "").strip()]

#     def one_page(idx, text):
#         prompt = prompt_template.replace("{page_text}", text).replace("{extra_context}", extra_context or "")
#         try:
#             out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
#                                          max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#             return _questions_of(tolerant_json(out)), int(i_tok or 0), int(o_tok or 0)
#         except Exception:
#             return {}, 0, 0

#     per_page = [None] * len(non_empty)
#     with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(non_empty) or 1)) as ex:
#         futs = {ex.submit(one_page, idx, text): pos for pos, (idx, text) in enumerate(non_empty)}
#         for fut in concurrent.futures.as_completed(futs):
#             per_page[futs[fut]] = fut.result()

#     merged, in_tok, out_tok = {}, 0, 0
#     for res in per_page:                                # keep page order for stable cross-page merge
#         if not res:
#             continue
#         qs, i_tok, o_tok = res
#         in_tok += i_tok; out_tok += o_tok
#         if isinstance(qs, dict):
#             _merge_into(merged, qs)
#     return merged, in_tok, out_tok


# def _merge_into(acc, page_qs):
#     """Merge one page's questions into the accumulator. A question id that appears on two pages (a
#     long answer that spilled across a page break) is COMBINED: text/answer concatenated, marks kept as
#     the MAX seen (never summed -- so a continuation can't double the marks)."""
#     for qid, v in page_qs.items():
#         if not isinstance(v, dict):
#             continue
#         key = str(qid).strip()
#         if not key:
#             continue
#         if key not in acc:
#             acc[key] = dict(v)
#             acc[key].setdefault("question_id", key)
#             continue
#         prev = acc[key]
#         for fld in ("question", "answer"):
#             a, b = str(prev.get(fld, "") or ""), str(v.get(fld, "") or "")
#             if b and b not in a:
#                 prev[fld] = (a + ("\n" if a else "") + b).strip()
#         try:
#             prev["marks"] = max(float(prev.get("marks", 0) or 0), float(v.get("marks", 0) or 0))
#         except (TypeError, ValueError):
#             pass


# def global_metadata_pass(full_text, prompt_template, model, max_tokens):
#     """One call over the WHOLE paper to detect what needs global context: metadata (class, subject) +
#     internal choices (choice_groups / inline_choice_ids). `prompt_template` has a `{full_text}`
#     placeholder. Returns (metadata_dict, in_tok, out_tok); {} on failure (choices just won't be
#     applied, which the downstream structural detector + reconciler still catch)."""
#     prompt = prompt_template.replace("{full_text}", full_text)
#     try:
#         out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
#                                      max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#         data = tolerant_json(out)
#         meta = data.get("metadata", data) if isinstance(data, dict) else {}
#         if not isinstance(meta, dict):
#             meta = {}
#         return meta, int(i_tok or 0), int(o_tok or 0)
#     except Exception:
#         return {}, 0, 0


# # ---------------------------------------------------------------------------------------------------
# # SECTION-MARKS MAP -- a global, document-wide pass that reads ONLY this paper's own section headers
# # ("SECTION A -- MCQ (20 x 1 = 20)", "Section D consists of 4 questions ... Each question carries
# # 5 Marks", ..., or a one-line summary such as "B: Q21-26 VSA, 2 marks each") and turns them into an
# # authoritative {question-number-range -> per-question marks} table. Nothing here is templated to any
# # known board/pattern: every paper's section boundaries and marks are read fresh from THAT paper's own
# # printed headers/instructions, because different papers number their sections completely differently.
# #
# # Why this exists (two distinct failure modes it corrects):
# #   1. MARKS -- the PER-PAGE parallel parser has no visibility beyond one page, so a question whose own
# #      page doesn't repeat its section's header can end up with the WRONG per-question marks, or 0.
# #   2. NUMBERING -- some papers state a GLOBAL numbering scheme in their General Instructions (e.g.
# #      "B: Q21-26") but then print each section's own questions with a LOCAL count that restarts at 1
# #      within the section body ("1.", "2.", "3." for what are really Q21, Q22, Q23). A page-local view
# #      cannot tell "this page's local 3." apart from another section's unrelated local "3." -- both
# #      look identical without the whole-document instructions line that ties them to a global range.
# # This pass sees the section headers/instructions WHEREVER they appear across the whole document in one
# # shot, so it becomes the authoritative tie-breaker for both marks (via reconcile_marks_with_sections in
# # extract_json_from_question_paper.py) and numbering (via format_section_context below, fed back into
# # each page's own prompt BEFORE that page is parsed).
# # ---------------------------------------------------------------------------------------------------
# SECTION_MARKS_PROMPT = """You are given the FULL text of an exam question paper. Find EVERY section
# header or summary line that states how many questions are in it, its GLOBAL question-number range,
# and how many marks each one carries -- for example "SECTION A -- MCQ (20 x 1 = 20)", "Section B
# consists of 7 questions (22 to 28). Each question carries 2 Marks", "SECTION D (4 x 5 = 20 Marks)",
# "B: Q21-26 VSA, 2 marks each", or any similar wording this specific paper uses (this information is
# very often stated once, near the top, in the paper's General Instructions, even when the section
# BODIES further down print their own questions with different-looking local numbering).

# For EACH such section, work out the GLOBAL QUESTION NUMBER RANGE it covers (its first and last
# question number AS THE PAPER'S OWN OVERALL NUMBERING SCHEME COUNTS THEM, e.g. Q21 to Q26 -- NOT the
# section body's own possibly-restarted local numbering) and the MARKS PER QUESTION in that section --
# using ONLY what is printed or stated in THIS paper. Do NOT assume any standard, typical, or
# previously-seen layout: different papers number their sections completely differently, so the range
# and marks must come from what is actually written here.

# - If a section states an explicit global question range ("Q21 to Q25", "Questions 22 to 28", "B:
#   Q21-26"), use exactly that range.
# - If a section does not state its range explicitly, infer it from which GLOBAL question numbers are
#   physically listed under that header, up to (but not including) the next section header.
# - For a section made of multi-part / case-study questions (each WHOLE question is worth one total
#   even though its own sub-parts carry different sub-marks that add up to that total), report the
#   section's per-QUESTION total, not a sub-part's marks.
# - If the paper has NO section structure at all (marks are printed individually next to every single
#   question, with no grouping headers), return an empty list -- do not invent sections.

# Return ONLY this JSON shape:
# {{"sections": [{{"from": <first GLOBAL question number as an integer>, "to": <last GLOBAL question
# number as an integer>, "marks": <marks per question in this section, as a number>, "label": "<the
# section name or heading exactly as printed>"}}, ...]}}
# ordered by "from". Return {{"sections": []}} if you cannot find this structure.

# FULL QUESTION PAPER TEXT:
# {full_text}

# Return ONLY the raw JSON. No markdown."""


# def extract_section_marks_map(full_text, model, max_tokens=None):
#     """One document-wide call that turns THIS paper's own section headers/instructions into an
#     authoritative [{"from", "to", "marks", "label"}, ...] list of GLOBAL question-number ranges,
#     sorted by "from". Purely descriptive of what this specific paper prints -- no template, no assumed
#     board/pattern, so it produces a different map for every paper. Returns ([], 0, 0) on failure or
#     when the paper has no such structure, so a caller can always fall back to whatever the
#     per-page/single-call parse already produced."""
#     max_tokens = max_tokens or int(os.environ.get("QP_SECTION_MARKS_MAX_TOKENS", "2048"))
#     prompt = SECTION_MARKS_PROMPT.replace("{full_text}", full_text)
#     try:
#         out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
#                                      max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#         data = tolerant_json(out)
#         secs = data.get("sections") if isinstance(data, dict) else None
#         if not isinstance(secs, list):
#             return [], int(i_tok or 0), int(o_tok or 0)
#         clean = []
#         for s in secs:
#             if not isinstance(s, dict):
#                 continue
#             try:
#                 lo = int(s.get("from"))
#                 hi = int(s.get("to"))
#                 mk = float(s.get("marks"))
#             except (TypeError, ValueError):
#                 continue
#             if lo > 0 and hi >= lo and mk > 0:
#                 clean.append({"from": lo, "to": hi, "marks": mk, "label": str(s.get("label") or "")})
#         clean.sort(key=lambda r: r["from"])
#         return clean, int(i_tok or 0), int(o_tok or 0)
#     except Exception:
#         return [], 0, 0


# def format_section_context(section_map):
#     """Render a section map into instructional text spliced into EVERY per-page prompt via
#     `{extra_context}`, so a page that only sees a section's LOCAL restarted numbering ('1.', '2.',
#     '3.' inside that section's own body) can still report the correct GLOBAL question id (Q21, Q22,
#     Q23) -- the mapping a single page's own text cannot supply on its own. Returns '' when there is no
#     section map (a plain-numbered paper), so the placeholder is spliced out to nothing and behaviour
#     is unchanged for papers that need no such correction."""
#     if not section_map:
#         return ""
#     lines = ["\n\nDOCUMENT-WIDE SECTION MAP (derived from this paper's own General "
#             "Instructions/section headers -- authoritative for GLOBAL question numbering):"]
#     for s in section_map:
#         lines.append(f"- {s['label'] or 'Section'}: GLOBAL question numbers Q{s['from']}-Q{s['to']}, "
#                      f"{s['marks']:g} mark(s) each.")
#     lines.append(
#         "\nIMPORTANT -- LOCAL vs GLOBAL numbering: some papers print each section's own questions "
#         "with a LOCAL count that RESTARTS at 1 within that section's body (e.g. the first question "
#         "physically printed under a section heading is labelled plain '1.', the next '2.', and so on) "
#         "even though the paper's own General Instructions assign that section a DIFFERENT global "
#         "range (see the map above, e.g. a section whose instructions say 'Q21-26' but whose body "
#         "prints '1.' through '6.'). When you can tell WHICH SECTION a question on THIS page belongs "
#         "to -- from a section heading on or just above this page, or from its position/content "
#         "matching one of the sections described above -- you MUST report its id using the GLOBAL "
#         "number from that section's range, NEVER the bare local number printed next to it. Example: "
#         "if this page falls under the section mapped to Q21-26 and shows a locally-numbered '3.', "
#         "its correct id is 'Q23' (the section's first global number plus the local number minus 1), "
#         "never a bare '3'. If you cannot confidently tell which section a question belongs to, keep "
#         "its id exactly as printed rather than guessing a global number.")
#     return "\n".join(lines)





# """Per-page parallel parsing for question papers + answer keys.

# The single-call parsers (one LLM call over the whole document's text) are SLOW on the flagship 235B
# model (~180s for a 3-page key) and 235B sometimes returns an empty {} for a whole question paper. This
# module splits a PDF into pages and extracts the questions/answers from each page CONCURRENTLY -- many
# small, fast, reliable calls -- then runs ONE global pass to detect the things that need the whole-paper
# view (metadata + internal choices). Net: the accurate 235B model becomes fast enough to use, and each
# call is a small, easy task.

# Shared by extract_json_from_key.py and extract_json_from_question_paper.py. Gated by
# PARSER_PARALLEL_PAGES (default on); falls back to the caller's single-call path for docx / 1-page PDFs /
# when disabled, so nothing regresses.
# """
# import os
# import re
# import json
# import concurrent.futures

# from llm_client import generate, strip_reasoning


# def parallel_enabled():
#     return os.environ.get("PARSER_PARALLEL_PAGES", "1").strip().lower() not in ("0", "false", "no", "off")


# def pdf_page_texts(path):
#     """List of per-page extracted text (one string per PDF page). Text layer only (same as the
#     single-call extractor); a scanned/no-text page yields ''."""
#     import PyPDF2
#     pages = []
#     with open(path, "rb") as f:
#         reader = PyPDF2.PdfReader(f)
#         for pg in reader.pages:
#             try:
#                 pages.append(pg.extract_text() or "")
#             except Exception:
#                 pages.append("")
#     return pages


# def _sanitize_json_escapes(s):
#     """Double any backslash that is not a valid JSON escape, so LaTeX-heavy math (\\frac, \\sqrt) parses
#     instead of raising 'Invalid \\escape'. Mirrors extract_json_from_key._sanitize_json_escapes."""
#     out, i, n = [], 0, len(s)
#     while i < n:
#         c = s[i]
#         if c != "\\":
#             out.append(c); i += 1; continue
#         nxt = s[i + 1] if i + 1 < n else ""
#         if nxt in '"\\/':
#             out.append("\\" + nxt); i += 2
#         elif nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", s[i + 2:i + 6] or ""):
#             out.append(s[i:i + 6]); i += 6
#         elif nxt == "n":
#             out.append("\\n"); i += 2
#         elif nxt in "tfbr" and not (i + 2 < n and s[i + 2].isalpha()):
#             out.append("\\" + nxt); i += 2
#         else:
#             out.append("\\\\"); i += 1
#     return "".join(out)


# def tolerant_json(text):
#     """Parse model JSON, tolerating <think> blocks, unescaped LaTeX backslashes, and leading/trailing
#     noise. Returns {} on total failure (a page with no parseable questions is not fatal)."""
#     content = strip_reasoning((text or "").strip())
#     for candidate in (content, _sanitize_json_escapes(content)):
#         try:
#             return json.loads(candidate)
#         except json.JSONDecodeError:
#             m = re.search(r"(\{.*\})", candidate, re.DOTALL)
#             if m:
#                 try:
#                     return json.loads(m.group(1))
#                 except json.JSONDecodeError:
#                     continue
#     return {}


# def _questions_of(obj):
#     if isinstance(obj, dict) and isinstance(obj.get("questions"), dict):
#         return obj["questions"]
#     return obj if isinstance(obj, dict) else {}


# def extract_pages_parallel(page_texts, prompt_template, model, max_tokens, max_workers=None,
#                            extra_context=""):
#     """Extract questions from every page CONCURRENTLY. `prompt_template` contains a `{page_text}`
#     placeholder and must ask for {"questions": {...}} for THAT page only. `extra_context` (optional)
#     is spliced in via a `{extra_context}` placeholder in the template -- used to give every page the
#     SAME document-wide context (e.g. a section-numbering map) that a single page's own text cannot
#     supply; templates without that placeholder are unaffected (the .replace is then simply a no-op).
#     Returns (merged_questions_dict, total_in_tokens, total_out_tokens). A page that raises or returns
#     unparseable JSON contributes nothing for that page alone -- it never aborts the whole document --
#     but this function does NOT try to guess whether an empty page result means "this page truly had no
#     questions" or "the call silently failed": that judgment needs the WHOLE document's expected
#     question count, which only the caller (via a section map) can know. See
#     extract_json_from_question_paper.verify_and_backfill_completeness for that safety net."""
#     workers = int(max_workers or os.environ.get("PARSER_MAX_WORKERS", "8"))
#     non_empty = [(i, t) for i, t in enumerate(page_texts) if (t or "").strip()]

#     def one_page(idx, text):
#         prompt = prompt_template.replace("{page_text}", text).replace("{extra_context}", extra_context or "")
#         try:
#             out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
#                                          max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#             return _questions_of(tolerant_json(out)), int(i_tok or 0), int(o_tok or 0)
#         except Exception:
#             return {}, 0, 0

#     per_page = [None] * len(non_empty)
#     with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(non_empty) or 1)) as ex:
#         futs = {ex.submit(one_page, idx, text): pos for pos, (idx, text) in enumerate(non_empty)}
#         for fut in concurrent.futures.as_completed(futs):
#             per_page[futs[fut]] = fut.result()

#     merged, in_tok, out_tok = {}, 0, 0
#     for res in per_page:                                # keep page order for stable cross-page merge
#         if not res:
#             continue
#         qs, i_tok, o_tok = res
#         in_tok += i_tok; out_tok += o_tok
#         if isinstance(qs, dict):
#             _merge_into(merged, qs)
#     return merged, in_tok, out_tok


# def _merge_into(acc, page_qs):
#     """Merge one page's questions into the accumulator. A question id that appears on two pages (a
#     long answer that spilled across a page break) is COMBINED: text/answer concatenated, marks kept as
#     the MAX seen (never summed -- so a continuation can't double the marks)."""
#     for qid, v in page_qs.items():
#         if not isinstance(v, dict):
#             continue
#         key = str(qid).strip()
#         if not key:
#             continue
#         if key not in acc:
#             acc[key] = dict(v)
#             acc[key].setdefault("question_id", key)
#             continue
#         prev = acc[key]
#         for fld in ("question", "answer"):
#             a, b = str(prev.get(fld, "") or ""), str(v.get(fld, "") or "")
#             if b and b not in a:
#                 prev[fld] = (a + ("\n" if a else "") + b).strip()
#         try:
#             prev["marks"] = max(float(prev.get("marks", 0) or 0), float(v.get("marks", 0) or 0))
#         except (TypeError, ValueError):
#             pass


# def global_metadata_pass(full_text, prompt_template, model, max_tokens):
#     """One call over the WHOLE paper to detect what needs global context: metadata (class, subject) +
#     internal choices (choice_groups / inline_choice_ids). `prompt_template` has a `{full_text}`
#     placeholder. Returns (metadata_dict, in_tok, out_tok); {} on failure (choices just won't be
#     applied, which the downstream structural detector + reconciler still catch)."""
#     prompt = prompt_template.replace("{full_text}", full_text)
#     try:
#         out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
#                                      max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#         data = tolerant_json(out)
#         meta = data.get("metadata", data) if isinstance(data, dict) else {}
#         if not isinstance(meta, dict):
#             meta = {}
#         return meta, int(i_tok or 0), int(o_tok or 0)
#     except Exception:
#         return {}, 0, 0


# # ---------------------------------------------------------------------------------------------------
# # SECTION-MARKS MAP -- a global, document-wide pass that reads ONLY this paper's own section headers
# # ("SECTION A -- MCQ (20 x 1 = 20)", "Section D consists of 4 questions ... Each question carries
# # 5 Marks", ..., or a one-line summary such as "B: Q21-26 VSA, 2 marks each") and turns them into an
# # authoritative {question-number-range -> per-question marks} table. Nothing here is templated to any
# # known board/pattern: every paper's section boundaries and marks are read fresh from THAT paper's own
# # printed headers/instructions, because different papers number their sections completely differently.
# #
# # Serves THREE distinct purposes for the caller (extract_json_from_question_paper.py), all reusing this
# # SAME one call so a paper only ever pays for it once:
# #   1. MARKS       -- corrects a question whose own page/parse got the wrong (or 0) per-question marks.
# #   2. NUMBERING   -- lets a page whose questions are printed with a section-local restarted numbering
# #                     ("1.", "2.", "3." inside that section's own body) resolve to the correct GLOBAL id
# #                     (see format_section_context, fed back into each page's prompt BEFORE it is parsed).
# #   3. COMPLETENESS -- gives the total expected question count (the highest "to" across every mapped
# #                     section) so the caller can detect and recover any question that silently failed
# #                     to parse on every page/pass, instead of the paper quietly coming back short.
# # ---------------------------------------------------------------------------------------------------
# SECTION_MARKS_PROMPT = """You are given the FULL text of an exam question paper. Find EVERY section
# header or summary line that states how many questions are in it, its GLOBAL question-number range,
# and how many marks each one carries -- for example "SECTION A -- MCQ (20 x 1 = 20)", "Section B
# consists of 7 questions (22 to 28). Each question carries 2 Marks", "SECTION D (4 x 5 = 20 Marks)",
# "B: Q21-26 VSA, 2 marks each", or any similar wording this specific paper uses (this information is
# very often stated once, near the top, in the paper's General Instructions, even when the section
# BODIES further down print their own questions with different-looking local numbering).

# For EACH such section, work out the GLOBAL QUESTION NUMBER RANGE it covers (its first and last
# question number AS THE PAPER'S OWN OVERALL NUMBERING SCHEME COUNTS THEM, e.g. Q21 to Q26 -- NOT the
# section body's own possibly-restarted local numbering) and the MARKS PER QUESTION in that section --
# using ONLY what is printed or stated in THIS paper. Do NOT assume any standard, typical, or
# previously-seen layout: different papers number their sections completely differently, so the range
# and marks must come from what is actually written here.

# - If a section states an explicit global question range ("Q21 to Q25", "Questions 22 to 28", "B:
#   Q21-26", "E: Q37-39"), use exactly that range.
# - If a section does not state its range explicitly, infer it from which GLOBAL question numbers are
#   physically listed under that header, up to (but not including) the next section header.
# - For a section made of multi-part / case-study questions (each WHOLE question is worth one total
#   even though its own sub-parts carry different sub-marks that add up to that total), report the
#   section's per-QUESTION total, not a sub-part's marks.
# - Cover EVERY section in the paper, including the LAST one -- do not stop early. A paper's very last
#   section (often case-study/long-answer questions near the end) is just as important to map as its
#   first.
# - If the paper has NO section structure at all (marks are printed individually next to every single
#   question, with no grouping headers), return an empty list -- do not invent sections.

# Return ONLY this JSON shape:
# {{"sections": [{{"from": <first GLOBAL question number as an integer>, "to": <last GLOBAL question
# number as an integer>, "marks": <marks per question in this section, as a number>, "label": "<the
# section name or heading exactly as printed>"}}, ...]}}
# ordered by "from". Return {{"sections": []}} if you cannot find this structure.

# FULL QUESTION PAPER TEXT:
# {full_text}

# Return ONLY the raw JSON. No markdown."""


# def extract_section_marks_map(full_text, model, max_tokens=None):
#     """One document-wide call that turns THIS paper's own section headers/instructions into an
#     authoritative [{"from", "to", "marks", "label"}, ...] list of GLOBAL question-number ranges,
#     sorted by "from". Purely descriptive of what this specific paper prints -- no template, no assumed
#     board/pattern, so it produces a different map for every paper. Returns ([], 0, 0) on failure or
#     when the paper has no such structure, so a caller can always fall back to whatever the
#     per-page/single-call parse already produced."""
#     max_tokens = max_tokens or int(os.environ.get("QP_SECTION_MARKS_MAX_TOKENS", "2048"))
#     prompt = SECTION_MARKS_PROMPT.replace("{full_text}", full_text)
#     try:
#         out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
#                                      max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#         data = tolerant_json(out)
#         secs = data.get("sections") if isinstance(data, dict) else None
#         if not isinstance(secs, list):
#             return [], int(i_tok or 0), int(o_tok or 0)
#         clean = []
#         for s in secs:
#             if not isinstance(s, dict):
#                 continue
#             try:
#                 lo = int(s.get("from"))
#                 hi = int(s.get("to"))
#                 mk = float(s.get("marks"))
#             except (TypeError, ValueError):
#                 continue
#             if lo > 0 and hi >= lo and mk > 0:
#                 clean.append({"from": lo, "to": hi, "marks": mk, "label": str(s.get("label") or "")})
#         clean.sort(key=lambda r: r["from"])
#         return clean, int(i_tok or 0), int(o_tok or 0)
#     except Exception:
#         return [], 0, 0


# def format_section_context(section_map):
#     """Render a section map into instructional text spliced into EVERY per-page prompt via
#     `{extra_context}`, so a page that only sees a section's LOCAL restarted numbering ('1.', '2.',
#     '3.' inside that section's own body) can still report the correct GLOBAL question id (Q21, Q22,
#     Q23) -- the mapping a single page's own text cannot supply on its own. Returns '' when there is no
#     section map (a plain-numbered paper), so the placeholder is spliced out to nothing and behaviour
#     is unchanged for papers that need no such correction."""
#     if not section_map:
#         return ""
#     lines = ["\n\nDOCUMENT-WIDE SECTION MAP (derived from this paper's own General "
#             "Instructions/section headers -- authoritative for GLOBAL question numbering):"]
#     for s in section_map:
#         lines.append(f"- {s['label'] or 'Section'}: GLOBAL question numbers Q{s['from']}-Q{s['to']}, "
#                      f"{s['marks']:g} mark(s) each.")
#     lines.append(
#         "\nIMPORTANT -- LOCAL vs GLOBAL numbering: some papers print each section's own questions "
#         "with a LOCAL count that RESTARTS at 1 within that section's body (e.g. the first question "
#         "physically printed under a section heading is labelled plain '1.', the next '2.', and so on) "
#         "even though the paper's own General Instructions assign that section a DIFFERENT global "
#         "range (see the map above, e.g. a section whose instructions say 'Q21-26' but whose body "
#         "prints '1.' through '6.'). When you can tell WHICH SECTION a question on THIS page belongs "
#         "to -- from a section heading on or just above this page, or from its position/content "
#         "matching one of the sections described above -- you MUST report its id using the GLOBAL "
#         "number from that section's range, NEVER the bare local number printed next to it. Example: "
#         "if this page falls under the section mapped to Q21-26 and shows a locally-numbered '3.', "
#         "its correct id is 'Q23' (the section's first global number plus the local number minus 1), "
#         "never a bare '3'. If you cannot confidently tell which section a question belongs to, keep "
#         "its id exactly as printed rather than guessing a global number.")
#     return "\n".join(lines)






# """Per-page parallel parsing for question papers + answer keys.

# The single-call parsers (one LLM call over the whole document's text) are SLOW on the flagship 235B
# model (~180s for a 3-page key) and 235B sometimes returns an empty {} for a whole question paper. This
# module splits a PDF into pages and extracts the questions/answers from each page CONCURRENTLY -- many
# small, fast, reliable calls -- then runs ONE global pass to detect the things that need the whole-paper
# view (metadata + internal choices). Net: the accurate 235B model becomes fast enough to use, and each
# call is a small, easy task.

# Shared by extract_json_from_key.py and extract_json_from_question_paper.py. Gated by
# PARSER_PARALLEL_PAGES (default on); falls back to the caller's single-call path for docx / 1-page PDFs /
# when disabled, so nothing regresses.
# """
# import os
# import re
# import json
# import concurrent.futures

# from llm_client import generate, strip_reasoning


# def parallel_enabled():
#     return os.environ.get("PARSER_PARALLEL_PAGES", "1").strip().lower() not in ("0", "false", "no", "off")


# def pdf_page_texts(path):
#     """List of per-page extracted text (one string per PDF page). Text layer only (same as the
#     single-call extractor); a scanned/no-text page yields ''."""
#     import PyPDF2
#     pages = []
#     with open(path, "rb") as f:
#         reader = PyPDF2.PdfReader(f)
#         for pg in reader.pages:
#             try:
#                 pages.append(pg.extract_text() or "")
#             except Exception:
#                 pages.append("")
#     return pages


# def _sanitize_json_escapes(s):
#     """Double any backslash that is not a valid JSON escape, so LaTeX-heavy math (\\frac, \\sqrt) parses
#     instead of raising 'Invalid \\escape'. Mirrors extract_json_from_key._sanitize_json_escapes."""
#     out, i, n = [], 0, len(s)
#     while i < n:
#         c = s[i]
#         if c != "\\":
#             out.append(c); i += 1; continue
#         nxt = s[i + 1] if i + 1 < n else ""
#         if nxt in '"\\/':
#             out.append("\\" + nxt); i += 2
#         elif nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", s[i + 2:i + 6] or ""):
#             out.append(s[i:i + 6]); i += 6
#         elif nxt == "n":
#             out.append("\\n"); i += 2
#         elif nxt in "tfbr" and not (i + 2 < n and s[i + 2].isalpha()):
#             out.append("\\" + nxt); i += 2
#         else:
#             out.append("\\\\"); i += 1
#     return "".join(out)


# def tolerant_json(text):
#     """Parse model JSON, tolerating <think> blocks, unescaped LaTeX backslashes, and leading/trailing
#     noise. Returns {} on total failure (a page with no parseable questions is not fatal)."""
#     content = strip_reasoning((text or "").strip())
#     for candidate in (content, _sanitize_json_escapes(content)):
#         try:
#             return json.loads(candidate)
#         except json.JSONDecodeError:
#             m = re.search(r"(\{.*\})", candidate, re.DOTALL)
#             if m:
#                 try:
#                     return json.loads(m.group(1))
#                 except json.JSONDecodeError:
#                     continue
#     return {}


# def _questions_of(obj):
#     if isinstance(obj, dict) and isinstance(obj.get("questions"), dict):
#         return obj["questions"]
#     return obj if isinstance(obj, dict) else {}


# def extract_pages_parallel(page_texts, prompt_template, model, max_tokens, max_workers=None,
#                            extra_context=""):
#     """Extract questions from every page CONCURRENTLY. `prompt_template` contains a `{page_text}`
#     placeholder and must ask for {"questions": {...}} for THAT page only. `extra_context` (optional)
#     is spliced in via a `{extra_context}` placeholder in the template -- used to give every page the
#     SAME document-wide context (e.g. a section-numbering map) that a single page's own text cannot
#     supply; templates without that placeholder are unaffected (the .replace is then simply a no-op).

#     Each call is given an OVERLAPPING WINDOW -- this page's own text PLUS a chunk of the NEXT page's
#     leading text -- rather than the page in strict isolation. This is what lets a question whose tail
#     CONTINUES onto the next page WITHOUT repeating its own number (a case-study's numbered sub-parts
#     printed just below a page break, a table continuing across pages) still be captured under its
#     correct id. A page-local view with no look-ahead has no way to know an unlabelled continuation
#     belongs to the question that opened on the page before it -- from that later page's OWN text
#     alone, the continuation looks like body text with no question header at all, so it was previously
#     just silently dropped rather than merged into the question it belongs to. The look-ahead text is
#     clearly fenced off in the prompt so the model does not mistake it for THIS page's own content;
#     `_merge_into`'s text-concatenation + substring de-duplication then means the overlap can only ever
#     ADD what a strictly page-local view was missing, never inflate or duplicate a question's text.

#     Window size is tunable via PARSER_LOOKAHEAD_CHARS (default 1200, a modest per-call token cost that
#     covers the common single-page overflow case) and can be disabled entirely with
#     PARSER_LOOKAHEAD_CHARS=0 to fall back to the original strictly-isolated per-page behaviour.

#     Returns (merged_questions_dict, total_in_tokens, total_out_tokens). Pages with no questions
#     contribute nothing; a failed page is skipped (never aborts the whole parse)."""
#     workers = int(max_workers or os.environ.get("PARSER_MAX_WORKERS", "8"))
#     try:
#         lookahead_chars = int(os.environ.get("PARSER_LOOKAHEAD_CHARS", "1200"))
#     except (TypeError, ValueError):
#         lookahead_chars = 1200
#     non_empty = [(i, t) for i, t in enumerate(page_texts) if (t or "").strip()]

#     _LOOKAHEAD_FENCE = (
#         "\n\n[--- START OF NEXT PAGE (shown for CONTEXT ONLY, so you can tell whether the LAST "
#         "question on THIS page continues here). A passage below this fence with NO new question "
#         "number belongs to the last question ABOVE the fence -- fold it into that question's text. "
#         "Do NOT start a new question purely because you see more text after this fence, and do NOT "
#         "treat this fence's content as belonging to THIS page if it clearly starts a brand-new "
#         "numbered question -- in that case just ignore it here (the next page's own call will "
#         "capture it under its own number). ---]\n\n"
#     )

#     def _windowed_text(pos):
#         idx, text = non_empty[pos]
#         if lookahead_chars <= 0 or pos + 1 >= len(non_empty):
#             return text
#         _next_idx, next_text = non_empty[pos + 1]
#         lookahead = (next_text or "").strip()[:lookahead_chars]
#         if not lookahead:
#             return text
#         return text + _LOOKAHEAD_FENCE + lookahead

#     def one_page(pos, text):
#         prompt = prompt_template.replace("{page_text}", text).replace("{extra_context}", extra_context or "")
#         try:
#             out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
#                                          max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#             return _questions_of(tolerant_json(out)), int(i_tok or 0), int(o_tok or 0)
#         except Exception:
#             return {}, 0, 0

#     per_page = [None] * len(non_empty)
#     with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(non_empty) or 1)) as ex:
#         futs = {ex.submit(one_page, pos, _windowed_text(pos)): pos for pos in range(len(non_empty))}
#         for fut in concurrent.futures.as_completed(futs):
#             per_page[futs[fut]] = fut.result()

#     merged, in_tok, out_tok = {}, 0, 0
#     for res in per_page:                                # keep page order for stable cross-page merge
#         if not res:
#             continue
#         qs, i_tok, o_tok = res
#         in_tok += i_tok; out_tok += o_tok
#         if isinstance(qs, dict):
#             _merge_into(merged, qs)
#     return merged, in_tok, out_tok


# def _merge_into(acc, page_qs):
#     """Merge one page's questions into the accumulator. A question id that appears on two pages (a
#     long answer that spilled across a page break, or the look-ahead window in extract_pages_parallel
#     re-capturing the SAME tail a neighbouring page already captured) is COMBINED: text/answer
#     concatenated -- but only the part NOT ALREADY PRESENT as a substring, so an overlapping window can
#     only ever ADD missing content, never duplicate what's already there. Marks are kept as the MAX
#     seen (never summed -- so a continuation can't double the marks)."""
#     for qid, v in page_qs.items():
#         if not isinstance(v, dict):
#             continue
#         key = str(qid).strip()
#         if not key:
#             continue
#         if key not in acc:
#             acc[key] = dict(v)
#             acc[key].setdefault("question_id", key)
#             continue
#         prev = acc[key]
#         for fld in ("question", "answer"):
#             a, b = str(prev.get(fld, "") or ""), str(v.get(fld, "") or "")
#             if b and b not in a:
#                 prev[fld] = (a + ("\n" if a else "") + b).strip()
#         try:
#             prev["marks"] = max(float(prev.get("marks", 0) or 0), float(v.get("marks", 0) or 0))
#         except (TypeError, ValueError):
#             pass


# def global_metadata_pass(full_text, prompt_template, model, max_tokens):
#     """One call over the WHOLE paper to detect what needs global context: metadata (class, subject) +
#     internal choices (choice_groups / inline_choice_ids). `prompt_template` has a `{full_text}`
#     placeholder. Returns (metadata_dict, in_tok, out_tok); {} on failure (choices just won't be
#     applied, which the downstream structural detector + reconciler still catch)."""
#     prompt = prompt_template.replace("{full_text}", full_text)
#     try:
#         out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
#                                      max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#         data = tolerant_json(out)
#         meta = data.get("metadata", data) if isinstance(data, dict) else {}
#         if not isinstance(meta, dict):
#             meta = {}
#         return meta, int(i_tok or 0), int(o_tok or 0)
#     except Exception:
#         return {}, 0, 0


# # ---------------------------------------------------------------------------------------------------
# # SECTION-MARKS MAP -- a global, document-wide pass that reads ONLY this paper's own section headers
# # ("SECTION A -- MCQ (20 x 1 = 20)", "Section D consists of 4 questions ... Each question carries
# # 5 Marks", ..., or a one-line summary such as "B: Q21-26 VSA, 2 marks each") and turns them into an
# # authoritative {question-number-range -> per-question marks} table. Nothing here is templated to any
# # known board/pattern: every paper's section boundaries and marks are read fresh from THAT paper's own
# # printed headers/instructions, because different papers number their sections completely differently.
# #
# # Serves THREE distinct purposes for the caller (extract_json_from_question_paper.py), all reusing this
# # SAME one call so a paper only ever pays for it once:
# #   1. MARKS       -- corrects a question whose own page/parse got the wrong (or 0) per-question marks.
# #   2. NUMBERING   -- lets a page whose questions are printed with a section-local restarted numbering
# #                     ("1.", "2.", "3." inside that section's own body) resolve to the correct GLOBAL id
# #                     (see format_section_context, fed back into each page's prompt BEFORE it is parsed).
# #   3. COMPLETENESS -- gives the total expected question count (the highest "to" across every mapped
# #                     section) so the caller can detect and recover any question that silently failed
# #                     to parse on every page/pass, instead of the paper quietly coming back short.
# # ---------------------------------------------------------------------------------------------------
# SECTION_MARKS_PROMPT = """You are given the FULL text of an exam question paper. Find EVERY section
# header or summary line that states how many questions are in it, its GLOBAL question-number range,
# and how many marks each one carries -- for example "SECTION A -- MCQ (20 x 1 = 20)", "Section B
# consists of 7 questions (22 to 28). Each question carries 2 Marks", "SECTION D (4 x 5 = 20 Marks)",
# "B: Q21-26 VSA, 2 marks each", or any similar wording this specific paper uses (this information is
# very often stated once, near the top, in the paper's General Instructions, even when the section
# BODIES further down print their own questions with different-looking local numbering).

# For EACH such section, work out the GLOBAL QUESTION NUMBER RANGE it covers (its first and last
# question number AS THE PAPER'S OWN OVERALL NUMBERING SCHEME COUNTS THEM, e.g. Q21 to Q26 -- NOT the
# section body's own possibly-restarted local numbering) and the MARKS PER QUESTION in that section --
# using ONLY what is printed or stated in THIS paper. Do NOT assume any standard, typical, or
# previously-seen layout: different papers number their sections completely differently, so the range
# and marks must come from what is actually written here.

# - If a section states an explicit global question range ("Q21 to Q25", "Questions 22 to 28", "B:
#   Q21-26", "E: Q37-39"), use exactly that range.
# - If a section does not state its range explicitly, infer it from which GLOBAL question numbers are
#   physically listed under that header, up to (but not including) the next section header.
# - For a section made of multi-part / case-study questions (each WHOLE question is worth one total
#   even though its own sub-parts carry different sub-marks that add up to that total), report the
#   section's per-QUESTION total, not a sub-part's marks.
# - Cover EVERY section in the paper, including the LAST one -- do not stop early. A paper's very last
#   section (often case-study/long-answer questions near the end) is just as important to map as its
#   first.
# - If the paper has NO section structure at all (marks are printed individually next to every single
#   question, with no grouping headers), return an empty list -- do not invent sections.

# Return ONLY this JSON shape:
# {{"sections": [{{"from": <first GLOBAL question number as an integer>, "to": <last GLOBAL question
# number as an integer>, "marks": <marks per question in this section, as a number>, "label": "<the
# section name or heading exactly as printed>"}}, ...]}}
# ordered by "from". Return {{"sections": []}} if you cannot find this structure.

# FULL QUESTION PAPER TEXT:
# {full_text}

# Return ONLY the raw JSON. No markdown."""


# def extract_section_marks_map(full_text, model, max_tokens=None):
#     """One document-wide call that turns THIS paper's own section headers/instructions into an
#     authoritative [{"from", "to", "marks", "label"}, ...] list of GLOBAL question-number ranges,
#     sorted by "from". Purely descriptive of what this specific paper prints -- no template, no assumed
#     board/pattern, so it produces a different map for every paper. Returns ([], 0, 0) on failure or
#     when the paper has no such structure, so a caller can always fall back to whatever the
#     per-page/single-call parse already produced."""
#     max_tokens = max_tokens or int(os.environ.get("QP_SECTION_MARKS_MAX_TOKENS", "2048"))
#     prompt = SECTION_MARKS_PROMPT.replace("{full_text}", full_text)
#     try:
#         out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
#                                      max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#         data = tolerant_json(out)
#         secs = data.get("sections") if isinstance(data, dict) else None
#         if not isinstance(secs, list):
#             return [], int(i_tok or 0), int(o_tok or 0)
#         clean = []
#         for s in secs:
#             if not isinstance(s, dict):
#                 continue
#             try:
#                 lo = int(s.get("from"))
#                 hi = int(s.get("to"))
#                 mk = float(s.get("marks"))
#             except (TypeError, ValueError):
#                 continue
#             if lo > 0 and hi >= lo and mk > 0:
#                 clean.append({"from": lo, "to": hi, "marks": mk, "label": str(s.get("label") or "")})
#         clean.sort(key=lambda r: r["from"])
#         return clean, int(i_tok or 0), int(o_tok or 0)
#     except Exception:
#         return [], 0, 0


# def format_section_context(section_map):
#     """Render a section map into instructional text spliced into EVERY per-page prompt via
#     `{extra_context}`, so a page that only sees a section's LOCAL restarted numbering ('1.', '2.',
#     '3.' inside that section's own body) can still report the correct GLOBAL question id (Q21, Q22,
#     Q23) -- the mapping a single page's own text cannot supply on its own. Returns '' when there is no
#     section map (a plain-numbered paper), so the placeholder is spliced out to nothing and behaviour
#     is unchanged for papers that need no such correction."""
#     if not section_map:
#         return ""
#     lines = ["\n\nDOCUMENT-WIDE SECTION MAP (derived from this paper's own General "
#             "Instructions/section headers -- authoritative for GLOBAL question numbering):"]
#     for s in section_map:
#         lines.append(f"- {s['label'] or 'Section'}: GLOBAL question numbers Q{s['from']}-Q{s['to']}, "
#                      f"{s['marks']:g} mark(s) each.")
#     lines.append(
#         "\nIMPORTANT -- LOCAL vs GLOBAL numbering: some papers print each section's own questions "
#         "with a LOCAL count that RESTARTS at 1 within that section's body (e.g. the first question "
#         "physically printed under a section heading is labelled plain '1.', the next '2.', and so on) "
#         "even though the paper's own General Instructions assign that section a DIFFERENT global "
#         "range (see the map above, e.g. a section whose instructions say 'Q21-26' but whose body "
#         "prints '1.' through '6.'). When you can tell WHICH SECTION a question on THIS page belongs "
#         "to -- from a section heading on or just above this page, or from its position/content "
#         "matching one of the sections described above -- you MUST report its id using the GLOBAL "
#         "number from that section's range, NEVER the bare local number printed next to it. Example: "
#         "if this page falls under the section mapped to Q21-26 and shows a locally-numbered '3.', "
#         "its correct id is 'Q23' (the section's first global number plus the local number minus 1), "
#         "never a bare '3'. If you cannot confidently tell which section a question belongs to, keep "
#         "its id exactly as printed rather than guessing a global number.")
#     return "\n".join(lines)






# """Per-page parallel parsing for question papers + answer keys.

# The single-call parsers (one LLM call over the whole document's text) are SLOW on the flagship 235B
# model (~180s for a 3-page key) and 235B sometimes returns an empty {} for a whole question paper. This
# module splits a PDF into pages and extracts the questions/answers from each page CONCURRENTLY -- many
# small, fast, reliable calls -- then runs ONE global pass to detect the things that need the whole-paper
# view (metadata + internal choices). Net: the accurate 235B model becomes fast enough to use, and each
# call is a small, easy task.

# Shared by extract_json_from_key.py and extract_json_from_question_paper.py. Gated by
# PARSER_PARALLEL_PAGES (default on); falls back to the caller's single-call path for docx / 1-page PDFs /
# when disabled, so nothing regresses.
# """
# import os
# import re
# import json
# import difflib
# import concurrent.futures

# from llm_client import generate, strip_reasoning


# def parallel_enabled():
#     return os.environ.get("PARSER_PARALLEL_PAGES", "1").strip().lower() not in ("0", "false", "no", "off")


# def pdf_page_texts(path):
#     """List of per-page extracted text (one string per PDF page). Text layer only (same as the
#     single-call extractor); a scanned/no-text page yields ''."""
#     import PyPDF2
#     pages = []
#     with open(path, "rb") as f:
#         reader = PyPDF2.PdfReader(f)
#         for pg in reader.pages:
#             try:
#                 pages.append(pg.extract_text() or "")
#             except Exception:
#                 pages.append("")
#     return pages


# def _sanitize_json_escapes(s):
#     """Double any backslash that is not a valid JSON escape, so LaTeX-heavy math (\\frac, \\sqrt) parses
#     instead of raising 'Invalid \\escape'. Mirrors extract_json_from_key._sanitize_json_escapes."""
#     out, i, n = [], 0, len(s)
#     while i < n:
#         c = s[i]
#         if c != "\\":
#             out.append(c); i += 1; continue
#         nxt = s[i + 1] if i + 1 < n else ""
#         if nxt in '"\\/':
#             out.append("\\" + nxt); i += 2
#         elif nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", s[i + 2:i + 6] or ""):
#             out.append(s[i:i + 6]); i += 6
#         elif nxt == "n":
#             out.append("\\n"); i += 2
#         elif nxt in "tfbr" and not (i + 2 < n and s[i + 2].isalpha()):
#             out.append("\\" + nxt); i += 2
#         else:
#             out.append("\\\\"); i += 1
#     return "".join(out)


# def tolerant_json(text):
#     """Parse model JSON, tolerating <think> blocks, unescaped LaTeX backslashes, and leading/trailing
#     noise. Returns {} on total failure (a page with no parseable questions is not fatal)."""
#     content = strip_reasoning((text or "").strip())
#     for candidate in (content, _sanitize_json_escapes(content)):
#         try:
#             return json.loads(candidate)
#         except json.JSONDecodeError:
#             m = re.search(r"(\{.*\})", candidate, re.DOTALL)
#             if m:
#                 try:
#                     return json.loads(m.group(1))
#                 except json.JSONDecodeError:
#                     continue
#     return {}


# def _questions_of(obj):
#     if isinstance(obj, dict) and isinstance(obj.get("questions"), dict):
#         return obj["questions"]
#     return obj if isinstance(obj, dict) else {}


# def extract_pages_parallel(page_texts, prompt_template, model, max_tokens, max_workers=None,
#                            extra_context=""):
#     """Extract questions from every page CONCURRENTLY. `prompt_template` contains a `{page_text}`
#     placeholder and must ask for {"questions": {...}} for THAT page only. `extra_context` (optional)
#     is spliced in via a `{extra_context}` placeholder in the template -- used to give every page the
#     SAME document-wide context (e.g. a section-numbering map) that a single page's own text cannot
#     supply; templates without that placeholder are unaffected (the .replace is then simply a no-op).

#     Each call is given an OVERLAPPING WINDOW -- this page's own text PLUS a chunk of the NEXT page's
#     leading text -- rather than the page in strict isolation. This is what lets a question whose tail
#     CONTINUES onto the next page WITHOUT repeating its own number (a case-study's numbered sub-parts
#     printed just below a page break, a table continuing across pages) still be captured under its
#     correct id, complete. OWNERSHIP of a boundary-spanning question is assigned to the EARLIER page
#     (via this look-ahead); the prompt_template is expected to instruct the LATER page to ignore an
#     unlabeled leading continuation rather than re-capturing it under a guessed id -- see
#     extract_json_from_question_paper.QP_PER_PAGE_PROMPT for the actual wording. That ownership split
#     is what prevents the same boundary-spanning content from being captured (and therefore merged and
#     duplicated) by BOTH the earlier and the later page's calls.

#     `_merge_into` additionally applies FUZZY near-duplicate detection as a second, independent line of
#     defense: even if a duplicate slips through (e.g. the model re-captures a boundary question despite
#     the prompt's instruction not to, or the two captures differ only in incidental formatting), the
#     merge step recognises the near-duplicate and keeps the better version instead of concatenating
#     both -- so a bug in either layer alone cannot reach the final output as visible duplication.

#     Returns (merged_questions_dict, total_in_tokens, total_out_tokens). Pages with no questions
#     contribute nothing; a failed page is skipped (never aborts the whole parse)."""
#     workers = int(max_workers or os.environ.get("PARSER_MAX_WORKERS", "8"))
#     try:
#         lookahead_chars = int(os.environ.get("PARSER_LOOKAHEAD_CHARS", "1200"))
#     except (TypeError, ValueError):
#         lookahead_chars = 1200
#     non_empty = [(i, t) for i, t in enumerate(page_texts) if (t or "").strip()]

#     _LOOKAHEAD_FENCE = (
#         "\n\n[--- START OF NEXT PAGE (shown for CONTEXT ONLY, so you can tell whether the LAST "
#         "question on THIS page continues here). A passage below this fence with NO new question "
#         "number belongs to the last question ABOVE the fence -- fold it into that question's text. "
#         "Do NOT start a new question purely because you see more text after this fence, and do NOT "
#         "treat this fence's content as belonging to THIS page if it clearly starts a brand-new "
#         "numbered question -- in that case just ignore it here (the next page's own call will "
#         "capture it under its own number). ---]\n\n"
#     )

#     def _windowed_text(pos):
#         idx, text = non_empty[pos]
#         if lookahead_chars <= 0 or pos + 1 >= len(non_empty):
#             return text
#         _next_idx, next_text = non_empty[pos + 1]
#         lookahead = (next_text or "").strip()[:lookahead_chars]
#         if not lookahead:
#             return text
#         return text + _LOOKAHEAD_FENCE + lookahead

#     def one_page(pos, text):
#         prompt = prompt_template.replace("{page_text}", text).replace("{extra_context}", extra_context or "")
#         try:
#             out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
#                                          max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#             return _questions_of(tolerant_json(out)), int(i_tok or 0), int(o_tok or 0)
#         except Exception:
#             return {}, 0, 0

#     per_page = [None] * len(non_empty)
#     with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(non_empty) or 1)) as ex:
#         futs = {ex.submit(one_page, pos, _windowed_text(pos)): pos for pos in range(len(non_empty))}
#         for fut in concurrent.futures.as_completed(futs):
#             per_page[futs[fut]] = fut.result()

#     merged, in_tok, out_tok = {}, 0, 0
#     for res in per_page:                                # keep page order for stable cross-page merge
#         if not res:
#             continue
#         qs, i_tok, o_tok = res
#         in_tok += i_tok; out_tok += o_tok
#         if isinstance(qs, dict):
#             _merge_into(merged, qs)
#     return merged, in_tok, out_tok


# def _normalize_ws(s):
#     """Whitespace-collapsed, lowercased view of a string, for fuzzy comparison that ignores
#     incidental formatting differences (extra spaces, curly vs straight quotes handled by str.lower()
#     doing nothing harmful to them, line-break placement) between two captures of the SAME content."""
#     return re.sub(r"\s+", " ", (s or "").strip()).lower()


# def _is_near_duplicate(a, b, threshold=0.90):
#     """True when `a` and `b` are, for all practical purposes, the SAME content -- identical after
#     whitespace normalization, one fully contains the other, or they are highly similar by
#     difflib's ratio (catches near-identical re-transcriptions that differ only in minor wording/
#     punctuation, e.g. a curly vs straight apostrophe or a missing word from an OCR quirk). Deliberately
#     conservative (threshold 0.90): two DIFFERENT continuations of the same question (e.g. two more
#     rows of a genuinely growing table) must never be caught here, only genuine duplicates."""
#     if not a or not b:
#         return False
#     na, nb = _normalize_ws(a), _normalize_ws(b)
#     if na == nb:
#         return True
#     shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
#     if shorter and shorter in longer:
#         return True
#     if not na or not nb:
#         return False
#     return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


# def _merge_texts(a, b):
#     """Merge two text chunks for the SAME field of the SAME question, collapsing three distinct
#     situations instead of naively concatenating (which is what produced literal duplicate paragraphs
#     when a boundary-spanning question was independently captured, with slightly different
#     formatting, by both the page before and the page after a page break):

#       1. Near-duplicate (see _is_near_duplicate) -- keep whichever is LONGER (assumed more complete),
#          discard the other entirely. Handles the exact failure mode above.
#       2. One is a straightforward superset of the other (a is a prefix/subset of b, or vice versa) --
#          keep the superset.
#       3. Genuine partial overlap at the a-tail / b-head boundary (the classic page-break case where
#          the two chunks share a run of text right at the join) -- splice the overlap once instead of
#          duplicating it.
#       4. No meaningful overlap at all -- plain concatenation (today's original behaviour), used only
#          when none of the smarter cases above apply.
#     """
#     a, b = a or "", b or ""
#     if not b:
#         return a
#     if not a:
#         return b
#     if _is_near_duplicate(a, b):
#         return b if len(b) > len(a) else a
#     na, nb = _normalize_ws(a), _normalize_ws(b)
#     if nb and nb in na:
#         return a
#     if na and na in nb:
#         return b
#     max_overlap = min(len(a), len(b), 400)
#     for k in range(max_overlap, 19, -1):
#         if _normalize_ws(a[-k:]) == _normalize_ws(b[:k]):
#             return (a + b[k:]).strip()
#     return (a + "\n" + b).strip()


# def _merge_into(acc, page_qs):
#     """Merge one page's questions into the accumulator. A question id that appears from more than one
#     source (a long answer that spilled across a page break, or the look-ahead window re-surfacing
#     content near a boundary) is combined via `_merge_texts`, which recognises and collapses a
#     near-duplicate instead of concatenating it -- see that function's docstring for the failure mode
#     this specifically fixes. Marks are kept as the MAX seen (never summed -- so a continuation can't
#     double the marks)."""
#     for qid, v in page_qs.items():
#         if not isinstance(v, dict):
#             continue
#         key = str(qid).strip()
#         if not key:
#             continue
#         if key not in acc:
#             acc[key] = dict(v)
#             acc[key].setdefault("question_id", key)
#             continue
#         prev = acc[key]
#         for fld in ("question", "answer"):
#             prev[fld] = _merge_texts(str(prev.get(fld, "") or ""), str(v.get(fld, "") or ""))
#         try:
#             prev["marks"] = max(float(prev.get("marks", 0) or 0), float(v.get("marks", 0) or 0))
#         except (TypeError, ValueError):
#             pass


# def global_metadata_pass(full_text, prompt_template, model, max_tokens):
#     """One call over the WHOLE paper to detect what needs global context: metadata (class, subject) +
#     internal choices (choice_groups / inline_choice_ids). `prompt_template` has a `{full_text}`
#     placeholder. Returns (metadata_dict, in_tok, out_tok); {} on failure (choices just won't be
#     applied, which the downstream structural detector + reconciler still catch)."""
#     prompt = prompt_template.replace("{full_text}", full_text)
#     try:
#         out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
#                                      max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#         data = tolerant_json(out)
#         meta = data.get("metadata", data) if isinstance(data, dict) else {}
#         if not isinstance(meta, dict):
#             meta = {}
#         return meta, int(i_tok or 0), int(o_tok or 0)
#     except Exception:
#         return {}, 0, 0


# # ---------------------------------------------------------------------------------------------------
# # SECTION-MARKS MAP -- a global, document-wide pass that reads ONLY this paper's own section headers
# # ("SECTION A -- MCQ (20 x 1 = 20)", "Section D consists of 4 questions ... Each question carries
# # 5 Marks", ..., or a one-line summary such as "B: Q21-26 VSA, 2 marks each") and turns them into an
# # authoritative {question-number-range -> per-question marks} table. Nothing here is templated to any
# # known board/pattern: every paper's section boundaries and marks are read fresh from THAT paper's own
# # printed headers/instructions, because different papers number their sections completely differently.
# #
# # Serves THREE distinct purposes for the caller (extract_json_from_question_paper.py), all reusing this
# # SAME one call so a paper only ever pays for it once:
# #   1. MARKS       -- corrects a question whose own page/parse got the wrong (or 0) per-question marks.
# #   2. NUMBERING   -- lets a page whose questions are printed with a section-local restarted numbering
# #                     ("1.", "2.", "3." inside that section's own body) resolve to the correct GLOBAL id
# #                     (see format_section_context, fed back into each page's prompt BEFORE it is parsed).
# #   3. COMPLETENESS -- gives the total expected question count (the highest "to" across every mapped
# #                     section) so the caller can detect and recover any question that silently failed
# #                     to parse on every page/pass, instead of the paper quietly coming back short.
# # ---------------------------------------------------------------------------------------------------
# SECTION_MARKS_PROMPT = """You are given the FULL text of an exam question paper. Find EVERY section
# header or summary line that states how many questions are in it, its GLOBAL question-number range,
# and how many marks each one carries -- for example "SECTION A -- MCQ (20 x 1 = 20)", "Section B
# consists of 7 questions (22 to 28). Each question carries 2 Marks", "SECTION D (4 x 5 = 20 Marks)",
# "B: Q21-26 VSA, 2 marks each", or any similar wording this specific paper uses (this information is
# very often stated once, near the top, in the paper's General Instructions, even when the section
# BODIES further down print their own questions with different-looking local numbering).

# For EACH such section, work out the GLOBAL QUESTION NUMBER RANGE it covers (its first and last
# question number AS THE PAPER'S OWN OVERALL NUMBERING SCHEME COUNTS THEM, e.g. Q21 to Q26 -- NOT the
# section body's own possibly-restarted local numbering) and the MARKS PER QUESTION in that section --
# using ONLY what is printed or stated in THIS paper. Do NOT assume any standard, typical, or
# previously-seen layout: different papers number their sections completely differently, so the range
# and marks must come from what is actually written here.

# - If a section states an explicit global question range ("Q21 to Q25", "Questions 22 to 28", "B:
#   Q21-26", "E: Q37-39"), use exactly that range.
# - If a section does not state its range explicitly, infer it from which GLOBAL question numbers are
#   physically listed under that header, up to (but not including) the next section header.
# - For a section made of multi-part / case-study questions (each WHOLE question is worth one total
#   even though its own sub-parts carry different sub-marks that add up to that total), report the
#   section's per-QUESTION total, not a sub-part's marks.
# - Cover EVERY section in the paper, including the LAST one -- do not stop early. A paper's very last
#   section (often case-study/long-answer questions near the end) is just as important to map as its
#   first.
# - If the paper has NO section structure at all (marks are printed individually next to every single
#   question, with no grouping headers), return an empty list -- do not invent sections.

# Return ONLY this JSON shape:
# {{"sections": [{{"from": <first GLOBAL question number as an integer>, "to": <last GLOBAL question
# number as an integer>, "marks": <marks per question in this section, as a number>, "label": "<the
# section name or heading exactly as printed>"}}, ...]}}
# ordered by "from". Return {{"sections": []}} if you cannot find this structure.

# FULL QUESTION PAPER TEXT:
# {full_text}

# Return ONLY the raw JSON. No markdown."""


# def extract_section_marks_map(full_text, model, max_tokens=None):
#     """One document-wide call that turns THIS paper's own section headers/instructions into an
#     authoritative [{"from", "to", "marks", "label"}, ...] list of GLOBAL question-number ranges,
#     sorted by "from". Purely descriptive of what this specific paper prints -- no template, no assumed
#     board/pattern, so it produces a different map for every paper. Returns ([], 0, 0) on failure or
#     when the paper has no such structure, so a caller can always fall back to whatever the
#     per-page/single-call parse already produced."""
#     max_tokens = max_tokens or int(os.environ.get("QP_SECTION_MARKS_MAX_TOKENS", "2048"))
#     prompt = SECTION_MARKS_PROMPT.replace("{full_text}", full_text)
#     try:
#         out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
#                                      max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#         data = tolerant_json(out)
#         secs = data.get("sections") if isinstance(data, dict) else None
#         if not isinstance(secs, list):
#             return [], int(i_tok or 0), int(o_tok or 0)
#         clean = []
#         for s in secs:
#             if not isinstance(s, dict):
#                 continue
#             try:
#                 lo = int(s.get("from"))
#                 hi = int(s.get("to"))
#                 mk = float(s.get("marks"))
#             except (TypeError, ValueError):
#                 continue
#             if lo > 0 and hi >= lo and mk > 0:
#                 clean.append({"from": lo, "to": hi, "marks": mk, "label": str(s.get("label") or "")})
#         clean.sort(key=lambda r: r["from"])
#         return clean, int(i_tok or 0), int(o_tok or 0)
#     except Exception:
#         return [], 0, 0


# def format_section_context(section_map):
#     """Render a section map into instructional text spliced into EVERY per-page prompt via
#     `{extra_context}`, so a page that only sees a section's LOCAL restarted numbering ('1.', '2.',
#     '3.' inside that section's own body) can still report the correct GLOBAL question id (Q21, Q22,
#     Q23) -- the mapping a single page's own text cannot supply on its own. Returns '' when there is no
#     section map (a plain-numbered paper), so the placeholder is spliced out to nothing and behaviour
#     is unchanged for papers that need no such correction."""
#     if not section_map:
#         return ""
#     lines = ["\n\nDOCUMENT-WIDE SECTION MAP (derived from this paper's own General "
#             "Instructions/section headers -- authoritative for GLOBAL question numbering):"]
#     for s in section_map:
#         lines.append(f"- {s['label'] or 'Section'}: GLOBAL question numbers Q{s['from']}-Q{s['to']}, "
#                      f"{s['marks']:g} mark(s) each.")
#     lines.append(
#         "\nIMPORTANT -- LOCAL vs GLOBAL numbering: some papers print each section's own questions "
#         "with a LOCAL count that RESTARTS at 1 within that section's body (e.g. the first question "
#         "physically printed under a section heading is labelled plain '1.', the next '2.', and so on) "
#         "even though the paper's own General Instructions assign that section a DIFFERENT global "
#         "range (see the map above, e.g. a section whose instructions say 'Q21-26' but whose body "
#         "prints '1.' through '6.'). When you can tell WHICH SECTION a question on THIS page belongs "
#         "to -- from a section heading on or just above this page, or from its position/content "
#         "matching one of the sections described above -- you MUST report its id using the GLOBAL "
#         "number from that section's range, NEVER the bare local number printed next to it. Example: "
#         "if this page falls under the section mapped to Q21-26 and shows a locally-numbered '3.', "
#         "its correct id is 'Q23' (the section's first global number plus the local number minus 1), "
#         "never a bare '3'. If you cannot confidently tell which section a question belongs to, keep "
#         "its id exactly as printed rather than guessing a global number.")
#     return "\n".join(lines)






"""Per-page parallel parsing for question papers + answer keys.

The single-call parsers (one LLM call over the whole document's text) are SLOW on the flagship 235B
model (~180s for a 3-page key) and 235B sometimes returns an empty {} for a whole question paper. This
module splits a PDF into pages and extracts the questions/answers from each page CONCURRENTLY -- many
small, fast, reliable calls -- then runs ONE global pass to detect the things that need the whole-paper
view (metadata + internal choices). Net: the accurate 235B model becomes fast enough to use, and each
call is a small, easy task.

Shared by extract_json_from_key.py and extract_json_from_question_paper.py. Gated by
PARSER_PARALLEL_PAGES (default on); falls back to the caller's single-call path for docx / 1-page PDFs /
when disabled, so nothing regresses.
"""
import os
import re
import json
import difflib
import concurrent.futures

from llm_client import generate, strip_reasoning


def parallel_enabled():
    return os.environ.get("PARSER_PARALLEL_PAGES", "1").strip().lower() not in ("0", "false", "no", "off")


def pdf_page_texts(path):
    """List of per-page extracted text (one string per PDF page). Text layer only (same as the
    single-call extractor); a scanned/no-text page yields ''."""
    import PyPDF2
    pages = []
    with open(path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        for pg in reader.pages:
            try:
                pages.append(pg.extract_text() or "")
            except Exception:
                pages.append("")
    return pages


# ---------------------------------------------------------------------------------------------------
# REPEATING BOILERPLATE (page headers/footers). Many exam-board PDFs print the same disclaimer/footer
# line ("*Please note that the assessment scheme ... will continue ...", "Page N") at the bottom (or
# top) of EVERY page. That text is real content on the PAGE but is NEVER part of any question -- yet a
# naive per-page extraction (and especially the look-ahead window in extract_pages_parallel, which
# hands a slice of raw next-page text to the model as "possible continuation") can pick it up and
# splice it into a question's text, interleaved with the question's own real continuation. Detected
# GENERICALLY (a line that recurs verbatim, or near-verbatim, on 3+ pages) rather than by a hardcoded
# phrase, so it adapts to whatever boilerplate a given exam board happens to use.
#
# `min_line_len` is the guard that keeps this from misfiring on short STRUCTURAL keywords: a bare "OR"
# separating two internal-choice alternatives legitimately appears near-identically on many pages of a
# CBSE-style paper, and without a length floor it looks EXACTLY like a repeating footer to the same
# frequency-based detector -- which is precisely what happened once: "OR" got silently stripped from
# every internal-choice question, which is a correctness bug (full_evaluator's choice-group detection
# keys off a standalone "OR"; losing it makes an "answer either A or B" question get graded as
# "answer BOTH A and B", inflating marks). A genuine disclaimer/footer line is always a real sentence
# (well over 15 characters), so raising the floor costs nothing on the boilerplate this exists to catch
# while making short recurring keywords structurally ineligible to be flagged at all.
# ---------------------------------------------------------------------------------------------------
def _detect_boilerplate_lines(page_texts, min_repeats=3, max_line_len=200, min_line_len=15):
    """Lines that repeat near-identically across at least `min_repeats` pages -- almost certainly a
    running header/footer, never question content (a real question is essentially never repeated
    verbatim, word-for-word, across three-plus separate pages). Short-circuits to an empty set for a
    short document (fewer than min_repeats pages), since "repeats across pages" is meaningless there.

    Only considers lines with length in [min_line_len, max_line_len]: too short and it risks catching
    a legitimately-repeating structural keyword ("OR", a section label) rather than a footer sentence;
    too long and it is unlikely to be a single clean recurring line at all."""
    if len(page_texts) < min_repeats:
        return set()
    from collections import Counter
    counts = Counter()
    for text in page_texts:
        seen_this_page = set()
        for raw in (text or "").split("\n"):
            line = raw.strip()
            if not line or len(line) < min_line_len or len(line) > max_line_len:
                continue
            key = re.sub(r"\s+", " ", line).lower()
            key = re.sub(r"\bpage\s*\d+\b", "page #", key)          # "Page 2" / "Page 7" -> same bucket
            if key not in seen_this_page:                          # count each page at most once
                counts[key] += 1
                seen_this_page.add(key)
    return {k for k, c in counts.items() if c >= min_repeats}


def _strip_boilerplate(text, boilerplate_keys):
    """Remove any line of `text` that matches a detected boilerplate key (see
    _detect_boilerplate_lines), collapsing the resulting blank-line runs. No-op when there is no
    boilerplate to strip, so a document with no repeating header/footer is completely unaffected."""
    if not boilerplate_keys or not text:
        return text
    out = []
    for raw in text.split("\n"):
        line = raw.strip()
        key = re.sub(r"\s+", " ", line).lower()
        key = re.sub(r"\bpage\s*\d+\b", "page #", key)
        if key in boilerplate_keys:
            continue
        out.append(raw)
    cleaned = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _sanitize_json_escapes(s):
    """Double any backslash that is not a valid JSON escape, so LaTeX-heavy math (\\frac, \\sqrt) parses
    instead of raising 'Invalid \\escape'. Mirrors extract_json_from_key._sanitize_json_escapes."""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c != "\\":
            out.append(c); i += 1; continue
        nxt = s[i + 1] if i + 1 < n else ""
        if nxt in '"\\/':
            out.append("\\" + nxt); i += 2
        elif nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", s[i + 2:i + 6] or ""):
            out.append(s[i:i + 6]); i += 6
        elif nxt == "n":
            out.append("\\n"); i += 2
        elif nxt in "tfbr" and not (i + 2 < n and s[i + 2].isalpha()):
            out.append("\\" + nxt); i += 2
        else:
            out.append("\\\\"); i += 1
    return "".join(out)


def tolerant_json(text):
    """Parse model JSON, tolerating <think> blocks, unescaped LaTeX backslashes, and leading/trailing
    noise. Returns {} on total failure (a page with no parseable questions is not fatal)."""
    content = strip_reasoning((text or "").strip())
    for candidate in (content, _sanitize_json_escapes(content)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            m = re.search(r"(\{.*\})", candidate, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(1))
                except json.JSONDecodeError:
                    continue
    return {}


def _questions_of(obj):
    if isinstance(obj, dict) and isinstance(obj.get("questions"), dict):
        return obj["questions"]
    return obj if isinstance(obj, dict) else {}


def _strip_stray_answer_field(questions):
    """Drop any 'answer' key a question-paper extraction call accidentally emitted, despite the prompt
    explicitly saying a question paper carries NO answer field. This can slip in on a targeted
    recovery/backfill call (which reuses the same general-purpose JSON-parsing scaffolding as the
    answer-key parser) even though the prompt forbids it. Harmless if left in (nothing downstream reads
    it), but stripped here so the output stays exactly the shape the rest of the pipeline expects.
    No-op (and safe) on a dict that never had the field."""
    for v in questions.values():
        if isinstance(v, dict):
            v.pop("answer", None)
    return questions


def extract_pages_parallel(page_texts, prompt_template, model, max_tokens, max_workers=None,
                           extra_context=""):
    """Extract questions from every page CONCURRENTLY. `prompt_template` contains a `{page_text}`
    placeholder and must ask for {"questions": {...}} for THAT page only. `extra_context` (optional)
    is spliced in via a `{extra_context}` placeholder in the template -- used to give every page the
    SAME document-wide context (e.g. a section-numbering map) that a single page's own text cannot
    supply; templates without that placeholder are unaffected (the .replace is then simply a no-op).

    Each call is given an OVERLAPPING WINDOW -- this page's own text PLUS a chunk of the NEXT page's
    leading text -- rather than the page in strict isolation. This is what lets a question whose tail
    CONTINUES onto the next page WITHOUT repeating its own number (a case-study's numbered sub-parts
    printed just below a page break, a table continuing across pages) still be captured under its
    correct id, complete. OWNERSHIP of a boundary-spanning question is assigned to the EARLIER page
    (via this look-ahead); the prompt_template is expected to instruct the LATER page to ignore an
    unlabeled leading continuation rather than re-capturing it under a guessed id -- see
    extract_json_from_question_paper.QP_PER_PAGE_PROMPT for the actual wording. That ownership split
    is what prevents the same boundary-spanning content from being captured (and therefore merged and
    duplicated) by BOTH the earlier and the later page's calls.

    The next page's leading text is first stripped of any REPEATING BOILERPLATE (a running
    header/footer detected across the whole document -- see _detect_boilerplate_lines) before it is
    used as look-ahead. Without that, a footer line sitting right at the top of the next page's text
    (many PDF text-extraction orders put the footer before the real body) gets treated as "possible
    continuation" and spliced into whatever question is open on the earlier page, interleaved with
    that question's own real continuation. The boilerplate detector has a minimum-line-length floor
    specifically so it can never mistake a short, legitimately-repeating structural keyword (a bare
    "OR" between internal-choice alternatives) for a footer -- see that function's docstring.

    `_merge_into` additionally applies FUZZY near-duplicate detection as a second, independent line of
    defense: even if a duplicate slips through (e.g. the model re-captures a boundary question despite
    the prompt's instruction not to, or the two captures differ only in incidental formatting), the
    merge step recognises the near-duplicate and keeps the better version instead of concatenating
    both -- so a bug in either layer alone cannot reach the final output as visible duplication.

    Returns (merged_questions_dict, total_in_tokens, total_out_tokens). Pages with no questions
    contribute nothing; a failed page is skipped (never aborts the whole parse)."""
    workers = int(max_workers or os.environ.get("PARSER_MAX_WORKERS", "8"))
    try:
        lookahead_chars = int(os.environ.get("PARSER_LOOKAHEAD_CHARS", "1200"))
    except (TypeError, ValueError):
        lookahead_chars = 1200
    non_empty = [(i, t) for i, t in enumerate(page_texts) if (t or "").strip()]
    boilerplate = _detect_boilerplate_lines([t for _i, t in non_empty])

    _LOOKAHEAD_FENCE = (
        "\n\n[--- START OF NEXT PAGE (shown for CONTEXT ONLY, so you can tell whether the LAST "
        "question on THIS page continues here). A passage below this fence with NO new question "
        "number belongs to the last question ABOVE the fence -- fold it into that question's text. "
        "Do NOT start a new question purely because you see more text after this fence, and do NOT "
        "treat this fence's content as belonging to THIS page if it clearly starts a brand-new "
        "numbered question -- in that case just ignore it here (the next page's own call will "
        "capture it under its own number). ---]\n\n"
    )

    def _windowed_text(pos):
        idx, text = non_empty[pos]
        if lookahead_chars <= 0 or pos + 1 >= len(non_empty):
            return text
        _next_idx, next_text_raw = non_empty[pos + 1]
        next_text = _strip_boilerplate(next_text_raw, boilerplate)
        lookahead = (next_text or "").strip()[:lookahead_chars]
        if not lookahead:
            return text
        return text + _LOOKAHEAD_FENCE + lookahead

    def one_page(pos, text):
        prompt = prompt_template.replace("{page_text}", text).replace("{extra_context}", extra_context or "")
        try:
            out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
                                         max_tokens=max_tokens, json_mode=True, thinking_budget=0)
            return _questions_of(tolerant_json(out)), int(i_tok or 0), int(o_tok or 0)
        except Exception:
            return {}, 0, 0

    per_page = [None] * len(non_empty)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(non_empty) or 1)) as ex:
        futs = {ex.submit(one_page, pos, _windowed_text(pos)): pos for pos in range(len(non_empty))}
        for fut in concurrent.futures.as_completed(futs):
            per_page[futs[fut]] = fut.result()

    merged, in_tok, out_tok = {}, 0, 0
    for res in per_page:                                # keep page order for stable cross-page merge
        if not res:
            continue
        qs, i_tok, o_tok = res
        in_tok += i_tok; out_tok += o_tok
        if isinstance(qs, dict):
            _merge_into(merged, qs)
    # Final cleanup pass: strip any lingering boilerplate that made it into a question's own text
    # (e.g. a footer sitting at the very END of a page, ahead of that page's own next-page look-ahead
    # rather than inside it) and any stray 'answer' field a question-paper call should never emit.
    for v in merged.values():
        if isinstance(v, dict) and v.get("question"):
            v["question"] = _strip_boilerplate(str(v["question"]), boilerplate) or v["question"]
    return merged, in_tok, out_tok


def _normalize_ws(s):
    """Whitespace-collapsed, lowercased view of a string, for fuzzy comparison that ignores
    incidental formatting differences (extra spaces, curly vs straight quotes handled by str.lower()
    doing nothing harmful to them, line-break placement) between two captures of the SAME content."""
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _is_near_duplicate(a, b, threshold=0.90):
    """True when `a` and `b` are, for all practical purposes, the SAME content -- identical after
    whitespace normalization, one fully contains the other, or they are highly similar by
    difflib's ratio (catches near-identical re-transcriptions that differ only in minor wording/
    punctuation, e.g. a curly vs straight apostrophe or a missing word from an OCR quirk). Deliberately
    conservative (threshold 0.90): two DIFFERENT continuations of the same question (e.g. two more
    rows of a genuinely growing table) must never be caught here, only genuine duplicates."""
    if not a or not b:
        return False
    na, nb = _normalize_ws(a), _normalize_ws(b)
    if na == nb:
        return True
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if shorter and shorter in longer:
        return True
    if not na or not nb:
        return False
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


def _merge_texts(a, b):
    """Merge two text chunks for the SAME field of the SAME question, collapsing three distinct
    situations instead of naively concatenating (which is what produced literal duplicate paragraphs
    when a boundary-spanning question was independently captured, with slightly different
    formatting, by both the page before and the page after a page break):

      1. Near-duplicate (see _is_near_duplicate) -- keep whichever is LONGER (assumed more complete),
         discard the other entirely. Handles the exact failure mode above.
      2. One is a straightforward superset of the other (a is a prefix/subset of b, or vice versa) --
         keep the superset.
      3. Genuine partial overlap at the a-tail / b-head boundary (the classic page-break case where
         the two chunks share a run of text right at the join) -- splice the overlap once instead of
         duplicating it.
      4. No meaningful overlap at all -- plain concatenation (today's original behaviour), used only
         when none of the smarter cases above apply.
    """
    a, b = a or "", b or ""
    if not b:
        return a
    if not a:
        return b
    if _is_near_duplicate(a, b):
        return b if len(b) > len(a) else a
    na, nb = _normalize_ws(a), _normalize_ws(b)
    if nb and nb in na:
        return a
    if na and na in nb:
        return b
    max_overlap = min(len(a), len(b), 400)
    for k in range(max_overlap, 19, -1):
        if _normalize_ws(a[-k:]) == _normalize_ws(b[:k]):
            return (a + b[k:]).strip()
    return (a + "\n" + b).strip()


def _merge_into(acc, page_qs):
    """Merge one page's questions into the accumulator. A question id that appears from more than one
    source (a long answer that spilled across a page break, or the look-ahead window re-surfacing
    content near a boundary) is combined via `_merge_texts`, which recognises and collapses a
    near-duplicate instead of concatenating it -- see that function's docstring for the failure mode
    this specifically fixes. Marks are kept as the MAX seen (never summed -- so a continuation can't
    double the marks)."""
    for qid, v in page_qs.items():
        if not isinstance(v, dict):
            continue
        key = str(qid).strip()
        if not key:
            continue
        if key not in acc:
            acc[key] = dict(v)
            acc[key].setdefault("question_id", key)
            continue
        prev = acc[key]
        for fld in ("question", "answer"):
            prev[fld] = _merge_texts(str(prev.get(fld, "") or ""), str(v.get(fld, "") or ""))
        try:
            prev["marks"] = max(float(prev.get("marks", 0) or 0), float(v.get("marks", 0) or 0))
        except (TypeError, ValueError):
            pass


def global_metadata_pass(full_text, prompt_template, model, max_tokens):
    """One call over the WHOLE paper to detect what needs global context: metadata (class, subject) +
    internal choices (choice_groups / inline_choice_ids). `prompt_template` has a `{full_text}`
    placeholder. Returns (metadata_dict, in_tok, out_tok); {} on failure (choices just won't be
    applied, which the downstream structural detector + reconciler still catch)."""
    prompt = prompt_template.replace("{full_text}", full_text)
    try:
        out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
                                     max_tokens=max_tokens, json_mode=True, thinking_budget=0)
        data = tolerant_json(out)
        meta = data.get("metadata", data) if isinstance(data, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        return meta, int(i_tok or 0), int(o_tok or 0)
    except Exception:
        return {}, 0, 0


# ---------------------------------------------------------------------------------------------------
# SECTION-MARKS MAP -- a global, document-wide pass that reads ONLY this paper's own section headers
# ("SECTION A -- MCQ (20 x 1 = 20)", "Section D consists of 4 questions ... Each question carries
# 5 Marks", ..., or a one-line summary such as "B: Q21-26 VSA, 2 marks each") and turns them into an
# authoritative {question-number-range -> per-question marks} table. Nothing here is templated to any
# known board/pattern: every paper's section boundaries and marks are read fresh from THAT paper's own
# printed headers/instructions, because different papers number their sections completely differently.
#
# Serves THREE distinct purposes for the caller (extract_json_from_question_paper.py), all reusing this
# SAME one call so a paper only ever pays for it once:
#   1. MARKS       -- corrects a question whose own page/parse got the wrong (or 0) per-question marks.
#   2. NUMBERING   -- lets a page whose questions are printed with a section-local restarted numbering
#                     ("1.", "2.", "3." inside that section's own body) resolve to the correct GLOBAL id
#                     (see format_section_context, fed back into each page's prompt BEFORE it is parsed).
#   3. COMPLETENESS -- gives the total expected question count (the highest "to" across every mapped
#                     section) so the caller can detect and recover any question that silently failed
#                     to parse on every page/pass, instead of the paper quietly coming back short.
# ---------------------------------------------------------------------------------------------------
SECTION_MARKS_PROMPT = """You are given the FULL text of an exam question paper. Find EVERY section
header or summary line that states how many questions are in it, its GLOBAL question-number range,
and how many marks each one carries -- for example "SECTION A -- MCQ (20 x 1 = 20)", "Section B
consists of 7 questions (22 to 28). Each question carries 2 Marks", "SECTION D (4 x 5 = 20 Marks)",
"B: Q21-26 VSA, 2 marks each", or any similar wording this specific paper uses (this information is
very often stated once, near the top, in the paper's General Instructions, even when the section
BODIES further down print their own questions with different-looking local numbering).

For EACH such section, work out the GLOBAL QUESTION NUMBER RANGE it covers (its first and last
question number AS THE PAPER'S OWN OVERALL NUMBERING SCHEME COUNTS THEM, e.g. Q21 to Q26 -- NOT the
section body's own possibly-restarted local numbering) and the MARKS PER QUESTION in that section --
using ONLY what is printed or stated in THIS paper. Do NOT assume any standard, typical, or
previously-seen layout: different papers number their sections completely differently, so the range
and marks must come from what is actually written here.

- If a section states an explicit global question range ("Q21 to Q25", "Questions 22 to 28", "B:
  Q21-26", "E: Q37-39"), use exactly that range.
- If a section does not state its range explicitly, infer it from which GLOBAL question numbers are
  physically listed under that header, up to (but not including) the next section header.
- For a section made of multi-part / case-study questions (each WHOLE question is worth one total
  even though its own sub-parts carry different sub-marks that add up to that total), report the
  section's per-QUESTION total, not a sub-part's marks.
- Cover EVERY section in the paper, including the LAST one -- do not stop early. A paper's very last
  section (often case-study/long-answer questions near the end) is just as important to map as its
  first.
- If the paper has NO section structure at all (marks are printed individually next to every single
  question, with no grouping headers), return an empty list -- do not invent sections.

Return ONLY this JSON shape:
{{"sections": [{{"from": <first GLOBAL question number as an integer>, "to": <last GLOBAL question
number as an integer>, "marks": <marks per question in this section, as a number>, "label": "<the
section name or heading exactly as printed>"}}, ...]}}
ordered by "from". Return {{"sections": []}} if you cannot find this structure.

FULL QUESTION PAPER TEXT:
{full_text}

Return ONLY the raw JSON. No markdown."""


def extract_section_marks_map(full_text, model, max_tokens=None):
    """One document-wide call that turns THIS paper's own section headers/instructions into an
    authoritative [{"from", "to", "marks", "label"}, ...] list of GLOBAL question-number ranges,
    sorted by "from". Purely descriptive of what this specific paper prints -- no template, no assumed
    board/pattern, so it produces a different map for every paper. Returns ([], 0, 0) on failure or
    when the paper has no such structure, so a caller can always fall back to whatever the
    per-page/single-call parse already produced."""
    max_tokens = max_tokens or int(os.environ.get("QP_SECTION_MARKS_MAX_TOKENS", "2048"))
    prompt = SECTION_MARKS_PROMPT.replace("{full_text}", full_text)
    try:
        out, i_tok, o_tok = generate(model=model, prompt=prompt, temperature=0.0,
                                     max_tokens=max_tokens, json_mode=True, thinking_budget=0)
        data = tolerant_json(out)
        secs = data.get("sections") if isinstance(data, dict) else None
        if not isinstance(secs, list):
            return [], int(i_tok or 0), int(o_tok or 0)
        clean = []
        for s in secs:
            if not isinstance(s, dict):
                continue
            try:
                lo = int(s.get("from"))
                hi = int(s.get("to"))
                mk = float(s.get("marks"))
            except (TypeError, ValueError):
                continue
            if lo > 0 and hi >= lo and mk > 0:
                clean.append({"from": lo, "to": hi, "marks": mk, "label": str(s.get("label") or "")})
        clean.sort(key=lambda r: r["from"])
        return clean, int(i_tok or 0), int(o_tok or 0)
    except Exception:
        return [], 0, 0


def format_section_context(section_map):
    """Render a section map into instructional text spliced into EVERY per-page prompt via
    `{extra_context}`, so a page that only sees a section's LOCAL restarted numbering ('1.', '2.',
    '3.' inside that section's own body) can still report the correct GLOBAL question id (Q21, Q22,
    Q23) -- the mapping a single page's own text cannot supply on its own. Returns '' when there is no
    section map (a plain-numbered paper), so the placeholder is spliced out to nothing and behaviour
    is unchanged for papers that need no such correction."""
    if not section_map:
        return ""
    lines = ["\n\nDOCUMENT-WIDE SECTION MAP (derived from this paper's own General "
            "Instructions/section headers -- authoritative for GLOBAL question numbering):"]
    for s in section_map:
        lines.append(f"- {s['label'] or 'Section'}: GLOBAL question numbers Q{s['from']}-Q{s['to']}, "
                     f"{s['marks']:g} mark(s) each.")
    lines.append(
        "\nIMPORTANT -- LOCAL vs GLOBAL numbering: some papers print each section's own questions "
        "with a LOCAL count that RESTARTS at 1 within that section's body (e.g. the first question "
        "physically printed under a section heading is labelled plain '1.', the next '2.', and so on) "
        "even though the paper's own General Instructions assign that section a DIFFERENT global "
        "range (see the map above, e.g. a section whose instructions say 'Q21-26' but whose body "
        "prints '1.' through '6.'). When you can tell WHICH SECTION a question on THIS page belongs "
        "to -- from a section heading on or just above this page, or from its position/content "
        "matching one of the sections described above -- you MUST report its id using the GLOBAL "
        "number from that section's range, NEVER the bare local number printed next to it. Example: "
        "if this page falls under the section mapped to Q21-26 and shows a locally-numbered '3.', "
        "its correct id is 'Q23' (the section's first global number plus the local number minus 1), "
        "never a bare '3'. If you cannot confidently tell which section a question belongs to, keep "
        "its id exactly as printed rather than guessing a global number.")
    return "\n".join(lines)