
import os, re, copy, functools, itertools, tempfile, gc
import fitz
from fontTools.ttLib import TTFont

REPH = "\ue000"
DOTTED = "\ue001"
VOWEL_MARKS = set("ािीुूृॄॅेैॉोौंःँ")
CONSONANTS = set(chr(c) for c in range(0x0915, 0x093A))
REGEX_REGISTRY = re.compile(r"^(\d+)/(\d{4}-\d{4})/(\d+)$")

def clean(s):
    s = (s or "").replace("\u200b", "")
    s = re.sub(r"\s+", " ", s).strip()
    # Only deterministic, high-confidence shaping fixes.
    replacements = {
        "सिह": "सिंह",
        "विदया": "विद्या",
        "सरंपच": "सरपंच",
        "संरपच": "सरपंच",
        "हिरयाणा": "हरियाणा",
    }
    parts = re.split(r"(\s+|[,;/()\-])", s)
    for i, p in enumerate(parts):
        if p in replacements:
            parts[i] = replacements[p]
    s = "".join(parts)

    # Exact phrase/layout repairs verified against rendered sample pages.
    s = s.replace("सर्व हरियाणा ग्रामीण बैक", "सर्व हरियाणा ग्रामीण बैंक")
    s = s.replace("हुड्डापानीपत", "हुड्डा पानीपत")
    s = s.replace("DDARajinderN agarDelhi", "DDA Rajinder Nagar Delhi")
    return s

def suspicious_text(s):
    if not s:
        return False
    if "�" in s:
        return True
    return bool(re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", s))

class FontProgramDecoder:
    def __init__(self, font_bytes):
        tmp = tempfile.NamedTemporaryFile(suffix=".ttf", delete=False)
        tmp.write(font_bytes)
        tmp.close()
        self.tmp_path = tmp.name
        self.font = TTFont(tmp.name)
        self.glyph_order = self.font.getGlyphOrder()
        self._build_maps()

    def close(self):
        try:
            self.font.close()
        except Exception:
            pass
        try:
            if os.path.exists(self.tmp_path):
                os.unlink(self.tmp_path)
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
        # Contextual glyphs verified in the Haryana report-family.
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

        # Visual pre-base i-matra -> logical Unicode order.
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

        return clean("".join(fixed))

class MangalDecoder:
    """
    Important V6 change:
    Decode each embedded Mangal font with ITS OWN font program.
    The big PDF uses both Mangal and Mangal,Bold. Using one font program
    for both was a major source of broken Hindi.
    """
    def __init__(self, doc):
        self.decoders = {}
        seen_xrefs = set()

        for pno in range(min(50, len(doc))):
            for f in doc.get_page_fonts(pno, full=True):
                xref, ext, ftype, basefont, resource_name, encoding, *_ = f
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)

                if "Mangal" not in (basefont or "") and "Mangal" not in (resource_name or ""):
                    continue

                try:
                    font_bytes = doc.extract_font(xref)[3]
                    dec = FontProgramDecoder(font_bytes)

                    # texttrace names are normally "Mangal" or "Mangal,Bold".
                    key = (basefont or resource_name or "Mangal").split("+")[-1]
                    self.decoders[key] = dec
                except Exception:
                    continue

        if not self.decoders:
            raise RuntimeError("Embedded Mangal font नहीं मिला।")

    def close(self):
        for dec in self.decoders.values():
            dec.close()

    def _decoder_for_span(self, span_font):
        if span_font in self.decoders:
            return self.decoders[span_font]

        # Safe name matching fallback.
        for key, dec in self.decoders.items():
            if key.replace(" ", "") == span_font.replace(" ", ""):
                return dec

        # Bold/non-bold must not be silently mixed if both exist.
        if "Bold" in span_font:
            for key, dec in self.decoders.items():
                if "Bold" in key:
                    return dec
        else:
            for key, dec in self.decoders.items():
                if "Bold" not in key:
                    return dec
        return None

    def decode_span(self, sp):
        if "Mangal" not in sp["font"]:
            return clean("".join(chr(c[0]) for c in sp["chars"]))

        dec = self._decoder_for_span(sp["font"])
        if dec is None:
            return "�"

        tokens = [dec.decode_gid(c[1]) for c in sp["chars"]]
        return dec.normalize_tokens(tokens)

def decoded_spans(page, decoder):
    out = []
    for sp in page.get_texttrace():
        text = decoder.decode_span(sp)
        if not text:
            continue
        x0, y0, x1, y1 = sp["bbox"]
        out.append({
            "text": text, "font": sp["font"],
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

def scaled_layout(page):
    """
    Tested geometry for the Panipat Index Report family.
    The 5-page, 32-page and 2163-page PDFs all use the same 963.78pt layout.
    Ratios make it safe if the page width is scaled.
    """
    w = page.rect.width
    return {
        "village_start": w * (150.0 / 963.78),
        "area_start": w * (220.0 / 963.78),
        "txn_start": w * (350.0 / 963.78),
        "market_start": w * (450.0 / 963.78),
        "deed_start": w * (530.0 / 963.78),
        "party_start": w * (600.0 / 963.78),
        "father_start": w * (753.0 / 963.78),
        "address_start": w * (850.0 / 963.78),
        "end": w + 1,
    }

def band(line, x0, x1):
    return clean(" ".join(
        s["text"] for s in line["spans"]
        if x0 <= s["x0"] < x1
    ))

def raw_registry_words(page):
    """
    Registry detection is independent of Hindi and independent of columns.
    ASCII registry tokens are obtained directly from PyMuPDF words.
    """
    found = []
    for w in page.get_text("words"):
        m = REGEX_REGISTRY.match(w[4].strip())
        if m:
            yc = (w[1] + w[3]) / 2
            found.append((yc, m.groups()))
    found.sort(key=lambda x: x[0])
    return found

def parse_page(page, page_no, decoder):
    layout = scaled_layout(page)
    lines = [
        line for line in group_lines(page, decoder)
        if 28 < line["y"] < page.rect.height - 25
    ]

    raw_starts = raw_registry_words(page)
    if not raw_starts:
        return [], 0

    # Map each raw Registry token to the nearest decoded visual line.
    starts = []
    used = set()
    for reg_y, reg in raw_starts:
        best_idx = None
        best_dist = 1e9
        for idx, line in enumerate(lines):
            d = abs(line["y"] - reg_y)
            if d < best_dist:
                best_idx = idx
                best_dist = d
        if best_idx is not None and best_idx not in used and best_dist <= 8:
            starts.append((best_idx, reg))
            used.add(best_idx)

    starts.sort(key=lambda x: x[0])

    records = []
    for n, (start_idx, reg) in enumerate(starts):
        end_idx = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        block = lines[start_idx:end_idx]

        fixed = {k: [] for k in ("village", "area", "txn", "market", "deed")}
        people = {"first": [], "second": [], "witness": []}
        current = None

        for line in block:
            for key, a, b in [
                ("village", layout["village_start"], layout["area_start"]),
                ("area", layout["area_start"], layout["txn_start"]),
                ("txn", layout["txn_start"], layout["market_start"]),
                ("market", layout["market_start"], layout["deed_start"]),
                ("deed", layout["deed_start"], layout["party_start"]),
            ]:
                value = band(line, a, b)
                if value:
                    fixed[key].append(value)

            role = None
            label_span = None
            tail = ""

            for s in line["spans"]:
                if s["x0"] < layout["party_start"] - 8:
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
                            tail = clean(m.group(1))
                        break
                if role:
                    break

            if role:
                name_start = max(layout["party_start"], label_span["x1"] - 0.7)
                remainder = clean(" ".join(
                    s["text"] for s in line["spans"]
                    if s is not label_span
                    and name_start <= s["x0"] < layout["father_start"]
                ))
                person = {
                    "name": clean(tail + " " + remainder),
                    "father": band(line, layout["father_start"], layout["address_start"]),
                    "address": band(line, layout["address_start"], layout["end"]),
                }
                people[role].append(person)
                current = person
            elif current is not None:
                name_more = band(line, layout["party_start"], layout["father_start"])
                father_more = band(line, layout["father_start"], layout["address_start"])
                address_more = band(line, layout["address_start"], layout["end"])
                if name_more or father_more or address_more:
                    current["name"] = clean(current["name"] + " " + name_more)
                    current["father"] = clean(current["father"] + " " + father_more)
                    current["address"] = clean(current["address"] + " " + address_more)

        village = clean(" ".join(fixed["village"]))
        village = re.sub(r"\bवार्ड न0\b", "वार्ड नं", village)

        deed = clean(" ".join(fixed["deed"]))
        deed = re.sub(r"CONVEYANC\s+E", "CONVEYANCE", deed)
        deed = re.sub(r"CANCELLATI\s+ON", "CANCELLATION", deed)
        deed = re.sub(
            r"TRANSFER\s+OF\s+IMMOVABLE\s+PROPERTY",
            "TRANSFER OF IMMOVABLE PROPERTY",
            deed
        )

        # User-specified deed rule.
        if "WILL" in deed.upper():
            people["second"] = []

        records.append({
            "registry": reg[0], "year": reg[1], "book": reg[2],
            "village": village,
            "areas": [clean(" ".join(fixed["area"]))] if fixed["area"] else [],
            "txns": [clean(" ".join(fixed["txn"]))] if fixed["txn"] else [],
            "markets": [clean(" ".join(fixed["market"]))] if fixed["market"] else [],
            "deeds": [deed] if deed else [],
            **people,
            "pages": [page_no],
            "parts": 1,
        })

    return records, len(raw_starts)

def uniq(vals):
    out = []
    for v in vals:
        v = clean(v)
        if v and v not in out:
            out.append(v)
    return out

def signature(p):
    return (clean(p["name"]), clean(p["father"]), clean(p["address"]))

def merge_one(by, order, r):
    key = (r["registry"], r["year"], r["book"])
    if key not in by:
        by[key] = copy.deepcopy(r)
        order.append(key)
        return

    x = by[key]
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

def structural_issues(r):
    issues = []
    deed = " | ".join(r["deeds"]).upper()

    if not r["deeds"]:
        issues.append("DEED BLANK")
    if not r["first"]:
        issues.append("FIRST PARTY BLANK")
    if len(uniq(r["txns"])) > 1:
        issues.append("TRANSACTION VALUE CONFLICT")
    if len(uniq(r["deeds"])) > 1:
        issues.append("DEED NAME CONFLICT")
    if "WILL" in deed and r["second"]:
        issues.append("WILL SECOND PARTY SHOULD BE BLANK")

    all_text = [r["village"]] + r["areas"] + r["deeds"]
    for role in ("first", "second", "witness"):
        for p in r[role]:
            all_text += [p["name"], p["father"], p["address"]]

    if any(suspicious_text(x) for x in all_text):
        issues.append("HINDI / GLYPH REVIEW")

    return issues

def build_rows(records):
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
            f"First Party {i+1} Address",
        ]
    for i in range(ms):
        headers += [
            f"Second Party {i+1}",
            f"Second Party {i+1} Father's Name",
            f"Second Party {i+1} Address",
        ]
    for i in range(mw):
        headers += [f"Witness {i+1}", f"Witness {i+1} Address"]

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

        issues = structural_issues(r)
        row += [r["parts"], ", ".join(map(str, r["pages"])), " ; ".join(issues)]
        rows.append(row)

        if issues:
            review_rows.append(row)

    return headers, rows, review_rows

def self_test(doc, decoder, pages=20):
    """
    Before processing thousands of pages, verify that every ASCII Registry
    token found on the sample pages is turned into a record.
    """
    pages = min(pages, len(doc))
    expected = 0
    parsed = 0
    first_party_nonblank = 0
    deed_nonblank = 0

    for i in range(pages):
        page = doc.load_page(i)
        recs, raw_n = parse_page(page, i + 1, decoder)
        expected += raw_n
        parsed += len(recs)
        first_party_nonblank += sum(bool(r["first"]) for r in recs)
        deed_nonblank += sum(bool(r["deeds"]) for r in recs)

    ok = (
        expected > 0 and
        parsed == expected and
        first_party_nonblank >= max(1, int(parsed * 0.80)) and
        deed_nonblank >= max(1, int(parsed * 0.90))
    )

    return {
        "ok": ok,
        "pages": pages,
        "expected_blocks": expected,
        "parsed_blocks": parsed,
        "first_party_nonblank": first_party_nonblank,
        "deed_nonblank": deed_nonblank,
    }
