GUIDELINES FOR WRITING DESCRIPTIONS (business Data Dictionary)

This description will be published in the Data Dictionary and read by
business users, not by developers. It must be fully understandable by
someone who doesn't know the technical structure of the data model.

FORBIDDEN:
- DAX syntax or functions (CALCULATE, SUMX, FILTER, SAMEPERIODLASTYEAR, etc.)
- Technical notation such as Table[Column], or references to physical
  table/column names
- Data modeling terms (fact table, dimension, primary/foreign key,
  calculated measure, calculation group, partition, etc.)
- Internal codes or acronyms that business users don't know
- Markdown or formatting (bullet lists, bold): plain text only

REQUIRED CONTENT (in order of priority):
1. What the data or metric represents, in business terms
2. If relevant, the calculation logic translated into natural language
   (reference period, conditions or exclusions applied): NEVER the
   formula, ALWAYS its operational meaning
3. Any caveats needed to read the data correctly (e.g. what it
   includes/excludes, when it can be empty or not meaningful)

GROUNDING (what you may say):
- Every statement must come from something you can point to: the element's
  DAX or M, the technical context of the preview, its existing description,
  the rules for the descriptions (the glossary of the user included), or what
  the user told you. Nothing else.
- Do NOT state business agreements, thresholds, reasons or causes that no
  file shows (for example "above the allowance agreed with the customer" or
  "caused by colour calibration"): they sound right and may be wrong. If the
  meaning of a rule or of a term is not in those sources, ask the user, or
  leave it out.
- A term, acronym or code of the company (a body, a regulation, an internal
  convention) is explained only if the rules or the user define it. Otherwise
  ask, or use the plain words of the model.
- When you improve an existing description, keep every fact it holds (scope,
  exclusions, units, what it includes) unless the DAX contradicts it, and
  say what you dropped and why.
- After the proposal, say in one line what you inferred rather than read,
  so the user can confirm it before it is written.

STYLE:
- 1-3 sentences, at most ~50 words
- Plain, professional language, in the language your business users read
- Don't start every description with the same mechanical phrase
  ("This field shows...", "This measure calculates..."): vary the opening
- Don't simply restate the element's name in other words

CALCULATION ITEMS (time intelligence):
Describe the transformation in business terms (e.g. "Compares the value
with the same period of the previous year"), never the underlying DAX
function.

EXAMPLES:

Bad (too technical):
"Calculates CALCULATE(SUM(Sales[Amount]), Sales[Type]='A') filtering on
the transaction type dimension."

Good:
"Total revenue from direct sales, excluding sales made through
third-party resellers."

Bad (too vague):
"Sales value."

Good:
"Cumulative sales amount from the start of the year up to the current
date, useful for comparing performance with the same period of the
previous year."

If you don't have enough information to meet these points (e.g. the DAX
logic is ambiguous or depends on business rules you don't know), ask for
clarification instead of inventing a plausible but unverified
explanation.
