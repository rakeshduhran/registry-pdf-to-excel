import * as pdfjsLib from 'https://cdn.jsdelivr.net/npm/pdfjs-dist@6.2.108/build/pdf.mjs';
pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdn.jsdelivr.net/npm/pdfjs-dist@6.2.108/build/pdf.worker.mjs';

const $ = (id) => document.getElementById(id);
const fileInput = $('pdfFile');
const startBtn = $('startBtn');
const stopBtn = $('stopBtn');
const excelBtn = $('excelBtn');
const clearBtn = $('clearBtn');
const statusEl = $('status');
const progressEl = $('progress');
const pageStat = $('pageStat');
const recordStat = $('recordStat');
const reviewStat = $('reviewStat');
const fileName = $('fileName');
const table = $('previewTable');

let selectedFile = null;
let rows = [];
let stopped = false;
let totalPages = 0;

const HEADERS = [
  'Registry No','Registry Year','Book No','Village',
  'Transaction Value','Market Value','Deed Name',
  'First Party 1','First Party 1 Address','First Party 2','First Party 2 Address',
  'First Party 3','First Party 3 Address','First Party 4','First Party 4 Address',
  'Second Party 1','Second Party 1 Address','Second Party 2','Second Party 2 Address',
  'Second Party 3','Second Party 3 Address','Second Party 4','Second Party 4 Address',
  'Witness 1','Witness 2','Witness 3','Witness 4','Review'
];

fileInput.addEventListener('change', () => {
  selectedFile = fileInput.files?.[0] || null;
  fileName.textContent = selectedFile ? `${selectedFile.name} — ${(selectedFile.size/1024/1024).toFixed(2)} MB` : 'PDF चुनें';
  startBtn.disabled = !selectedFile;
});

clearBtn.addEventListener('click', resetAll);
stopBtn.addEventListener('click', () => { stopped = true; statusEl.textContent = 'Processing रोकने का अनुरोध किया गया…'; });
startBtn.addEventListener('click', processPdf);
excelBtn.addEventListener('click', downloadExcel);

function resetAll(){
  rows = []; stopped = false; totalPages = 0;
  progressEl.value = 0; pageStat.textContent = '0/0'; recordStat.textContent='0'; reviewStat.textContent='0';
  excelBtn.disabled = true; clearBtn.disabled = true;
  renderPreview();
  statusEl.textContent = selectedFile ? 'PDF तैयार है।' : 'PDF चुनें।';
}

function cleanText(s){
  return String(s ?? '')
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g,'')
    .replace(/\s+/g,' ')
    .trim();
}

function containsSuspiciousChars(s){
  // Do not repair/translate text. Only flag likely bad font extraction for manual review.
  return /[\u0080-\u009F]|�/.test(s || '');
}

function makeItem(raw){
  const t = raw.transform;
  return {
    str: cleanText(raw.str),
    x: t[4],
    y: t[5],
    w: raw.width || 0,
    h: raw.height || Math.abs(t[3] || 0)
  };
}

function isRegistryAnchor(item){
  return item.x < 170 && /^\d+\s*\/\s*\d{4}\s*-\s*\d{4}\s*\/\s*\d+$/.test(item.str);
}

function parseRegistryId(text){
  const m = text.replace(/\s/g,'').match(/^(\d+)\/(\d{4}-\d{4})\/(\d+)$/);
  return m ? {no:m[1], year:m[2], book:m[3]} : {no:'',year:'',book:''};
}

function joinByReadingOrder(items){
  return cleanText(items
    .sort((a,b) => Math.abs(b.y-a.y) > 2 ? b.y-a.y : a.x-b.x)
    .map(i=>i.str).filter(Boolean).join(' '));
}

function clusterLines(items, tolerance=3.5){
  const sorted = [...items].sort((a,b)=> b.y-a.y || a.x-b.x);
  const lines = [];
  for(const item of sorted){
    let line = lines.find(l => Math.abs(l.y-item.y) <= tolerance);
    if(!line){ line={y:item.y, items:[]}; lines.push(line); }
    line.items.push(item);
  }
  lines.sort((a,b)=>b.y-a.y);
  for(const line of lines) line.items.sort((a,b)=>a.x-b.x);
  return lines;
}

function textInX(items, minX, maxX){
  return joinByReadingOrder(items.filter(i=>i.x>=minX && i.x<maxX));
}

function pickNumericColumn(items, minX, maxX){
  const candidates = items.filter(i=>i.x>=minX && i.x<maxX && /^[-+]?\d[\d,]*(?:\.\d+)?$/.test(i.str));
  return candidates.length ? cleanText(candidates[0].str.replace(/,/g,'')) : '';
}

function parseParties(items){
  const partyItems = items.filter(i=>i.x >= 590 && i.x < 980 && i.str && !/^Page$/i.test(i.str));
  const lines = clusterLines(partyItems, 4.2);
  const first=[], second=[], witnesses=[];
  let current = null;

  for(const line of lines){
    const lineText = cleanText(line.items.map(i=>i.str).join(' '));
    const left = line.items.filter(i=>i.x < 835);
    const right = line.items.filter(i=>i.x >= 835);
    const leftText = cleanText(left.map(i=>i.str).join(' '));
    const rightText = cleanText(right.map(i=>i.str).join(' '));

    let type = null;
    if(/^First\s+Party\s*:/i.test(lineText)) type='first';
    else if(/^Second\s+Party\s*:/i.test(lineText)) type='second';
    else if(/^Witness\s*:/i.test(lineText)) type='witness';

    if(type){
      const name = cleanText(leftText.replace(/^First\s+Party\s*:\s*/i,'').replace(/^Second\s+Party\s*:\s*/i,'').replace(/^Witness\s*:\s*/i,''));
      current = {type, name, address:rightText};
      if(type==='first') first.push(current);
      if(type==='second') second.push(current);
      if(type==='witness') witnesses.push(current);
      continue;
    }

    // Wrapped lines belong to the last party/witness until another label appears.
    if(current){
      if(leftText) current.name = cleanText(`${current.name} ${leftText}`);
      if(rightText) current.address = cleanText(`${current.address} ${rightText}`);
    }
  }
  return {first,second,witnesses};
}

function parseRecord(recordItems, pageNo){
  const anchor = recordItems.find(isRegistryAnchor);
  if(!anchor) return null;
  const id = parseRegistryId(anchor.str);

  // Report columns are fixed in this family of index PDFs.
  // Area lies roughly x=215..330 and is intentionally ignored.
  const village = textInX(recordItems.filter(i=>Math.abs(i.y-anchor.y)<16), 145, 215);
  const transactionValue = pickNumericColumn(recordItems.filter(i=>Math.abs(i.y-anchor.y)<18), 330, 450);
  const marketValue = pickNumericColumn(recordItems.filter(i=>Math.abs(i.y-anchor.y)<18), 450, 525);

  // Deed names sometimes wrap, e.g. CONVEYANC / E.
  const partyY = Math.max(...recordItems.filter(i=>i.x>=590).map(i=>i.y), -Infinity);
  const deedCandidates = recordItems.filter(i=>i.x>=520 && i.x<590 && i.y <= anchor.y+5 && i.y >= anchor.y-28);
  const deedName = joinByReadingOrder(deedCandidates);
  const parties = parseParties(recordItems);

  const row = Object.fromEntries(HEADERS.map(h=>[h,'']));
  row['Registry No']=id.no; row['Registry Year']=id.year; row['Book No']=id.book;
  row['Village']=village; row['Transaction Value']=transactionValue; row['Market Value']=marketValue; row['Deed Name']=deedName;
  parties.first.slice(0,4).forEach((p,idx)=>{ row[`First Party ${idx+1}`]=p.name; row[`First Party ${idx+1} Address`]=p.address; });
  parties.second.slice(0,4).forEach((p,idx)=>{ row[`Second Party ${idx+1}`]=p.name; row[`Second Party ${idx+1} Address`]=p.address; });
  parties.witnesses.slice(0,4).forEach((p,idx)=>{ row[`Witness ${idx+1}`]=cleanText(`${p.name}${p.address?' '+p.address:''}`); });

  const allText = recordItems.map(i=>i.str).join(' ');
  const notes=[];
  if(containsSuspiciousChars(allText)) notes.push('PDF font/encoding review');
  if(parties.first.length>4) notes.push(`First Party ${parties.first.length} (only first 4 columns)`);
  if(parties.second.length>4) notes.push(`Second Party ${parties.second.length} (only first 4 columns)`);
  if(parties.witnesses.length>4) notes.push(`Witness ${parties.witnesses.length} (only first 4 columns)`);
  if(!deedName) notes.push('Deed Name blank');
  row['Review']=notes.join('; ');
  row.__page=pageNo;
  return row;
}

function splitPageIntoRecords(items, pageNo){
  const anchors = items.filter(isRegistryAnchor).sort((a,b)=>b.y-a.y);
  const result=[];
  for(let i=0;i<anchors.length;i++){
    const a=anchors[i];
    const next=anchors[i+1];
    const top=a.y+8;
    const bottom=next ? next.y+4 : 70; // exclude footer/header region on typical pages
    const recItems=items.filter(it=>it.y<=top && it.y>bottom && it.y<540);
    const rec=parseRecord(recItems,pageNo);
    if(rec) result.push(rec);
  }
  return result;
}

async function processPdf(){
  if(!selectedFile) return;
  resetAll(); stopped=false;
  startBtn.disabled=true; stopBtn.disabled=false; clearBtn.disabled=true;
  statusEl.textContent='PDF खोली जा रही है…';

  try{
    const bytes = new Uint8Array(await selectedFile.arrayBuffer());
    const task = pdfjsLib.getDocument({data:bytes, useSystemFonts:true, isEvalSupported:false});
    const pdf = await task.promise;
    totalPages=pdf.numPages;
    pageStat.textContent=`0/${totalPages}`;

    for(let p=1;p<=totalPages;p++){
      if(stopped) break;
      const page=await pdf.getPage(p);
      const content=await page.getTextContent({includeMarkedContent:false, disableNormalization:true});
      const items=content.items.filter(x=>x.str).map(makeItem);
      const pageRows=splitPageIntoRecords(items,p);
      rows.push(...pageRows);

      progressEl.value=Math.round((p/totalPages)*100);
      pageStat.textContent=`${p}/${totalPages}`;
      recordStat.textContent=String(rows.length);
      reviewStat.textContent=String(rows.filter(r=>r.Review).length);
      statusEl.textContent=`Page ${p} / ${totalPages} — ${rows.length} records मिले`;

      if(p<=5 || p%50===0) renderPreview();
      if(p%20===0) await new Promise(r=>setTimeout(r,0)); // keep UI responsive on very large PDFs
      page.cleanup();
    }

    renderPreview();
    excelBtn.disabled = rows.length===0;
    clearBtn.disabled=false;
    statusEl.textContent = stopped
      ? `Processing रोकी गई। अभी तक ${rows.length} records तैयार हैं।`
      : `पूरा हुआ: ${totalPages} pages से ${rows.length} records तैयार हैं।`;
  }catch(err){
    console.error(err);
    statusEl.textContent=`Error: ${err?.message || err}`;
  }finally{
    startBtn.disabled=false; stopBtn.disabled=true;
  }
}

function renderPreview(){
  const shown=rows.slice(0,100);
  table.tHead.innerHTML=''; table.tBodies[0].innerHTML='';
  const hr=document.createElement('tr');
  for(const h of HEADERS){ const th=document.createElement('th'); th.textContent=h; hr.appendChild(th); }
  table.tHead.appendChild(hr);
  for(const r of shown){
    const tr=document.createElement('tr'); if(r.Review) tr.classList.add('review');
    for(const h of HEADERS){ const td=document.createElement('td'); td.textContent=r[h]??''; tr.appendChild(td); }
    table.tBodies[0].appendChild(tr);
  }
}

function downloadExcel(){
  if(!rows.length || !window.XLSX) return;
  const cleanRows=rows.map(r=>Object.fromEntries(HEADERS.map(h=>[h,r[h]??''])));
  const ws=XLSX.utils.json_to_sheet(cleanRows,{header:HEADERS});
  ws['!freeze']={xSplit:0,ySplit:1};
  ws['!cols']=HEADERS.map(h=>({wch: h.includes('Party')||h.includes('Address')||h.includes('Witness')?28: h==='Review'?24:18}));
  const wb=XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb,ws,'Registry Data');
  const base=(selectedFile?.name || 'registry').replace(/\.pdf$/i,'').replace(/[\\/:*?"<>|]+/g,'_');
  XLSX.writeFile(wb,`${base}_extracted.xlsx`,{compression:true});
}
