# Symbol box search

The user approved the existing symbol input searching Yahoo by ticker or company name. Show up to eight suggestions with ticker, name, and exchange. Users explicitly choose a listing by mouse or keyboard. Direct ticker submission continues through the existing entry point, including when typing starts on the chart. No dependency or provider routing changes.

Debounce edits for 250 ms. Run one Yahoo lookup at a time and retain the newest pending edit. Focus loss, a new edit, symbol submission, and shutdown invalidate earlier results. Both application close checks wait for an active lookup to finish.

The integration test initially failed at the missing search UI. It now types a company name, delays an obsolete lookup, selects the intended listing, verifies direct ticker entry, and exercises shutdown with a lookup pending. A live request through the production adapter returned BABA and 9988.HK for Alibaba, with distinct exchange labels. Claude design QC covered completion activation and deferred focus selection.

Code QC flagged two Enter edge cases. The highlighted item now takes precedence, and Enter without a highlighted item still submits the typed ticker even when Yahoo suggests other results. The production path test covers both, plus mouse selection and one chart load per submission.
