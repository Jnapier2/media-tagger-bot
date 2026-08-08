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
RIGHTS='Copyright © 2026 Gateway Information Group LLC. All rights reserved.'

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
    meta.update({'canonical_project_name':'MediaTaggerBot','canonical_thread_title':'MediaTaggerBot — Public Runtime Identity Release','product_alias':'Music File Renaming Bot','generated_cdt':'2026-08-08 CDT / America/Chicago','source_baseline':'MediaTaggerBot v0.5.9 verified package','source_baseline_sha256':PRIVATE_ZIP_SHA256,'parameter_baseline':'Runtime Release Identity and Managed-File Integrity Gate v2.17.5','license':'All rights reserved for first-party source; third-party licenses remain separate.','distribution_scope':'public-source','private_material_excluded':True})
    meta.pop('parameter_package_sha256',None)
    write_text(out/'PACKAGE_METADATA.json',json.dumps(meta,indent=2,ensure_ascii=False)+'\n')
    p=out/'pyproject.toml'; p.write_text(p.read_text(encoding='utf-8').replace('classifiers = ["Private :: Do Not Upload"]','classifiers = ["License :: Other/Proprietary License", "Operating System :: Microsoft :: Windows", "Programming Language :: Python :: 3"]'),encoding='utf-8')
    write_text(out/'requirements-test.txt','pytest==9.0.2\n')
    p=out/'src/mediataggerbot/diagnostics.py'; p.write_text(p.read_text(encoding='utf-8').replace('            "target_collisions_csv": 12,','            "target_collisions_csv": 7,'),encoding='utf-8')
    p=out/'tests/test_diagnostics.py'; s=p.read_text(encoding='utf-8').replace('        assert "TRANSFER_BRIEF.md" in names\n','        assert "VERSION.txt" in names\n        assert "MANIFEST.json" in names\n        assert "PACKAGE_METADATA.json" in names\n        assert "TRANSFER_BRIEF.md" not in names\n'); p.write_text(s,encoding='utf-8')
    write_text(out/'tests/test_release_asset_manifest_v053.py','''from __future__ import annotations\nimport hashlib, json\nfrom pathlib import Path\nfrom mediataggerbot import __version__\nROOT=Path(__file__).resolve().parents[1]\nEXCLUDED={".venv","__pycache__",".pytest_cache","logs","exports","diagnostics","state","temp","archive","cache"}\ndef retained():\n    result=set()\n    for path in ROOT.rglob("*"):\n        if not path.is_file(): continue\n        rel=path.relative_to(ROOT)\n        if any(part in EXCLUDED for part in rel.parts) or path.suffix in {".pyc",".pyo"}: continue\n        if rel.as_posix() in {"MANIFEST.json","MANIFEST.csv"}: continue\n        result.add(rel.as_posix())\n    return result\ndef test_public_release_manifest_is_complete_and_verified():\n    m=json.loads((ROOT/"MANIFEST.json").read_text(encoding="utf-8")); rows=m["files"]\n    assert m["metadata_schema"]=="asset-metadata-v1" and m["package_asset_id"]=="MTB-PACKAGE"\n    assert m["package_id"]=="media-tagger-bot" and m["version"]==f"v{__version__}"\n    assert m["build_id"]=="MTB-0.5.9-B20260807-01" and m["runtime_identity_gate"]["pre_authentication_gate"] is True\n    assert m["file_count"]==len(rows) and {r["path"] for r in rows}==retained()\n    assert len({r["path"].casefold() for r in rows})==len(rows)\n    for r in rows:\n        p=ROOT/r["path"]; assert p.stat().st_size==r["size_bytes"]; assert hashlib.sha256(p.read_bytes()).hexdigest()==r["sha256"]; assert isinstance(r["package_managed"],bool)\n    by={r["path"]:r for r in rows}; assert by["VERSION.txt"]["package_managed"] is True; assert by["PACKAGE_METADATA.json"]["package_managed"] is True; assert by["config/config.toml"]["package_managed"] is False; assert by["config/canonical_overrides.toml"]["package_managed"] is False\n''')
    write_text(out/'README.md',f'''# MediaTaggerBot v0.5.9\n\n[![CI](https://github.com/Jnapier2/media-tagger-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/Jnapier2/media-tagger-bot/actions/workflows/ci.yml)\n\nMediaTaggerBot is a local-first Windows utility for reviewing, standardizing, tagging, and renaming music files. It combines multiple evidence sources, keeps uncertain matches visible for review, and places potentially destructive changes behind an explicit apply boundary.\n\n## Runtime identity in v0.5.9\n\nBefore runtime configuration, credentials, authenticated metadata requests, or media mutation, the application verifies that the running package ID, version, build ID, `VERSION.txt`, `MANIFEST.json`, and `PACKAGE_METADATA.json` agree and that every immutable package-managed file matches its recorded SHA-256 value. A stale or mixed package is blocked rather than repaired in place.\n\n## Safety and review controls\n\n- Dry-run and review-first operation before file mutation\n- Complete-scan and ambiguity checks before apply\n- Journaled changes, result readback, and rollback manifests\n- Exact runtime dependency lock with bundled wheels\n- Same-computer duplicate-run protection only\n- Computer recognition used only for helpful local labels and defaults\n- Redacted diagnostics that exclude credentials and private media contents\n- No cross-computer ownership, handoff, lease, or launch restriction\n\n## Run on Windows\n\nKeep all release files together, run `Start_MediaTaggerBot.bat`, and use Preflight before enabling authenticated metadata services or applying changes. Runtime configuration is mutable and intentionally excluded from package-managed hashing.\n\n## Verify\n\n```powershell\npy -3.11 -m pip install --no-index --find-links wheels --require-hashes -r requirements.lock.txt\npy -3.11 -m pip install -e .\npy -3.11 -m pip install -r requirements-test.txt\npy -3.11 -m pytest -q\n```\n\n## Public-source boundary\n\nThis repository excludes private music libraries, credentials, caches, logs, diagnostics, transfer notes, internal project-management records, and user-specific paths. Example paths in tests are synthetic and exist to verify redaction and portability.\n\n{RIGHTS}\n\nThis notice does not replace or infer a license for third-party components.\n''')
    write_text(out/'CHANGELOG.md',f'''# Changelog\n\n## v0.5.9 — Runtime identity and managed-file integrity\n\n- Added the pre-authentication release identity gate.\n- Added SHA-256 verification for every immutable package-managed file.\n- Added a blocked-startup diagnostic path that does not load credentials or mutate media.\n- Preserved lock recovery, Windows path/config recovery, nonrestrictive computer awareness, and same-computer duplicate protection.\n\nExact private-release provenance SHA-256: `{PRIVATE_ZIP_SHA256}`\n\n## v0.5.7 — Previous public source baseline\n\nPreserved evidence-based matching, dry-run review, journaled writes, readback verification, rollback behavior, and launcher/dependency consistency checks.\n\n{RIGHTS}\n''')
    write_text(out/'KNOWN_GOOD_STATE.md',f'''# Known-Good State\n\nMediaTaggerBot v0.5.9 is the current user-confirmed working release baseline. The public source preserves the verified identity, dependency lock, source, launcher, tests, and recovery behavior while excluding runtime credentials, private media, logs, diagnostics, and internal transfer material.\n\nExact private-release provenance SHA-256: `{PRIVATE_ZIP_SHA256}`\n\n{RIGHTS}\n''')
    write_text(out/'RIGHTS_NOTICE.txt',RIGHTS+'\n\nThis first-party rights notice is not a license grant. Third-party components retain their own licenses and notices.\n')
    write_text(out/'THIRD_PARTY_NOTICES.md','# Third-Party Notices\n\nMediaTaggerBot uses Requests 2.32.5, Mutagen 1.47.0, urllib3 2.7.0, idna 3.18, charset-normalizer 3.4.9, certifi 2026.6.17, pytest 9.0.2, and setuptools 80.9.0. Their original licenses remain in force. Exact bundled-wheel hashes are recorded in `DEPENDENCY_SBOM.json` and `requirements.lock.txt`.\n')
    write_text(out/'LICENSE.md',f'''# Rights\n\n{RIGHTS}\n\nNo permission is granted to copy, modify, redistribute, sublicense, sell, or incorporate the first-party source into another work without written authorization. This notice does not create or replace a software license. Third-party components retain their own licenses and notices.\n''')
    write_text(out/'SECURITY.md',f'''# Security Policy\n\nUse private vulnerability reporting for suspected security issues. Do not include credentials, private media, API keys, personal paths, or exploit details in public issues. Do not test against systems, data, or services you do not own or have explicit permission to assess.\n\n{RIGHTS}\n''')
    write_text(out/'.gitignore','__pycache__/\n*.py[cod]\n.pytest_cache/\n.venv/\nlogs/\nstate/\ndiagnostics/\ntemp/\ncache/\nexports/\n*.log\n')
    write_text(out/'.gitattributes','* text=auto\n*.bat text eol=crlf\n*.py text eol=lf\n*.md text eol=lf\n*.json text eol=lf\n')
    write_text(out/'.github/workflows/ci.yml','''name: CI\n\non:\n  push:\n    branches: [main]\n  pull_request:\n  workflow_dispatch:\n\npermissions:\n  contents: read\n\njobs:\n  windows-runtime:\n    runs-on: windows-latest\n    timeout-minutes: 20\n    strategy:\n      fail-fast: false\n      matrix:\n        python-version: ["3.11", "3.13"]\n    steps:\n      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n        with:\n          persist-credentials: false\n      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0\n        with:\n          python-version: ${{ matrix.python-version }}\n      - name: Install exact runtime\n        shell: pwsh\n        run: python -m pip install --no-index --find-links wheels --require-hashes -r requirements.lock.txt\n      - name: Install source and tests\n        shell: pwsh\n        run: |\n          python -m pip install -e .\n          python -m pip install -r requirements-test.txt\n          python -m pip check\n      - name: Compile and test\n        shell: pwsh\n        run: |\n          python -m compileall -q src scripts tests\n          python -m pytest -q\n      - name: Verify release identity gate\n        shell: pwsh\n        run: |\n          @'\n          from pathlib import Path\n          from mediataggerbot.package_identity import verify_runtime_identity\n          result=verify_runtime_identity(Path.cwd(),"0.5.9","MTB-0.5.9-B20260807-01")\n          print(result)\n          raise SystemExit(0 if result["gate_result"]=="PASS" else 1)\n          '@ | python -\n''')
    write_text(out/'.github/dependabot.yml','''version: 2\nupdates:\n  - package-ecosystem: github-actions\n    directory: "/"\n    schedule:\n      interval: monthly\n    open-pull-requests-limit: 1\n''')
    files=[]
    for p in sorted(x for x in out.rglob('*') if x.is_file() and x.relative_to(out).as_posix() not in {'MANIFEST.json','MANIFEST.csv'}):
        rel=p.relative_to(out).as_posix(); data=p.read_bytes(); files.append({'path':rel,'size_bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'package_managed':rel not in {'config/config.toml','config/canonical_overrides.toml'},'sensitivity':'public','status':'current'})
    manifest={'metadata_schema':'asset-metadata-v1','package_asset_id':'MTB-PACKAGE','package':'MediaTaggerBot public source release','package_id':PACKAGE_ID,'project_slug':PACKAGE_ID,'version':VERSION,'build_id':BUILD_ID,'status':'current','sensitivity':'public','generated_cdt':'2026-08-08 CDT / America/Chicago','source_provenance_sha256':PRIVATE_ZIP_SHA256,'rights_notice':RIGHTS,'license':'All rights reserved for first-party source; third-party licenses remain separate.','runtime_identity_gate':{'schema':'MediaTaggerBot.runtime_identity_status.v1','pre_authentication_gate':True,'control_files':['VERSION.txt','MANIFEST.json','PACKAGE_METADATA.json'],'package_managed_field':'package_managed','failure_policy':'block_authenticated_processing_allow_local_status_support_export'},'file_count':len(files),'files':files}
    write_text(out/'MANIFEST.json',json.dumps(manifest,indent=2,ensure_ascii=False)+'\n')
    with (out/'MANIFEST.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['path','size_bytes','sha256','package_managed','sensitivity','status']); w.writeheader(); w.writerows(files)
    for p in out.rglob('*'):
        if p.is_file() and p.suffix.lower() in {'.py','.md','.txt','.json','.toml','.yml','.yaml','.csv','.bat'}:
            text=p.read_text(encoding='utf-8',errors='replace')
            for marker in FORBIDDEN:
                if marker in text: raise RuntimeError(f'private marker {marker!r} in {p.relative_to(out)}')

def replace_repo(public:Path,repo:Path)->None:
    for child in list(repo.iterdir()):
        if child.name=='.git': continue
        if child.is_dir() and not child.is_symlink(): shutil.rmtree(child)
        else: child.unlink()
    for child in public.iterdir():
        dest=repo/child.name
        if child.is_dir(): shutil.copytree(child,dest)
        else: shutil.copy2(child,dest)

def main()->int:
    parser=argparse.ArgumentParser(); parser.add_argument('--source-zip',type=Path); parser.add_argument('--repo',type=Path,default=Path.cwd()); parser.add_argument('--build-only',type=Path)
    args=parser.parse_args(); repo=args.repo.resolve(); work=Path(tempfile.mkdtemp(prefix='mtb-public-',dir=os.environ.get('RUNNER_TEMP'))); zpath=args.source_zip.resolve() if args.source_zip else work/'source.zip'
    if not args.source_zip: download_drive(DRIVE_FILE_ID,zpath)
    if sha256(zpath)!=PRIVATE_ZIP_SHA256: raise RuntimeError(f'source package SHA-256 mismatch: {sha256(zpath)}')
    source=safe_extract(zpath,work/'extract'); public=args.build_only.resolve() if args.build_only else work/'public'; build_public(source,public)
    if not args.build_only: replace_repo(public,repo)
    print(f'Prepared MediaTaggerBot {VERSION} public source from exact package {PRIVATE_ZIP_SHA256}')
    return 0
if __name__=='__main__': raise SystemExit(main())
