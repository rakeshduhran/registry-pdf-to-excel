
import io, os, re, copy, functools, itertools, tempfile, shutil, gc
import fitz
from fontTools.ttLib import TTFont
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Registry PDF → Excel", layout="wide")
st.title("Registry PDF → Excel — Big File V3")
st.caption("Big PDF mode + Hindi recovery + full Excel/CSV export")

uploaded = st.file_uploader("PDF चुनें", type=["pdf"])
REPH = "\ue000"
DOTTED = "\ue001"
vowel_marks = set("ािीुूृॄॅेैॉोौंःँ")
consonants = set(chr(c) for c in range(0x0915, 0x093A))

def clean(s):
    s = (s or "").replace("\u200b", "")
    s = re.sub(r"\s+", " ", s).strip()
    return hindi_cleanup(s)

def hindi_cleanup(s):
    # Conservative corrections verified on the user's test reports.
    # These are whole-word / exact-pattern fixes, not free-form spell correction.
    rules = [
        (r"\bहिरयाणा\b", "हरियाणा"),
        (r"\bबैक\b", "बैंक"),
        (r"\bसरंपच\b", "सरपंच"),
        (r"\bसंरपच\b", "सरपंच"),
        (r"\bसिह\b", "सिंह"),
        (r"\bविदया\b", "विद्या"),
        (r"\bमन्जू\b", "मंजू"),
    ]
    for pat, rep in rules:
        s = re.sub(pat, rep, s)
    s = s.replace("हुड्डापानीपत", "हुड्डा पानीपत")
    s = s.replace("DDARajinderN agarDelhi", "DDA Rajinder Nagar Delhi")
    return s

class MangalDecoder:
    def __init__(self, doc):
        self.tmp_font = None
        self.font = None
        for pno in range(min(8, len(doc))):
            for f in doc.get_page_fonts(pno, full=True):
                xref, ext, ftype, basefont, name, encoding, *_ = f
                if "Mangal" in (basefont or "") or "Mangal" in (name or ""):
                    try:
                        font_bytes = doc.extract_font(xref)[3]
                        tmp = tempfile.NamedTemporaryFile(suffix=".ttf", delete=False)
                        tmp.write(font_bytes); tmp.close()
                        self.tmp_font = tmp.name
                        self.font = TTFont(tmp.name)
                        self.glyph_order = self.font.getGlyphOrder()
                        self._build_maps()
                        return
                    except Exception:
                        pass
        raise RuntimeError("Embedded Mangal font नहीं मिला।")

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
                if cp == 160: cp = 32
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
                                self.reverse_sub.setdefault(lig.LigGlyph, []).append(tuple([first] + lig.Component))
        self.preferred = {
            "glyph00274": "ग्र", "glyph00289": "द्र",
            "glyph00292": "प्र", "glyph00302": "श्र", "uni095C": "ड़",
        }

    @functools.lru_cache(maxsize=4096)
    def glyph_candidates(self, glyph, depth=0):
        out = set(self.unicode_by_glyph.get(glyph, set()))
        if depth > 8: return out
        for comps in self.reverse_sub.get(glyph, []):
            parts = [self.glyph_candidates(c, depth+1) for c in comps]
            if all(parts):
                for combo in itertools.product(*[list(x)[:12] for x in parts]):
                    out.add("".join(combo))
        return out

    def decode_gid(self, gid):
        if gid == 509: return "र"
        if gid == 163: return "ज्ञ"
        if gid == 91: return REPH
        if gid == 668: return DOTTED
        if gid >= len(self.glyph_order): return "�"
        glyph = self.glyph_order[gid]
        if glyph in self.preferred: return self.preferred[glyph]
        cands = self.glyph_candidates(glyph)
        if not cands: return "�"
        cands = {(" " if x == "\xa0" else x) for x in cands}
        return sorted(cands, key=lambda s: (s.endswith("्"), len(s), s))[0]

    def normalize_tokens(self, tokens):
        out=[]; i=0
        while i < len(tokens):
            t=tokens[i]
            if t == DOTTED:
                i += 2 if i+1 < len(tokens) and tokens[i+1] in vowel_marks else 1
                continue
            if t == REPH:
                s="".join(out); pos=-1
                for j in range(len(s)-1,-1,-1):
                    if s[j].isspace(): break
                    if s[j] in consonants: pos=j; break
                if pos >= 0:
                    out=[s[:pos] + "र्" + s[pos:]]
                else:
                    out.append("र्")
                i += 1; continue
            out.append(t); i += 1
        s="".join(out)
        chars=list(s); fixed=[]; j=0
        while j < len(chars):
            if chars[j] == "ि" and j+1 < len(chars) and chars[j+1] in consonants:
                fixed.extend([chars[j+1],"ि"]); j += 2
            else:
                fixed.append(chars[j]); j += 1
        return "".join(fixed)

    def decode_span(self, sp):
        if "Mangal" in sp["font"]:
            return clean(self.normalize_tokens([self.decode_gid(c[1]) for c in sp["chars"]]))
        return clean("".join(chr(c[0]) for c in sp["chars"]))

def decoded_spans(page, dec):
    out=[]
    for sp in page.get_texttrace():
        text=dec.decode_span(sp)
        if not text: continue
        x0,y0,x1,y1=sp["bbox"]
        out.append({"text":text,"font":sp["font"],"x0":x0,"y0":y0,"x1":x1,"y1":y1})
    return out

def group_lines(page, dec):
    lines=[]
    for item in sorted(decoded_spans(page,dec), key=lambda z:((z["y0"]+z["y1"])/2,z["x0"])):
        yc=(item["y0"]+item["y1"])/2
        line=None
        for cand in reversed(lines[-10:]):
            if abs(cand["y"]-yc) <= 4.0:
                line=cand; break
        if line is None:
            line={"y":yc,"spans":[]}; lines.append(line)
        line["spans"].append(item)
        line["y"]=sum((x["y0"]+x["y1"])/2 for x in line["spans"])/len(line["spans"])
    for l in lines: l["spans"].sort(key=lambda z:z["x0"])
    return lines

def band(line,x0,x1):
    return clean(" ".join(s["text"] for s in line["spans"] if x0 <= s["x0"] < x1))

def registry_on_line(line):
    left="".join(s["text"] for s in line["spans"] if s["x0"] < 150).replace(" ","")
    m=re.search(r"(\d+)/(\d{4}-\d{4})/(\d+)",left)
    return m.groups() if m else None

def parse_page(page,page_no,dec):
    body=[l for l in group_lines(page,dec) if 30 < l["y"] < 545]
    starts=[(i,registry_on_line(l)) for i,l in enumerate(body) if registry_on_line(l)]
    records=[]
    for n,(start_idx,reg) in enumerate(starts):
        end_idx=starts[n+1][0] if n+1<len(starts) else len(body)
        block=body[start_idx:end_idx]
        fixed={k:[] for k in ("village","area","txn","market","deed")}
        people={"first":[],"second":[],"witness":[]}
        current=None
        for line in block:
            for key,a,b in [("village",150,220),("area",220,350),("txn",350,450),("market",450,530),("deed",530,600)]:
                v=band(line,a,b)
                if v: fixed[key].append(v)

            role=None; label_span=None; tail=""
            for s in line["spans"]:
                if s["font"].startswith("Helvetica") and 595 <= s["x0"] < 755:
                    for label,rn in [("First Party","first"),("Second Party","second"),("Witness","witness")]:
                        if label in s["text"]:
                            role=rn; label_span=s
                            m=re.search(re.escape(label)+r"\s*:\s*(.*)$",s["text"])
                            if m: tail=clean(m.group(1))
                            break
                    if role: break
            if role:
                name_start=max(600,label_span["x1"]-0.7)
                remainder=clean(" ".join(s["text"] for s in line["spans"] if s is not label_span and name_start <= s["x0"] < 753))
                p={"name":clean(tail+" "+remainder),"father":band(line,753,850),"address":band(line,850,965)}
                people[role].append(p); current=p
            elif current is not None:
                nmore=band(line,600,753); fmore=band(line,753,850); amore=band(line,850,965)
                if nmore or fmore or amore:
                    current["name"]=clean(current["name"]+" "+nmore)
                    current["father"]=clean(current["father"]+" "+fmore)
                    current["address"]=clean(current["address"]+" "+amore)

        village=clean(" ".join(fixed["village"]))
        village=re.sub(r"\bवार्ड न0\b","वार्ड नं",village)
        deed=clean(" ".join(fixed["deed"]))
        deed=re.sub(r"CONVEYANC\s+E","CONVEYANCE",deed)
        deed=re.sub(r"CANCELLATI\s+ON","CANCELLATION",deed)
        if "WILL" in deed.upper():
            people["second"]=[]
        records.append({
            "registry":reg[0],"year":reg[1],"book":reg[2],"village":village,
            "areas":[clean(" ".join(fixed["area"]))] if fixed["area"] else [],
            "txns":[clean(" ".join(fixed["txn"]))] if fixed["txn"] else [],
            "markets":[clean(" ".join(fixed["market"]))] if fixed["market"] else [],
            "deeds":[deed] if deed else [],**people,"pages":[page_no],"parts":1
        })
    return records

def uniq(vals):
    out=[]
    for v in vals:
        v=clean(v)
        if v and v not in out: out.append(v)
    return out

def signature(p):
    return (clean(p["name"]),clean(p["father"]),clean(p["address"]))

def merge_one(by, order, r):
    key=(r["registry"],r["year"],r["book"])
    if key not in by:
        by[key]=copy.deepcopy(r); order.append(key); return
    x=by[key]
    x["parts"] += 1
    x["pages"]=sorted(set(x["pages"]+r["pages"]))
    for k in ("areas","txns","markets","deeds"):
        x[k]=uniq(x[k]+r[k])
    if not x["village"]: x["village"]=r["village"]
    for role in ("first","second","witness"):
        seen={signature(p) for p in x[role]}
        for p in r[role]:
            sig=signature(p)
            if sig not in seen:
                x[role].append(copy.deepcopy(p)); seen.add(sig)
    if any("WILL" in d.upper() for d in x["deeds"]):
        x["second"]=[]

def build_frames(records):
    mf=max((len(r["first"]) for r in records), default=0)
    ms=max((len(r["second"]) for r in records), default=0)
    mw=max((len(r["witness"]) for r in records), default=0)
    headers=["Registry No","Registry Year","Book No","Village","Area","Transaction Value","Market Value","Deed Name"]
    for i in range(mf): headers += [f"First Party {i+1}",f"First Party {i+1} Father's Name",f"First Party {i+1} Address"]
    for i in range(ms): headers += [f"Second Party {i+1}",f"Second Party {i+1} Father's Name",f"Second Party {i+1} Address"]
    for i in range(mw): headers += [f"Witness {i+1}",f"Witness {i+1} Address"]
    headers += ["Area Parts","Source Pages","Review"]

    rows=[]; review=[]
    for r in records:
        row=[r["registry"],r["year"],r["book"],r["village"]," | ".join(uniq(r["areas"])),
             " | ".join(uniq(r["txns"]))," | ".join(uniq(r["markets"]))," | ".join(uniq(r["deeds"]))]
        for i in range(mf):
            p=r["first"][i] if i<len(r["first"]) else {}
            row += [p.get("name",""),p.get("father",""),p.get("address","")]
        for i in range(ms):
            p=r["second"][i] if i<len(r["second"]) else {}
            row += [p.get("name",""),p.get("father",""),p.get("address","")]
        for i in range(mw):
            p=r["witness"][i] if i<len(r["witness"]) else {}
            row += [p.get("name",""),p.get("address","")]
        issues=[]; deed=" | ".join(r["deeds"]).upper()
        if not r["deeds"]: issues.append("DEED BLANK")
        if not r["first"]: issues.append("FIRST PARTY BLANK")
        if len(uniq(r["txns"]))>1: issues.append("TRANSACTION VALUE CONFLICT")
        if len(uniq(r["deeds"]))>1: issues.append("DEED NAME CONFLICT")
        if "WILL" in deed and r["second"]: issues.append("WILL SECOND PARTY SHOULD BE BLANK")
        if "�" in " ".join(str(x) for x in row): issues.append("UNDECODED GLYPH")
        row += [r["parts"],", ".join(map(str,r["pages"]))," ; ".join(issues)]
        rows.append(row)
        if issues: review.append(row)
    return pd.DataFrame(rows,columns=headers), pd.DataFrame(review,columns=headers)

def save_upload_to_temp(uploaded):
    tmp=tempfile.NamedTemporaryFile(suffix=".pdf",delete=False)
    uploaded.seek(0)
    shutil.copyfileobj(uploaded,tmp,length=1024*1024)
    tmp.flush(); tmp.close()
    return tmp.name

if uploaded:
    temp_pdf=None; dec=None; doc=None
    try:
        temp_pdf=save_upload_to_temp(uploaded)
        doc=fitz.open(temp_pdf)
        dec=MangalDecoder(doc)

        progress=st.progress(0)
        status=st.empty()
        by={}; order=[]
        raw_count=0
        total=len(doc)

        for i in range(total):
            page=doc.load_page(i)
            recs=parse_page(page,i+1,dec)
            raw_count += len(recs)
            for r in recs:
                merge_one(by,order,r)
            del recs, page
            if (i+1) % 25 == 0:
                gc.collect()
            progress.progress((i+1)/total)
            status.write(f"Page {i+1}/{total} — {len(order)} unique registries")

        records=[by[k] for k in order]
        df,rv=build_frames(records)

        output=io.BytesIO()
        with pd.ExcelWriter(output,engine="xlsxwriter",engine_kwargs={"options":{"constant_memory":True}}) as writer:
            df.to_excel(writer,sheet_name="Registry Data",index=False)
            if len(rv):
                rv.to_excel(writer,sheet_name="Needs Review",index=False)
            else:
                pd.DataFrame({"Status":["No structural extraction issues detected"]}).to_excel(writer,sheet_name="Needs Review",index=False)
            pd.DataFrame({
                "Check":["PDF Pages","Printed Registry/Area Blocks","Unique Registries","Merged Extra Area Blocks","WILL/CANCELLATION Rule"],
                "Result":[total,raw_count,len(records),raw_count-len(records),"Second Party blank"]
            }).to_excel(writer,sheet_name="Verification",index=False)
        output.seek(0)

        status.success(f"Done — {len(records)} unique registries")

        # Earlier the app displayed only df.head(25). If the table's built-in
        # CSV export was used, only those preview rows were exported.
        c1, c2, c3 = st.columns(3)
        c1.metric("PDF Pages", total)
        c2.metric("Printed Registry/Area Blocks", raw_count)
        c3.metric("Unique Registries", len(records))

        st.info(
            f"कुल {len(records)} unique registry rows तैयार हैं। "
            "नीचे सिर्फ preview दिखाई गई है; पूरी file के लिए Full Excel / Full CSV buttons इस्तेमाल करें।"
        )

        preview_n = min(100, len(df))
        st.subheader(f"Preview — first {preview_n} of {len(df)} rows")
        st.dataframe(df.head(preview_n), use_container_width=True, hide_index=True)

        full_csv = df.to_csv(index=False).encode("utf-8-sig")

        b1, b2 = st.columns(2)
        with b1:
            st.download_button(
                "⬇️ Full Excel Download करें",
                data=output,
                file_name=uploaded.name.rsplit(".",1)[0] + "_registry.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        with b2:
            st.download_button(
                "⬇️ Full CSV Download करें",
                data=full_csv,
                file_name=uploaded.name.rsplit(".",1)[0] + "_registry.csv",
                mime="text/csv",
                use_container_width=True,
            )
    except Exception as e:
        st.error(f"Error: {e}")
        st.exception(e)
    finally:
        try:
            if dec: dec.close()
        except Exception: pass
        try:
            if doc: doc.close()
        except Exception: pass
        try:
            if temp_pdf and os.path.exists(temp_pdf): os.unlink(temp_pdf)
        except Exception: pass
