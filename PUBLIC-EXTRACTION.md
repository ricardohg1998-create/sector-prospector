# Public extraction · 13 October 2026

This repository extracts the reusable Python implementation of my sector-prospecting system. It does not publish the workspace itself.

## Changes for publication

1. Removed the private CRM project identifier. Batch creation requires an explicit project or an environment variable.
2. Replaced the internal Node/desktop XLSX exporter with a separate Python/openpyxl adapter. The 26-field contract is retained; the original workbook presentation is not reproduced exactly.
3. Removed the implicit Codex runtime path and adjusted `doctor` to report public dependencies.
4. Added an offline demonstration with fictional businesses and public CI.
5. Excluded three captured-source replay test files and two adapter replay tests that need private historical captures. The retained tests create their own data; no private fixture collection is copied.

No contacts, captured business pages, databases, state files, mailboxes, credentials, generated deliveries, logs or CRM exports are included. Source configuration JSON describes extraction rules and public sources; it is not a contact database.

The original operational version remains separate. The public version's tests must be assessed independently; results from the private project's broader suite are not assigned to this extraction.
