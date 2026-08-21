# Registry PDF → Excel

Static GitHub Pages web app for Haryana-style Registry Index Report PDFs.

## What it extracts

- Registry No
- Registry Year
- Book No
- Village
- Transaction Value
- Market Value
- Deed Name
- First Party 1–4 + addresses
- Second Party 1–4 + addresses
- Witness 1–4
- Review flag

**Area is intentionally ignored.**

## Privacy

The PDF is read locally in the browser. This project has no server/database/upload endpoint.

## GitHub Pages deployment

1. Create a GitHub repository.
2. Upload `index.html`, `style.css`, and `app.js` to the repository root.
3. Open **Settings → Pages**.
4. Under **Build and deployment**, choose **Deploy from a branch**.
5. Select `main` and `/ (root)`, then Save.

## Important accuracy note

The app does not translate, spell-correct, or rewrite Hindi. It exports the text returned by the PDF text layer. Some old PDFs use custom/non-Unicode Hindi fonts; if the PDF itself exposes broken character mappings, a text extractor cannot reconstruct the visual Hindi perfectly. Such records are flagged in the `Review` column when suspicious control characters are detected.

## Libraries

- Mozilla PDF.js (`pdfjs-dist` 6.2.108)
- SheetJS CE 0.20.3
