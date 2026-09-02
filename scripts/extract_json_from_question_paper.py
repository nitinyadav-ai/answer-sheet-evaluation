# import os
# import sys
# import json
# import warnings
# import re
# # Suppress all warnings to keep stdout clean for JSON
# warnings.filterwarnings("ignore")

# from pathlib import Path

# # Reuse the answer-key parser's shared helpers rather than duplicating them. The key parser lives
# # beside this file; add its directory to the path so the import resolves even when this script is
# # run as a subprocess from the Flask app's cwd. Forking the parser (instead of parametrizing it)
# # keeps each prompt single-purpose so a question-paper prompt change can never regress key parsing.
# sys.path.append(os.path.dirname(os.path.abspath(__file__)))
# from extract_json_from_key import (
#     extract_text_from_docx,
#     extract_text_from_pdf,
#     _sanitize_json_escapes,
#     _load_project_env,
# )
# from llm_client import generate, strip_reasoning
# import parallel_parse as pp


# def parse_question_paper_with_gemini(text):
#     # Make .env settings (provider + model + keys) visible whether run standalone or from Flask.
#     _load_project_env()
#     # Reuse KEY_PARSER_MODEL so the key + question-paper parsers move together. Default to a capable
#     # Qwen3-VL Instruct model.
#     model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")

#     prompt = f"""
#     Extract the questions from the following EXAM QUESTION PAPER into a structured JSON object.

#     Return a JSON object with EXACTLY one top-level key:
#     1. "questions": a dictionary where keys are question IDs (e.g., Q1, Q2, or specific IDs like
#        Q31(a), Q31(b) exactly as printed). Each value is a dictionary with:
#        - question_id: string
#        - question: string (the FULL, verbatim text of the question, including any data table,
#          passage, figure caption, or sub-prompt the student must read in order to answer it)
#        - marks: number (maximum marks for this question if printed; otherwise 0)
#        - type: string (one of: MCQ, Short Answer, Long Answer, Numerical)

#     CRITICAL RULES:
#     - This is a QUESTION PAPER, not a marking scheme. DO NOT invent, infer, or include answers.
#       There is NO "answer" field. Capture ONLY what the paper asks.
#     - Capture EVERY numbered question, INCLUDING short OBJECTIVE questions (MCQ / True-False /
#       fill-in-the-blank / one-line "predict the output" / SQL / assertion-reason) -- e.g. a
#       "Section A, 1 mark each" list. Do NOT skip a whole section, and do NOT mistake objective
#       questions or a section that follows the General Instructions for instructions.
#     - Use the EXACT question IDs as printed (Q1, Q31(a), Q31(b), ...) so they can be matched to the
#       answer key later.
#     - "marks": use the number printed for each question; if only a section header gives it
#       ("Section A ... Each question carries 1 Mark"), apply that per-question value to that section.
#     - Preserve the full question wording; do not summarize or truncate it.

#     Text to parse:
#     {text}

#     Return ONLY the raw JSON. No markdown blocks.
#     """

#     # A full question paper overflows the default output cap, silently truncating the JSON. Raise the
#     # ceiling + force JSON mode. thinking_budget=0 keeps this verbatim EXTRACTION (Gemini-only knob;
#     # ignored by Qwen). strip_reasoning drops any <think> block a Qwen -Thinking model emits.
#     text_out, _in_tok, _out_tok = generate(
#         model=model_id, prompt=prompt, temperature=0.0,
#         # 32768 is ample for a 40-50 question paper; env-tunable (shared with the key parser).
#         # OpenRouter PRE-AUTHORISES max_tokens against your balance, so keep this modest.
#         max_tokens=int(os.environ.get("KEY_PARSER_MAX_TOKENS", "32768")),
#         json_mode=True, thinking_budget=0,
#     )
#     content = strip_reasoning((text_out or "").strip())

#     # JSON mode returns clean JSON, but a math/symbol-heavy paper can carry UNescaped backslashes
#     # (\frac, \sqrt) -> "Invalid \escape". Try the raw text, then a backslash-repaired copy, each
#     # with a brace-extraction fallback for any leading/trailing noise (same as the key parser).
#     for candidate in (content, _sanitize_json_escapes(content)):
#         try:
#             return json.loads(candidate)
#         except json.JSONDecodeError:
#             m = re.search(r'(\{.*\})', candidate, re.DOTALL)
#             if m:
#                 try:
#                     return json.loads(m.group(1))
#                 except json.JSONDecodeError:
#                     continue
#     raise Exception(f"AI response was not valid JSON (length {len(content)}): {content[:120]}...")


# # ---------------------------------------------------------------------------------------------------
# # PER-PAGE PARALLEL PATH. Each page's questions are extracted concurrently as WHOLE-question entries
# # carrying the question's TOTAL printed marks -- so the paper is the clean per-question marks authority
# # (Q36 case study -> one entry worth 4, never split into leaves the reconciler would mis-total).
# # ---------------------------------------------------------------------------------------------------
# QP_PER_PAGE_PROMPT = """Extract the exam questions that appear on THIS ONE PAGE of a QUESTION PAPER into a JSON object.

# Return ONLY: {"questions": {"<Qn>": {"question_id": "Qn", "question": "<full verbatim text>", "marks": <number>, "type": "..."}, ...}}

# CRITICAL RULES for THIS PAGE:
# - Extract EVERY numbered question printed on this page. A page often MIXES non-question text (a cover heading, "General Instructions", or a "Section A / Section B ..." header) WITH questions -- capture the questions and simply ignore the surrounding non-question text. NEVER skip a question just because the page also has a heading or instructions above it.
# - Short OBJECTIVE questions ARE questions and must be captured just like long ones: MCQ, True/False, fill-in-the-blank, one-line "predict the output", SQL, and assertion-reason. Keep their (a)/(b)/(c)/(d) options inside the question text. A "Section A, 1 mark each" list of one-mark questions must be captured in FULL (every number in the range).
# - ONE entry per QUESTION NUMBER (Q1, Q22, Q36). Do NOT split a question into separate (a)/(b)/(i)/(ii) entries -- keep ALL of its sub-parts, data tables, passages, figures and any "OR" alternatives INSIDE that one question's "question" text.
# - "marks": the marks for that WHOLE question -- a number at the end of the question, in the margin/brackets, or a "Marks" column. If only a SECTION header on this page states the per-question marks (e.g. "Section A ... Each question carries 1 Mark" or "Section -A (21 x 1 = 21 Marks)"), use that value for every question in that section. A question offering "(a) ... OR (b) ..." is worth its single printed total -- do NOT add the alternatives. Use 0 only if no marks are printed or implied anywhere.
# - This is a QUESTION PAPER: capture ONLY what is asked. Do NOT invent, infer, or include answers. There is NO "answer" field.
# - If a question started on an earlier page and continues here, still emit it under its number with the text visible here (it will be merged).
# - "type": one of MCQ, Short Answer, Long Answer, Numerical.
# - Return {"questions": {}} ONLY when this page has NO numbered questions at all (a pure cover/instructions page with nothing numbered below).

# THIS PAGE:
# {page_text}

# Return ONLY the raw JSON. No markdown."""


# def parse_qp_parallel(page_texts):
#     """Per-page parallel extraction of WHOLE-question entries (with total marks) -> {questions}. No
#     global choice pass: the paper's authority is the per-question TOTAL, so no OR-splitting is needed."""
#     _load_project_env()
#     model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
#     per_page_max = int(os.environ.get("KEY_PARSER_PAGE_MAX_TOKENS", "8192"))
#     questions, _i, _o = pp.extract_pages_parallel(page_texts, QP_PER_PAGE_PROMPT, model_id, per_page_max)
#     return {"questions": questions}


# def main():
#     if len(sys.argv) < 2:
#         print("Usage: python3 extract_json_from_question_paper.py <file_path>")
#         sys.exit(1)

#     file_path = sys.argv[1]
#     ext = Path(file_path).suffix.lower()

#     try:
#         if ext == '.json':
#             with open(file_path, 'r') as f:
#                 content = f.read()
#                 json.loads(content)          # validate, then echo a raw .json upload through unchanged
#                 print(content)
#                 return
#         elif ext == '.docx':
#             raw_text = extract_text_from_docx(file_path)
#             if not raw_text.strip():
#                 print("ERROR: No text extracted from file.")
#                 sys.exit(1)
#             parsed_json = parse_question_paper_with_gemini(raw_text)   # docx -> single call
#         elif ext == '.pdf':
#             page_texts = pp.pdf_page_texts(file_path)
#             raw_text = "\n".join(page_texts)
#             if not raw_text.strip():
#                 print("ERROR: No text extracted from file.")
#                 sys.exit(1)
#             if pp.parallel_enabled() and len([t for t in page_texts if t.strip()]) > 1:
#                 parsed_json = parse_qp_parallel(page_texts)
#             else:
#                 parsed_json = parse_question_paper_with_gemini(raw_text)
#         else:
#             print(f"ERROR: Unsupported file extension {ext}")
#             sys.exit(1)

#         print(json.dumps(parsed_json, indent=2))

#     except Exception as e:
#         print(f"ERROR: {str(e)}", file=sys.stderr)
#         sys.exit(1)


# if __name__ == "__main__":
#     main()






# import os
# import sys
# import json
# import warnings
# import re
# # Suppress all warnings to keep stdout clean for JSON
# warnings.filterwarnings("ignore")

# from pathlib import Path

# # Reuse the answer-key parser's shared helpers rather than duplicating them. The key parser lives
# # beside this file; add its directory to the path so the import resolves even when this script is
# # run as a subprocess from the Flask app's cwd. Forking the parser (instead of parametrizing it)
# # keeps each prompt single-purpose so a question-paper prompt change can never regress key parsing.
# sys.path.append(os.path.dirname(os.path.abspath(__file__)))
# from extract_json_from_key import (
#     extract_text_from_docx,
#     extract_text_from_pdf,
#     _sanitize_json_escapes,
#     _load_project_env,
# )
# from llm_client import generate, strip_reasoning
# import parallel_parse as pp


# def parse_question_paper_with_gemini(text):
#     # Make .env settings (provider + model + keys) visible whether run standalone or from Flask.
#     _load_project_env()
#     # Reuse KEY_PARSER_MODEL so the key + question-paper parsers move together. Default to a capable
#     # Qwen3-VL Instruct model.
#     model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")

#     prompt = f"""
#     Extract the questions from the following EXAM QUESTION PAPER into a structured JSON object.

#     Return a JSON object with EXACTLY one top-level key:
#     1. "questions": a dictionary where keys are question IDs (e.g., Q1, Q2, or specific IDs like
#        Q31(a), Q31(b) exactly as printed). Each value is a dictionary with:
#        - question_id: string
#        - question: string (the FULL, verbatim text of the question, including any data table,
#          passage, figure caption, or sub-prompt the student must read in order to answer it)
#        - marks: number (maximum marks for this question if printed; otherwise 0)
#        - type: string (one of: MCQ, Short Answer, Long Answer, Numerical)

#     CRITICAL RULES:
#     - This is a QUESTION PAPER, not a marking scheme. DO NOT invent, infer, or include answers.
#       There is NO "answer" field. Capture ONLY what the paper asks.
#     - Capture EVERY numbered question, INCLUDING short OBJECTIVE questions (MCQ / True-False /
#       fill-in-the-blank / one-line "predict the output" / SQL / assertion-reason) -- e.g. a
#       "Section A, 1 mark each" list. Do NOT skip a whole section, and do NOT mistake objective
#       questions or a section that follows the General Instructions for instructions.
#     - Use the EXACT question IDs as printed (Q1, Q31(a), Q31(b), ...) so they can be matched to the
#       answer key later.
#     - "marks": use the number printed for each question; if only a section header gives it
#       ("Section A ... Each question carries 1 Mark"), apply that per-question value to EVERY
#       question physically located in that section -- with NO exceptions. A question's marks come
#       from its SECTION, never from how long or complex its text is.
#     - DATA TABLES / EMBEDDED CONTENT DO NOT CHANGE THIS RULE. A question that includes a
#       frequency table, a data list, a figure, or a multi-line passage is STILL just one question
#       belonging to its section, and it STILL inherits that section's per-question marks exactly
#       like every other question around it. Do NOT default such a question's marks to 0 just
#       because the table/data occupies most of the question's space -- the marks come from the
#       SECTION HEADER, not from counting rows in the table.
#       Worked example: a Section D header reads "SECTION D — LA (4 x 5 = 20)" and lists Q32, Q33,
#       Q34, Q35 as its four questions. Q35 happens to print a 9-row age/frequency table before
#       asking to "find the modal age and median age". Q35 is STILL worth 5 marks (the section's
#       per-question value), the SAME as Q32/Q33/Q34 -- NOT 0. Every question inside a section
#       carries that section's marks regardless of whether it has a table, a figure, or a long
#       passage.
#     - SELF-CHECK before returning: for every section that states "N questions x M marks", count
#       how many questions you assigned marks=M in that section. If it is fewer than N, you missed
#       one -- go back and fix its marks instead of leaving it as 0.
#     - Preserve the full question wording; do not summarize or truncate it.

#     Text to parse:
#     {text}

#     Return ONLY the raw JSON. No markdown blocks.
#     """

#     # A full question paper overflows the default output cap, silently truncating the JSON. Raise the
#     # ceiling + force JSON mode. thinking_budget=0 keeps this verbatim EXTRACTION (Gemini-only knob;
#     # ignored by Qwen). strip_reasoning drops any <think> block a Qwen -Thinking model emits.
#     text_out, _in_tok, _out_tok = generate(
#         model=model_id, prompt=prompt, temperature=0.0,
#         # 32768 is ample for a 40-50 question paper; env-tunable (shared with the key parser).
#         # OpenRouter PRE-AUTHORISES max_tokens against your balance, so keep this modest.
#         max_tokens=int(os.environ.get("KEY_PARSER_MAX_TOKENS", "32768")),
#         json_mode=True, thinking_budget=0,
#     )
#     content = strip_reasoning((text_out or "").strip())

#     # JSON mode returns clean JSON, but a math/symbol-heavy paper can carry UNescaped backslashes
#     # (\frac, \sqrt) -> "Invalid \escape". Try the raw text, then a backslash-repaired copy, each
#     # with a brace-extraction fallback for any leading/trailing noise (same as the key parser).
#     for candidate in (content, _sanitize_json_escapes(content)):
#         try:
#             return json.loads(candidate)
#         except json.JSONDecodeError:
#             m = re.search(r'(\{.*\})', candidate, re.DOTALL)
#             if m:
#                 try:
#                     return json.loads(m.group(1))
#                 except json.JSONDecodeError:
#                     continue
#     raise Exception(f"AI response was not valid JSON (length {len(content)}): {content[:120]}...")


# # ---------------------------------------------------------------------------------------------------
# # PER-PAGE PARALLEL PATH. Each page's questions are extracted concurrently as WHOLE-question entries
# # carrying the question's TOTAL printed marks -- so the paper is the clean per-question marks authority
# # (Q36 case study -> one entry worth 4, never split into leaves the reconciler would mis-total).
# # ---------------------------------------------------------------------------------------------------
# QP_PER_PAGE_PROMPT = """Extract the exam questions that appear on THIS ONE PAGE of a QUESTION PAPER into a JSON object.

# Return ONLY: {"questions": {"<Qn>": {"question_id": "Qn", "question": "<full verbatim text>", "marks": <number>, "type": "..."}, ...}}

# CRITICAL RULES for THIS PAGE:
# - Extract EVERY numbered question printed on this page. A page often MIXES non-question text (a cover heading, "General Instructions", or a "Section A / Section B ..." header) WITH questions -- capture the questions and simply ignore the surrounding non-question text. NEVER skip a question just because the page also has a heading or instructions above it.
# - Short OBJECTIVE questions ARE questions and must be captured just like long ones: MCQ, True/False, fill-in-the-blank, one-line "predict the output", SQL, and assertion-reason. Keep their (a)/(b)/(c)/(d) options inside the question text. A "Section A, 1 mark each" list of one-mark questions must be captured in FULL (every number in the range).
# - ONE entry per QUESTION NUMBER (Q1, Q22, Q36). Do NOT split a question into separate (a)/(b)/(i)/(ii) entries -- keep ALL of its sub-parts, data tables, passages, figures and any "OR" alternatives INSIDE that one question's "question" text.
# - "marks": the marks for that WHOLE question -- a number at the end of the question, in the margin/brackets, or a "Marks" column. If only a SECTION header on this page states the per-question marks (e.g. "Section A ... Each question carries 1 Mark" or "Section -A (21 x 1 = 21 Marks)"), use that value for EVERY question in that section, with NO exceptions. A question offering "(a) ... OR (b) ..." is worth its single printed total -- do NOT add the alternatives. Use 0 ONLY if no marks are printed AND no section header on this page (or a header line carried over from an earlier page) implies a value for it.
# - A question that embeds a DATA TABLE, a frequency/grouped-data table, a long passage, or a figure is STILL just one question in its section and STILL inherits that section's per-question marks exactly like the plainer questions around it. Never default such a question to marks=0 just because most of its text is a table -- the marks come from the SECTION, not from how the question is laid out. Example: if the page shows "SECTION D — LA (4 x 5 = 20)" followed by four questions, one of which prints a 9-row age-frequency table before asking to find the modal/median age, that question is STILL worth 5 -- the same as its neighbours.
# - SELF-CHECK before returning: for every section header on this page stating "N questions x M marks", make sure you assigned marks=M to every one of that section's questions on this page, including any question built mostly around a table or figure.
# - This is a QUESTION PAPER: capture ONLY what is asked. Do NOT invent, infer, or include answers. There is NO "answer" field.
# - If a question started on an earlier page and continues here, still emit it under its number with the text visible here (it will be merged).
# - "type": one of MCQ, Short Answer, Long Answer, Numerical.
# - Return {"questions": {}} ONLY when this page has NO numbered questions at all (a pure cover/instructions page with nothing numbered below).

# THIS PAGE:
# {page_text}

# Return ONLY the raw JSON. No markdown."""


# def parse_qp_parallel(page_texts):
#     """Per-page parallel extraction of WHOLE-question entries (with total marks) -> {questions}. No
#     global choice pass: the paper's authority is the per-question TOTAL, so no OR-splitting is needed."""
#     _load_project_env()
#     model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
#     per_page_max = int(os.environ.get("KEY_PARSER_PAGE_MAX_TOKENS", "8192"))
#     questions, _i, _o = pp.extract_pages_parallel(page_texts, QP_PER_PAGE_PROMPT, model_id, per_page_max)
#     return {"questions": questions}


# def main():
#     if len(sys.argv) < 2:
#         print("Usage: python3 extract_json_from_question_paper.py <file_path>")
#         sys.exit(1)

#     file_path = sys.argv[1]
#     ext = Path(file_path).suffix.lower()

#     try:
#         if ext == '.json':
#             with open(file_path, 'r') as f:
#                 content = f.read()
#                 json.loads(content)          # validate, then echo a raw .json upload through unchanged
#                 print(content)
#                 return
#         elif ext == '.docx':
#             raw_text = extract_text_from_docx(file_path)
#             if not raw_text.strip():
#                 print("ERROR: No text extracted from file.")
#                 sys.exit(1)
#             parsed_json = parse_question_paper_with_gemini(raw_text)   # docx -> single call
#         elif ext == '.pdf':
#             page_texts = pp.pdf_page_texts(file_path)
#             raw_text = "\n".join(page_texts)
#             if not raw_text.strip():
#                 print("ERROR: No text extracted from file.")
#                 sys.exit(1)
#             if pp.parallel_enabled() and len([t for t in page_texts if t.strip()]) > 1:
#                 parsed_json = parse_qp_parallel(page_texts)
#             else:
#                 parsed_json = parse_question_paper_with_gemini(raw_text)
#         else:
#             print(f"ERROR: Unsupported file extension {ext}")
#             sys.exit(1)

#         print(json.dumps(parsed_json, indent=2))

#     except Exception as e:
#         print(f"ERROR: {str(e)}", file=sys.stderr)
#         sys.exit(1)


# if __name__ == "__main__":
#     main()






# import os
# import sys
# import json
# import warnings
# import re
# # Suppress all warnings to keep stdout clean for JSON
# warnings.filterwarnings("ignore")

# from pathlib import Path

# # Reuse the answer-key parser's shared helpers rather than duplicating them. The key parser lives
# # beside this file; add its directory to the path so the import resolves even when this script is
# # run as a subprocess from the Flask app's cwd. Forking the parser (instead of parametrizing it)
# # keeps each prompt single-purpose so a question-paper prompt change can never regress key parsing.
# sys.path.append(os.path.dirname(os.path.abspath(__file__)))
# from extract_json_from_key import (
#     extract_text_from_docx,
#     extract_text_from_pdf,
#     _sanitize_json_escapes,
#     _load_project_env,
# )
# from llm_client import generate, strip_reasoning
# import parallel_parse as pp


# def parse_question_paper_with_gemini(text, section_context=""):
#     # Make .env settings (provider + model + keys) visible whether run standalone or from Flask.
#     _load_project_env()
#     # Reuse KEY_PARSER_MODEL so the key + question-paper parsers move together. Default to a capable
#     # Qwen3-VL Instruct model.
#     model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")

#     prompt = f"""
#     Extract the questions from the following EXAM QUESTION PAPER into a structured JSON object.

#     Return a JSON object with EXACTLY one top-level key:
#     1. "questions": a dictionary where keys are question IDs (e.g., Q1, Q2, or specific IDs like
#        Q31(a), Q31(b) exactly as printed). Each value is a dictionary with:
#        - question_id: string
#        - question: string (the FULL, verbatim text of the question, including any data table,
#          passage, figure caption, or sub-prompt the student must read in order to answer it)
#        - marks: number (maximum marks for this question if printed; otherwise 0)
#        - type: string (one of: MCQ, Short Answer, Long Answer, Numerical)

#     CRITICAL RULES:
#     - This is a QUESTION PAPER, not a marking scheme. DO NOT invent, infer, or include answers.
#       There is NO "answer" field. Capture ONLY what the paper asks.
#     - Capture EVERY numbered question in the ENTIRE document, from the first to the LAST section --
#       including short OBJECTIVE questions (MCQ / True-False / fill-in-the-blank / one-line "predict
#       the output" / SQL / assertion-reason) AND the paper's final section (often case-study / long-
#       answer questions). Do NOT skip a whole section, and do NOT stop before the paper's last
#       question just because it is long or comes after several other sections.
#     - Use the paper's GLOBAL question numbering for question IDs (Q1, Q31(a), Q31(b), ...) so they
#       can be matched to the answer key later -- NOT a section's own local/restarted numbering. See
#       the LOCAL vs GLOBAL guidance below if this paper's sections restart their own numbering.
#     - "marks": use the number printed for each question; if only a section header gives it
#       ("Section A ... Each question carries 1 Mark"), apply that per-question value to EVERY
#       question physically located in that section -- with NO exceptions. A question's marks come
#       from its SECTION, never from how long or complex its text is.
#     - DATA TABLES / EMBEDDED CONTENT DO NOT CHANGE THIS RULE. A question that includes a
#       frequency table, a data list, a figure, or a multi-line passage is STILL just one question
#       belonging to its section, and it STILL inherits that section's per-question marks exactly
#       like every other question around it. Do NOT default such a question's marks to 0 just
#       because the table/data occupies most of the question's space -- the marks come from the
#       SECTION HEADER, not from counting rows in the table.
#     - SELF-CHECK before returning: for every section that states "N questions x M marks", count
#       how many questions you assigned marks=M in that section. If it is fewer than N, you missed
#       one -- go back and fix its marks instead of leaving it as 0.
#     - Preserve the full question wording; do not summarize or truncate it.
#     {section_context}

#     Text to parse:
#     {text}

#     Return ONLY the raw JSON. No markdown blocks.
#     """

#     # A full question paper overflows the default output cap, silently truncating the JSON. Raise the
#     # ceiling + force JSON mode. thinking_budget=0 keeps this verbatim EXTRACTION (Gemini-only knob;
#     # ignored by Qwen). strip_reasoning drops any <think> block a Qwen -Thinking model emits.
#     text_out, _in_tok, _out_tok = generate(
#         model=model_id, prompt=prompt, temperature=0.0,
#         # 32768 is ample for a 40-50 question paper; env-tunable (shared with the key parser).
#         # OpenRouter PRE-AUTHORISES max_tokens against your balance, so keep this modest.
#         max_tokens=int(os.environ.get("KEY_PARSER_MAX_TOKENS", "32768")),
#         json_mode=True, thinking_budget=0,
#     )
#     content = strip_reasoning((text_out or "").strip())

#     # JSON mode returns clean JSON, but a math/symbol-heavy paper can carry UNescaped backslashes
#     # (\frac, \sqrt) -> "Invalid \escape". Try the raw text, then a backslash-repaired copy, each
#     # with a brace-extraction fallback for any leading/trailing noise (same as the key parser).
#     for candidate in (content, _sanitize_json_escapes(content)):
#         try:
#             return json.loads(candidate)
#         except json.JSONDecodeError:
#             m = re.search(r'(\{.*\})', candidate, re.DOTALL)
#             if m:
#                 try:
#                     return json.loads(m.group(1))
#                 except json.JSONDecodeError:
#                     continue
#     raise Exception(f"AI response was not valid JSON (length {len(content)}): {content[:120]}...")


# # ---------------------------------------------------------------------------------------------------
# # PER-PAGE PARALLEL PATH. Each page's questions are extracted concurrently as WHOLE-question entries
# # carrying the question's TOTAL printed marks -- so the paper is the clean per-question marks authority
# # (Q36 case study -> one entry worth 4, never split into leaves the reconciler would mis-total).
# #
# # {extra_context} is spliced in by parallel_parse.extract_pages_parallel from
# # parallel_parse.format_section_context(section_map) -- a document-wide section/numbering map computed
# # BEFORE any page is parsed, so a page whose questions are printed with a section-local restarted
# # numbering ("1.", "2.", "3." inside that section, really Q21/Q22/Q23) can still resolve to the correct
# # GLOBAL id. Empty (a plain continuously-numbered paper) -> the placeholder is spliced out to nothing
# # and this prompt behaves exactly as before.
# # ---------------------------------------------------------------------------------------------------
# QP_PER_PAGE_PROMPT = """Extract the exam questions that appear on THIS ONE PAGE of a QUESTION PAPER into a JSON object.

# Return ONLY: {"questions": {"<Qn>": {"question_id": "Qn", "question": "<full verbatim text>", "marks": <number>, "type": "..."}, ...}}

# CRITICAL RULES for THIS PAGE:
# - Extract EVERY numbered question printed on this page, WHEREVER on the page it falls -- including questions near the BOTTOM of the page and questions belonging to the paper's LAST section (often case-study / long-answer questions). A page often MIXES non-question text (a cover heading, "General Instructions", or a "Section A / Section B ..." header) WITH questions -- capture the questions and simply ignore the surrounding non-question text. NEVER skip a question just because the page also has a heading or instructions above it, or because it is the final question on the page or in the paper.
# - Short OBJECTIVE questions ARE questions and must be captured just like long ones: MCQ, True/False, fill-in-the-blank, one-line "predict the output", SQL, and assertion-reason. Keep their (a)/(b)/(c)/(d) options inside the question text. A "Section A, 1 mark each" list of one-mark questions must be captured in FULL (every number in the range).
# - ONE entry per QUESTION NUMBER (Q1, Q22, Q36). Do NOT split a question into separate (a)/(b)/(i)/(ii) entries -- keep ALL of its sub-parts, data tables, passages, figures and any "OR" alternatives INSIDE that one question's "question" text. Bracketed sub-part marks like "[1]", "[2]" inside a case-study question are part of its TEXT, not a signal to split it into separate entries or to treat the text as a JSON array -- keep the whole question (with its brackets) as ONE plain string value.
# - "marks": the marks for that WHOLE question -- a number at the end of the question, in the margin/brackets, or a "Marks" column. If only a SECTION header on this page states the per-question marks (e.g. "Section A ... Each question carries 1 Mark" or "Section -A (21 x 1 = 21 Marks)"), use that value for EVERY question in that section, with NO exceptions. A question offering "(a) ... OR (b) ..." is worth its single printed total -- do NOT add the alternatives. Use 0 ONLY if no marks are printed AND no section header on this page (or a header line carried over from an earlier page) implies a value for it.
# - A question that embeds a DATA TABLE, a frequency/grouped-data table, a long passage, or a figure is STILL just one question in its section and STILL inherits that section's per-question marks exactly like the plainer questions around it. Never default such a question to marks=0 just because most of its text is a table -- the marks come from the SECTION, not from how the question is laid out.
# - SELF-CHECK before returning: for every section header on this page stating "N questions x M marks", make sure you assigned marks=M to every one of that section's questions on this page, including any question built mostly around a table or figure.
# - This is a QUESTION PAPER: capture ONLY what is asked. Do NOT invent, infer, or include answers. There is NO "answer" field.
# - If a question started on an earlier page and continues here, still emit it under its number with the text visible here (it will be merged).
# - "type": one of MCQ, Short Answer, Long Answer, Numerical.
# - Return {"questions": {}} ONLY when this page has NO numbered questions at all (a pure cover/instructions page with nothing numbered below).
# {extra_context}

# THIS PAGE:
# {page_text}

# Return ONLY the raw JSON. No markdown."""


# def _base_qnum_local(qid):
#     """Leading question number from an id like 'Q35', '35', 'Q35(a)' -> 35. None if no digit."""
#     m = re.search(r'(\d+)', str(qid))
#     return int(m.group(1)) if m else None


# def _marks_for_section(base_num, section_map):
#     """The per-question marks the section-map says base_num belongs to, or None if it falls outside
#     every mapped range (e.g. the map is incomplete/empty -- caller then leaves that question alone)."""
#     for s in section_map:
#         if s["from"] <= base_num <= s["to"]:
#             return s["marks"]
#     return None


# def reconcile_marks_with_sections(questions, section_map):
#     """Overlay the document-wide section-marks map (see parallel_parse.extract_section_marks_map)
#     onto the parsed questions: when a question's own marks disagree with (or are missing/zero
#     relative to) its section's stated per-question value, the SECTION value wins -- it was read from
#     the section headers across the WHOLE paper in one pass, so it cannot be fooled by a single page
#     missing its local header.

#     Deliberately does NOT touch a question whose base number falls OUTSIDE every mapped range (an
#     empty/partial map, or a genuinely un-sectioned paper) -- in that case whatever the parser already
#     produced is left exactly as-is, so this can only CORRECT a disagreement, never invent one.

#     Returns (questions, changed) where changed is [(qid, old_marks, new_marks), ...] for logging."""
#     if not section_map:
#         return questions, []
#     changed = []
#     for qid, v in questions.items():
#         if not isinstance(v, dict):
#             continue
#         bn = _base_qnum_local(qid)
#         if bn is None:
#             continue
#         expected = _marks_for_section(bn, section_map)
#         if expected is None:
#             continue
#         try:
#             current = float(v.get("marks") or 0)
#         except (TypeError, ValueError):
#             current = 0.0
#         if abs(current - expected) > 1e-9:
#             changed.append((str(qid), current, expected))
#             v["marks"] = expected
#     return questions, changed


# def _get_section_map(full_text, model_id):
#     """Compute the document-wide section map ONCE (reused for numbering context, marks reconciliation,
#     AND the completeness check below), so a paper only ever pays for this one extra cheap call
#     regardless of how many things consult it. [] on failure or when the paper has no detectable
#     section structure -- every downstream use degrades to a no-op."""
#     if os.environ.get("QP_SECTION_RECONCILE", "1").strip().lower() in ("0", "false", "no", "off"):
#         return []
#     try:
#         section_map, _i, _o = pp.extract_section_marks_map(full_text, model_id)
#         return section_map
#     except Exception as e:
#         print(f"Warning: section map extraction skipped ({e})", file=sys.stderr)
#         return []


# def _expected_total_questions(section_map, full_text):
#     """The highest GLOBAL question number this paper is expected to contain. Primarily the section
#     map's own highest "to" (it was built from the paper's own General Instructions / headers, so it
#     already covers the whole document). Falls back to an explicit "contains N questions" statement in
#     the text when there is no usable section map. None when neither source yields a number -- the
#     completeness check then simply does not run (never a regression versus not having it at all)."""
#     if section_map:
#         return max(s["to"] for s in section_map)
#     m = re.search(r'contains\s+(\d+)\s+questions', full_text, re.IGNORECASE)
#     if m:
#         return int(m.group(1))
#     return None


# def _missing_question_ranges(questions, expected_total):
#     """Contiguous [(lo, hi), ...] ranges of global question numbers 1..expected_total that have NO
#     entry anywhere in `questions` (by base number). Empty when nothing is missing or expected_total
#     is falsy."""
#     if not expected_total:
#         return []
#     present = {bn for qid in questions if (bn := _base_qnum_local(qid)) is not None}
#     missing = sorted(n for n in range(1, expected_total + 1) if n not in present)
#     ranges = []
#     for n in missing:
#         if ranges and n == ranges[-1][1] + 1:
#             ranges[-1] = (ranges[-1][0], n)
#         else:
#             ranges.append((n, n))
#     return ranges


# QP_BACKFILL_PROMPT = """You are given the FULL text of an exam question paper. A previous extraction
# pass over this SAME paper FAILED to capture the following question number(s): {missing_list}. Find
# and extract ONLY these specific question(s) from the text below -- ignore every other question, even
# though the full text of the whole paper is shown to you for context.

# Return ONLY: {{"questions": {{"<Qn>": {{"question_id": "Qn", "question": "<full verbatim text,
# including any data table, passage, figure caption, or bracketed sub-part marks it contains>",
# "marks": <number>, "type": "MCQ|Short Answer|Long Answer|Numerical"}}, ...}}}} containing an entry for
# EVERY one of the requested question number(s) that you can locate in the text below. Do not invent a
# question that genuinely is not present in the text.
# {section_context}

# FULL QUESTION PAPER TEXT:
# {full_text}

# Return ONLY the raw JSON. No markdown."""


# def _backfill_missing_questions(full_text, missing_numbers, section_context, model_id, max_tokens):
#     """One extra, TARGETED call scoped to ONLY the question number(s) a normal parse failed to
#     produce. Runs over the WHOLE document text (never a single page), so it is immune to the
#     page-local blind spots that dropped them in the first place, and it tells the model EXACTLY which
#     numbers must not be missing -- rather than re-running the same page-by-page extraction and hoping
#     it succeeds this time. Returns {} on failure (caller then reports the questions as still missing,
#     rather than silently proceeding as if nothing were wrong)."""
#     missing_list = ", ".join(f"Q{n}" for n in missing_numbers)
#     prompt = (QP_BACKFILL_PROMPT.replace("{missing_list}", missing_list)
#              .replace("{section_context}", section_context or "")
#              .replace("{full_text}", full_text))
#     try:
#         out, _i, _o = generate(model=model_id, prompt=prompt, temperature=0.0,
#                                max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#         data = pp.tolerant_json(out)
#         qs = pp._questions_of(data) if isinstance(data, dict) else {}
#         return qs if isinstance(qs, dict) else {}
#     except Exception as e:
#         print(f"[QP parse] backfill call failed: {type(e).__name__}: {e}", file=sys.stderr)
#         return {}


# def verify_and_backfill_completeness(parsed_json, full_text, section_map, section_context, model_id):
#     """THE completeness safety net. Compares what was actually parsed against the total question
#     count the paper's OWN section map/instructions declare, and runs one targeted recovery call for
#     anything missing -- rather than hoping a per-page retry happens to succeed, or (worse) silently
#     returning a paper short of questions with no indication anything went wrong.

#     This is the single place completeness is enforced, so it runs identically after EITHER the
#     per-page parallel path or the single-call path: a question can go missing from a single, long
#     call just as easily as from a page-local one, so both deserve the same safety net.

#     No-op (parsed_json returned unchanged) when the expected total cannot be determined (no section
#     map, no explicit question count in the text) -- this can only RECOVER a known gap, never guess one
#     into existence. Still-missing questions after the recovery attempt are logged loudly (never
#     silently absorbed), so a genuine extraction failure remains visible to whoever reads the logs."""
#     questions = parsed_json.get("questions") if isinstance(parsed_json, dict) else None
#     if not isinstance(questions, dict):
#         return parsed_json
#     expected_total = _expected_total_questions(section_map, full_text)
#     if not expected_total:
#         return parsed_json
#     ranges = _missing_question_ranges(questions, expected_total)
#     if not ranges:
#         return parsed_json

#     missing_numbers = [n for lo, hi in ranges for n in range(lo, hi + 1)]
#     print(f"[QP parse] {len(missing_numbers)} question(s) missing after the initial parse "
#          f"(expected up to Q{expected_total}): " + ", ".join(f"Q{n}" for n in missing_numbers)
#          + " -- running a targeted recovery pass.", file=sys.stderr)

#     max_tokens = int(os.environ.get("KEY_PARSER_MAX_TOKENS", "32768"))
#     recovered = _backfill_missing_questions(full_text, missing_numbers, section_context,
#                                             model_id, max_tokens)
#     if recovered:
#         for k, v in recovered.items():
#             if isinstance(v, dict):
#                 questions[k] = v
#         still_missing = _missing_question_ranges(questions, expected_total)
#         if still_missing:
#             still_nums = [n for lo, hi in still_missing for n in range(lo, hi + 1)]
#             print(f"[QP parse] WARNING: still missing after the recovery pass: "
#                  + ", ".join(f"Q{n}" for n in still_nums)
#                  + " -- these questions could not be located in the extracted text. Verify the "
#                    "source PDF manually (it may need OCR, or the page's text layer may be corrupt "
#                    "for this range).", file=sys.stderr)
#         else:
#             print(f"[QP parse] recovery pass succeeded -- all {len(missing_numbers)} question(s) "
#                  f"recovered.", file=sys.stderr)
#     else:
#         print(f"[QP parse] WARNING: recovery pass returned nothing; "
#              + ", ".join(f"Q{n}" for n in missing_numbers)
#              + " remain MISSING from the result. Verify the source PDF manually.", file=sys.stderr)

#     parsed_json["questions"] = questions
#     return parsed_json


# def _apply_marks_reconciliation(parsed_json, section_map):
#     """Post-parse marks safety net, reusing an ALREADY-COMPUTED section_map. Runs AFTER completeness
#     backfill so any newly-recovered question also gets its marks checked against the section map."""
#     if not section_map:
#         return parsed_json
#     questions = parsed_json.get("questions") if isinstance(parsed_json, dict) else None
#     if not isinstance(questions, dict) or not questions:
#         return parsed_json
#     questions, changed = reconcile_marks_with_sections(questions, section_map)
#     if changed:
#         print("[QP marks] corrected against the paper's own section headers: "
#              + ", ".join(f"{q} {o:g}->{n:g}" for q, o, n in changed), file=sys.stderr)
#     parsed_json["questions"] = questions
#     return parsed_json


# def parse_qp_parallel(page_texts, section_context=""):
#     """Per-page parallel extraction of WHOLE-question entries (with total marks) -> {questions}. No
#     global choice pass: the paper's authority is the per-question TOTAL, so no OR-splitting is needed.
#     `section_context` (from parallel_parse.format_section_context) is spliced into EVERY page's own
#     prompt via `{extra_context}`, so a page whose questions are printed with a section-local restarted
#     numbering can still resolve to the correct GLOBAL id -- a single page's own text cannot supply
#     that mapping on its own."""
#     _load_project_env()
#     model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
#     per_page_max = int(os.environ.get("KEY_PARSER_PAGE_MAX_TOKENS", "8192"))
#     questions, _i, _o = pp.extract_pages_parallel(page_texts, QP_PER_PAGE_PROMPT, model_id, per_page_max,
#                                                   extra_context=section_context)
#     return {"questions": questions}


# def main():
#     if len(sys.argv) < 2:
#         print("Usage: python3 extract_json_from_question_paper.py <file_path>")
#         sys.exit(1)

#     file_path = sys.argv[1]
#     ext = Path(file_path).suffix.lower()

#     try:
#         if ext == '.json':
#             with open(file_path, 'r') as f:
#                 content = f.read()
#                 json.loads(content)          # validate, then echo a raw .json upload through unchanged
#                 print(content)
#                 return

#         elif ext == '.docx':
#             raw_text = extract_text_from_docx(file_path)
#             if not raw_text.strip():
#                 print("ERROR: No text extracted from file.")
#                 sys.exit(1)
#             model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
#             section_map = _get_section_map(raw_text, model_id)
#             section_context = pp.format_section_context(section_map)
#             parsed_json = parse_question_paper_with_gemini(raw_text, section_context)  # docx -> single call
#             parsed_json = verify_and_backfill_completeness(parsed_json, raw_text, section_map,
#                                                            section_context, model_id)
#             parsed_json = _apply_marks_reconciliation(parsed_json, section_map)

#         elif ext == '.pdf':
#             page_texts = pp.pdf_page_texts(file_path)
#             raw_text = "\n".join(page_texts)
#             if not raw_text.strip():
#                 print("ERROR: No text extracted from file.")
#                 sys.exit(1)
#             model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
#             # Computed FIRST, from the whole document, so numbering context (below), the
#             # completeness check, and marks reconciliation all reuse this SAME map -- one extra call
#             # total, regardless of how many things consult it.
#             section_map = _get_section_map(raw_text, model_id)
#             section_context = pp.format_section_context(section_map)
#             if pp.parallel_enabled() and len([t for t in page_texts if t.strip()]) > 1:
#                 parsed_json = parse_qp_parallel(page_texts, section_context)
#             else:
#                 parsed_json = parse_question_paper_with_gemini(raw_text, section_context)
#             # Runs for BOTH the parallel and single-call PDF paths: a question can go missing from a
#             # single long call just as easily as from a page-local one, so both get the same net.
#             parsed_json = verify_and_backfill_completeness(parsed_json, raw_text, section_map,
#                                                            section_context, model_id)
#             parsed_json = _apply_marks_reconciliation(parsed_json, section_map)

#         else:
#             print(f"ERROR: Unsupported file extension {ext}")
#             sys.exit(1)

#         print(json.dumps(parsed_json, indent=2))

#     except Exception as e:
#         print(f"ERROR: {str(e)}", file=sys.stderr)
#         sys.exit(1)


# if __name__ == "__main__":
#     main()








# import os
# import sys
# import json
# import warnings
# import re
# # Suppress all warnings to keep stdout clean for JSON
# warnings.filterwarnings("ignore")

# from pathlib import Path

# # Reuse the answer-key parser's shared helpers rather than duplicating them. The key parser lives
# # beside this file; add its directory to the path so the import resolves even when this script is
# # run as a subprocess from the Flask app's cwd. Forking the parser (instead of parametrizing it)
# # keeps each prompt single-purpose so a question-paper prompt change can never regress key parsing.
# sys.path.append(os.path.dirname(os.path.abspath(__file__)))
# from extract_json_from_key import (
#     extract_text_from_docx,
#     extract_text_from_pdf,
#     _sanitize_json_escapes,
#     _load_project_env,
# )
# from llm_client import generate, strip_reasoning
# import parallel_parse as pp


# def parse_question_paper_with_gemini(text, section_context=""):
#     # Make .env settings (provider + model + keys) visible whether run standalone or from Flask.
#     _load_project_env()
#     # Reuse KEY_PARSER_MODEL so the key + question-paper parsers move together. Default to a capable
#     # Qwen3-VL Instruct model.
#     model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")

#     prompt = f"""
#     Extract the questions from the following EXAM QUESTION PAPER into a structured JSON object.

#     Return a JSON object with EXACTLY one top-level key:
#     1. "questions": a dictionary where keys are question IDs (e.g., Q1, Q2, or specific IDs like
#        Q31(a), Q31(b) exactly as printed). Each value is a dictionary with:
#        - question_id: string
#        - question: string (the FULL, verbatim text of the question, including any data table,
#          passage, figure caption, or sub-prompt the student must read in order to answer it)
#        - marks: number (maximum marks for this question if printed; otherwise 0)
#        - type: string (one of: MCQ, Short Answer, Long Answer, Numerical)

#     CRITICAL RULES:
#     - This is a QUESTION PAPER, not a marking scheme. DO NOT invent, infer, or include answers.
#       There is NO "answer" field. Capture ONLY what the paper asks.
#     - Capture EVERY numbered question in the ENTIRE document, from the first to the LAST section --
#       including short OBJECTIVE questions (MCQ / True-False / fill-in-the-blank / one-line "predict
#       the output" / SQL / assertion-reason) AND the paper's final section (often case-study / long-
#       answer questions). Do NOT skip a whole section, and do NOT stop before the paper's last
#       question just because it is long or comes after several other sections.
#     - A question printed near the BOTTOM of a page whose sub-parts, instructions, or continuing text
#       run onto the TOP of the next page (with no new question number repeated there) is STILL ONE
#       question -- capture ALL of it, including the part that continues past the page boundary. Never
#       cut a question short just because part of its content sits on the following page.
#     - Use the paper's GLOBAL question numbering for question IDs (Q1, Q31(a), Q31(b), ...) so they
#       can be matched to the answer key later -- NOT a section's own local/restarted numbering. See
#       the LOCAL vs GLOBAL guidance below if this paper's sections restart their own numbering.
#     - "marks": use the number printed for each question; if only a section header gives it
#       ("Section A ... Each question carries 1 Mark"), apply that per-question value to EVERY
#       question physically located in that section -- with NO exceptions. A question's marks come
#       from its SECTION, never from how long or complex its text is.
#     - DATA TABLES / EMBEDDED CONTENT DO NOT CHANGE THIS RULE. A question that includes a
#       frequency table, a data list, a figure, or a multi-line passage is STILL just one question
#       belonging to its section, and it STILL inherits that section's per-question marks exactly
#       like every other question around it. Do NOT default such a question's marks to 0 just
#       because the table/data occupies most of the question's space -- the marks come from the
#       SECTION HEADER, not from counting rows in the table.
#     - SELF-CHECK before returning: for every section that states "N questions x M marks", count
#       how many questions you assigned marks=M in that section. If it is fewer than N, you missed
#       one -- go back and fix its marks instead of leaving it as 0.
#     - Preserve the full question wording; do not summarize or truncate it.
#     {section_context}

#     Text to parse:
#     {text}

#     Return ONLY the raw JSON. No markdown blocks.
#     """

#     # A full question paper overflows the default output cap, silently truncating the JSON. Raise the
#     # ceiling + force JSON mode. thinking_budget=0 keeps this verbatim EXTRACTION (Gemini-only knob;
#     # ignored by Qwen). strip_reasoning drops any <think> block a Qwen -Thinking model emits.
#     text_out, _in_tok, _out_tok = generate(
#         model=model_id, prompt=prompt, temperature=0.0,
#         # 32768 is ample for a 40-50 question paper; env-tunable (shared with the key parser).
#         # OpenRouter PRE-AUTHORISES max_tokens against your balance, so keep this modest.
#         max_tokens=int(os.environ.get("KEY_PARSER_MAX_TOKENS", "32768")),
#         json_mode=True, thinking_budget=0,
#     )
#     content = strip_reasoning((text_out or "").strip())

#     # JSON mode returns clean JSON, but a math/symbol-heavy paper can carry UNescaped backslashes
#     # (\frac, \sqrt) -> "Invalid \escape". Try the raw text, then a backslash-repaired copy, each
#     # with a brace-extraction fallback for any leading/trailing noise (same as the key parser).
#     for candidate in (content, _sanitize_json_escapes(content)):
#         try:
#             return json.loads(candidate)
#         except json.JSONDecodeError:
#             m = re.search(r'(\{.*\})', candidate, re.DOTALL)
#             if m:
#                 try:
#                     return json.loads(m.group(1))
#                 except json.JSONDecodeError:
#                     continue
#     raise Exception(f"AI response was not valid JSON (length {len(content)}): {content[:120]}...")


# # ---------------------------------------------------------------------------------------------------
# # PER-PAGE PARALLEL PATH. Each page's questions are extracted concurrently as WHOLE-question entries
# # carrying the question's TOTAL printed marks -- so the paper is the clean per-question marks authority
# # (Q36 case study -> one entry worth 4, never split into leaves the reconciler would mis-total).
# #
# # {extra_context} is spliced in by parallel_parse.extract_pages_parallel from
# # parallel_parse.format_section_context(section_map) -- a document-wide section/numbering map computed
# # BEFORE any page is parsed, so a page whose questions are printed with a section-local restarted
# # numbering ("1.", "2.", "3." inside that section, really Q21/Q22/Q23) can still resolve to the correct
# # GLOBAL id. Empty (a plain continuously-numbered paper) -> the placeholder is spliced out to nothing
# # and this prompt behaves exactly as before.
# #
# # extract_pages_parallel ALSO appends a look-ahead window (the next page's leading text, clearly
# # fenced) to {page_text} itself before this template is even filled in -- so the CONTINUATION-ACROSS-
# # PAGE-BREAK guidance below has real look-ahead content to act on, not just an instruction with nothing
# # to apply it to.
# # ---------------------------------------------------------------------------------------------------
# QP_PER_PAGE_PROMPT = """Extract the exam questions that appear on THIS ONE PAGE of a QUESTION PAPER into a JSON object.

# Return ONLY: {"questions": {"<Qn>": {"question_id": "Qn", "question": "<full verbatim text>", "marks": <number>, "type": "..."}, ...}}

# CRITICAL RULES for THIS PAGE:
# - Extract EVERY numbered question printed on this page, WHEREVER on the page it falls -- including questions near the BOTTOM of the page and questions belonging to the paper's LAST section (often case-study / long-answer questions). A page often MIXES non-question text (a cover heading, "General Instructions", or a "Section A / Section B ..." header) WITH questions -- capture the questions and simply ignore the surrounding non-question text. NEVER skip a question just because the page also has a heading or instructions above it, or because it is the final question on the page or in the paper.
# - CONTINUATION ACROSS A PAGE BREAK: if this page ends partway through a question (its sub-parts, instructions, a table, or other content clearly continues past the bottom of this page), and a "[--- START OF NEXT PAGE ---]" fence appears below showing the next page's leading text, check whether that text is a CONTINUATION of the last question on THIS page (no new question number, reads as a direct continuation) -- if so, INCLUDE that continuation as part of that same question's "question" text. Do NOT include next-page text that clearly starts a brand-new numbered question (leave that one for the next page's own extraction).
# - Short OBJECTIVE questions ARE questions and must be captured just like long ones: MCQ, True/False, fill-in-the-blank, one-line "predict the output", SQL, and assertion-reason. Keep their (a)/(b)/(c)/(d) options inside the question text. A "Section A, 1 mark each" list of one-mark questions must be captured in FULL (every number in the range).
# - ONE entry per QUESTION NUMBER (Q1, Q22, Q36). Do NOT split a question into separate (a)/(b)/(i)/(ii) entries -- keep ALL of its sub-parts, data tables, passages, figures and any "OR" alternatives INSIDE that one question's "question" text. Bracketed sub-part marks like "[1]", "[2]" inside a case-study question are part of its TEXT, not a signal to split it into separate entries -- keep the whole question (with its brackets) as ONE plain string value.
# - "marks": the marks for that WHOLE question -- a number at the end of the question, in the margin/brackets, or a "Marks" column. If only a SECTION header on this page states the per-question marks (e.g. "Section A ... Each question carries 1 Mark" or "Section -A (21 x 1 = 21 Marks)"), use that value for EVERY question in that section, with NO exceptions. A question offering "(a) ... OR (b) ..." is worth its single printed total -- do NOT add the alternatives. Use 0 ONLY if no marks are printed AND no section header on this page (or a header line carried over from an earlier page) implies a value for it.
# - A question that embeds a DATA TABLE, a frequency/grouped-data table, a long passage, or a figure is STILL just one question in its section and STILL inherits that section's per-question marks exactly like the plainer questions around it. Never default such a question to marks=0 just because most of its text is a table -- the marks come from the SECTION, not from how the question is laid out.
# - SELF-CHECK before returning: for every section header on this page stating "N questions x M marks", make sure you assigned marks=M to every one of that section's questions on this page, including any question built mostly around a table or figure.
# - This is a QUESTION PAPER: capture ONLY what is asked. Do NOT invent, infer, or include answers. There is NO "answer" field.
# - If a question started on an earlier page and continues here, still emit it under its number with the text visible here (it will be merged).
# - "type": one of MCQ, Short Answer, Long Answer, Numerical.
# - Return {"questions": {}} ONLY when this page has NO numbered questions at all (a pure cover/instructions page with nothing numbered below).
# {extra_context}

# THIS PAGE:
# {page_text}

# Return ONLY the raw JSON. No markdown."""


# def _base_qnum_local(qid):
#     """Leading question number from an id like 'Q35', '35', 'Q35(a)' -> 35. None if no digit."""
#     m = re.search(r'(\d+)', str(qid))
#     return int(m.group(1)) if m else None


# def _marks_for_section(base_num, section_map):
#     """The per-question marks the section-map says base_num belongs to, or None if it falls outside
#     every mapped range (e.g. the map is incomplete/empty -- caller then leaves that question alone)."""
#     for s in section_map:
#         if s["from"] <= base_num <= s["to"]:
#             return s["marks"]
#     return None


# def reconcile_marks_with_sections(questions, section_map):
#     """Overlay the document-wide section-marks map (see parallel_parse.extract_section_marks_map)
#     onto the parsed questions: when a question's own marks disagree with (or are missing/zero
#     relative to) its section's stated per-question value, the SECTION value wins -- it was read from
#     the section headers across the WHOLE paper in one pass, so it cannot be fooled by a single page
#     missing its local header.

#     Deliberately does NOT touch a question whose base number falls OUTSIDE every mapped range (an
#     empty/partial map, or a genuinely un-sectioned paper) -- in that case whatever the parser already
#     produced is left exactly as-is, so this can only CORRECT a disagreement, never invent one.

#     Returns (questions, changed) where changed is [(qid, old_marks, new_marks), ...] for logging."""
#     if not section_map:
#         return questions, []
#     changed = []
#     for qid, v in questions.items():
#         if not isinstance(v, dict):
#             continue
#         bn = _base_qnum_local(qid)
#         if bn is None:
#             continue
#         expected = _marks_for_section(bn, section_map)
#         if expected is None:
#             continue
#         try:
#             current = float(v.get("marks") or 0)
#         except (TypeError, ValueError):
#             current = 0.0
#         if abs(current - expected) > 1e-9:
#             changed.append((str(qid), current, expected))
#             v["marks"] = expected
#     return questions, changed


# def _get_section_map(full_text, model_id):
#     """Compute the document-wide section map ONCE (reused for numbering context, marks reconciliation,
#     AND the completeness check below), so a paper only ever pays for this one extra cheap call
#     regardless of how many things consult it. [] on failure or when the paper has no detectable
#     section structure -- every downstream use degrades to a no-op."""
#     if os.environ.get("QP_SECTION_RECONCILE", "1").strip().lower() in ("0", "false", "no", "off"):
#         return []
#     try:
#         section_map, _i, _o = pp.extract_section_marks_map(full_text, model_id)
#         return section_map
#     except Exception as e:
#         print(f"Warning: section map extraction skipped ({e})", file=sys.stderr)
#         return []


# def _expected_total_questions(section_map, full_text):
#     """The highest GLOBAL question number this paper is expected to contain. Primarily the section
#     map's own highest "to" (it was built from the paper's own General Instructions / headers, so it
#     already covers the whole document). Falls back to an explicit "contains N questions" statement in
#     the text when there is no usable section map. None when neither source yields a number -- the
#     completeness check then simply does not run (never a regression versus not having it at all)."""
#     if section_map:
#         return max(s["to"] for s in section_map)
#     m = re.search(r'contains\s+(\d+)\s+questions', full_text, re.IGNORECASE)
#     if m:
#         return int(m.group(1))
#     return None


# def _missing_question_ranges(questions, expected_total):
#     """Contiguous [(lo, hi), ...] ranges of global question numbers 1..expected_total that have NO
#     entry anywhere in `questions` (by base number). Empty when nothing is missing or expected_total
#     is falsy."""
#     if not expected_total:
#         return []
#     present = {bn for qid in questions if (bn := _base_qnum_local(qid)) is not None}
#     missing = sorted(n for n in range(1, expected_total + 1) if n not in present)
#     ranges = []
#     for n in missing:
#         if ranges and n == ranges[-1][1] + 1:
#             ranges[-1] = (ranges[-1][0], n)
#         else:
#             ranges.append((n, n))
#     return ranges


# def _has_thin_content(questions, section_map, min_chars=40):
#     """Base numbers whose question TEXT looks suspiciously short relative to what its section is
#     worth -- the fingerprint of a question that was truncated at a page break rather than fully
#     missing (it exists, so the completeness check alone would miss it, but its content clearly cuts
#     off mid-thought). Heuristic and DELIBERATELY narrow: a genuinely short question (a one-line MCQ
#     worth 1 mark) must never be flagged, so this only fires for a question worth >= 3 marks (which the
#     paper's own section map says should carry real substance) whose captured text is still under
#     `min_chars`. False negatives are fine here (a real truncation with somewhat-more text than the
#     threshold is simply not caught by this pass) -- the cost of a false POSITIVE is an unnecessary
#     extra backfill call, while a false negative just leaves this specific safety net silent, so erring
#     towards fewer false positives is the safe direction."""
#     if not section_map:
#         return []
#     thin = []
#     for qid, v in questions.items():
#         if not isinstance(v, dict):
#             continue
#         bn = _base_qnum_local(qid)
#         if bn is None:
#             continue
#         expected_marks = _marks_for_section(bn, section_map)
#         if expected_marks is None or expected_marks < 3:
#             continue
#         text = str(v.get("question") or "").strip()
#         if len(text) < min_chars:
#             thin.append(bn)
#     return sorted(set(thin))


# QP_BACKFILL_PROMPT = """You are given the FULL text of an exam question paper. A previous extraction
# pass over this SAME paper either FAILED to capture, or captured only a TRUNCATED/incomplete version
# of, the following question number(s): {missing_list}. Find and extract the COMPLETE text of ONLY
# these specific question(s) from the text below -- ignore every other question, even though the full
# text of the whole paper is shown to you for context. Pay special attention to content that continues
# across a page break: a question's own text may run on well past where a truncated first attempt
# stopped, and you must capture that continuation too.

# Return ONLY: {{"questions": {{"<Qn>": {{"question_id": "Qn", "question": "<COMPLETE verbatim text,
# including any data table, passage, figure caption, or bracketed sub-part marks it contains, and
# including any continuation of the question that appears further down / on a later page>", "marks":
# <number>, "type": "MCQ|Short Answer|Long Answer|Numerical"}}, ...}}}} containing an entry for EVERY one
# of the requested question number(s) that you can locate in the text below. Do not invent a question
# that genuinely is not present in the text.
# {section_context}

# FULL QUESTION PAPER TEXT:
# {full_text}

# Return ONLY the raw JSON. No markdown."""


# def _backfill_missing_questions(full_text, missing_numbers, section_context, model_id, max_tokens):
#     """One extra, TARGETED call scoped to ONLY the question number(s) a normal parse failed to
#     produce (or produced only a truncated version of). Runs over the WHOLE document text (never a
#     single page), so it is immune to the page-local blind spots that dropped/truncated them in the
#     first place, and it tells the model EXACTLY which numbers must be complete -- rather than
#     re-running the same page-by-page extraction and hoping it succeeds this time. Returns {} on
#     failure (caller then reports the questions as still missing, rather than silently proceeding as
#     if nothing were wrong)."""
#     missing_list = ", ".join(f"Q{n}" for n in missing_numbers)
#     prompt = (QP_BACKFILL_PROMPT.replace("{missing_list}", missing_list)
#              .replace("{section_context}", section_context or "")
#              .replace("{full_text}", full_text))
#     try:
#         out, _i, _o = generate(model=model_id, prompt=prompt, temperature=0.0,
#                                max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#         data = pp.tolerant_json(out)
#         qs = pp._questions_of(data) if isinstance(data, dict) else {}
#         return qs if isinstance(qs, dict) else {}
#     except Exception as e:
#         print(f"[QP parse] backfill call failed: {type(e).__name__}: {e}", file=sys.stderr)
#         return {}


# def verify_and_backfill_completeness(parsed_json, full_text, section_map, section_context, model_id):
#     """THE completeness safety net. Runs TWO checks against what was actually parsed, both compared
#     to what the paper's OWN section map/instructions declare, and runs ONE combined targeted recovery
#     call for anything found wanting -- rather than hoping a per-page retry happens to succeed, or
#     (worse) silently returning a paper short of, or with truncated, questions with no indication
#     anything went wrong:

#       1. MISSING -- a global question number with NO entry at all anywhere in the result.
#       2. THIN/TRUNCATED -- a question that DOES have an entry, but whose captured text is
#          suspiciously short for a question its own section says should carry real substance (the
#          fingerprint of content that was cut off at a page break rather than genuinely absent).

#     This is the single place completeness is enforced, so it runs identically after EITHER the
#     per-page parallel path or the single-call path: a question can go missing or get truncated from a
#     single, long call just as easily as from a page-local one, so both deserve the same safety net.

#     No-op (parsed_json returned unchanged) when the expected total cannot be determined (no section
#     map, no explicit question count in the text) -- this can only RECOVER a known gap, never guess one
#     into existence. Anything still missing/thin after the recovery attempt is logged loudly (never
#     silently absorbed), so a genuine extraction failure remains visible to whoever reads the logs."""
#     questions = parsed_json.get("questions") if isinstance(parsed_json, dict) else None
#     if not isinstance(questions, dict):
#         return parsed_json
#     expected_total = _expected_total_questions(section_map, full_text)
#     if not expected_total:
#         return parsed_json

#     missing_ranges = _missing_question_ranges(questions, expected_total)
#     missing_numbers = [n for lo, hi in missing_ranges for n in range(lo, hi + 1)]
#     thin_numbers = [n for n in _has_thin_content(questions, section_map) if n not in missing_numbers]
#     target_numbers = sorted(set(missing_numbers) | set(thin_numbers))
#     if not target_numbers:
#         return parsed_json

#     if missing_numbers:
#         print(f"[QP parse] {len(missing_numbers)} question(s) missing after the initial parse "
#              f"(expected up to Q{expected_total}): " + ", ".join(f"Q{n}" for n in missing_numbers),
#              file=sys.stderr)
#     if thin_numbers:
#         print(f"[QP parse] {len(thin_numbers)} question(s) look truncated (short text for a "
#              f"substantial-mark question): " + ", ".join(f"Q{n}" for n in thin_numbers),
#              file=sys.stderr)
#     print(f"[QP parse] running a targeted recovery pass for: "
#          + ", ".join(f"Q{n}" for n in target_numbers), file=sys.stderr)

#     max_tokens = int(os.environ.get("KEY_PARSER_MAX_TOKENS", "32768"))
#     recovered = _backfill_missing_questions(full_text, target_numbers, section_context,
#                                             model_id, max_tokens)
#     if recovered:
#         for k, v in recovered.items():
#             if not isinstance(v, dict):
#                 continue
#             # For a THIN (already-present) entry, only replace it when the recovery genuinely got
#             # MORE text -- never let a worse/failed recovery call regress an entry that already had
#             # something. A MISSING entry is simply installed outright.
#             bn = _base_qnum_local(k)
#             existing = questions.get(k)
#             if isinstance(existing, dict) and bn in thin_numbers and bn not in missing_numbers:
#                 if len(str(v.get("question") or "")) > len(str(existing.get("question") or "")):
#                     questions[k] = v
#             else:
#                 questions[k] = v

#         still_missing = [n for lo, hi in _missing_question_ranges(questions, expected_total)
#                          for n in range(lo, hi + 1)]
#         still_thin = [n for n in _has_thin_content(questions, section_map) if n not in still_missing]
#         if still_missing or still_thin:
#             if still_missing:
#                 print(f"[QP parse] WARNING: still missing after the recovery pass: "
#                      + ", ".join(f"Q{n}" for n in still_missing)
#                      + " -- could not be located in the extracted text. Verify the source PDF "
#                        "manually (it may need OCR, or its text layer may be corrupt for this range).",
#                      file=sys.stderr)
#             if still_thin:
#                 print(f"[QP parse] WARNING: still look truncated after the recovery pass: "
#                      + ", ".join(f"Q{n}" for n in still_thin)
#                      + " -- verify these questions against the source PDF manually.", file=sys.stderr)
#         else:
#             print(f"[QP parse] recovery pass succeeded -- all {len(target_numbers)} question(s) "
#                  f"recovered/completed.", file=sys.stderr)
#     else:
#         print(f"[QP parse] WARNING: recovery pass returned nothing; "
#              + ", ".join(f"Q{n}" for n in target_numbers)
#              + " remain missing/incomplete in the result. Verify the source PDF manually.",
#              file=sys.stderr)

#     parsed_json["questions"] = questions
#     return parsed_json


# def _apply_marks_reconciliation(parsed_json, section_map):
#     """Post-parse marks safety net, reusing an ALREADY-COMPUTED section_map. Runs AFTER completeness
#     backfill so any newly-recovered/completed question also gets its marks checked against the map."""
#     if not section_map:
#         return parsed_json
#     questions = parsed_json.get("questions") if isinstance(parsed_json, dict) else None
#     if not isinstance(questions, dict) or not questions:
#         return parsed_json
#     questions, changed = reconcile_marks_with_sections(questions, section_map)
#     if changed:
#         print("[QP marks] corrected against the paper's own section headers: "
#              + ", ".join(f"{q} {o:g}->{n:g}" for q, o, n in changed), file=sys.stderr)
#     parsed_json["questions"] = questions
#     return parsed_json


# def parse_qp_parallel(page_texts, section_context=""):
#     """Per-page parallel extraction of WHOLE-question entries (with total marks) -> {questions}. No
#     global choice pass: the paper's authority is the per-question TOTAL, so no OR-splitting is needed.
#     `section_context` (from parallel_parse.format_section_context) is spliced into EVERY page's own
#     prompt via `{extra_context}`, so a page whose questions are printed with a section-local restarted
#     numbering can still resolve to the correct GLOBAL id -- a single page's own text cannot supply
#     that mapping on its own. extract_pages_parallel additionally gives each page an overlapping
#     look-ahead window (see its own docstring) so a question whose tail runs onto the next page without
#     repeating its number is not silently dropped."""
#     _load_project_env()
#     model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
#     per_page_max = int(os.environ.get("KEY_PARSER_PAGE_MAX_TOKENS", "8192"))
#     questions, _i, _o = pp.extract_pages_parallel(page_texts, QP_PER_PAGE_PROMPT, model_id, per_page_max,
#                                                   extra_context=section_context)
#     return {"questions": questions}


# def main():
#     if len(sys.argv) < 2:
#         print("Usage: python3 extract_json_from_question_paper.py <file_path>")
#         sys.exit(1)

#     file_path = sys.argv[1]
#     ext = Path(file_path).suffix.lower()

#     try:
#         if ext == '.json':
#             with open(file_path, 'r') as f:
#                 content = f.read()
#                 json.loads(content)          # validate, then echo a raw .json upload through unchanged
#                 print(content)
#                 return

#         elif ext == '.docx':
#             raw_text = extract_text_from_docx(file_path)
#             if not raw_text.strip():
#                 print("ERROR: No text extracted from file.")
#                 sys.exit(1)
#             model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
#             section_map = _get_section_map(raw_text, model_id)
#             section_context = pp.format_section_context(section_map)
#             parsed_json = parse_question_paper_with_gemini(raw_text, section_context)  # docx -> single call
#             parsed_json = verify_and_backfill_completeness(parsed_json, raw_text, section_map,
#                                                            section_context, model_id)
#             parsed_json = _apply_marks_reconciliation(parsed_json, section_map)

#         elif ext == '.pdf':
#             page_texts = pp.pdf_page_texts(file_path)
#             raw_text = "\n".join(page_texts)
#             if not raw_text.strip():
#                 print("ERROR: No text extracted from file.")
#                 sys.exit(1)
#             model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
#             # Computed FIRST, from the whole document, so numbering context (below), the
#             # completeness check, and marks reconciliation all reuse this SAME map -- one extra call
#             # total, regardless of how many things consult it.
#             section_map = _get_section_map(raw_text, model_id)
#             section_context = pp.format_section_context(section_map)
#             if pp.parallel_enabled() and len([t for t in page_texts if t.strip()]) > 1:
#                 parsed_json = parse_qp_parallel(page_texts, section_context)
#             else:
#                 parsed_json = parse_question_paper_with_gemini(raw_text, section_context)
#             # Runs for BOTH the parallel and single-call PDF paths: a question can go missing/get
#             # truncated from a single long call just as easily as from a page-local one, so both get
#             # the same net.
#             parsed_json = verify_and_backfill_completeness(parsed_json, raw_text, section_map,
#                                                            section_context, model_id)
#             parsed_json = _apply_marks_reconciliation(parsed_json, section_map)

#         else:
#             print(f"ERROR: Unsupported file extension {ext}")
#             sys.exit(1)

#         print(json.dumps(parsed_json, indent=2))

#     except Exception as e:
#         print(f"ERROR: {str(e)}", file=sys.stderr)
#         sys.exit(1)


# if __name__ == "__main__":
#     main()







# import os
# import sys
# import json
# import warnings
# import re
# # Suppress all warnings to keep stdout clean for JSON
# warnings.filterwarnings("ignore")

# from pathlib import Path

# # Reuse the answer-key parser's shared helpers rather than duplicating them. The key parser lives
# # beside this file; add its directory to the path so the import resolves even when this script is
# # run as a subprocess from the Flask app's cwd. Forking the parser (instead of parametrizing it)
# # keeps each prompt single-purpose so a question-paper prompt change can never regress key parsing.
# sys.path.append(os.path.dirname(os.path.abspath(__file__)))
# from extract_json_from_key import (
#     extract_text_from_docx,
#     extract_text_from_pdf,
#     _sanitize_json_escapes,
#     _load_project_env,
# )
# from llm_client import generate, strip_reasoning
# import parallel_parse as pp


# def parse_question_paper_with_gemini(text, section_context=""):
#     # Make .env settings (provider + model + keys) visible whether run standalone or from Flask.
#     _load_project_env()
#     # Reuse KEY_PARSER_MODEL so the key + question-paper parsers move together. Default to a capable
#     # Qwen3-VL Instruct model.
#     model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")

#     prompt = f"""
#     Extract the questions from the following EXAM QUESTION PAPER into a structured JSON object.

#     Return a JSON object with EXACTLY one top-level key:
#     1. "questions": a dictionary where keys are question IDs (e.g., Q1, Q2, or specific IDs like
#        Q31(a), Q31(b) exactly as printed). Each value is a dictionary with:
#        - question_id: string
#        - question: string (the FULL, verbatim text of the question, including any data table,
#          passage, figure caption, or sub-prompt the student must read in order to answer it)
#        - marks: number (maximum marks for this question if printed; otherwise 0)
#        - type: string (one of: MCQ, Short Answer, Long Answer, Numerical)

#     CRITICAL RULES:
#     - This is a QUESTION PAPER, not a marking scheme. DO NOT invent, infer, or include answers.
#       There is NO "answer" field. Capture ONLY what the paper asks.
#     - Capture EVERY numbered question in the ENTIRE document, from the first to the LAST section --
#       including short OBJECTIVE questions (MCQ / True-False / fill-in-the-blank / one-line "predict
#       the output" / SQL / assertion-reason) AND the paper's final section (often case-study / long-
#       answer questions). Do NOT skip a whole section, and do NOT stop before the paper's last
#       question just because it is long or comes after several other sections.
#     - A question printed near the BOTTOM of a page whose sub-parts, instructions, or continuing text
#       run onto the TOP of the next page (with no new question number repeated there) is STILL ONE
#       question -- capture ALL of it, including the part that continues past the page boundary. Never
#       cut a question short just because part of its content sits on the following page.
#     - Use the paper's GLOBAL question numbering for question IDs (Q1, Q31(a), Q31(b), ...) so they
#       can be matched to the answer key later -- NOT a section's own local/restarted numbering. See
#       the LOCAL vs GLOBAL guidance below if this paper's sections restart their own numbering.
#     - "marks": use the number printed for each question; if only a section header gives it
#       ("Section A ... Each question carries 1 Mark"), apply that per-question value to EVERY
#       question physically located in that section -- with NO exceptions. A question's marks come
#       from its SECTION, never from how long or complex its text is.
#     - DATA TABLES / EMBEDDED CONTENT DO NOT CHANGE THIS RULE. A question that includes a
#       frequency table, a data list, a figure, or a multi-line passage is STILL just one question
#       belonging to its section, and it STILL inherits that section's per-question marks exactly
#       like every other question around it. Do NOT default such a question's marks to 0 just
#       because the table/data occupies most of the question's space -- the marks come from the
#       SECTION HEADER, not from counting rows in the table.
#     - SELF-CHECK before returning: for every section that states "N questions x M marks", count
#       how many questions you assigned marks=M in that section. If it is fewer than N, you missed
#       one -- go back and fix its marks instead of leaving it as 0.
#     - Preserve the full question wording; do not summarize or truncate it.
#     {section_context}

#     Text to parse:
#     {text}

#     Return ONLY the raw JSON. No markdown blocks.
#     """

#     # A full question paper overflows the default output cap, silently truncating the JSON. Raise the
#     # ceiling + force JSON mode. thinking_budget=0 keeps this verbatim EXTRACTION (Gemini-only knob;
#     # ignored by Qwen). strip_reasoning drops any <think> block a Qwen -Thinking model emits.
#     text_out, _in_tok, _out_tok = generate(
#         model=model_id, prompt=prompt, temperature=0.0,
#         # 32768 is ample for a 40-50 question paper; env-tunable (shared with the key parser).
#         # OpenRouter PRE-AUTHORISES max_tokens against your balance, so keep this modest.
#         max_tokens=int(os.environ.get("KEY_PARSER_MAX_TOKENS", "32768")),
#         json_mode=True, thinking_budget=0,
#     )
#     content = strip_reasoning((text_out or "").strip())

#     # JSON mode returns clean JSON, but a math/symbol-heavy paper can carry UNescaped backslashes
#     # (\frac, \sqrt) -> "Invalid \escape". Try the raw text, then a backslash-repaired copy, each
#     # with a brace-extraction fallback for any leading/trailing noise (same as the key parser).
#     for candidate in (content, _sanitize_json_escapes(content)):
#         try:
#             return json.loads(candidate)
#         except json.JSONDecodeError:
#             m = re.search(r'(\{.*\})', candidate, re.DOTALL)
#             if m:
#                 try:
#                     return json.loads(m.group(1))
#                 except json.JSONDecodeError:
#                     continue
#     raise Exception(f"AI response was not valid JSON (length {len(content)}): {content[:120]}...")


# # ---------------------------------------------------------------------------------------------------
# # PER-PAGE PARALLEL PATH. Each page's questions are extracted concurrently as WHOLE-question entries
# # carrying the question's TOTAL printed marks -- so the paper is the clean per-question marks authority
# # (Q36 case study -> one entry worth 4, never split into leaves the reconciler would mis-total).
# #
# # {extra_context} is spliced in by parallel_parse.extract_pages_parallel from
# # parallel_parse.format_section_context(section_map) -- a document-wide section/numbering map computed
# # BEFORE any page is parsed, so a page whose questions are printed with a section-local restarted
# # numbering ("1.", "2.", "3." inside that section, really Q21/Q22/Q23) can still resolve to the correct
# # GLOBAL id. Empty (a plain continuously-numbered paper) -> the placeholder is spliced out to nothing
# # and this prompt behaves exactly as before.
# #
# # extract_pages_parallel ALSO appends a look-ahead window (the next page's leading text, clearly
# # fenced) to {page_text} itself before this template is even filled in. OWNERSHIP of a question that
# # spans a page break is assigned to THIS (the earlier) page via that look-ahead -- so the instruction
# # below for the LATER page is the mirror image: ignore an unlabeled leading continuation rather than
# # re-capturing it, because the earlier page's own call has already claimed it. Without that explicit
# # hand-off, BOTH pages independently captured the same boundary-spanning text (each transcribing it
# # slightly differently), and merging two near-duplicate captures produced literal duplicate paragraphs
# # in the final output.
# # ---------------------------------------------------------------------------------------------------
# QP_PER_PAGE_PROMPT = """Extract the exam questions that appear on THIS ONE PAGE of a QUESTION PAPER into a JSON object.

# Return ONLY: {"questions": {"<Qn>": {"question_id": "Qn", "question": "<full verbatim text>", "marks": <number>, "type": "..."}, ...}}

# CRITICAL RULES for THIS PAGE:
# - Extract EVERY numbered question printed on this page, WHEREVER on the page it falls -- including questions near the BOTTOM of the page and questions belonging to the paper's LAST section (often case-study / long-answer questions). A page often MIXES non-question text (a cover heading, "General Instructions", or a "Section A / Section B ..." header) WITH questions -- capture the questions and simply ignore the surrounding non-question text. NEVER skip a question just because the page also has a heading or instructions above it, or because it is the final question on the page or in the paper.
# - FORWARD continuation (this page's OWN question running onto the NEXT page): if this page ends partway through a question (its sub-parts, instructions, a table, or other content clearly continues past the bottom of this page), and a "[--- START OF NEXT PAGE ---]" fence appears below showing the next page's leading text, check whether that text is a CONTINUATION of the last question on THIS page (no new question number, reads as a direct continuation) -- if so, INCLUDE that continuation as part of that same question's "question" text, so the question is captured COMPLETE. Do NOT include next-page text that clearly starts a brand-new numbered question.
# - BACKWARD continuation (THIS page opening with the tail of a question that STARTED on the PREVIOUS page): if the very TOP of this page (before the fence, i.e. this page's own real content) is unlabeled continuation text with no question number at all -- reading as the tail end of whatever the previous page was discussing -- COMPLETELY IGNORE that leading fragment. Do NOT emit it as an entry under any question id, guessed or otherwise, and do NOT start counting this page's questions from it. That fragment has ALREADY been captured by the PREVIOUS page's own extraction (which was shown the same text as a forward-continuation look-ahead) -- capturing it again here would duplicate it. Only start extracting from the first genuinely NEW numbered question that begins on this page.
# - Short OBJECTIVE questions ARE questions and must be captured just like long ones: MCQ, True/False, fill-in-the-blank, one-line "predict the output", SQL, and assertion-reason. Keep their (a)/(b)/(c)/(d) options inside the question text. A "Section A, 1 mark each" list of one-mark questions must be captured in FULL (every number in the range).
# - ONE entry per QUESTION NUMBER (Q1, Q22, Q36). Do NOT split a question into separate (a)/(b)/(i)/(ii) entries -- keep ALL of its sub-parts, data tables, passages, figures and any "OR" alternatives INSIDE that one question's "question" text. Bracketed sub-part marks like "[1]", "[2]" inside a case-study question are part of its TEXT, not a signal to split it into separate entries -- keep the whole question (with its brackets) as ONE plain string value.
# - "marks": the marks for that WHOLE question -- a number at the end of the question, in the margin/brackets, or a "Marks" column. If only a SECTION header on this page states the per-question marks (e.g. "Section A ... Each question carries 1 Mark" or "Section -A (21 x 1 = 21 Marks)"), use that value for EVERY question in that section, with NO exceptions. A question offering "(a) ... OR (b) ..." is worth its single printed total -- do NOT add the alternatives. Use 0 ONLY if no marks are printed AND no section header on this page (or a header line carried over from an earlier page) implies a value for it.
# - A question that embeds a DATA TABLE, a frequency/grouped-data table, a long passage, or a figure is STILL just one question in its section and STILL inherits that section's per-question marks exactly like the plainer questions around it. Never default such a question to marks=0 just because most of its text is a table -- the marks come from the SECTION, not from how the question is laid out.
# - SELF-CHECK before returning: for every section header on this page stating "N questions x M marks", make sure you assigned marks=M to every one of that section's questions on this page, including any question built mostly around a table or figure.
# - This is a QUESTION PAPER: capture ONLY what is asked. Do NOT invent, infer, or include answers. There is NO "answer" field.
# - "type": one of MCQ, Short Answer, Long Answer, Numerical.
# - Return {"questions": {}} ONLY when this page has NO numbered questions at all (a pure cover/instructions page with nothing numbered below, or a page consisting ENTIRELY of a backward continuation you are ignoring per the rule above).
# {extra_context}

# THIS PAGE:
# {page_text}

# Return ONLY the raw JSON. No markdown."""


# def _base_qnum_local(qid):
#     """Leading question number from an id like 'Q35', '35', 'Q35(a)' -> 35. None if no digit."""
#     m = re.search(r'(\d+)', str(qid))
#     return int(m.group(1)) if m else None


# def _marks_for_section(base_num, section_map):
#     """The per-question marks the section-map says base_num belongs to, or None if it falls outside
#     every mapped range (e.g. the map is incomplete/empty -- caller then leaves that question alone)."""
#     for s in section_map:
#         if s["from"] <= base_num <= s["to"]:
#             return s["marks"]
#     return None


# def reconcile_marks_with_sections(questions, section_map):
#     """Overlay the document-wide section-marks map (see parallel_parse.extract_section_marks_map)
#     onto the parsed questions: when a question's own marks disagree with (or are missing/zero
#     relative to) its section's stated per-question value, the SECTION value wins -- it was read from
#     the section headers across the WHOLE paper in one pass, so it cannot be fooled by a single page
#     missing its local header.

#     Deliberately does NOT touch a question whose base number falls OUTSIDE every mapped range (an
#     empty/partial map, or a genuinely un-sectioned paper) -- in that case whatever the parser already
#     produced is left exactly as-is, so this can only CORRECT a disagreement, never invent one.

#     Returns (questions, changed) where changed is [(qid, old_marks, new_marks), ...] for logging."""
#     if not section_map:
#         return questions, []
#     changed = []
#     for qid, v in questions.items():
#         if not isinstance(v, dict):
#             continue
#         bn = _base_qnum_local(qid)
#         if bn is None:
#             continue
#         expected = _marks_for_section(bn, section_map)
#         if expected is None:
#             continue
#         try:
#             current = float(v.get("marks") or 0)
#         except (TypeError, ValueError):
#             current = 0.0
#         if abs(current - expected) > 1e-9:
#             changed.append((str(qid), current, expected))
#             v["marks"] = expected
#     return questions, changed


# def _get_section_map(full_text, model_id):
#     """Compute the document-wide section map ONCE (reused for numbering context, marks reconciliation,
#     AND the completeness check below), so a paper only ever pays for this one extra cheap call
#     regardless of how many things consult it. [] on failure or when the paper has no detectable
#     section structure -- every downstream use degrades to a no-op."""
#     if os.environ.get("QP_SECTION_RECONCILE", "1").strip().lower() in ("0", "false", "no", "off"):
#         return []
#     try:
#         section_map, _i, _o = pp.extract_section_marks_map(full_text, model_id)
#         return section_map
#     except Exception as e:
#         print(f"Warning: section map extraction skipped ({e})", file=sys.stderr)
#         return []


# def _expected_total_questions(section_map, full_text):
#     """The highest GLOBAL question number this paper is expected to contain. Primarily the section
#     map's own highest "to" (it was built from the paper's own General Instructions / headers, so it
#     already covers the whole document). Falls back to an explicit "contains N questions" statement in
#     the text when there is no usable section map. None when neither source yields a number -- the
#     completeness check then simply does not run (never a regression versus not having it at all)."""
#     if section_map:
#         return max(s["to"] for s in section_map)
#     m = re.search(r'contains\s+(\d+)\s+questions', full_text, re.IGNORECASE)
#     if m:
#         return int(m.group(1))
#     return None


# def _missing_question_ranges(questions, expected_total):
#     """Contiguous [(lo, hi), ...] ranges of global question numbers 1..expected_total that have NO
#     entry anywhere in `questions` (by base number). Empty when nothing is missing or expected_total
#     is falsy."""
#     if not expected_total:
#         return []
#     present = {bn for qid in questions if (bn := _base_qnum_local(qid)) is not None}
#     missing = sorted(n for n in range(1, expected_total + 1) if n not in present)
#     ranges = []
#     for n in missing:
#         if ranges and n == ranges[-1][1] + 1:
#             ranges[-1] = (ranges[-1][0], n)
#         else:
#             ranges.append((n, n))
#     return ranges


# def _has_thin_content(questions, section_map, min_chars=40):
#     """Base numbers whose question TEXT looks suspiciously short relative to what its section is
#     worth -- the fingerprint of a question that was truncated at a page break rather than fully
#     missing (it exists, so the completeness check alone would miss it, but its content clearly cuts
#     off mid-thought). Heuristic and DELIBERATELY narrow: a genuinely short question (a one-line MCQ
#     worth 1 mark) must never be flagged, so this only fires for a question worth >= 3 marks (which the
#     paper's own section map says should carry real substance) whose captured text is still under
#     `min_chars`. False negatives are fine here (a real truncation with somewhat-more text than the
#     threshold is simply not caught by this pass) -- the cost of a false POSITIVE is an unnecessary
#     extra backfill call, while a false negative just leaves this specific safety net silent, so erring
#     towards fewer false positives is the safe direction."""
#     if not section_map:
#         return []
#     thin = []
#     for qid, v in questions.items():
#         if not isinstance(v, dict):
#             continue
#         bn = _base_qnum_local(qid)
#         if bn is None:
#             continue
#         expected_marks = _marks_for_section(bn, section_map)
#         if expected_marks is None or expected_marks < 3:
#             continue
#         text = str(v.get("question") or "").strip()
#         if len(text) < min_chars:
#             thin.append(bn)
#     return sorted(set(thin))


# QP_BACKFILL_PROMPT = """You are given the FULL text of an exam question paper. A previous extraction
# pass over this SAME paper either FAILED to capture, or captured only a TRUNCATED/incomplete version
# of, the following question number(s): {missing_list}. Find and extract the COMPLETE text of ONLY
# these specific question(s) from the text below -- ignore every other question, even though the full
# text of the whole paper is shown to you for context. Pay special attention to content that continues
# across a page break: a question's own text may run on well past where a truncated first attempt
# stopped, and you must capture that continuation too.

# Return ONLY: {{"questions": {{"<Qn>": {{"question_id": "Qn", "question": "<COMPLETE verbatim text,
# including any data table, passage, figure caption, or bracketed sub-part marks it contains, and
# including any continuation of the question that appears further down / on a later page>", "marks":
# <number>, "type": "MCQ|Short Answer|Long Answer|Numerical"}}, ...}}}} containing an entry for EVERY one
# of the requested question number(s) that you can locate in the text below. Do not invent a question
# that genuinely is not present in the text.
# {section_context}

# FULL QUESTION PAPER TEXT:
# {full_text}

# Return ONLY the raw JSON. No markdown."""


# def _backfill_missing_questions(full_text, missing_numbers, section_context, model_id, max_tokens):
#     """One extra, TARGETED call scoped to ONLY the question number(s) a normal parse failed to
#     produce (or produced only a truncated version of). Runs over the WHOLE document text (never a
#     single page), so it is immune to the page-local blind spots that dropped/truncated them in the
#     first place, and it tells the model EXACTLY which numbers must be complete -- rather than
#     re-running the same page-by-page extraction and hoping it succeeds this time. Returns {} on
#     failure (caller then reports the questions as still missing, rather than silently proceeding as
#     if nothing were wrong)."""
#     missing_list = ", ".join(f"Q{n}" for n in missing_numbers)
#     prompt = (QP_BACKFILL_PROMPT.replace("{missing_list}", missing_list)
#              .replace("{section_context}", section_context or "")
#              .replace("{full_text}", full_text))
#     try:
#         out, _i, _o = generate(model=model_id, prompt=prompt, temperature=0.0,
#                                max_tokens=max_tokens, json_mode=True, thinking_budget=0)
#         data = pp.tolerant_json(out)
#         qs = pp._questions_of(data) if isinstance(data, dict) else {}
#         return qs if isinstance(qs, dict) else {}
#     except Exception as e:
#         print(f"[QP parse] backfill call failed: {type(e).__name__}: {e}", file=sys.stderr)
#         return {}


# def verify_and_backfill_completeness(parsed_json, full_text, section_map, section_context, model_id):
#     """THE completeness safety net. Runs TWO checks against what was actually parsed, both compared
#     to what the paper's OWN section map/instructions declare, and runs ONE combined targeted recovery
#     call for anything found wanting -- rather than hoping a per-page retry happens to succeed, or
#     (worse) silently returning a paper short of, or with truncated, questions with no indication
#     anything went wrong:

#       1. MISSING -- a global question number with NO entry at all anywhere in the result.
#       2. THIN/TRUNCATED -- a question that DOES have an entry, but whose captured text is
#          suspiciously short for a question its own section says should carry real substance (the
#          fingerprint of content that was cut off at a page break rather than genuinely absent).

#     This is the single place completeness is enforced, so it runs identically after EITHER the
#     per-page parallel path or the single-call path: a question can go missing or get truncated from a
#     single, long call just as easily as from a page-local one, so both deserve the same safety net.

#     No-op (parsed_json returned unchanged) when the expected total cannot be determined (no section
#     map, no explicit question count in the text) -- this can only RECOVER a known gap, never guess one
#     into existence. Anything still missing/thin after the recovery attempt is logged loudly (never
#     silently absorbed), so a genuine extraction failure remains visible to whoever reads the logs."""
#     questions = parsed_json.get("questions") if isinstance(parsed_json, dict) else None
#     if not isinstance(questions, dict):
#         return parsed_json
#     expected_total = _expected_total_questions(section_map, full_text)
#     if not expected_total:
#         return parsed_json

#     missing_ranges = _missing_question_ranges(questions, expected_total)
#     missing_numbers = [n for lo, hi in missing_ranges for n in range(lo, hi + 1)]
#     thin_numbers = [n for n in _has_thin_content(questions, section_map) if n not in missing_numbers]
#     target_numbers = sorted(set(missing_numbers) | set(thin_numbers))
#     if not target_numbers:
#         return parsed_json

#     if missing_numbers:
#         print(f"[QP parse] {len(missing_numbers)} question(s) missing after the initial parse "
#              f"(expected up to Q{expected_total}): " + ", ".join(f"Q{n}" for n in missing_numbers),
#              file=sys.stderr)
#     if thin_numbers:
#         print(f"[QP parse] {len(thin_numbers)} question(s) look truncated (short text for a "
#              f"substantial-mark question): " + ", ".join(f"Q{n}" for n in thin_numbers),
#              file=sys.stderr)
#     print(f"[QP parse] running a targeted recovery pass for: "
#          + ", ".join(f"Q{n}" for n in target_numbers), file=sys.stderr)

#     max_tokens = int(os.environ.get("KEY_PARSER_MAX_TOKENS", "32768"))
#     recovered = _backfill_missing_questions(full_text, target_numbers, section_context,
#                                             model_id, max_tokens)
#     if recovered:
#         for k, v in recovered.items():
#             if not isinstance(v, dict):
#                 continue
#             # For a THIN (already-present) entry, only replace it when the recovery genuinely got
#             # MORE text -- never let a worse/failed recovery call regress an entry that already had
#             # something. A MISSING entry is simply installed outright.
#             bn = _base_qnum_local(k)
#             existing = questions.get(k)
#             if isinstance(existing, dict) and bn in thin_numbers and bn not in missing_numbers:
#                 if len(str(v.get("question") or "")) > len(str(existing.get("question") or "")):
#                     questions[k] = v
#             else:
#                 questions[k] = v

#         still_missing = [n for lo, hi in _missing_question_ranges(questions, expected_total)
#                          for n in range(lo, hi + 1)]
#         still_thin = [n for n in _has_thin_content(questions, section_map) if n not in still_missing]
#         if still_missing or still_thin:
#             if still_missing:
#                 print(f"[QP parse] WARNING: still missing after the recovery pass: "
#                      + ", ".join(f"Q{n}" for n in still_missing)
#                      + " -- could not be located in the extracted text. Verify the source PDF "
#                        "manually (it may need OCR, or its text layer may be corrupt for this range).",
#                      file=sys.stderr)
#             if still_thin:
#                 print(f"[QP parse] WARNING: still look truncated after the recovery pass: "
#                      + ", ".join(f"Q{n}" for n in still_thin)
#                      + " -- verify these questions against the source PDF manually.", file=sys.stderr)
#         else:
#             print(f"[QP parse] recovery pass succeeded -- all {len(target_numbers)} question(s) "
#                  f"recovered/completed.", file=sys.stderr)
#     else:
#         print(f"[QP parse] WARNING: recovery pass returned nothing; "
#              + ", ".join(f"Q{n}" for n in target_numbers)
#              + " remain missing/incomplete in the result. Verify the source PDF manually.",
#              file=sys.stderr)

#     parsed_json["questions"] = questions
#     return parsed_json


# def _apply_marks_reconciliation(parsed_json, section_map):
#     """Post-parse marks safety net, reusing an ALREADY-COMPUTED section_map. Runs AFTER completeness
#     backfill so any newly-recovered/completed question also gets its marks checked against the map."""
#     if not section_map:
#         return parsed_json
#     questions = parsed_json.get("questions") if isinstance(parsed_json, dict) else None
#     if not isinstance(questions, dict) or not questions:
#         return parsed_json
#     questions, changed = reconcile_marks_with_sections(questions, section_map)
#     if changed:
#         print("[QP marks] corrected against the paper's own section headers: "
#              + ", ".join(f"{q} {o:g}->{n:g}" for q, o, n in changed), file=sys.stderr)
#     parsed_json["questions"] = questions
#     return parsed_json


# def parse_qp_parallel(page_texts, section_context=""):
#     """Per-page parallel extraction of WHOLE-question entries (with total marks) -> {questions}. No
#     global choice pass: the paper's authority is the per-question TOTAL, so no OR-splitting is needed.
#     `section_context` (from parallel_parse.format_section_context) is spliced into EVERY page's own
#     prompt via `{extra_context}`, so a page whose questions are printed with a section-local restarted
#     numbering can still resolve to the correct GLOBAL id -- a single page's own text cannot supply
#     that mapping on its own. extract_pages_parallel additionally gives each page an overlapping
#     look-ahead window (see its own docstring) so a question whose tail runs onto the next page without
#     repeating its number is not silently dropped, WITHOUT being double-captured by the following page
#     (see QP_PER_PAGE_PROMPT's forward/backward continuation rules)."""
#     _load_project_env()
#     model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
#     per_page_max = int(os.environ.get("KEY_PARSER_PAGE_MAX_TOKENS", "8192"))
#     questions, _i, _o = pp.extract_pages_parallel(page_texts, QP_PER_PAGE_PROMPT, model_id, per_page_max,
#                                                   extra_context=section_context)
#     return {"questions": questions}


# def main():
#     if len(sys.argv) < 2:
#         print("Usage: python3 extract_json_from_question_paper.py <file_path>")
#         sys.exit(1)

#     file_path = sys.argv[1]
#     ext = Path(file_path).suffix.lower()

#     try:
#         if ext == '.json':
#             with open(file_path, 'r') as f:
#                 content = f.read()
#                 json.loads(content)          # validate, then echo a raw .json upload through unchanged
#                 print(content)
#                 return

#         elif ext == '.docx':
#             raw_text = extract_text_from_docx(file_path)
#             if not raw_text.strip():
#                 print("ERROR: No text extracted from file.")
#                 sys.exit(1)
#             model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
#             section_map = _get_section_map(raw_text, model_id)
#             section_context = pp.format_section_context(section_map)
#             parsed_json = parse_question_paper_with_gemini(raw_text, section_context)  # docx -> single call
#             parsed_json = verify_and_backfill_completeness(parsed_json, raw_text, section_map,
#                                                            section_context, model_id)
#             parsed_json = _apply_marks_reconciliation(parsed_json, section_map)

#         elif ext == '.pdf':
#             page_texts = pp.pdf_page_texts(file_path)
#             raw_text = "\n".join(page_texts)
#             if not raw_text.strip():
#                 print("ERROR: No text extracted from file.")
#                 sys.exit(1)
#             model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
#             # Computed FIRST, from the whole document, so numbering context (below), the
#             # completeness check, and marks reconciliation all reuse this SAME map -- one extra call
#             # total, regardless of how many things consult it.
#             section_map = _get_section_map(raw_text, model_id)
#             section_context = pp.format_section_context(section_map)
#             if pp.parallel_enabled() and len([t for t in page_texts if t.strip()]) > 1:
#                 parsed_json = parse_qp_parallel(page_texts, section_context)
#             else:
#                 parsed_json = parse_question_paper_with_gemini(raw_text, section_context)
#             # Runs for BOTH the parallel and single-call PDF paths: a question can go missing/get
#             # truncated from a single long call just as easily as from a page-local one, so both get
#             # the same net.
#             parsed_json = verify_and_backfill_completeness(parsed_json, raw_text, section_map,
#                                                            section_context, model_id)
#             parsed_json = _apply_marks_reconciliation(parsed_json, section_map)

#         else:
#             print(f"ERROR: Unsupported file extension {ext}")
#             sys.exit(1)

#         print(json.dumps(parsed_json, indent=2))

#     except Exception as e:
#         print(f"ERROR: {str(e)}", file=sys.stderr)
#         sys.exit(1)


# if __name__ == "__main__":
#     main()







import os
import sys
import json
import warnings
import re
# Suppress all warnings to keep stdout clean for JSON
warnings.filterwarnings("ignore")

from pathlib import Path

# Reuse the answer-key parser's shared helpers rather than duplicating them. The key parser lives
# beside this file; add its directory to the path so the import resolves even when this script is
# run as a subprocess from the Flask app's cwd. Forking the parser (instead of parametrizing it)
# keeps each prompt single-purpose so a question-paper prompt change can never regress key parsing.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from extract_json_from_key import (
    extract_text_from_docx,
    extract_text_from_pdf,
    _sanitize_json_escapes,
    _load_project_env,
)
from llm_client import generate, strip_reasoning
import parallel_parse as pp


def parse_question_paper_with_gemini(text, section_context=""):
    # Make .env settings (provider + model + keys) visible whether run standalone or from Flask.
    _load_project_env()
    # Reuse KEY_PARSER_MODEL so the key + question-paper parsers move together. Default to a capable
    # Qwen3-VL Instruct model.
    model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")

    prompt = f"""
    Extract the questions from the following EXAM QUESTION PAPER into a structured JSON object.

    Return a JSON object with EXACTLY one top-level key:
    1. "questions": a dictionary where keys are question IDs (e.g., Q1, Q2, or specific IDs like
       Q31(a), Q31(b) exactly as printed). Each value is a dictionary with:
       - question_id: string
       - question: string (the FULL, verbatim text of the question, including any data table,
         passage, figure caption, or sub-prompt the student must read in order to answer it)
       - marks: number (maximum marks for this question if printed; otherwise 0)
       - type: string (one of: MCQ, Short Answer, Long Answer, Numerical)

    CRITICAL RULES:
    - This is a QUESTION PAPER, not a marking scheme. DO NOT invent, infer, or include answers.
      There is NO "answer" field -- never emit one, even if you think you know the answer.
      Capture ONLY what the paper asks.
    - Capture EVERY numbered question in the ENTIRE document, from the first to the LAST section --
      including short OBJECTIVE questions (MCQ / True-False / fill-in-the-blank / one-line "predict
      the output" / SQL / assertion-reason) AND the paper's final section (often case-study / long-
      answer questions). Do NOT skip a whole section, and do NOT stop before the paper's last
      question just because it is long or comes after several other sections.
    - A question printed near the BOTTOM of a page whose sub-parts, instructions, or continuing text
      run onto the TOP of the next page (with no new question number repeated there) is STILL ONE
      question -- capture ALL of it, including the part that continues past the page boundary. Never
      cut a question short just because part of its content sits on the following page. However, a
      repeated PAGE HEADER/FOOTER line (a disclaimer, a page number, a running title) that appears
      near a page boundary is NOT part of any question's continuation -- leave it out entirely.
    - NEVER drop the word "OR" that separates two internal-choice alternatives (e.g. "A. ... OR
      B. ..."). It is structurally significant -- it tells the grader the student may attempt EITHER
      alternative, not both -- and must be preserved verbatim, exactly where it appears in the
      question text, whenever the paper prints it. Dropping it silently changes an "answer either A
      or B" question into what looks like "answer both A and B".
    - Use the paper's GLOBAL question numbering for question IDs (Q1, Q31(a), Q31(b), ...) so they
      can be matched to the answer key later -- NOT a section's own local/restarted numbering. See
      the LOCAL vs GLOBAL guidance below if this paper's sections restart their own numbering.
    - "marks": use the number printed for each question; if only a section header gives it
      ("Section A ... Each question carries 1 Mark"), apply that per-question value to EVERY
      question physically located in that section -- with NO exceptions. A question's marks come
      from its SECTION, never from how long or complex its text is.
    - DATA TABLES / EMBEDDED CONTENT DO NOT CHANGE THIS RULE. A question that includes a
      frequency table, a data list, a figure, or a multi-line passage is STILL just one question
      belonging to its section, and it STILL inherits that section's per-question marks exactly
      like every other question around it. Do NOT default such a question's marks to 0 just
      because the table/data occupies most of the question's space -- the marks come from the
      SECTION HEADER, not from counting rows in the table.
    - SELF-CHECK before returning: for every section that states "N questions x M marks", count
      how many questions you assigned marks=M in that section. If it is fewer than N, you missed
      one -- go back and fix its marks instead of leaving it as 0.
    - Preserve the full question wording; do not summarize or truncate it.
    {section_context}

    Text to parse:
    {text}

    Return ONLY the raw JSON. No markdown blocks.
    """

    # A full question paper overflows the default output cap, silently truncating the JSON. Raise the
    # ceiling + force JSON mode. thinking_budget=0 keeps this verbatim EXTRACTION (Gemini-only knob;
    # ignored by Qwen). strip_reasoning drops any <think> block a Qwen -Thinking model emits.
    text_out, _in_tok, _out_tok = generate(
        model=model_id, prompt=prompt, temperature=0.0,
        # 32768 is ample for a 40-50 question paper; env-tunable (shared with the key parser).
        # OpenRouter PRE-AUTHORISES max_tokens against your balance, so keep this modest.
        max_tokens=int(os.environ.get("KEY_PARSER_MAX_TOKENS", "32768")),
        json_mode=True, thinking_budget=0,
    )
    content = strip_reasoning((text_out or "").strip())

    # JSON mode returns clean JSON, but a math/symbol-heavy paper can carry UNescaped backslashes
    # (\frac, \sqrt) -> "Invalid \escape". Try the raw text, then a backslash-repaired copy, each
    # with a brace-extraction fallback for any leading/trailing noise (same as the key parser).
    for candidate in (content, _sanitize_json_escapes(content)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            m = re.search(r'(\{.*\})', candidate, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(1))
                except json.JSONDecodeError:
                    continue
    raise Exception(f"AI response was not valid JSON (length {len(content)}): {content[:120]}...")


# ---------------------------------------------------------------------------------------------------
# PER-PAGE PARALLEL PATH. Each page's questions are extracted concurrently as WHOLE-question entries
# carrying the question's TOTAL printed marks -- so the paper is the clean per-question marks authority
# (Q36 case study -> one entry worth 4, never split into leaves the reconciler would mis-total).
#
# {extra_context} is spliced in by parallel_parse.extract_pages_parallel from
# parallel_parse.format_section_context(section_map) -- a document-wide section/numbering map computed
# BEFORE any page is parsed, so a page whose questions are printed with a section-local restarted
# numbering ("1.", "2.", "3." inside that section, really Q21/Q22/Q23) can still resolve to the correct
# GLOBAL id. Empty (a plain continuously-numbered paper) -> the placeholder is spliced out to nothing
# and this prompt behaves exactly as before.
#
# extract_pages_parallel ALSO appends a look-ahead window (the next page's leading text, clearly
# fenced and stripped of detected page-boilerplate) to {page_text} itself before this template is even
# filled in. OWNERSHIP of a question that spans a page break is assigned to THIS (the earlier) page via
# that look-ahead -- so the instruction below for the LATER page is the mirror image: ignore an
# unlabeled leading continuation rather than re-capturing it, because the earlier page's own call has
# already claimed it.
# ---------------------------------------------------------------------------------------------------
QP_PER_PAGE_PROMPT = """Extract the exam questions that appear on THIS ONE PAGE of a QUESTION PAPER into a JSON object.

Return ONLY: {"questions": {"<Qn>": {"question_id": "Qn", "question": "<full verbatim text>", "marks": <number>, "type": "..."}, ...}}

CRITICAL RULES for THIS PAGE:
- Extract EVERY numbered question printed on this page, WHEREVER on the page it falls -- including questions near the BOTTOM of the page and questions belonging to the paper's LAST section (often case-study / long-answer questions). A page often MIXES non-question text (a cover heading, "General Instructions", or a "Section A / Section B ..." header) WITH questions -- capture the questions and simply ignore the surrounding non-question text. NEVER skip a question just because the page also has a heading or instructions above it, or because it is the final question on the page or in the paper.
- IGNORE repeated PAGE HEADERS/FOOTERS entirely: a disclaimer line, a "Page N" marker, or any running title that appears near the top or bottom of the page is NOT part of any question and must NEVER be included in a question's "question" text, wherever it happens to sit relative to that question's own content.
- NEVER drop the word "OR" that separates two internal-choice alternatives (e.g. "A. ... OR B. ..."). It is structurally significant -- it tells the grader the student may attempt EITHER alternative, not both -- and must be preserved verbatim, exactly where it appears, whenever the page prints it. Dropping it silently turns an "answer either A or B" question into what looks like "answer both A and B".
- FORWARD continuation (this page's OWN question running onto the NEXT page): if this page ends partway through a question (its sub-parts, instructions, a table, or other content clearly continues past the bottom of this page), and a "[--- START OF NEXT PAGE ---]" fence appears below showing the next page's leading text, check whether that text is a CONTINUATION of the last question on THIS page (no new question number, reads as a direct continuation) -- if so, INCLUDE that continuation as part of that same question's "question" text, so the question is captured COMPLETE. Do NOT include next-page text that clearly starts a brand-new numbered question.
- BACKWARD continuation (THIS page opening with the tail of a question that STARTED on the PREVIOUS page): if the very TOP of this page (before the fence, i.e. this page's own real content) is unlabeled continuation text with no question number at all -- reading as the tail end of whatever the previous page was discussing -- COMPLETELY IGNORE that leading fragment. Do NOT emit it as an entry under any question id, guessed or otherwise, and do NOT start counting this page's questions from it. That fragment has ALREADY been captured by the PREVIOUS page's own extraction (which was shown the same text as a forward-continuation look-ahead) -- capturing it again here would duplicate it. Only start extracting from the first genuinely NEW numbered question that begins on this page.
- Short OBJECTIVE questions ARE questions and must be captured just like long ones: MCQ, True/False, fill-in-the-blank, one-line "predict the output", SQL, and assertion-reason. Keep their (a)/(b)/(c)/(d) options inside the question text. A "Section A, 1 mark each" list of one-mark questions must be captured in FULL (every number in the range).
- ONE entry per QUESTION NUMBER (Q1, Q22, Q36). Do NOT split a question into separate (a)/(b)/(i)/(ii) entries -- keep ALL of its sub-parts, data tables, passages, figures and any "OR" alternatives INSIDE that one question's "question" text. Bracketed sub-part marks like "[1]", "[2]" inside a case-study question are part of its TEXT, not a signal to split it into separate entries -- keep the whole question (with its brackets) as ONE plain string value.
- "marks": the marks for that WHOLE question -- a number at the end of the question, in the margin/brackets, or a "Marks" column. If only a SECTION header on this page states the per-question marks (e.g. "Section A ... Each question carries 1 Mark" or "Section -A (21 x 1 = 21 Marks)"), use that value for EVERY question in that section, with NO exceptions. A question offering "(a) ... OR (b) ..." is worth its single printed total -- do NOT add the alternatives. Use 0 ONLY if no marks are printed AND no section header on this page (or a header line carried over from an earlier page) implies a value for it.
- A question that embeds a DATA TABLE, a frequency/grouped-data table, a long passage, or a figure is STILL just one question in its section and STILL inherits that section's per-question marks exactly like the plainer questions around it. Never default such a question to marks=0 just because most of its text is a table -- the marks come from the SECTION, not from how the question is laid out.
- SELF-CHECK before returning: for every section header on this page stating "N questions x M marks", make sure you assigned marks=M to every one of that section's questions on this page, including any question built mostly around a table or figure.
- This is a QUESTION PAPER: capture ONLY what is asked. Do NOT invent, infer, or include answers. There is NO "answer" field -- never emit one.
- "type": one of MCQ, Short Answer, Long Answer, Numerical.
- Return {"questions": {}} ONLY when this page has NO numbered questions at all (a pure cover/instructions page with nothing numbered below, or a page consisting ENTIRELY of a backward continuation you are ignoring per the rule above).
{extra_context}

THIS PAGE:
{page_text}

Return ONLY the raw JSON. No markdown."""


def _base_qnum_local(qid):
    """Leading question number from an id like 'Q35', '35', 'Q35(a)' -> 35. None if no digit."""
    m = re.search(r'(\d+)', str(qid))
    return int(m.group(1)) if m else None


def _marks_for_section(base_num, section_map):
    """The per-question marks the section-map says base_num belongs to, or None if it falls outside
    every mapped range (e.g. the map is incomplete/empty -- caller then leaves that question alone)."""
    for s in section_map:
        if s["from"] <= base_num <= s["to"]:
            return s["marks"]
    return None


def reconcile_marks_with_sections(questions, section_map):
    """Overlay the document-wide section-marks map (see parallel_parse.extract_section_marks_map)
    onto the parsed questions: when a question's own marks disagree with (or are missing/zero
    relative to) its section's stated per-question value, the SECTION value wins -- it was read from
    the section headers across the WHOLE paper in one pass, so it cannot be fooled by a single page
    missing its local header.

    Deliberately does NOT touch a question whose base number falls OUTSIDE every mapped range (an
    empty/partial map, or a genuinely un-sectioned paper) -- in that case whatever the parser already
    produced is left exactly as-is, so this can only CORRECT a disagreement, never invent one.

    Returns (questions, changed) where changed is [(qid, old_marks, new_marks), ...] for logging."""
    if not section_map:
        return questions, []
    changed = []
    for qid, v in questions.items():
        if not isinstance(v, dict):
            continue
        bn = _base_qnum_local(qid)
        if bn is None:
            continue
        expected = _marks_for_section(bn, section_map)
        if expected is None:
            continue
        try:
            current = float(v.get("marks") or 0)
        except (TypeError, ValueError):
            current = 0.0
        if abs(current - expected) > 1e-9:
            changed.append((str(qid), current, expected))
            v["marks"] = expected
    return questions, changed


def _get_section_map(full_text, model_id):
    """Compute the document-wide section map ONCE (reused for numbering context, marks reconciliation,
    AND the completeness check below), so a paper only ever pays for this one extra cheap call
    regardless of how many things consult it. [] on failure or when the paper has no detectable
    section structure -- every downstream use degrades to a no-op."""
    if os.environ.get("QP_SECTION_RECONCILE", "1").strip().lower() in ("0", "false", "no", "off"):
        return []
    try:
        section_map, _i, _o = pp.extract_section_marks_map(full_text, model_id)
        return section_map
    except Exception as e:
        print(f"Warning: section map extraction skipped ({e})", file=sys.stderr)
        return []


def _expected_total_questions(section_map, full_text):
    """The highest GLOBAL question number this paper is expected to contain. Primarily the section
    map's own highest "to" (it was built from the paper's own General Instructions / headers, so it
    already covers the whole document). Falls back to an explicit "contains N questions" statement in
    the text when there is no usable section map. None when neither source yields a number -- the
    completeness check then simply does not run (never a regression versus not having it at all)."""
    if section_map:
        return max(s["to"] for s in section_map)
    m = re.search(r'contains\s+(\d+)\s+questions', full_text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _missing_question_ranges(questions, expected_total):
    """Contiguous [(lo, hi), ...] ranges of global question numbers 1..expected_total that have NO
    entry anywhere in `questions` (by base number). Empty when nothing is missing or expected_total
    is falsy."""
    if not expected_total:
        return []
    present = {bn for qid in questions if (bn := _base_qnum_local(qid)) is not None}
    missing = sorted(n for n in range(1, expected_total + 1) if n not in present)
    ranges = []
    for n in missing:
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], n)
        else:
            ranges.append((n, n))
    return ranges


def _has_thin_content(questions, section_map, min_chars=40):
    """Base numbers whose question TEXT looks suspiciously short relative to what its section is
    worth -- the fingerprint of a question that was truncated at a page break rather than fully
    missing (it exists, so the completeness check alone would miss it, but its content clearly cuts
    off mid-thought). Heuristic and DELIBERATELY narrow: a genuinely short question (a one-line MCQ
    worth 1 mark) must never be flagged, so this only fires for a question worth >= 3 marks (which the
    paper's own section map says should carry real substance) whose captured text is still under
    `min_chars`. False negatives are fine here (a real truncation with somewhat-more text than the
    threshold is simply not caught by this pass) -- the cost of a false POSITIVE is an unnecessary
    extra backfill call, while a false negative just leaves this specific safety net silent, so erring
    towards fewer false positives is the safe direction."""
    if not section_map:
        return []
    thin = []
    for qid, v in questions.items():
        if not isinstance(v, dict):
            continue
        bn = _base_qnum_local(qid)
        if bn is None:
            continue
        expected_marks = _marks_for_section(bn, section_map)
        if expected_marks is None or expected_marks < 3:
            continue
        text = str(v.get("question") or "").strip()
        if len(text) < min_chars:
            thin.append(bn)
    return sorted(set(thin))


QP_BACKFILL_PROMPT = """You are given the FULL text of an exam question paper. A previous extraction
pass over this SAME paper either FAILED to capture, or captured only a TRUNCATED/incomplete version
of, the following question number(s): {missing_list}. Find and extract the COMPLETE text of ONLY
these specific question(s) from the text below -- ignore every other question, even though the full
text of the whole paper is shown to you for context. Pay special attention to content that continues
across a page break: a question's own text may run on well past where a truncated first attempt
stopped, and you must capture that continuation too. Exclude any repeated page header/footer text
(disclaimers, page numbers, running titles) -- that is never part of a question's own content. If a
question presents two alternatives joined by the word "OR", preserve that word verbatim -- do not
drop it.

Return ONLY: {{"questions": {{"<Qn>": {{"question_id": "Qn", "question": "<COMPLETE verbatim text,
including any data table, passage, figure caption, or bracketed sub-part marks it contains, and
including any continuation of the question that appears further down / on a later page, but
EXCLUDING any page header/footer text>", "marks": <number>, "type":
"MCQ|Short Answer|Long Answer|Numerical"}}, ...}}}} containing an entry for EVERY one of the requested
question number(s) that you can locate in the text below. Do not invent a question that genuinely is
not present in the text. Do NOT include an "answer" field -- this is a question paper, not a marking
scheme.
{section_context}

FULL QUESTION PAPER TEXT:
{full_text}

Return ONLY the raw JSON. No markdown."""


def _backfill_missing_questions(full_text, missing_numbers, section_context, model_id, max_tokens):
    """One extra, TARGETED call scoped to ONLY the question number(s) a normal parse failed to
    produce (or produced only a truncated version of). Runs over the WHOLE document text (never a
    single page), so it is immune to the page-local blind spots that dropped/truncated them in the
    first place, and it tells the model EXACTLY which numbers must be complete -- rather than
    re-running the same page-by-page extraction and hoping it succeeds this time. Returns {} on
    failure (caller then reports the questions as still missing, rather than silently proceeding as
    if nothing were wrong)."""
    missing_list = ", ".join(f"Q{n}" for n in missing_numbers)
    prompt = (QP_BACKFILL_PROMPT.replace("{missing_list}", missing_list)
             .replace("{section_context}", section_context or "")
             .replace("{full_text}", full_text))
    try:
        out, _i, _o = generate(model=model_id, prompt=prompt, temperature=0.0,
                               max_tokens=max_tokens, json_mode=True, thinking_budget=0)
        data = pp.tolerant_json(out)
        qs = pp._questions_of(data) if isinstance(data, dict) else {}
        return qs if isinstance(qs, dict) else {}
    except Exception as e:
        print(f"[QP parse] backfill call failed: {type(e).__name__}: {e}", file=sys.stderr)
        return {}


def verify_and_backfill_completeness(parsed_json, full_text, section_map, section_context, model_id):
    """THE completeness safety net. Runs TWO checks against what was actually parsed, both compared
    to what the paper's OWN section map/instructions declare, and runs ONE combined targeted recovery
    call for anything found wanting -- rather than hoping a per-page retry happens to succeed, or
    (worse) silently returning a paper short of, or with truncated, questions with no indication
    anything went wrong:

      1. MISSING -- a global question number with NO entry at all anywhere in the result.
      2. THIN/TRUNCATED -- a question that DOES have an entry, but whose captured text is
         suspiciously short for a question its own section says should carry real substance (the
         fingerprint of content that was cut off at a page break rather than genuinely absent).

    This is the single place completeness is enforced, so it runs identically after EITHER the
    per-page parallel path or the single-call path: a question can go missing or get truncated from a
    single, long call just as easily as from a page-local one, so both deserve the same safety net.

    No-op (parsed_json returned unchanged) when the expected total cannot be determined (no section
    map, no explicit question count in the text) -- this can only RECOVER a known gap, never guess one
    into existence. Anything still missing/thin after the recovery attempt is logged loudly (never
    silently absorbed), so a genuine extraction failure remains visible to whoever reads the logs."""
    questions = parsed_json.get("questions") if isinstance(parsed_json, dict) else None
    if not isinstance(questions, dict):
        return parsed_json
    expected_total = _expected_total_questions(section_map, full_text)
    if not expected_total:
        return parsed_json

    missing_ranges = _missing_question_ranges(questions, expected_total)
    missing_numbers = [n for lo, hi in missing_ranges for n in range(lo, hi + 1)]
    thin_numbers = [n for n in _has_thin_content(questions, section_map) if n not in missing_numbers]
    target_numbers = sorted(set(missing_numbers) | set(thin_numbers))
    if not target_numbers:
        return parsed_json

    if missing_numbers:
        print(f"[QP parse] {len(missing_numbers)} question(s) missing after the initial parse "
             f"(expected up to Q{expected_total}): " + ", ".join(f"Q{n}" for n in missing_numbers),
             file=sys.stderr)
    if thin_numbers:
        print(f"[QP parse] {len(thin_numbers)} question(s) look truncated (short text for a "
             f"substantial-mark question): " + ", ".join(f"Q{n}" for n in thin_numbers),
             file=sys.stderr)
    print(f"[QP parse] running a targeted recovery pass for: "
         + ", ".join(f"Q{n}" for n in target_numbers), file=sys.stderr)

    max_tokens = int(os.environ.get("KEY_PARSER_MAX_TOKENS", "32768"))
    recovered = _backfill_missing_questions(full_text, target_numbers, section_context,
                                            model_id, max_tokens)
    if recovered:
        for k, v in recovered.items():
            if not isinstance(v, dict):
                continue
            # For a THIN (already-present) entry, only replace it when the recovery genuinely got
            # MORE text -- never let a worse/failed recovery call regress an entry that already had
            # something. A MISSING entry is simply installed outright.
            bn = _base_qnum_local(k)
            existing = questions.get(k)
            if isinstance(existing, dict) and bn in thin_numbers and bn not in missing_numbers:
                if len(str(v.get("question") or "")) > len(str(existing.get("question") or "")):
                    questions[k] = v
            else:
                questions[k] = v

        still_missing = [n for lo, hi in _missing_question_ranges(questions, expected_total)
                         for n in range(lo, hi + 1)]
        still_thin = [n for n in _has_thin_content(questions, section_map) if n not in still_missing]
        if still_missing or still_thin:
            if still_missing:
                print(f"[QP parse] WARNING: still missing after the recovery pass: "
                     + ", ".join(f"Q{n}" for n in still_missing)
                     + " -- could not be located in the extracted text. Verify the source PDF "
                       "manually (it may need OCR, or its text layer may be corrupt for this range).",
                     file=sys.stderr)
            if still_thin:
                print(f"[QP parse] WARNING: still look truncated after the recovery pass: "
                     + ", ".join(f"Q{n}" for n in still_thin)
                     + " -- verify these questions against the source PDF manually.", file=sys.stderr)
        else:
            print(f"[QP parse] recovery pass succeeded -- all {len(target_numbers)} question(s) "
                 f"recovered/completed.", file=sys.stderr)
    else:
        print(f"[QP parse] WARNING: recovery pass returned nothing; "
             + ", ".join(f"Q{n}" for n in target_numbers)
             + " remain missing/incomplete in the result. Verify the source PDF manually.",
             file=sys.stderr)

    parsed_json["questions"] = questions
    return parsed_json


def _apply_marks_reconciliation(parsed_json, section_map):
    """Post-parse marks safety net, reusing an ALREADY-COMPUTED section_map. Runs AFTER completeness
    backfill so any newly-recovered/completed question also gets its marks checked against the map."""
    if not section_map:
        return parsed_json
    questions = parsed_json.get("questions") if isinstance(parsed_json, dict) else None
    if not isinstance(questions, dict) or not questions:
        return parsed_json
    questions, changed = reconcile_marks_with_sections(questions, section_map)
    if changed:
        print("[QP marks] corrected against the paper's own section headers: "
             + ", ".join(f"{q} {o:g}->{n:g}" for q, o, n in changed), file=sys.stderr)
    parsed_json["questions"] = questions
    return parsed_json


def _finalize(parsed_json):
    """Last-mile cleanup applied identically on every path (single-call, parallel, docx, pdf) right
    before the result is returned: strip any stray 'answer' field a question-paper extraction call
    should never emit (see parallel_parse._strip_stray_answer_field). Kept as its own small step so a
    future additional cleanup rule has one obvious place to live."""
    questions = parsed_json.get("questions") if isinstance(parsed_json, dict) else None
    if isinstance(questions, dict):
        pp._strip_stray_answer_field(questions)
        parsed_json["questions"] = questions
    return parsed_json


def parse_qp_parallel(page_texts, section_context=""):
    """Per-page parallel extraction of WHOLE-question entries (with total marks) -> {questions}. No
    global choice pass: the paper's authority is the per-question TOTAL, so no OR-splitting is needed.
    `section_context` (from parallel_parse.format_section_context) is spliced into EVERY page's own
    prompt via `{extra_context}`, so a page whose questions are printed with a section-local restarted
    numbering can still resolve to the correct GLOBAL id -- a single page's own text cannot supply
    that mapping on its own. extract_pages_parallel additionally gives each page an overlapping
    look-ahead window (see its own docstring) so a question whose tail runs onto the next page without
    repeating its number is not silently dropped, WITHOUT being double-captured by the following page
    (see QP_PER_PAGE_PROMPT's forward/backward continuation rules), and with page boilerplate
    (headers/footers) stripped out of that look-ahead before it is ever shown to the model -- using a
    minimum-line-length floor so a short, legitimately-repeating structural keyword like "OR" is never
    mistaken for boilerplate and stripped."""
    _load_project_env()
    model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
    per_page_max = int(os.environ.get("KEY_PARSER_PAGE_MAX_TOKENS", "8192"))
    questions, _i, _o = pp.extract_pages_parallel(page_texts, QP_PER_PAGE_PROMPT, model_id, per_page_max,
                                                  extra_context=section_context)
    return {"questions": questions}


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 extract_json_from_question_paper.py <file_path>")
        sys.exit(1)

    file_path = sys.argv[1]
    ext = Path(file_path).suffix.lower()

    try:
        if ext == '.json':
            with open(file_path, 'r') as f:
                content = f.read()
                json.loads(content)          # validate, then echo a raw .json upload through unchanged
                print(content)
                return

        elif ext == '.docx':
            raw_text = extract_text_from_docx(file_path)
            if not raw_text.strip():
                print("ERROR: No text extracted from file.")
                sys.exit(1)
            model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
            section_map = _get_section_map(raw_text, model_id)
            section_context = pp.format_section_context(section_map)
            parsed_json = parse_question_paper_with_gemini(raw_text, section_context)  # docx -> single call
            parsed_json = verify_and_backfill_completeness(parsed_json, raw_text, section_map,
                                                           section_context, model_id)
            parsed_json = _apply_marks_reconciliation(parsed_json, section_map)
            parsed_json = _finalize(parsed_json)

        elif ext == '.pdf':
            page_texts = pp.pdf_page_texts(file_path)
            raw_text = "\n".join(page_texts)
            if not raw_text.strip():
                print("ERROR: No text extracted from file.")
                sys.exit(1)
            model_id = os.environ.get("KEY_PARSER_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
            # Computed FIRST, from the whole document, so numbering context (below), the
            # completeness check, and marks reconciliation all reuse this SAME map -- one extra call
            # total, regardless of how many things consult it.
            section_map = _get_section_map(raw_text, model_id)
            section_context = pp.format_section_context(section_map)
            if pp.parallel_enabled() and len([t for t in page_texts if t.strip()]) > 1:
                parsed_json = parse_qp_parallel(page_texts, section_context)
            else:
                parsed_json = parse_question_paper_with_gemini(raw_text, section_context)
            # Runs for BOTH the parallel and single-call PDF paths: a question can go missing/get
            # truncated from a single long call just as easily as from a page-local one, so both get
            # the same net.
            parsed_json = verify_and_backfill_completeness(parsed_json, raw_text, section_map,
                                                           section_context, model_id)
            parsed_json = _apply_marks_reconciliation(parsed_json, section_map)
            parsed_json = _finalize(parsed_json)

        else:
            print(f"ERROR: Unsupported file extension {ext}")
            sys.exit(1)

        print(json.dumps(parsed_json, indent=2))

    except Exception as e:
        print(f"ERROR: {str(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()