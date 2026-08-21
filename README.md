# Registry PDF → Excel

This app extracts Registry No, Year, Book, Village, Area, Transaction Value, Market Value,
Deed Name, First Parties, Father's Name, Address, Second Parties, Witnesses and merges
multiple area rows belonging to the same registry.

Important:
- WILL and CANCELLATION OF WILL -> Second Party is forced blank.
- Hindi is recovered from the embedded Mangal glyph IDs, not from the broken PDF text layer.
- This parser is tuned for the same Haryana Index Report layout family as the tested PDF.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy

GitHub Pages cannot run Python. Deploy this repo on Streamlit Community Cloud, Render, Railway, or another Python host.
