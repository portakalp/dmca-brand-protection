# Legal paragraphs of the notices

Each file is the fixed legal text of one template (or the ICANN citation) in one language; the
notice renders it as it is (`{{ brand }}` is the only placeholder). The AI never writes or
changes these paragraphs (architecture.md §15).

A file whose first line is `LEGAL_REVIEW_REQUIRED` has not been approved by the lawyer yet: a
notice that uses it is only ever sent as a dry-run preview; a real send stops at the
`legal_review` gate. After the lawyer's approval, delete that first line (and apply the
lawyer's changes), deploy, then press "Re-check blocked cases" on the ops Settings page
(docs/operations.md, "Dry-run and going live"). The ICANN quotations are verbatim from the 2024 amendments; the translations around
them are drafts.
