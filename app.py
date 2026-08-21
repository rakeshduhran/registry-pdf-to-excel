import io, os, re, copy, functools, itertools, tempfile, shutil, gc
import fitz
from fontTools.ttLib import TTFont
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Registry PDF → Excel", layout="wide")
st.title("Registry PDF → Excel — V5 Hindi Safe")
st.caption("PDF-native Hindi recovery + adaptive layout + conservative correction + validation")

uploaded = st.file_uploader("PDF चुनें", type=["pdf"])

REPH = "\ue000"
DOTTED = "\ue001"
VOWEL_MARKS = set("ािीुूृॄॅेैॉोौंःँ")
CONSONANTS = set(chr(c) for c in range(0x0915, 0x093A))

def clean(s):
    s = (s or "").replace("\u200b", "")
    return re.sub(r"\s+", " ", s).strip()

def suspicious_text(s):
    if not s:
        return False
    if "�" in s:
        return True
    # Controls / legacy remnants that should not survive our glyph decoder.
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", s):
        return True
    return False


# Hindi is never blindly spell-corrected.  We only repair shaping patterns
# that are deterministic in this report family, and otherwise keep the
# decoded PDF text unchanged.
def normalize_hindi_visual(s):
    s = clean(s)
    if not s:
        return s

    # Unicode normalization-like cleanup without changing proper nouns.
    s = s.replace("़", "़")
    s = re.sub(r"([क-ह])्\1्\b", r"\1", s)

    # Very high-confidence whole-word repairs observed repeatedly in the
    # same Haryana registry-index report family.
    exact_words = {
        "सिह": "सिंह",
        "विदया": "विद्या",
        "सरंपच": "सरपंच",
        "संरपच": "सरपंच",
        "हिरयाणा": "हरियाणा",
    }
    parts = re.split(r"(\s+|[,;/()\-])", s)
    for i, p in enumerate(parts):
        if p in exact_words:
            parts[i] = exact_words[p]
    return "".join(parts)

def hindi_tokens(s):
    return re.findall(r"[\u0900-\u097F]{2,}", s or "")

def edit_distance(a, b):
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b)+1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                cur[-1] + 1,
                prev[j] + 1,
                prev[j-1] + (ca != cb)
            ))
        prev = cur
    return prev[-1]

class PdfVocabulary:
    """Learns only from this PDF; never substitutes from an outside dictionary."""
    def __init__(self):
        self.counts = {}

    def add(self, text):
        for w in hindi_tokens(normalize_hindi_visual(text)):
            if not suspicious_text(w):
                self.counts[w] = self.counts.get(w, 0) + 1

    def conservative_fix(self, text):
        text = normalize_hindi_visual(text)
        if not text or suspicious_text(text):
            return text, False

        parts = re.split(r"(\s+|[,;/()\-])", text)
        changed = False

        for i, w in enumerate(parts):
            if not re.fullmatch(r"[\u0900-\u097F]{3,}", w):
                continue
            # Never alter a word already seen in the same PDF.
            if w in self.counts:
                continue

            best = None
            best_score = None
            for cand, freq in self.counts.items():
                if abs(len(cand) - len(w)) > 1:
                    continue
                d = edit_distance(w, cand)
                # Only one-edit candidates, and require repeated evidence.
                if d != 1 or freq < 3:
                    continue
                score = (d, -freq, cand)
                if best_score is None or score < best_score:
                    best_score = score
                    best = cand

            # Proper nouns are risky. Only accept a one-edit repair when
            # the candidate is strongly repeated in this exact PDF.
            if best is not None and self.counts.get(best, 0) >= 5:
                parts[i] = best
                changed = True

        return "".join(parts), changed

class MangalDecoder:
    def __init__(self, doc):
        self.tmp_font = None
        self.font = None
        self.glyph_order = []
        for pno in range(min(12, len(doc))):
            for f in doc.get_page_fonts(pno, full=True):
                xref, ext, ftype, basefont, name, encoding, *_ = f
                if "Mangal" in (basefont or "") or "Mangal" in (name or ""):
                    try:
                        font_bytes = doc.extract_font(xref)[3]
                        tmp = tempfile.NamedTemporaryFile(suffix=".ttf", delete=False)
                        tmp.write(font_bytes)
                        tmp.close()
                        self.tmp_font = tmp.name
                        self.font = TTFont(tmp.name)
                        self.glyph_order = self.font.getGlyphOrder()
                        self._build_maps()
                        return
                    except Exception:
                        pass
        raise RuntimeError("इस PDF में embedded Mangal font नहीं मिला।")

    def close(self):
        try:
            if self.font:
                self.font.close()
        except Exception:
            pass
        try:
            if self.tmp_font and os.path.exists(self.tmp_font):
                os.unlink(self.tmp_font)
        except Exception:
            pass

    def _build_maps(self):
        self.unicode_by_glyph = {}
        for table in self.font["cmap"].tables:
            for cp, glyph in table.cmap.items():
                if cp == 160:
                    cp = 32
                self.unicode_by_glyph.setdefault(glyph, set()).add(chr(cp))

        self.reverse_sub = {}
        if "GSUB" in self.font:
            for lookup in self.font["GSUB"].table.LookupList.Lookup:
                for stbl in lookup.SubTable:
                    if hasattr(stbl, "mapping"):
                        for src, dst in stbl.mapping.items():
                            self.reverse_sub.setdefault(dst, []).append((src,))
                    if hasattr(stbl, "ligatures"):
                        for first, ligs in stbl.ligatures.items():
                            for lig in ligs:
                                self.reverse_sub.setdefault(lig.LigGlyph, []).append(
                                    tuple([first] + lig.Component)
                                )

        self.preferred = {
            "glyph00274": "ग्र",
            "glyph00289": "द्र",
            "glyph00292": "प्र",
            "glyph00302": "श्र",
            "uni095C": "ड़",
        }

    @functools.lru_cache(maxsize=4096)
    def glyph_candidates(self, glyph, depth=0):
        out = set(self.unicode_by_glyph.get(glyph, set()))
        if depth > 8:
            return out
        for comps in self.reverse_sub.get(glyph, []):
            parts = [self.glyph_candidates(c, depth + 1) for c in comps]
            if all(parts):
                for combo in itertools.product(*[list(x)[:12] for x in parts]):
                    out.add("".join(combo))
        return out

    def decode_gid(self, gid):
        # Contextual glyph forms verified on this report family.
        if gid == 509:
            return "र"
        if gid == 163:
            return "ज्ञ"
        if gid == 91:
            return REPH
        if gid == 668:
            return DOTTED

        if gid >= len(self.glyph_order):
            return "�"

        glyph = self.glyph_order[gid]
        if glyph in self.preferred:
            return self.preferred[glyph]

        cands = self.glyph_candidates(glyph)
        if not cands:
            return "�"

        cands = {(" " if x == "\xa0" else x) for x in cands}
        return sorted(cands, key=lambda s: (s.endswith("्"), len(s), s))[0]

    def normalize_tokens(self, tokens):
        out = []
        i = 0

        while i < len(tokens):
            t = tokens[i]

            if t == DOTTED:
                if i + 1 < len(tokens) and tokens[i + 1] in VOWEL_MARKS:
                    i += 2
                else:
                    i += 1
                continue

            if t == REPH:
                s = "".join(out)
                pos = -1
                for j in range(len(s) - 1, -1, -1):
                    if s[j].isspace():
                        break
                    if s[j] in CONSONANTS:
                        pos = j
                        break
                if pos >= 0:
                    out = [s[:pos] + "र्" + s[pos:]]
                else:
                    out.append("र्")
                i += 1
                continue

            out.append(t)
            i += 1

        s = "".join(out)

        # Restore logical order for visual pre-base 'ि'
        chars = list(s)
        fixed = []
        j = 0
        while j < len(chars):
            if chars[j] == "ि" and j + 1 < len(chars) and chars[j + 1] in CONSONANTS:
                fixed.extend([chars[j + 1], "ि"])
                j += 2
            else:
                fixed.append(chars[j])
                j += 1

        return "".join(fixed)

    def decode_span(self, sp):
        if "Mangal" in sp["font"]:
            tokens = [self.decode_gid(c[1]) for c in sp["chars"]]
            return normalize_hindi_visual(self.normalize_tokens(tokens))
        return clean("".join(chr(c[0]) for c in sp["chars"]))

def decoded_spans(page, decoder):
    out = []
    for sp in page.get_texttrace():
        text = decoder.decode_span(sp)
        if not text:
            continue
        x0, y0, x1, y1 = sp["bbox"]
        out.append({
            "text": text,
            "font": sp["font"],
            "x0": x0, "y0": y0, "x1": x1, "y1": y1
        })
    return out

def group_lines(page, decoder):
    lines = []
    for item in sorted(
        decoded_spans(page, decoder),
        key=lambda z: ((z["y0"] + z["y1"]) / 2, z["x0"])
    ):
        yc = (item["y0"] + item["y1"]) / 2
        line = None

        for cand in reversed(lines[-10:]):
            if abs(cand["y"] - yc) <= 4.0:
                line = cand
                break

        if line is None:
            line = {"y": yc, "spans": []}
            lines.append(line)

        line["spans"].append(item)
        line["y"] = sum(
            (x["y0"] + x["y1"]) / 2 for x in line["spans"]
        ) / len(line["spans"])

    for line in lines:
        line["spans"].sort(key=lambda z: z["x0"])

    return lines

def text_in_band(line, x0, x1):
    return clean(" ".join(
        s["text"] for s in line["spans"]
        if x0 <= s["x0"] < x1
    ))

# -------------------------------------------------
# Adaptive layout detection from the printed header
# -------------------------------------------------
def detect_layout(doc):
    """
    Detect column boundaries from header labels using MIDPOINTS between
    neighboring header starts.  The old version used the label starts as
    cell boundaries, which shifted Village->Area etc. on some PDFs.
    """
    for pno in range(min(30, len(doc))):
        page = doc.load_page(pno)
        words = page.get_text("words")
        top = [w for w in words if w[1] < 140]

        # Join nearby header words on the same visual line.
        lines = {}
        for w in top:
            key = round(w[1] / 4) * 4
            lines.setdefault(key, []).append(w)

        for _, ws in lines.items():
            ws = sorted(ws, key=lambda x: x[0])
            joined = " ".join(w[4] for w in ws).lower()
            if "village" not in joined or "area" not in joined or "deed" not in joined:
                continue

            labels = {}
            for w in ws:
                t = re.sub(r"[^a-z]", "", w[4].lower())
                if "village" in t: labels["village"] = w[0]
                elif t == "area": labels["area"] = w[0]
                elif "transaction" in t: labels["txn"] = w[0]
                elif "market" in t: labels["market"] = w[0]
                elif "deed" in t: labels["deed"] = w[0]
                elif "party" in t: labels["party"] = w[0]
                elif t == "name": labels["name"] = w[0]
                elif "father" in t: labels["father"] = w[0]
                elif "address" in t: labels["address"] = w[0]

            req = ["village","area","txn","market","deed"]
            if not all(k in labels for k in req):
                continue

            width = page.rect.width
            starts = [
                ("reg", 0.0),
                ("village", labels["village"]),
                ("area", labels["area"]),
                ("txn", labels["txn"]),
                ("market", labels["market"]),
                ("deed", labels["deed"]),
                ("party", labels.get("party", labels["deed"] + width*0.08)),
            ]
            starts = sorted(starts, key=lambda x: x[1])

            # Boundaries are halfway between neighboring header starts.
            boundary = {}
            for i in range(1, len(starts)):
                left_name, left_x = starts[i-1]
                right_name, right_x = starts[i]
                boundary[right_name] = (left_x + right_x) / 2

            party_start = boundary.get("party", labels["deed"] + width*0.07)

            # Party subcolumns are detected independently if present.
            name_x = labels.get("name", party_start)
            father_x = labels.get("father", party_start + width*0.16)
            address_x = labels.get("address", party_start + width*0.27)

            # Use midpoints for subcolumn boundaries too.
            father_start = (name_x + father_x) / 2 if father_x > name_x else party_start + width*0.16
            address_start = (father_x + address_x) / 2 if address_x > father_x else party_start + width*0.27

            return {
                "width": width,
                "reg_start": 0.0,
                "village_start": boundary["village"],
                "area_start": boundary["area"],
                "txn_start": boundary["txn"],
                "market_start": boundary["market"],
                "deed_start": boundary["deed"],
                "party_start": party_start,
                "name_start": party_start,
                "father_start": father_start,
                "address_start": address_start,
                "end": width + 1,
                "source": "header-midpoint"
            }

    # Fallback is based on the known report geometry.
    width = doc[0].rect.width
    return {
        "width": width,
        "reg_start": 0.0,
        "village_start": width * 0.145,
        "area_start": width * 0.215,
        "txn_start": width * 0.345,
        "market_start": width * 0.445,
        "deed_start": width * 0.535,
        "party_start": width * 0.595,
        "name_start": width * 0.595,
        "father_start": width * 0.755,
        "address_start": width * 0.855,
        "end": width + 1,
        "source": "fallback"
    }

def registry_on_line(line, layout):
    left = "".join(
        s["text"] for s in line["spans"]
        if s["x0"] < layout["village_start"]
    ).replace(" ", "")

    m = re.search(r"(\d+)/(\d{4}-\d{4})/(\d+)", left)
    return m.groups() if m else None

def parse_page(page, page_no, decoder, layout):
    body = [line for line in group_lines(page, decoder) if 28 < line["y"] < page.rect.height - 30]

    starts = []
    for idx, line in enumerate(body):
        reg = registry_on_line(line, layout)
        if reg:
            starts.append((idx, reg))

    records = []

    for n, (start_idx, reg) in enumerate(starts):
        end_idx = starts[n + 1][0] if n + 1 < len(starts) else len(body)
        block = body[start_idx:end_idx]

        fixed = {k: [] for k in ("village", "area", "txn", "market", "deed")}
        people = {"first": [], "second": [], "witness": []}
        current_person = None

        for line in block:
            bands = [
                ("village", layout["village_start"], layout["area_start"]),
                ("area", layout["area_start"], layout["txn_start"]),
                ("txn", layout["txn_start"], layout["market_start"]),
                ("market", layout["market_start"], layout["deed_start"]),
                ("deed", layout["deed_start"], layout["party_start"]),
            ]

            for key, a, b in bands:
                value = text_in_band(line, a, b)
                if value:
                    fixed[key].append(value)

            # Detect printed labels, independent of Hindi.
            role = None
            label_span = None
            label_tail = ""

            for s in line["spans"]:
                if s["x0"] < layout["party_start"] - 10:
                    continue

                for label, role_name in [
                    ("First Party", "first"),
                    ("Second Party", "second"),
                    ("Witness", "witness"),
                ]:
                    if label in s["text"]:
                        role = role_name
                        label_span = s
                        m = re.search(re.escape(label) + r"\s*:\s*(.*)$", s["text"])
                        if m:
                            label_tail = clean(m.group(1))
                        break

                if role:
                    break

            if role:
                name_start = max(layout["name_start"], label_span["x1"] - 1)

                remainder = clean(" ".join(
                    s["text"] for s in line["spans"]
                    if s is not label_span
                    and name_start <= s["x0"] < layout["father_start"]
                ))

                person = {
                    "name": clean(label_tail + " " + remainder),
                    "father": text_in_band(line, layout["father_start"], layout["address_start"]),
                    "address": text_in_band(line, layout["address_start"], layout["end"]),
                }

                people[role].append(person)
                current_person = person

            elif current_person is not None:
                name_more = text_in_band(line, layout["name_start"], layout["father_start"])
                father_more = text_in_band(line, layout["father_start"], layout["address_start"])
                address_more = text_in_band(line, layout["address_start"], layout["end"])

                if name_more or father_more or address_more:
                    current_person["name"] = clean(current_person["name"] + " " + name_more)
                    current_person["father"] = clean(current_person["father"] + " " + father_more)
                    current_person["address"] = clean(current_person["address"] + " " + address_more)

        village = clean(" ".join(fixed["village"]))

        deed = clean(" ".join(fixed["deed"]))
        deed = re.sub(r"CONVEYANC\s+E", "CONVEYANCE", deed)
        deed = re.sub(r"CANCELLATI\s+ON", "CANCELLATION", deed)
        deed = re.sub(r"TRANSFER\s+OF\s+IMMOVABLE\s+PROPERTY", "TRANSFER OF IMMOVABLE PROPERTY", deed)

        # Deed-wise rule provided by the user.
        if "WILL" in deed.upper():
            people["second"] = []

        records.append({
            "registry": reg[0],
            "year": reg[1],
            "book": reg[2],
            "village": village,
            "areas": [clean(" ".join(fixed["area"]))] if fixed["area"] else [],
            "txns": [clean(" ".join(fixed["txn"]))] if fixed["txn"] else [],
            "markets": [clean(" ".join(fixed["market"]))] if fixed["market"] else [],
            "deeds": [deed] if deed else [],
            **people,
            "pages": [page_no],
            "parts": 1,
        })

    return records

def uniq(values):
    out = []
    for value in values:
        value = clean(value)
        if value and value not in out:
            out.append(value)
    return out

def signature(p):
    return (clean(p["name"]), clean(p["father"]), clean(p["address"]))

def merge_one(by_key, order, r):
    key = (r["registry"], r["year"], r["book"])

    if key not in by_key:
        by_key[key] = copy.deepcopy(r)
        order.append(key)
        return

    x = by_key[key]
    x["parts"] += 1
    x["pages"] = sorted(set(x["pages"] + r["pages"]))

    for k in ("areas", "txns", "markets", "deeds"):
        x[k] = uniq(x[k] + r[k])

    if not x["village"]:
        x["village"] = r["village"]

    for role in ("first", "second", "witness"):
        seen = {signature(p) for p in x[role]}
        for p in r[role]:
            sig = signature(p)
            if sig not in seen:
                x[role].append(copy.deepcopy(p))
                seen.add(sig)

    if any("WILL" in d.upper() for d in x["deeds"]):
        x["second"] = []

def build_frames(records):
    mf = max((len(r["first"]) for r in records), default=0)
    ms = max((len(r["second"]) for r in records), default=0)
    mw = max((len(r["witness"]) for r in records), default=0)

    headers = [
        "Registry No", "Registry Year", "Book No", "Village", "Area",
        "Transaction Value", "Market Value", "Deed Name"
    ]

    for i in range(mf):
        headers += [
            f"First Party {i+1}",
            f"First Party {i+1} Father's Name",
            f"First Party {i+1} Address"
        ]

    for i in range(ms):
        headers += [
            f"Second Party {i+1}",
            f"Second Party {i+1} Father's Name",
            f"Second Party {i+1} Address"
        ]

    for i in range(mw):
        headers += [
            f"Witness {i+1}",
            f"Witness {i+1} Address"
        ]

    headers += ["Area Parts", "Source Pages", "Review"]

    rows = []
    review_rows = []

    for r in records:
        row = [
            r["registry"], r["year"], r["book"], r["village"],
            " | ".join(uniq(r["areas"])),
            " | ".join(uniq(r["txns"])),
            " | ".join(uniq(r["markets"])),
            " | ".join(uniq(r["deeds"])),
        ]

        for i in range(mf):
            p = r["first"][i] if i < len(r["first"]) else {}
            row += [p.get("name", ""), p.get("father", ""), p.get("address", "")]

        for i in range(ms):
            p = r["second"][i] if i < len(r["second"]) else {}
            row += [p.get("name", ""), p.get("father", ""), p.get("address", "")]

        for i in range(mw):
            p = r["witness"][i] if i < len(r["witness"]) else {}
            row += [p.get("name", ""), p.get("address", "")]

        issues = []
        deed_text = " | ".join(r["deeds"]).upper()

        if not r["deeds"]:
            issues.append("DEED BLANK")
        if not r["first"]:
            issues.append("FIRST PARTY BLANK")
        if len(uniq(r["txns"])) > 1:
            issues.append("TRANSACTION VALUE CONFLICT")
        if len(uniq(r["deeds"])) > 1:
            issues.append("DEED NAME CONFLICT")
        if "WILL" in deed_text and r["second"]:
            issues.append("WILL SECOND PARTY SHOULD BE BLANK")

        check_text = " ".join(str(x) for x in row)
        if suspicious_text(check_text):
            issues.append("HINDI / GLYPH REVIEW")

        row += [
            r["parts"],
            ", ".join(map(str, r["pages"])),
            " ; ".join(issues),
        ]

        rows.append(row)

        if issues:
            review_rows.append(row)

    return (
        pd.DataFrame(rows, columns=headers),
        pd.DataFrame(review_rows, columns=headers)
    )

def save_upload_to_temp(uploaded):
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    uploaded.seek(0)
    shutil.copyfileobj(uploaded, tmp, length=1024 * 1024)
    tmp.flush()
    tmp.close()
    return tmp.name

if uploaded:
    temp_pdf = None
    doc = None
    decoder = None

    try:
        temp_pdf = save_upload_to_temp(uploaded)
        doc = fitz.open(temp_pdf)

        layout = detect_layout(doc)
        decoder = MangalDecoder(doc)

        # Build a vocabulary from THIS PDF only. This is a light first pass
        # over decoded spans, not OCR and not an outside Hindi dictionary.
        vocab = PdfVocabulary()
        vocab_status = st.empty()
        sample_pages = min(len(doc), 250)
        step = max(1, len(doc) // sample_pages)
        scanned = 0
        for vpno in range(0, len(doc), step):
            vpage = doc.load_page(vpno)
            for item in decoded_spans(vpage, decoder):
                vocab.add(item["text"])
            scanned += 1
            del vpage
            if scanned >= sample_pages:
                break
        vocab_status.write(f"PDF Hindi vocabulary: {len(vocab.counts)} repeated words learned")

        st.write(
            f"Layout detection: **{layout['source']}** | "
            f"PDF pages: **{len(doc)}**"
        )

        progress = st.progress(0)
        status = st.empty()

        by_key = {}
        order = []
        raw_count = 0
        total = len(doc)

        for i in range(total):
            page = doc.load_page(i)
            records = parse_page(page, i + 1, decoder, layout)

            # Conservative correction using words learned from this same PDF.
            for r in records:
                r["village"], _ = vocab.conservative_fix(r["village"])
                r["areas"] = [normalize_hindi_visual(x) for x in r["areas"]]
                r["deeds"] = [normalize_hindi_visual(x) for x in r["deeds"]]
                for role in ("first", "second", "witness"):
                    for p in r[role]:
                        p["name"], _ = vocab.conservative_fix(p["name"])
                        p["father"], _ = vocab.conservative_fix(p["father"])
                        p["address"], _ = vocab.conservative_fix(p["address"])

            raw_count += len(records)

            for r in records:
                merge_one(by_key, order, r)

            del records, page

            if (i + 1) % 25 == 0:
                gc.collect()

            progress.progress((i + 1) / total)
            status.write(
                f"Page {i+1}/{total} — "
                f"{len(order)} unique registries — "
                f"{raw_count} printed blocks"
            )

        merged = [by_key[k] for k in order]
        df, review_df = build_frames(merged)

        # No constant_memory mode: it caused incomplete Excel cells in the previous version.
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Registry Data", index=False)

            if len(review_df):
                review_df.to_excel(writer, sheet_name="Needs Review", index=False)
            else:
                pd.DataFrame({
                    "Status": ["No structural extraction issues detected"]
                }).to_excel(writer, sheet_name="Needs Review", index=False)

            pd.DataFrame({
                "Check": [
                    "PDF Pages",
                    "Printed Registry/Area Blocks",
                    "Unique Registry/Year/Book Records",
                    "Merged Extra Area Blocks",
                    "Layout Detection",
                    "WILL/CANCELLATION OF WILL Rule",
                    "Hindi Correction Policy",
                ],
                "Result": [
                    total,
                    raw_count,
                    len(merged),
                    raw_count - len(merged),
                    layout["source"],
                    "Second Party blank",
                    "PDF-native; only repeated one-edit words auto-corrected",
                ],
            }).to_excel(writer, sheet_name="Verification", index=False)

        output.seek(0)

        full_csv = df.to_csv(index=False).encode("utf-8-sig")

        status.success(
            f"Done — {len(merged)} unique registries, "
            f"{raw_count} printed blocks"
        )

        c1, c2, c3 = st.columns(3)
        c1.metric("PDF Pages", total)
        c2.metric("Printed Blocks", raw_count)
        c3.metric("Unique Registries", len(merged))

        preview_n = min(100, len(df))
        st.subheader(f"Preview — first {preview_n} of {len(df)} rows")
        st.dataframe(df.head(preview_n), use_container_width=True, hide_index=True)

        b1, b2 = st.columns(2)

        with b1:
            st.download_button(
                "⬇️ Full Excel Download करें",
                data=output,
                file_name=uploaded.name.rsplit(".", 1)[0] + "_registry.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

        with b2:
            st.download_button(
                "⬇️ Full CSV Download करें",
                data=full_csv,
                file_name=uploaded.name.rsplit(".", 1)[0] + "_registry.csv",
                mime="text/csv",
                use_container_width=True,
            )

    except Exception as e:
        st.error(f"Error: {e}")
        st.exception(e)

    finally:
        try:
            if decoder:
                decoder.close()
        except Exception:
            pass

        try:
            if doc:
                doc.close()
        except Exception:
            pass

        try:
            if temp_pdf and os.path.exists(temp_pdf):
                os.unlink(temp_pdf)
        except Exception:
            pass

