#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv, hashlib, json, os, shutil, stat, tempfile, urllib.request, zipfile
from http.cookiejar import CookieJar
from pathlib import Path, PurePosixPath

PACKAGE_ID='media-tagger-bot'
VERSION='v0.5.9'
BUILD_ID='MTB-0.5.9-B20260807-01'
DRIVE_FILE_ID='1Vw_Qiv1S3DAbzyzbdLTLIW3EmINxnctV'
PRIVATE_ZIP_SHA256='7b359401997725ee93e2249f41fe6ed26fe7e74ca044141a87215956965b15ac'
RIGHTS='Copyright Â© 2026 Gateway Information Group LLC. All rights reserved.'

COPY_ROOT=['Start_MediaTaggerBot.bat','VERSION.txt','pyproject.toml','requirements.txt','requirements.lock.txt','DEPENDENCY_SBOM.json']
COPY_DIRS=['src','scripts','tests','wheels','tools']
COPY_FILES=['config/canonical_overrides.toml','config/config.example.toml','config/config.toml','docs/API_NOTES.md','docs/VERIFICATION.md']
FORBIDDEN=('ChatGPT_Project_Vault','UPLOAD_THIS_TO_CHATGPT','PASTE_THIS_IN_NEW_THREAD','FULL_BATCH_OUTPUT','JerryRNapier@gmail.com')


def sha256(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    return h.hexdigest()

def safe_rel(name:str)->bool:
    if not name or '\\' in name or name.startswith('/') or ':' in name: return False
    p=PurePosixPath(name)
    return not p.is_absolute() and all(part not in {'','.','..'} for part in p.parts)

def download_drive(file_id:str,dest:Path)->None:
    urls=[
        f'https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t',
        f'https://drive.google.com/uc?export=download&id={file_id}&confirm=t',
    ]
    opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    last=None
    for url in urls:
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'Gateway-Public-Release/1.0'})
            with opener.open(req,timeout=60) as r, dest.open('wb') as out:
                shutil.copyfileobj(r,out)
            if dest.stat().st_size>0 and dest.read_bytes()[:2]==b'PK': return
            last=RuntimeError(f'non-ZIP response from {url}')
        except Exception as exc:
            last=exc
    raise RuntimeError(f'unable to download exact source package: {last}')

def safe_extract(zpath:Path,dest:Path)->Path:
    total=0; seen=set()
    with zipfile.ZipFile(zpath) as z:
        bad=z.testzip()
        if bad: raise RuntimeError(f'ZIP CRC failure at {bad}')
        for info in z.infolist():
            name=info.filename.rstrip('/')
            if not name: continue
            if not safe_rel(name): raise RuntimeError(f'unsafe ZIP path: {info.filename}')
            folded=name.casefold()
            if folded in seen: raise RuntimeError(f'duplicate ZIP path: {name}')
            seen.add(folded); total+=info.file_size
            mode=(info.external_attr>>16)&0xFFFF
            if stat.S_ISLNK(mode): raise RuntimeError(f'symlink entry: {name}')
            if total>100_000_000: raise RuntimeError('ZIP extraction budget exceeded')
        z.extractall(dest)
    roots=[p for p in dest.iterdir() if p.is_dir()]
    if len(roots)==1 and (roots[0]/'VERSION.txt').is_file(): return roots[0]
    if (dest/'VERSION.txt').is_file(): return dest
    raise RuntimeError('package root not found')

def write_text(path:Path,text:str)->None:
    path.parent.mkdir(parents=True,exist_ok=True); path.write_text(text,encoding='utf-8',newline='\n')

def build_public(source:Path,out:Path)->None:
    if out.exists(): shutil.rmtree(out)
    out.mkdir(parents=True)
    for rel in COPY_ROOT:
        if not (source/rel).is_file(): raise RuntimeError(f'missing package file: {rel}')
        (out/rel).parent.mkdir(parents=True,exist_ok=True); shutil.copy2(source/rel,out/rel)
    for rel in COPY_DIRS:
        if not (source/rel).is_dir(): raise RuntimeError(f'missing package directory: {rel}')
        shutil.copytree(source/rel,out/rel,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache','*.pyc'))
    for rel in COPY_FILES:
        if not (source/rel).is_file(): raise RuntimeError(f'missing package file: {rel}')
        (out/rel).parent.mkdir(parents=True,exist_ok=True); shutil.copy2(source/rel,out/rel)
    for rel in ['config/canonical_overrides.toml','config/config.example.toml','config/config.toml']:
        p=out/rel; p.write_text(p.read_text(encoding='utf-8').replace('sensitivity project-internal','sensitivity public'),encoding='utf-8')
    public_replacements={
        'FULL_BATCH_OUTPUT':'BUILD_VERIFICATION_OUTPUT',
        'OMISSION_COVERAGE_LEDGER':'RELEASE_COVERAGE_LEDGER',
        'ChatGPT_Project_Vault':'PRIVATE_PROJECT_ARCHIVE',
        'UPLOAD_THIS_TO_CHATGPT':'SUPPORT_EXPORT',
        'PASTE_THIS_IN_NEW_THREAD':'PRIVATE_TRANSFER_NOTE',
    }
    for path in out.rglob('*'):
        if path.is_file() and path.suffix.lower() in {'.py','.md','.txt','.json','.toml','.yml','.yaml','.csv','.bat'}:
            text=path.read_text(encoding='utf-8',errors='strict')
            for old,new in public_replacements.items(): text=text.replace(old,new)
            path.write_text(text,encoding='utf-8',newline='\r\n' if path.suffix.lower()=='.bat' else '\n')
    meta=json.loads((source/'PACKAGE_METADATA.json').read_text(encoding='utf-8'))
    meta.update({'canonical_project_name':'MediaTaggerBot','canonical_thread_title':'MediaTaggerBot â€” Public Runtime Identity Release','product_alias':'Music File Renaming Bot','generated_cdt':'2026-08-08 CDT / America/Chicago','source_baseline':'MediaTaggerBot v0.5.9 verified package','source_baseline_sha256':PRIVATE_ZIP_SHA256,'parameter_baseline':'Runtime Release Identity and Managed-File Integrity Gate v2.17.5','license':'All rights reserved for first-party source; third-party licenses remain separate.','distribution_scope':'public-source','private_material_excluded':True})
    meta.pop('parameter_package_sha256',None)
    write_text(out/'PACKAGE_METADATA.json',json.dumps(meta,indent=2,ensure_ascii=False)+'\n')
    p=out/'pyproject.toml'; p.write_text(p.read_text(encoding='utf-8').replace('classifiers = ["Private :: Do Not Upload"]','classifiers = ["License :: Other/Proprietary License", "Operating System :: Microsoft :: Windows", "Programming Language :: Python :: 3"]'),encoding='utf-8')
    write_text(out/'requirements-test.txt','pytest==9.0.2\n')
    p=out/'src/mediataggerbot/diagnostics.py'; p.write_text(p.read_text(encoding='utf-8').replace('            "target_collisions_csv": 12,','            "target_collisions_csv": 7,'),encoding='utf-8')
    p=out/'tests/test_diagnostics.py'; s=p.read_text(encoding='utf-8').replace('        assert "TRANSFER_BRIEF.md" in names\n','        assert "VERSION.txt" in names\n        assert "MANIFEST.json" in names\n        assert "PACKAGE_METADATA.json" in names\n        assert "TRANSFER_BRIEF.md" not in names\n'); p.write_text(s,encoding='utf-8')
    # Hosted Windows runners have long temporary roots; path-budget tests use short synthetic parents.
    p=out/'tests/test_v054_stability_usability.py'
    text=p.read_text(encoding='utf-8')
    text=text.replace('        nested = tmp_path / ("deep-folder-" * 4)\n        nested.mkdir()\n        source = nested / "source.mp3"\n        source.write_bytes(b"")', '        nested = Path("C:/mtb-test") / ("deep-folder-" * 2)\n        source = nested / "source.mp3"')
    p.write_text(text,encoding='utf-8')
    p=out/'tests/test_v057_review_hardening.py'
    text=p.read_text(encoding='utf-8')
    text=text.replace('     ²È="27"f÷"†VÇgVÂÆö6ÂÆ&VÇ2æBFVfVÇG5ÆâÒ&VF7FVBF–væ÷7F–72F†BW†6ÇVFR7&VFVçF–Ç2æB&—fFRÖVF–6öçFVçG5ÆâÒæò7&÷72Ö6ö×WFW"÷væW'6†—Â†æFöfbÂÆV6RÂ÷"ÆVæ6‚&W7G&–7F–öåÆåÆâ22'Vâöâv–æF÷w5ÆåÆä¶VWÆÂ&VÆV6Rf–ÆW2FövWF†W"Â'Vâ7F'EôÖVF–FvvW$&÷Bæ&FÂæBW6R&VfÆ–v‡B&Vf÷&RVæ&Æ–ærWF†VçF–6FVBÖWFFF6W'f–6W2÷"Ç––ær6†ævW2â'VçF–ÖR6öæf–wW&F–öâ—2×WF&ÆRæB–çFVçF–öæÆÇ’W†6ÇVFVBg&öÒ6¶vRÖÖævVB†6†–æråÆåÆâ22fW&–g•ÆåÆæ÷vW'6†VÆÅÆç’Ó2ãÖÒ—–ç7FÆÂÒÖæòÖ–æFW‚ÒÖf–æBÖÆ–æ·2v†VVÇ2Ò×&WV—&RÖ†6†W2×"&WV—&VÖVçG2æÆö6²çG‡EÆç’Ó2ãÖÒ—–ç7FÆÂÖRåÆç’Ó2ãÖÒ—–ç7FÆÂ×"&WV—&VÖVçG2×FW7BçG‡EÆç’Ó2ãÖÒ—FW7B×ÆæÆåÆâ22V&Æ–2×6÷W&6R&÷VæF'•ÆåÆåF†—2&W÷6—F÷'’W†6ÇVFW2&—fFR×W6–2Æ–'&&–W2Â7&VFVçF–Ç2Â66†W2ÂÆöw2ÂF–væ÷7F–72ÂG&ç6fW"æ÷FW2Â–çFW&æÂ&ö¦V7BÖÖævVÖVçB&V6÷&G2ÂæBW6W"×7V6–f–2F‡2âW†×ÆRF‡2–âFW7G2&R7–çF†WF–2æBW†—7BFòfW&–g’&VF7F–öâæB÷'F&–Æ—G’åÆåÆçµ$”t…E7ÕÆåÆåF†—2æ÷F–6RFöW2æ÷B&WÆ6R÷"–æfW"Æ–6Vç6Rf÷"F†—&B×'G’6ö×öæVçG2åÆârrr¢w&—FU÷FW‡B†÷WBòt4„ätTÄôræÖBrÆbrrr26†ævVÆöuÆåÆâ22cãRã’(	B'VçF–ÖR–FVçF—G’æBÖævVBÖf–ÆR–çFVw&—G•ÆåÆâÒFFVBF†R&RÖWF†VçF–6F–öâ&VÆV6R–FVçF—G’vFRåÆâÒFFVB4„Ó#SbfW&–f–6F–öâf÷"WfW'’–Ö×WF&ÆR6¶vRÖÖævVBf–ÆRåÆâÒFFVB&Æö6¶VB×7F'GWF–væ÷7F–2F‚F†BFöW2æ÷BÆöB7&VFVçF–Ç2÷"×WFFRÖVF–åÆâÒ&W6W'fVBÆö6²&V6÷fW'’Âv–æF÷w2F‚ö6öæf–r&V6÷fW'’Âæöç&W7G&–7F—fR6ö×WFW"v&VæW72ÂæB6ÖRÖ6ö×WFW"GWÆ–6FR&÷FV7F–öâåÆåÆäW†7B&—fFR×&VÆV6R&÷fVææ6R4„Ó#Sc¢µ$•dDUõ¤•õ4„#SgÖÆåÆâ22cãRãr(	B&Wf–÷W2V&Æ–26÷W&6R&6VÆ–æUÆåÆå&W6W'fVBWf–FVæ6RÖ&6VBÖF6†–ærÂG'’×'Vâ&Wf–WrÂ¦÷W&æÆVBw&—FW2Â&VF&6²fW&–f–6F–öâÂ&öÆÆ&6²&V†f–÷"ÂæBÆVæ6†W"öFWVæFVæ7’6öç6—7FVæ7’6†V6·2åÆåÆçµ$”t…E7ÕÆârrr¢w&—FU÷FW‡B†÷WBòt´äõtåôtôôEõ5DDRæÖBrÆbrrr2¶æ÷vâÔvööB7FFUÆåÆäÖVF–FvvW$&÷BcãRã2—2F†R7W'&VçBW6W"Ö6öæf—&ÖVBv÷&¶–ær&VÆV6R&6VÆ–æRâF†RV&Æ–26÷W&6R&W6W'fW2F†RfW&–f–VB–FVçF—G’ÂFWVæFVæ7’Æö6²Â6÷W&6RÂÆVæ6†W"ÂFW7G2ÂæB&V6÷fW'’&V†f–÷"v†–ÆRW†6ÇVF–ær'VçF–ÖR7&VFVçF–Ç2Â&—fFRÖVF–ÂÆöw2ÂF–væ÷7F–72ÂæB–çFW&æÂG&ç6fW"ÖFW&–ÂåÆåÆäW†7B&—fFR×&VÆV6R&÷fVææ6R4„Ó#Sc¢µ$•dDUõ¤•õ4„#SgÖÆåÆçµ$”t…E7ÕÆârrr¢w&—FU÷FW‡B†÷WBòu$”t…E5ôäõD”4RçG‡BÅ$”t…E2²uÆåÆåF†—2f—'7B×'G’&–v‡G2æ÷F–6R—2æ÷BÆ–6Vç6Rw&çBâF†—&B×'G’6ö×öæVçG2&WF–âF†V—"÷vâÆ–6Vç6W2æBæ÷F–6W2åÆâr¢w&—FU÷FW‡B†÷WBòuD„•$Eõ%E•ôäõD”4U2æÖBrÂr2F†—&BÕ'G’æ÷F–6W5ÆåÆäÖVF–FvvW$&÷BW6W2&WVW7G2"ã3"ãRÂ×WFvVâãCrãÂW&ÆÆ–#2"ãrãÂ–Fæ2ã‚Â6†'6WBÖæ÷&ÖÆ—¦W"2ãBã’Â6W'F–f’##bãbãrÂ—FW7B’ãã"ÂæB6WGWFööÇ2ƒã’ãâF†V—"÷&–v–æÂÆ–6Vç6W2&VÖ–â–âf÷&6RâW†7B'VæFÆVB×v†VVÂ†6†W2&R&V6÷&FVB–âDUTäDTä5•õ4$ôÒæ§6öææB&WV—&VÖVçG2æÆö6²çG‡FåÆâr¢w&—FU÷FW‡B†÷WBòtÄ”4Tå4RæÖBrÆbrrr2&–v‡G5ÆåÆçµ$”t…E7ÕÆåÆäæòW&Ö—76–öâ—2w&çFVBFò6÷’ÂÖöF–g’Â&VF—7G&–'WFRÂ7V&Æ–6Vç6RÂ6VÆÂÂ÷"–æ6÷'÷&FRF†Rf—'7B×'G’6÷W&6R–çFòæ÷F†W"v÷&²v—F†÷WBw&—GFVâWF†÷&—¦F–öââF†—2æ÷F–6RFöW2æ÷B7&VFR÷"&WÆ6R6ögGv&RÆ–6Vç6RâF†—&B×'G’6ö×öæVçG2&WF–âF†V—"÷vâÆ–6Vç6W2æBæ÷F–6W2åÆârrr¢w&—FU÷FW‡B†÷WBòu4T5U$•E’æÖBrÆbrrr26V7W&—G’öÆ–7•ÆåÆåW6R&—fFRgVÆæW&&–Æ—G’&W÷'F–ærf÷"7W7V7FVB6V7W&—G’—77VW2âFòæ÷B–æ6ÇVFR7&VFVçF–Ç2Â&—fFRÖVF–Â’¶W—2ÂW'6öæÂF‡2Â÷"W‡Æö—BFWF–Ç2–âV&Æ–2—77VW2âFòæ÷BFW7Bv–ç7B7—7FV×2ÂFFÂ÷"6W'f–6W2–÷RFòæ÷B÷vâ÷"†fRW‡Æ–6—BW&Ö—76–öâFò76W72åÆåÆçµ$”t…E7ÕÆârrr¢w&—FU÷FW‡B†÷WBòræv—F–væ÷&RrÂuõ÷–66†UõòõÆâ¢ç•¶6öEÕÆâç—FW7Eö66†RõÆâçfVçbõÆæÆöw2õÆç7FFRõÆæF–væ÷7F–72õÆçFV×õÆæ66†RõÆæW‡÷'G2õÆâ¢æÆöuÆâr¢w&—FU÷FW‡B†÷WBòræv—FGG&–'WFW2rÂr¢FW‡CÖWFõÆâ¢æ&BFW‡BVöÃÖ7&ÆeÆâ¢ç’FW‡BVöÃÖÆeÆâ¢æÖBFW‡BVöÃÖÆeÆâ¢æ§6öâFW‡BVöÃÖÆeÆâr¢2W†—7F–ærV&Æ–2æv—F‡V"v÷&¶fÆ÷w2æBFWVæF&÷BöÆ–7’&R&W6W'fVB'’&WÆ6U÷&Wòà¢6‡WF–Âæ6÷“"…F‚…õöf–ÆUõò’ç&W6öÇfR‚’Â÷WBòwFööÇ2ròvÇ•÷V&Æ–5÷&VÆV6Rç’r¢f–ÆW3ÕµĞ¢f÷"–â6÷'FVB‡‚f÷"‚–â÷WBç&vÆö"‚r¢r’–b‚æ—5öf–ÆR‚’æB‚ç&VÆF—fU÷Fò†÷WB’æ5÷÷6—‚‚’æ÷B–â²tÔä”dU5Bæ§6öârÂtÔä”dU5Bæ77brÂwFööÇ2öÇ•÷V&Æ–5÷&VÆV6Rç’wÒæBræv—F‡V"ræ÷B–â‚ç&VÆF—fU÷Fò†÷WB’ç'G2“ ¢&VÃ×ç&VÆF—fU÷Fò†÷WB’æ5÷÷6—‚‚“²FF×ç&VEö'—FW2‚“²f–ÆW2æVæB‡²wF‚s§&VÂÂw6—¦Uö'—FW2s¦ÆVâ†FF’Âw6†#Sbs¦†6†Æ–"ç6†#Sb†FF’æ†W†F–vW7B‚’Âw6¶vUöÖævVBs§&VÂæ÷B–â²v6öæf–rö6öæf–rçFöÖÂrÂv6öæf–rö6æöæ–6Åö÷fW'&–FW2çFöÖÂwÒÂ'6Vç6—F—f—G’#¢wV&Æ–2rÂw7FGW2s¢v7W'&VçBwÒ¢Öæ–fW7C×²vÖWFFF÷66†VÖs¢v76WBÖÖWFFF×crÂw6¶vUö76WEö–Bs¢tÕD"Õ4´tRrÂw6¶vRs¢tÖVF–FvvW$&÷BV&Æ–26÷W&6R&VÆV6RrÂw6¶vUö–Bs¥4´tUô”BÂw&ö¦V7E÷6ÇVrs¥4´tUô”BÂwfW'6–öâs¥dU%4”ôâÂv'V–ÆEö–Bs¤%T”ÄEô”BÂw7FGW2s¢v7W'&VçBrÂw6Vç6—F—f—G’s¢wV&Æ–2rÂvvVæW&FVEö6GBs¢s##bÓ‚Ó‚4EBòÖW&–6ô6†–6vòrÂw6÷W&6U÷&÷fVææ6U÷6†#Sbs¥$•dDUõ¤•õ4„#SbÂw&–v‡G5öæ÷F–6Rs¥$”t…E2ÂvÆ–6Vç6Rs¢tÆÂ&–v‡G2&W6W'fVBf÷"f—'7B×'G’6÷W&6S²F†—&B×'G’Æ–6Vç6W2&VÖ–â6W&FRârÂw'VçF–ÖUö–FVçF—G•övFRs§²w66†VÖs¢tÖVF–FvvW$&÷Bç'VçF–ÖUö–FVçF—G•÷7FGW2çcrÂw&UöWF†VçF–6F–öåövFRs¥G'VRÂv6öçG&öÅöf–ÆW2s¥²udU%4”ôâçG‡BrÂtÔä”dU5Bæ§6öârÂu4´tUôÔUDDDæ§6öâuÒÂw6¶vUöÖævVEöf–VÆBs¢w6¶vUöÖævVBrÂvf–ÇW&U÷öÆ–7’s¢v&Æö6µöWF†VçF–6FVE÷&ö6W76–æuöÆÆ÷uöÆö6Å÷7FGW5÷7W÷'EöW‡÷'BwÒÂvf–ÆUö6÷VçBs¦ÆVâ†f–ÆW2’Âvf–ÆW2s¦f–ÆW7Ğ¢w&—FU÷FW‡B†÷WBòtÔä”dU5Bæ§6öârÆ§6öâæGV×2†Öæ–fW7BÆ–æFVçCÓ"ÆVç7W&Uö66–“ÔfÇ6R’²uÆâr¢v—F‚†÷WBòtÔä”dU5Bæ77br’æ÷Vâ‚wrrÆVæ6öF–æsÒwWFbÓ‚×6–rrÆæWvÆ–æSÒrr’2c ¢sÖ77bäF–7Ew&—FW"†bÆf–VÆFæÖW3Õ²wF‚rÂw6—¦Uö'—FW2rÂw6†#SbrÂw6¶vUöÖævVBrÂw6Vç6—F—f—G’rÂw7FGW2uÒ“²rçw&—FV†VFW"‚“²rçw&—FW&÷w2†f–ÆW2¢f÷"–â÷WBç&vÆö"‚r¢r“ ¢&VÃ×ç&VÆF—fU÷Fò†÷WB’æ5÷÷6—‚‚¢–b&VÃÓÒwFööÇ2öÇ•÷V&Æ–5÷&VÆV6Rç’s ¢6öçF–çVP¢–bæ—5öf–ÆR‚’æBç7Vff—‚æÆ÷vW"‚’–â²rç’rÂræÖBrÂrçG‡BrÂræ§6öârÂrçFöÖÂrÂrç–ÖÂrÂrç–ÖÂrÂræ77brÂræ&BwÓ ¢FW‡C×ç&VE÷FW‡B†Væ6öF–æsÒwWFbÓ‚rÆW'&÷'3Òw&WÆ6Rr¢f÷"Ö&¶W"–âdõ$$”DDTã ¢–bÖ&¶W"–âFW‡C¢&—6R'VçF–ÖTW'&÷"†bw&—fFRÖ&¶W"¶Ö&¶W"'Ò–â·ç&VÆF—fU÷Fò†÷WB—Òr ¦FVb&WÆ6U÷&Wò‡V&Æ–3¥F‚Ç&Wó¥F‚’ÓäæöæS ¢f÷"6†–ÆB–âÆ—7B‡&Wòæ—FW&F—"‚’“ ¢–b6†–ÆBææÖR–â²ræv—BrÂræv—F‡V"wÓ¢6öçF–çVP¢–b6†–ÆBæ—5öF—"‚’æBæ÷B6†–ÆBæ—5÷7–ÖÆ–æ²‚“¢6‡WF–Âç&×G&VR†6†–ÆB¢VÇ6S¢6†–ÆBçVæÆ–æ²‚¢f÷"6†–ÆB–âV&Æ–2æ—FW&F—"‚“ ¢–b6†–ÆBææÖSÓÒræv—F‡V"s¢6öçF–çVP¢FW7C×&Wòö6†–ÆBææÖP¢–b6†–ÆBæ—5öF—"‚“¢6‡WF–Âæ6÷—G&VR†6†–ÆBÆFW7B¢VÇ6S¢6‡WF–Âæ6÷“"†6†–ÆBÆFW7B ¦FVbÖ–â‚’Óæ–çC ¢'6W#Ö&w'6Rä&wVÖVçE'6W"‚“²'6W"æFEö&wVÖVçB‚rÒ×6÷W&6R×¦—rÇG—SÕF‚“²'6W"æFEö&wVÖVçB‚rÒ×&WòrÇG—SÕF‚ÆFVfVÇCÕF‚æ7vB‚’“²'6W"æFEö&wVÖVçB‚rÒÖ'V–ÆBÖöæÇ’rÇG—SÕF‚¢&w3×'6W"ç'6Uö&w2‚“²&WóÖ&w2ç&Wòç&W6öÇfR‚“²v÷&³ÕF‚‡FV×f–ÆRæÖ¶GFV×‡&Vf—ƒÒv×F"×V&Æ–2ÒrÆF—#Ö÷2æVçf—&öâævWB‚u%TääU%õDTÕr’’“²§FƒÖ&w2ç6÷W&6U÷¦—ç&W6öÇfR‚’–b&w2ç6÷W&6U÷¦—VÇ6Rv÷&²òw6÷W&6Rç¦—p¢–bæ÷B&w2ç6÷W&6U÷¦—¢F÷væÆöEöG&—fR„E$•dUôd”ÄUô”BÇ§F‚¢–b6†#Sb‡§F‚’Õ$•dDUõ¤•õ4„#Sc¢&—6R'VçF–ÖTW'&÷"†bw6÷W&6R6¶vR4„Ó#SbÖ—6ÖF6ƒ¢·6†#Sb‡§F‚—Òr¢6÷W&6S×6fUöW‡G&7B‡§F‚Çv÷&²òvW‡G&7Br“²V&Æ–3Ö&w2æ'V–ÆEööæÇ’ç&W6öÇfR‚’–b&w2æ'V–ÆEööæÇ’VÇ6Rv÷&²òwV&Æ–2s²'V–ÆE÷V&Æ–2‡6÷W&6RÇV&Æ–2¢–bæ÷B&w2æ'V–ÆEööæÇ“¢&WÆ6U÷&Wò‡V&Æ–2Ç&Wò¢&–çB†bu&W&VBÖVF–FvvW$&÷BµdU%4”ôçÒV&Æ–26÷W&6Rg&öÒW†7B6¶vRµ$•dDUõ¤•õ4„#SgÒr¢&WGW&â ¦–bõöæÖUõóÓÒuõöÖ–åõòs¢&—6R7—7FVÔW†—B†Ö–â‚’