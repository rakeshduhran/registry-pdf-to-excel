Registry PDF → Excel — V6 Verified
Major fixes:
Registry detection is independent of Village/Area columns.
Registry starts come from the raw ASCII token 1234/2025-2026/1.
Separate embedded font decoders are used for Mangal and Mangal,Bold.
Uses the tested Panipat Index Report geometry (scaled by page width).
20-page self-test runs before a large PDF is processed.
Final export is blocked if any Registry/Area block is lost.
Multiple Area rows merge into one Registry/Year/Book row.
WILL and CANCELLATION OF WILL => Second Party blank.
Full Excel + Full CSV; preview export is not the full data.
Normal openpyxl export to avoid constant-memory cell loss.
