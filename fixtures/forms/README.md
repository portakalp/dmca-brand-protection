# Form sample pages

Small pages in English, Turkish, Polish and Russian for the form signals (architecture.md §12):
login, card and IBAN deposits, phone registration, deposit buttons outside forms,
forms that post to another domain, and pages with harmless forms only. All names and domains
are fictional.

`backend/tests/integration/test_signal_forms.py` opens each page in Chromium, reads it with the
browser stage's inventory script and lists the signals each page must raise. Add a page there
when real-world pages show a pattern the YAML (`backend/src/kalkan/scanning/form_patterns.yaml`)
misses.
