# EDA findings

`data/raw_applications.csv`, 196 rows. Numbers come from [applications_eda.ipynb](applications_eda.ipynb).

**Clean:** revenue and requested amount are present and numeric in every row; repeated rows of one company agree on
all company fields.

**Not clean:**

1. **Duplicates.** 196 rows are 180 companies: 4 exact copies, plus 12 repeats under another id (`RAW-0151-DUP`) or
   another tax-ID spelling (`A-54437256` vs `A54437256`).
2. **Headcount.** 22 rows have no employees (13 of them from `legacy_crm`), 2 have zero, 1 has −3. The prototype
   crashes on 24 rows and scores −3 employees as high risk.
3. **Revenue unit.** Every high-risk result on valid rows asks for more than a year of revenue. 12 of these 13 rows
   report revenue of 1.4k–4.9k € next to requests of 54k–595k €: revenue probably sent in thousands (9 from
   `new_partner_feed`). The 13th reports 50,000 employees on 90k € revenue.
4. **Dates.** 30 incorporation dates are dd/mm/yyyy (25 from `new_partner_feed`), 10 of them ambiguous; 10 are missing.
5. **EBITDA** is missing in 101 rows and above revenue in 5. v1 does not use it.

**What this means for the model:** on the other 146 companies v1 flags none (requests at most 15% of revenue, revenue
per employee at least 51.8k). On this export every v1 high-risk flag is a data problem, and a threshold inside the
observed range changes real answers: v2-demo (12% instead of 20%) moves 41 of 146 to high risk.

**Ask the sources:** revenue unit and currency, financial period, what counts as an employee, date format, and the
application timestamp (the export has none).
