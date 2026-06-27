#!/usr/bin/env python3
from typing import TYPE_CHECKING, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen, urlretrieve
from argparse import ArgumentParser
from sys import stderr
import sqlite3
import json
import gzip
import os
import re
import subprocess
import tempfile
from PIL import Image, PngImagePlugin, ImageFile

# Increase limit for large metadata chunks
PngImagePlugin.MAX_TEXT_CHUNK = 100 * 1024 * 1024  # 100MB
ImageFile.LOAD_TRUNCATED_IMAGES = True

import warnings
with warnings.catch_warnings():  # hide macOS LibreSSL warning
    warnings.filterwarnings('ignore')
    from remotezip import RemoteZip  # pip install remotezip

if TYPE_CHECKING:
    from zipfile import ZipInfo

# Optional Android manifest parser (binary AXML -> XML)
try:
    from axmlparserpy.axmlprinter import AXMLPrinter
except Exception:
    AXMLPrinter = None

import xml.etree.ElementTree as ET

import platform
USE_ZIP_FILESIZE = False
NESTED_SEP = '##'

# Detect OS and set pngdefry binary name (used only if needed)
if platform.system() == 'Windows':
    PNGDEFRY_BIN = Path(__file__).parent / 'pngdefry' / 'pngdefry.exe'
else:
    PNGDEFRY_BIN = Path(__file__).parent / 'pngdefry' / 'pngdefry'

# Archive.org metadata regex
re_archive_url = re.compile(
    r'https?://archive.org/(?:metadata|details|download)/([^/]+)(?:/.*)?')
CACHE_DIR = Path(__file__).parent / 'data'
CACHE_DIR.mkdir(exist_ok=True)

# Exceptions mapping file for image deduplication overrides
EXCEPTIONS_FILE = CACHE_DIR / 'exceptions.json'
EXCEPTIONS = {}
if EXCEPTIONS_FILE.exists():
    with open(EXCEPTIONS_FILE, 'r') as f:
        EXCEPTIONS = json.load(f)

# Android manifest filename (we capture exact entry from APK)
MANIFEST_NAME = 'AndroidManifest.xml'


def main():
    CacheDB().init()
    parser = ArgumentParser()
    cli = parser.add_subparsers(metavar='command', dest='cmd', required=True)

    cmd = cli.add_parser('add', help='Add urls to cache')
    cmd.add_argument('urls', metavar='URL', nargs='+',
                     help='Search URLs for .apk links. Use "continue" to resume interrupted progress.')

    cmd = cli.add_parser('update', help='Update all urls')
    cmd.add_argument('urls', metavar='URL', nargs='*', help='URLs or index')

    cmd = cli.add_parser('run', help='Download and process pending urls')
    cmd.add_argument('-force', '-f', action='store_true',
                     help='Reindex local data / populate DB.')
    cmd.add_argument('-retry', '-r', action='store_true',
                     help='Automatically retry entries that fail.')
    cmd.add_argument('pk', metavar='PK', type=int,
                     nargs='*', help='Primary key')

    cmd = cli.add_parser('export', help='Export data')
    cmd.add_argument('export_type', choices=['json', 'fsize'],
                     help='Export to json or temporary-filesize file')

    cmd = cli.add_parser('err', help='Handle problematic entries')
    cmd.add_argument('err_type', choices=['reset', 'fix', 'clear'],
                     help='reset: Set all done=3 to 0. fix: Reset and retry until no progress is made. clear: DELETE all entries with done=3 or done=4 from database.')

    cmd = cli.add_parser('get', help='Lookup value')
    cmd.add_argument('get_type', choices=['url', 'img', 'apk'],
                     help='Get data field or download image.')
    cmd.add_argument('pk', metavar='PK', type=int,
                     nargs='+', help='Primary key')

    cmd = cli.add_parser('set', help='(Re)set value')
    cmd.add_argument('set_type', choices=['err'], help='Data field/column')
    cmd.add_argument('pk', metavar='PK', type=int,
                     nargs='+', help='Primary key')

    cli.add_parser('fix-imgs', help='Check and fix missing images')
    cmd = cli.add_parser('clear-queue', help='DELETE pending entries from the database')
    cmd.add_argument('queue_type', choices=['run', 'add'], metavar='type',
                     help='run: Processing queue (done=0), add: Scraping queue')

    cmd = cli.add_parser('special', help='Consolidate all images for a package name to the first one')
    cmd.add_argument('package', help='The package name to consolidate')

    args = parser.parse_args()

    if args.cmd == 'add':
        if args.urls == ['continue']:
            queue = CacheDB().getScrapeQueue()
            if not queue:
                print('Nothing to resume.')
            else:
                print(f'Resuming {len(queue)} collections...')
                for url in queue:
                    addNewUrl(url, resume=True)
        else:
            db = CacheDB()
            for url in args.urls:
                db.addToScrapeQueue(url)
            for url in args.urls:
                addNewUrl(url, resume=False)
        print('done.')

    elif args.cmd == 'update':
        queue = args.urls or CacheDB().getUpdateUrlIds(sinceNow='-7 days')
        if queue:
            for i, url in enumerate(queue):
                updateUrl(url, i + 1, len(queue))
            print('done.')
        else:
            print('Nothing to do.')

    elif args.cmd == 'run':
        DB = CacheDB()
        if args.pk:
            for pk in args.pk:
                url = DB.getUrl(pk)
                print(pk, ': process', url)
                success, img_pk = loadApk(pk, url, overwrite=True)
                if success:
                    DB.setDone(pk)
                else:
                    DB.setError(pk, done=3)
        else:
            if args.force:
                print('Resetting done state ...')
                DB.setAllUndone(whereDone=1)
            while True:
                old_done_count = DB.count(done=1)
                processPending()
                new_done_count = DB.count(done=1)

                if args.retry and new_done_count > old_done_count:
                    err_count = DB.count(done=3)
                    if err_count > 0:
                        print(f'\nFixed {new_done_count - old_done_count} entries. {err_count} errors remain. Retrying...')
                        DB.setAllUndone(whereDone=3)
                        continue
                break

        fix_missing_images(DB)

    elif args.cmd == 'err':
        DB = CacheDB()
        if args.err_type == 'reset':
            print('Resetting error state ...')
            DB.setAllUndone(whereDone=3)
        elif args.err_type == 'clear':
            count = DB.deleteAllErrors()
            print(f'Successfully deleted {count} error entries from the database.')
        elif args.err_type == 'fix':
            while True:
                err_count = DB.count(done=3)
                if err_count == 0:
                    print('No errors to fix.')
                    break
                print(f'Resetting {err_count} errors and retrying...')
                DB.setAllUndone(whereDone=3)
                old_done_count = DB.count(done=1)
                processPending()
                new_done_count = DB.count(done=1)
                if new_done_count <= old_done_count:
                    print(f'No more progress. {DB.count(done=3)} errors remain.')
                    break
                print(f'Fixed {new_done_count - old_done_count} entries. Retrying remaining errors...')

    elif args.cmd == 'export':
        if args.export_type == 'json':
            export_json()
        elif args.export_type == 'fsize':
            export_filesize()

    elif args.cmd == 'get':
        DB = CacheDB()
        if args.get_type == 'url':
            for pk in args.pk:
                print(pk, ':', DB.getUrl(pk))
        elif args.get_type == 'img':
            for pk in args.pk:
                url = DB.getUrl(pk)
                print(pk, ': load image', url)
                loadApk(pk, url, overwrite=True, image_only=True)
        elif args.get_type == 'apk':
            dir = Path('apk_download')
            dir.mkdir(exist_ok=True)
            for pk in args.pk:
                url = DB.getUrl(pk)
                print(pk, ': load apk', url)
                urlretrieve(url, dir / f'{pk}.apk', printProgress)
                print(end='\r')

    elif args.cmd == 'set':
        DB = CacheDB()
        if args.set_type == 'err':
            for pk in args.pk:
                print(pk, ': set done=4')
                DB.setPermanentError(pk)

    elif args.cmd == 'fix-imgs':
        fix_missing_images(CacheDB())

    elif args.cmd == 'clear-queue':
        count = CacheDB().clearQueue(type=args.queue_type)
        print(f'Successfully cleared {count} {args.queue_type} entries from the database.')

    elif args.cmd == 'special':
        DB = CacheDB()
        pkg = args.package
        print(f"Consolidating images for: {pkg}")

        res = DB._db.execute("""
            SELECT image_pk FROM idx 
            WHERE package_id=? AND done=1 AND image_pk IS NOT NULL 
            ORDER BY pk ASC LIMIT 1
        """, [pkg]).fetchone()

        if not res:
            print(f"Error: No processed entries (done=1) found for {pkg} with an image.")
            return

        master_pk = res[0]
        if not diskPath(master_pk, '.jpg').exists():
            print(f"Error: Master image {master_pk}.jpg not found on disk.")
            return

        print(f"Master Image PK identified: {master_pk}")

        EXCEPTIONS[pkg] = master_pk
        with open(EXCEPTIONS_FILE, 'w') as f:
            json.dump(EXCEPTIONS, f, indent=4)
        print(f"Updated exceptions.json")

        cur = DB._db.execute("UPDATE idx SET image_pk=? WHERE package_id=?", [master_pk, pkg])
        count = cur.rowcount
        print(f"Updated {count} entries in database.")
        DB._db.commit()

        cur = DB._db.execute("SELECT pk FROM idx WHERE package_id=?", [pkg])
        pks = [row[0] for row in cur.fetchall()]
        deleted_count = 0
        for pk in pks:
            if pk == master_pk:
                continue
            for ext in ['.jpg', '.png']:
                p = diskPath(pk, ext)
                if p.exists():
                    p.unlink()
                    deleted_count += 1
        print(f"Deleted {deleted_count} orphaned icon files.")
        print("done.")


def fix_missing_images(DB: 'CacheDB'):
    missing = []
    print("Checking for missing images...")
    entries = list(DB.getUniqueImagePks())
    total = len(entries)

    shards = {}
    for pk, img_pk in entries:
        s = img_pk // 1000
        shards.setdefault(s, []).append(img_pk)

    checked = 0
    for s, pks in sorted(shards.items()):
        shard_dir = CACHE_DIR / str(s)
        if not shard_dir.exists():
            missing.extend(pks)
        else:
            existing = {f.name for f in shard_dir.iterdir() if f.suffix == '.jpg'}
            for img_pk in pks:
                if f"{img_pk}.jpg" not in existing:
                    missing.append(img_pk)
        checked += len(pks)
        if checked % 100 == 0 or checked == total:
            print(f"\rChecked {checked}/{total} unique images...", end="")

    print(f"\rChecked {total}/{total} unique images. Done.")

    if not missing:
        print("No missing images found.")
        return

    print(f"Found {len(missing)} missing unique images. Fixing...")
    for pk in missing:
        url = DB.getUrl(pk)
        print(f"[{pk}] Fix unique image: {url}")

        res = DB._db.execute("SELECT done FROM idx WHERE pk=?", [pk]).fetchone()
        state = res[0] if res else 1

        success, img_pk = loadApk(pk, url, overwrite=True, image_only=True)

        if success and img_pk != pk:
            print(f"  [FIX] [{pk}] deduplicated to {img_pk}. Updating database...")
            DB._db.execute("UPDATE idx SET image_pk=? WHERE image_pk=?", [img_pk, pk])
            DB._db.commit()

        if not diskPath(img_pk, ".jpg").exists():
            if state == 1:
                print(f"  [WARN] [{pk}] Still no image. Setting to retry state (done=2).")
                DB._db.execute("UPDATE idx SET done=2 WHERE image_pk=?", [img_pk])
                DB._db.commit()
            else:
                print(f"  [ERROR] [{pk}] Still no image after retry. Marking as permanent error.")
                uids = DB._db.execute("SELECT pk FROM idx WHERE image_pk=?", [img_pk]).fetchall()
                for (uid,) in uids:
                    DB.setPermanentError(uid)
    print("done.")


# --- PRE-COMPILED REGEX & HELPERS ---
RE_HASH = re.compile(r'-[0-9a-f]{32}')
RE_BRACKETS = re.compile(r'[\(\[].*?[\)\]]')
RE_VERSION = re.compile(r'[-.]v?\d+(\.\d+)*')
RE_CAMEL = re.compile(r'([a-z])([A-Z])')
RE_WORDS = re.compile(r'[a-z0-9]{2,}')
RE_PKG = re.compile(r'([a-z]{2,}\.[a-z0-9]{2,}\.[a-z0-9\.]+)')

def get_clean_words(text):
    text = RE_CAMEL.sub(r'\1 \2', str(text))
    return RE_WORDS.findall(text.lower())

def is_hint_match(word, target):
    if not word or not target:
        return False
    it = iter(target.lower())
    return all(c in it for c in word.lower())

def prettify_title(title: str, package_id: str, path_name: str) -> str:
    if not title or title.lower() in ["unknown", "null", ""]:
        source = path_name.split(NESTED_SEP)[-1].split('/')[-1].replace('.apk', '')
        if len(source) < 3 and package_id:
            source = package_id.split('.')[-1]
    elif title.lower().startswith(('com.', 'net.', 'org.')):
        source = title.split('.')[-1]
    else:
        source = title

    source = RE_HASH.sub('', source)
    source = RE_BRACKETS.sub('', source)
    source = RE_VERSION.sub('', source)
    pretty = source.replace('.', ' ').replace('-', ' ').replace('_', ' ')
    pretty = ' '.join(pretty.split()).title()
    if len(pretty) < 2 and package_id:
        pretty = str(package_id).split('.')[-1].title()
    return pretty


class CacheDB:
    def __init__(self) -> None:
        self._db = sqlite3.connect(CACHE_DIR / 'apk_cache.db', timeout=60.0)
        self._db.execute('PRAGMA journal_mode=WAL;')
        self._db.execute('PRAGMA busy_timeout=60000;')

    def init(self):
        self._db.execute('''
            CREATE TABLE IF NOT EXISTS urls(
                pk INTEGER PRIMARY KEY,
                url TEXT NOT NULL UNIQUE,
                date INTEGER DEFAULT (strftime('%s','now'))
            );
        ''')
        self._db.execute('''
            CREATE TABLE IF NOT EXISTS idx(
                pk INTEGER PRIMARY KEY,
                base_url INTEGER NOT NULL,
                path_name TEXT NOT NULL,
                done INTEGER DEFAULT 0,
                fsize INTEGER DEFAULT 0,

                min_sdk INTEGER DEFAULT NULL,
                platform INTEGER DEFAULT NULL,
                title TEXT DEFAULT NULL,
                package_id TEXT DEFAULT NULL,
                version TEXT DEFAULT NULL,
                image_pk INTEGER DEFAULT NULL,

                UNIQUE(base_url, path_name) ON CONFLICT ABORT,
                FOREIGN KEY (base_url) REFERENCES urls (pk) ON DELETE RESTRICT
            );
        ''')
        self._db.execute('''
            CREATE TABLE IF NOT EXISTS scrape_queue(
                url TEXT PRIMARY KEY
            );
        ''')
        self._db.execute('''
            CREATE TABLE IF NOT EXISTS scanned_archives(
                base_url_id INTEGER,
                archive_name TEXT,
                size INTEGER,
                crc TEXT,
                PRIMARY KEY(base_url_id, archive_name),
                FOREIGN KEY (base_url_id) REFERENCES urls (pk) ON DELETE CASCADE
            );
        ''')

    def __del__(self) -> None:
        self._db.close()

    def addToScrapeQueue(self, url: str):
        self._db.execute('INSERT OR IGNORE INTO scrape_queue (url) VALUES (?);', [url])
        self._db.commit()

    def removeFromScrapeQueue(self, url: str):
        self._db.execute('DELETE FROM scrape_queue WHERE url=?;', [url])
        self._db.commit()

    def getScrapeQueue(self) -> 'list[str]':
        x = self._db.execute('SELECT url FROM scrape_queue;')
        return [row[0] for row in x.fetchall()]

    def isArchiveScanned(self, baseUrlId: int, name: str, size: int, crc: str) -> bool:
        x = self._db.execute('''SELECT 1 FROM scanned_archives 
            WHERE base_url_id=? AND archive_name=? AND size=? AND crc=?;''',
            [baseUrlId, name, size, crc])
        return x.fetchone() is not None

    def markArchiveScanned(self, baseUrlId: int, name: str, size: int, crc: str):
        self._db.execute('''INSERT OR REPLACE INTO scanned_archives 
            (base_url_id, archive_name, size, crc) VALUES (?,?,?,?);''',
            [baseUrlId, name, size, crc])
        self._db.commit()

    def getNestedApksFromIdx(self, baseUrlId: int, archiveName: str) -> 'list[tuple[str, int, str]]':
        prefix = archiveName + NESTED_SEP
        x = self._db.execute('''SELECT path_name, fsize FROM idx 
            WHERE base_url=? AND path_name LIKE ?;''', [baseUrlId, prefix + '%'])
        return [(row[0], row[1], None) for row in x.fetchall()]

    def clearScannedArchives(self, baseUrlId: int = None):
        if baseUrlId:
            self._db.execute('DELETE FROM scanned_archives WHERE base_url_id=?;', [baseUrlId])
        else:
            self._db.execute('DELETE FROM scanned_archives;')
        self._db.commit()

    # Get URL helpers

    def getIdForBaseUrl(self, url: str) -> 'int|None':
        x = self._db.execute('SELECT pk FROM urls WHERE url=?', [url])
        row = x.fetchone()
        return row[0] if row else None

    def getBaseUrlForId(self, uid: int) -> 'str|None':
        x = self._db.execute('SELECT url FROM urls WHERE pk=?', [uid])
        row = x.fetchone()
        return row[0] if row else None

    def getId(self, baseUrlId: int, pathName: str) -> 'int|None':
        x = self._db.execute('''SELECT pk FROM idx
            WHERE base_url=? AND path_name=?;''', [baseUrlId, pathName])
        row = x.fetchone()
        return row[0] if row else None

    def getUrl(self, uid: int) -> str:
        x = self._db.execute('''SELECT url, path_name FROM idx
            INNER JOIN urls ON urls.pk=base_url WHERE idx.pk=?;''', [uid])
        base, path = x.fetchone()
        path = path.replace(NESTED_SEP, '/')
        return base + '/' + quote(path)

    def hasImage(self, package_id: str, version: str) -> 'int|None':
        if package_id in EXCEPTIONS:
            target_pk = EXCEPTIONS[package_id]
            if diskPath(target_pk, '.jpg').exists():
                return target_pk
        if not package_id or not version:
            return None
        res = self._db.execute('''SELECT image_pk FROM idx 
            WHERE package_id=? AND version=? AND image_pk IS NOT NULL 
            LIMIT 1''', [package_id, version]).fetchone()
        if res:
            pk = res[0]
            if diskPath(pk, '.jpg').exists():
                return pk
        return None

    def insertBaseUrl(self, base: str) -> int:
        try:
            x = self._db.execute('INSERT INTO urls (url) VALUES (?);', [base])
            self._db.commit()
            return x.lastrowid  # type: ignore
        except sqlite3.IntegrityError:
            x = self._db.execute('SELECT pk FROM urls WHERE url = ?;', [base])
            return x.fetchone()[0]

    def insertApkUrls(self, baseUrlId: int, entries: 'Iterable[tuple[str, int, str]]') -> int:
        before = self._db.total_changes
        self._db.executemany('''
        INSERT OR IGNORE INTO idx (base_url, path_name, fsize) VALUES (?,?,?);
        ''', ((baseUrlId, path, size) for path, size, _crc in entries))
        self._db.commit()
        return self._db.total_changes - before

    def getUpdateUrlIds(self, *, sinceNow: str) -> 'list[int]':
        x = self._db.execute('''SELECT pk FROM urls
            WHERE date IS NULL OR date < strftime('%s','now', ?)
        ''', [sinceNow])
        return [row[0] for row in x.fetchall()]

    def markBaseUrlUpdated(self, uid: int) -> None:
        self._db.execute('''UPDATE urls SET date=strftime('%s','now') WHERE pk=?''', [uid])
        self._db.commit()

    def updateApkUrl(self, baseUrlId: int, entry: 'tuple[str, int, str]') -> 'int|None':
        uid = self.getId(baseUrlId, entry[0])
        if uid:
            self._db.execute('UPDATE idx SET done=0, fsize=? WHERE pk=?;', [entry[1], uid])
            self._db.commit()
            return uid
        if self.insertApkUrls(baseUrlId, [entry]) > 0:
            x = self._db.execute('SELECT MAX(pk) FROM idx;')
            return x.fetchone()[0]
        return None

    def jsonUrlMap(self) -> 'dict[int, str]':
        x = self._db.execute('SELECT pk, url FROM urls')
        rv = {}
        for pk, url in x:
            rv[pk] = url
        return rv

    def enumJsonApk(self, *, done: int) -> Iterable[tuple]:
        yield from self._db.execute('''
            SELECT pk, IFNULL(min_sdk, 0),
                TRIM(IFNULL(title,
                    REPLACE(path_name,RTRIM(path_name,REPLACE(path_name,'/','')),'')
                )) as tt, IFNULL(package_id, ""),
                version, base_url, path_name, fsize / 1024,
                IFNULL(image_pk, pk)
            FROM idx WHERE done=?
            ORDER BY tt COLLATE NOCASE, package_id, min_sdk, version;''', [done])

    def getUniqueImagePks(self) -> Iterable[tuple]:
        yield from self._db.execute('''
            SELECT MIN(pk), image_pk 
            FROM idx 
            WHERE done IN (1, 2) AND image_pk IS NOT NULL 
            GROUP BY image_pk
        ''')

    def enumFilesize(self) -> Iterable[tuple]:
        yield from self._db.execute('SELECT pk, fsize FROM idx WHERE fsize>0;')

    def setFilesize(self, uid: int, size: int) -> None:
        if size > 0:
            self._db.execute('UPDATE idx SET fsize=? WHERE pk=?;', [size, uid])
            self._db.commit()

    def count(self, *, done: int) -> int:
        x = self._db.execute('SELECT COUNT() FROM idx WHERE done=?;', [done])
        return x.fetchone()[0]

    def getPendingQueue(self, *, done: int, batchsize: int) -> 'list[tuple[int, str, str]]':
        x = self._db.execute('''SELECT idx.pk, url, path_name
            FROM idx INNER JOIN urls ON urls.pk=base_url
            WHERE done=? LIMIT ?;''', [done, batchsize])
        return x.fetchall()

    def setAllUndone(self, *, whereDone: int) -> None:
        self._db.execute('UPDATE idx SET done=0 WHERE done=?;', [whereDone])
        self._db.commit()

    def deleteAllErrors(self) -> int:
        x = self._db.execute('SELECT pk FROM idx WHERE done IN (3, 4);')
        ids = [row[0] for row in x.fetchall()]
        for uid in ids:
            for ext in ['.manifest', '.png', '.jpg']:
                fname = diskPath(uid, ext)
                if fname.exists():
                    os.remove(fname)
        x = self._db.execute('DELETE FROM idx WHERE done IN (3, 4);')
        self._db.commit()
        return x.rowcount

    def clearQueue(self, type: str = 'run') -> int:
        if type == 'run':
            x = self._db.execute('DELETE FROM idx WHERE done=0;')
            self._db.commit()
            return x.rowcount
        elif type == 'add':
            x1 = self._db.execute('DELETE FROM scrape_queue;')
            x2 = self._db.execute('DELETE FROM scanned_archives;')
            self._db.commit()
            return x1.rowcount
        return 0

    def setError(self, uid: int, *, done: int) -> None:
        self._db.execute('UPDATE idx SET done=? WHERE pk=?;', [done, uid])
        self._db.commit()

    def setPermanentError(self, uid: int) -> None:
        self._db.execute('''
            UPDATE idx SET done=4, min_sdk=NULL, platform=NULL, title=NULL,
            package_id=NULL, version=NULL WHERE pk=?;''', [uid])
        self._db.commit()
        for ext in ['.manifest', '.png', '.jpg']:
            fname = diskPath(uid, ext)
            if fname.exists():
                os.remove(fname)

    def setDone(self, uid: int) -> None:
        manifest_path = diskPath(uid, '.manifest')
        if not manifest_path.exists():
            return
        with open(manifest_path, 'rb') as fp:
            try:
                manifest = self._parseManifest(fp.read())
            except Exception as e:
                print(f'ERROR: [{uid}] MANIFEST: {e}', file=stderr)
                self.setError(uid, done=3)
                return

        packageId = manifest.get('package', '')
        title = manifest.get('label', '')
        version = manifest.get('versionName', '')
        minSdk = int(manifest.get('minSdkVersion', 0) or 0)

        # path_name check for hints is retained (from ipa flow)
        res = self._db.execute('SELECT path_name FROM idx WHERE pk=?', [uid]).fetchone()
        path_name = res[0] if res else ""

        if path_name:
            fn_words = get_clean_words(path_name.split(NESTED_SEP)[-1])
            pkg_full = str(packageId).lower()
            tl_full = str(title).lower()
            has_hint = False
            for w in fn_words:
                if is_hint_match(w, pkg_full) or is_hint_match(w, tl_full):
                    has_hint = True
                    break
            if not has_hint:
                for ext in ['.manifest', '.png', '.jpg']:
                    p = diskPath(uid, ext)
                    if p.exists():
                        p.unlink()
                fn = path_name.split(NESTED_SEP)[-1].replace('.apk', '')
                pkg_pattern = RE_PKG.search(fn.lower())
                if pkg_pattern:
                    packageId = pkg_pattern.group(1)
                    title = packageId.split('.')[-1].replace('-', ' ').replace('_', ' ').title()
                else:
                    noise = {'old', 'apk', 'v1', 'v2', 'v3'}
                    parts = [w for w in re.split(r'[\.\-_\s\(\)\[\]/]', fn) if w and w.lower() not in noise]
                    title = (parts[0] if parts else fn).title()
                    packageId = f"com.archive.{title.lower()}"

        title = prettify_title(title, packageId, path_name)

        image_pk = uid
        if packageId in EXCEPTIONS:
            image_pk = EXCEPTIONS[packageId]
            for ext in ['.jpg', '.png']:
                p = diskPath(uid, ext)
                if p.exists(): p.unlink()
        elif packageId and version:
            res = self._db.execute('''
                SELECT image_pk FROM idx 
                WHERE package_id=? AND version=? AND image_pk IS NOT NULL 
                LIMIT 1''', [packageId, version]).fetchone()
            if res:
                potential_img_pk = res[0]
                if diskPath(potential_img_pk, '.jpg').exists():
                    image_pk = potential_img_pk
                    for ext in ['.jpg', '.png']:
                        p = diskPath(uid, ext)
                        if p.exists(): p.unlink()

        self._db.execute('''
            UPDATE idx SET
                done=1, min_sdk=?, platform=?, title=?, package_id=?, version=?, image_pk=?
            WHERE pk=?;''', [
            (minSdk or None),
            None,
            title or None,
            packageId or None,
            version or None,
            image_pk,
            uid,
        ])
        self._db.commit()

    @staticmethod
    def _parseManifest(data: bytes) -> dict:
        manifest = {}
        if AXMLPrinter:
            try:
                ap = AXMLPrinter(data)
                xml = ap.get_xml()
                root = ET.fromstring(xml)
                manifest['package'] = root.get('package') or ''
                manifest['versionName'] = root.get('{http://schemas.android.com/apk/res/android}versionName') or root.get('versionName') or ''
                uses_sdk = root.find('uses-sdk')
                if uses_sdk is not None:
                    minSdk = uses_sdk.get('{http://schemas.android.com/apk/res/android}minSdkVersion') or uses_sdk.get('minSdkVersion')
                    if minSdk:
                        manifest['minSdkVersion'] = int(''.join(filter(str.isdigit, str(minSdk))))
                application = root.find('application')
                if application is not None:
                    lbl = application.get('{http://schemas.android.com/apk/res/android}label') or application.get('label')
                    if lbl:
                        manifest['label'] = lbl
                return manifest
            except Exception:
                pass
        # Fallback crude extraction
        s = str(data)
        if 'package=' in s:
            try:
                manifest['package'] = s.split('package=')[1].split('\\')[0]
            except:
                pass
        if 'versionName=' in s:
            try:
                manifest['versionName'] = s.split('versionName=')[1].split('\\')[0]
            except:
                pass
        if 'minSdkVersion=' in s:
            try:
                sdk_str = s.split('minSdkVersion=')[1].split('\\')[0]
                manifest['minSdkVersion'] = int(''.join(filter(str.isdigit, sdk_str)))
            except:
                pass
        if 'application-label=' in s:
            try:
                manifest['label'] = s.split('application-label=')[1].split('\\')[0]
            except:
                pass
        return manifest


# [add] / metadata listing -> DB

def addNewUrl(url: str, resume: bool = False) -> None:
    DB = CacheDB()
    archiveId = extractArchiveOrgId(url)
    if not archiveId:
        return
    baseUrl = urlForArchiveOrgId(archiveId)
    baseUrlId = DB.insertBaseUrl(baseUrl)
    if not resume:
        print(f'Starting fresh scan for: {url}')
        DB.clearScannedArchives(baseUrlId)
    DB.addToScrapeQueue(url)
    initial_count = DB._db.execute("SELECT COUNT(*) FROM idx WHERE base_url=?", [baseUrlId]).fetchone()[0]
    json_file = pathToListJson(archiveId)
    entries = downloadListArchiveOrg(baseUrlId, archiveId, json_file, force=not resume, resume=resume)
    if entries is None:
        print(f'[ERROR] Could not fetch metadata for {archiveId}. Aborting.')
        return
    DB.insertApkUrls(baseUrlId, entries)
    final_count = DB._db.execute("SELECT COUNT(*) FROM idx WHERE base_url=?", [baseUrlId]).fetchone()[0]
    inserted = final_count - initial_count
    DB.removeFromScrapeQueue(url)
    print(f'new links added: {inserted} of {len(entries)}')


def extractArchiveOrgId(url: str) -> 'str|None':
    match = re_archive_url.match(url)
    if not match:
        print(f'[WARN] not an archive.org url. Ignoring "{url}"', file=stderr)
        return None
    return match.group(1)


def urlForArchiveOrgId(archiveId: str) -> str:
    return f'https://archive.org/download/{archiveId}'


def pathToListJson(archiveId: str, *, tmp: bool = False) -> Path:
    if tmp:
        return CACHE_DIR / 'url_cache' / f'tmp_{archiveId}.json.gz'
    return CACHE_DIR / 'url_cache' / f'{archiveId}.json.gz'


def getNestedApks(url: str, zipPath: str) -> 'list[tuple[str, int, str]]':
    print(f'  peeking into zip: {zipPath}')
    try:
        with RemoteZip(url) as rz:
            return [(f'{zipPath}{NESTED_SEP}{info.filename}', info.file_size, None)
                    for info in rz.infolist()
                    if info.filename.lower().endswith('.apk') and info.file_size > 0]
    except Exception as e:
        print(f'  [WARN] could not peek into zip {zipPath}: {e}', file=stderr)
    return []


def getNestedApksViaViewArchive(url: str, archivePath: str) -> 'list[tuple[str, int, str]]':
    print(f'  peeking into archive (via bridge): {archivePath}')
    try:
        bridge_url = url
        if not bridge_url.endswith('/'):
            bridge_url += '/'
        req = Request(bridge_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urlopen(req) as res:
            html = res.read().decode('utf-8', errors='ignore')
        pattern = r'<tr><td><a [^>]*href="[^"]*">([^<]+)</a><td><td>[^<]*<td [^>]*size">(\d+)</tr>'
        matches = re.findall(pattern, html)
        return [(f'{archivePath}{NESTED_SEP}{name}', int(size), None)
                for name, size in matches
                if name.lower().endswith('.apk') and int(size) > 0]
    except Exception as e:
        print(f'  [WARN] could not peek into archive {archivePath}: {e}', file=stderr)
    return []


def downloadListArchiveOrg(
    baseUrlId: int, archiveId: str, json_file: Path, *, force: bool = False, resume: bool = False
) -> 'list[tuple[str, int, str]]|None':
    if force or not json_file.exists():
        json_file.parent.mkdir(exist_ok=True)
        print(f'load: {archiveId}')
        req = Request(f'https://archive.org/metadata/{archiveId}/files')
        req.add_header('Accept-Encoding', 'deflate, gzip')
        import time
        for attempt in range(3):
            try:
                with urlopen(req) as page:
                    with open(json_file, 'wb') as fp:
                        while True:
                            block = page.read(8096)
                            if not block:
                                break
                            fp.write(block)
                break
            except Exception as e:
                if attempt == 2:
                    print(f'[ERROR] Failed to fetch metadata for {archiveId} after 3 attempts: {e}', file=stderr)
                    return None
                print(f'[WARN] Attempt {attempt+1} failed for {archiveId}: {e}. Retrying...', file=stderr)
                time.sleep(1)

    try:
        with gzip.open(json_file, 'rb') as fp:
            data = json.load(fp)
    except (EOFError, OSError, json.JSONDecodeError) as e:
        print(f'[WARN] Cache file corrupted for {archiveId} ({e}). Re-downloading...', file=stderr)
        if json_file.exists():
            json_file.unlink()
        return downloadListArchiveOrg(baseUrlId, archiveId, json_file, force=True, resume=resume)
    if 'result' not in data:
        if 'error' in data:
            print(f'[ERROR] Archive.org: {data["error"]}', file=stderr)
        return []

    baseUrl = urlForArchiveOrgId(archiveId)
    rv = []
    DB = CacheDB()
    for x in data['result']:
        if x.get('source') != 'original':
            continue
        name = x['name']
        size = int(x.get('size', 0))
        crc = x.get('crc32')

        name_lower = name.lower()
        if name_lower.endswith('.apk'):
            rv.append((name, size, crc))
        elif name_lower.endswith('.zip'):
            if resume and DB.isArchiveScanned(baseUrlId, name, size, crc):
                cached = DB.getNestedApksFromIdx(baseUrlId, name)
                if cached:
                    print(f'  skipping already scanned zip: {name}')
                    rv.extend(cached)
                    continue
            url = f'{baseUrl}/{quote(name)}'
            nested_apks = getNestedApks(url, name)
            DB.insertApkUrls(baseUrlId, nested_apks)
            DB.markArchiveScanned(baseUrlId, name, size, crc)
            rv.extend(nested_apks)
        elif name_lower.endswith(('.rar', '.7z', '.tar', '.tar.gz', '.tgz')) and not name_lower.endswith('_archive.torrent'):
            if resume and DB.isArchiveScanned(baseUrlId, name, size, crc):
                cached = DB.getNestedApksFromIdx(baseUrlId, name)
                if cached:
                    print(f'  skipping already scanned archive: {name}')
                    rv.extend(cached)
                    continue
            url = f'{baseUrl}/{quote(name)}'
            nested_apks = getNestedApksViaViewArchive(url, name)
            DB.insertApkUrls(baseUrlId, nested_apks)
            DB.markArchiveScanned(baseUrlId, name, size, crc)
            rv.extend(nested_apks)
    return rv


# [update] reindex existing caches

def updateUrl(url_or_uid: 'str|int', proc_i: int, proc_total: int):
    baseUrlId, url = _lookupBaseUrl(url_or_uid)
    if not baseUrlId or not url:
        print(f'[ERROR] Ignoring "{url_or_uid}". Not found in DB', file=stderr)
        return

    archiveId = extractArchiveOrgId(url) or ''
    print(f'Updating [{proc_i}/{proc_total}] {archiveId}')

    old_json_file = pathToListJson(archiveId)
    new_json_file = pathToListJson(archiveId, tmp=True)
    old_entries_raw = downloadListArchiveOrg(baseUrlId, archiveId, old_json_file, resume=True)
    new_entries_raw = downloadListArchiveOrg(baseUrlId, archiveId, new_json_file, resume=True)

    if old_entries_raw is None or new_entries_raw is None:
        print(f'  [SKIP] Could not fetch metadata for {archiveId}. Skipping update.')
        DB = CacheDB()
        DB.markBaseUrlUpdated(baseUrlId)
        return

    old_entries = set(old_entries_raw)
    new_entries = set(new_entries_raw)
    old_diff = old_entries - new_entries
    new_diff = new_entries - old_entries

    DB = CacheDB()
    if old_diff or new_diff:
        c_del = c_new = 0
        for old_entry in old_diff:
            uid = DB.getId(baseUrlId, old_entry[0])
            if uid:
                print(f'  rm: [{uid}] {old_entry}')
                DB.setPermanentError(uid)
                c_del += 1
            else:
                print(f'  [ERROR] could not find old entry {old_entry[0]}', file=stderr)
        for new_entry in sorted(new_diff):
            uid = DB.updateApkUrl(baseUrlId, new_entry)
            if uid:
                print(f'  add: [{uid}] {new_entry}')
                c_new += 1
            else:
                print(f'  [ERROR] updating {new_entry[0]}', file=stderr)
        print(f'  updated -{c_del}/+{c_new} entries.')
        os.rename(new_json_file, old_json_file)
    else:
        print('  no changes.')

    DB.markBaseUrlUpdated(baseUrlId)
    if new_json_file.exists():
        os.remove(new_json_file)


def _lookupBaseUrl(url_or_index: 'str|int') -> 'tuple[int|None, str|None]':
    if isinstance(url_or_index, str) and url_or_index.isnumeric():
        url_or_index = int(url_or_index)
    if isinstance(url_or_index, int):
        baseUrlId = url_or_index
        url = CacheDB().getBaseUrlForId(baseUrlId)
    else:
        archiveId = extractArchiveOrgId(url_or_index)
        if not archiveId:
            return None, None
        url = urlForArchiveOrgId(archiveId)
        baseUrlId = CacheDB().getIdForBaseUrl(url)
    return baseUrlId, url


# [run] process pending entries

def processPending():
    processed = 0
    with ThreadPoolExecutor(max_workers=10) as executor:
        while True:
            DB = CacheDB()
            pending = DB.count(done=0)
            batch = DB.getPendingQueue(done=0, batchsize=100)
            del DB
            if not batch:
                print('Queue empty. done.')
                break

            batch = [(processed + i + 1, pending - i - 1, *x)
                     for i, x in enumerate(batch)]

            for uid, res in executor.map(_procSinglePendingWrapper, batch):
                processed += 1
                success, _img_pk = res
                DB = CacheDB()
                fsize = onceReadSizeFromFile(uid)
                if fsize:
                    DB.setFilesize(uid, fsize)
                if success:
                    DB.setDone(uid)
                    print(f'  [DONE] [{uid}]')
                else:
                    DB.setError(uid, done=3)
                    print(f'  [FAILED] [{uid}]')
                del DB
    DB = CacheDB()
    err_count = DB.count(done=3)
    if err_count > 0:
        print()
        print('URLs with Error:', err_count)
        for uid, base, path_name in DB.getPendingQueue(done=3, batchsize=10):
            print(f' - [{uid}] {base}/{quote(path_name)}')


def _procSinglePendingWrapper(args):
    return procSinglePending(*args)


def procSinglePending(
    processed: int, pending: int, uid: int, base_url: str, path_name
) -> 'tuple[int, tuple[bool, int]]':
    full_path = path_name
    display_path = path_name.replace(NESTED_SEP, ' -> ')
    print(f'[{processed}|{pending} queued]: load[{uid}] {display_path}')

    DB = CacheDB()
    url = DB.getUrl(uid)
    del DB

    try:
        return uid, loadApk(uid, url)
    except Exception as e:
        print(f'ERROR: [{uid}] {e}', file=stderr)
    return uid, (False, uid)


def onceReadSizeFromFile(uid: int) -> 'int|None':
    size_path = diskPath(uid, '.size')
    if size_path.exists():
        with open(size_path, 'r') as fp:
            size = int(fp.read())
        os.remove(size_path)
        return size
    return None


# APK processing: extract AndroidManifest.xml + icon

def ensure_jpg(img_path: Path):
    jpg_path = img_path.with_suffix('.jpg')
    if not img_path.exists():
        return False
    try:
        with Image.open(img_path) as im:
            if im.mode in ('RGBA', 'LA'):
                bg = Image.new('RGB', im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                bg.save(jpg_path, format='JPEG', quality=80, optimize=True)
            else:
                im.convert('RGB').save(jpg_path, format='JPEG', quality=80, optimize=True)
        os.chmod(jpg_path, 0o644)
        try:
            img_path.unlink()
        except Exception:
            pass
        return True
    except Exception as e:
        print(f'WARN: could not write JPG for {img_path}: {e}', file=stderr)
        return False


def loadApk(uid: int, url: str, *,
            overwrite: bool = True, image_only: bool = False) -> 'tuple[bool, int]':
    basename = diskPath(uid, '')
    basename.parent.mkdir(exist_ok=True, mode=0o755)
    img_path = basename.with_suffix('.png')
    manifest_path = basename.with_suffix('.manifest')

    if not image_only:
        for ext in ['.manifest', '.png', '.jpg']:
            p = basename.with_suffix(ext)
            if p.exists():
                p.unlink()

    inner_path = None
    for sep in [NESTED_SEP, quote(NESTED_SEP)]:
        if sep in url:
            base_url, inner_path = url.split(sep, 1)
            url = base_url
            inner_path = unquote(inner_path)
            break

    if not inner_path:
        for ext in ['.zip/', '.rar/', '.7z/', '.tar/', '.tar.gz/', '.tgz/']:
            if ext in url.lower():
                idx = url.lower().find(ext) + len(ext) - 1
                base_url = url[:idx]
                inner_path = unquote(url[idx+1:])
                url = base_url
                break

    if inner_path and not url.lower().endswith('.zip'):
        direct_inner_url = f"{url}/{quote(inner_path)}"
        try:
            with tempfile.NamedTemporaryFile(suffix='.apk') as tmp:
                print(f"  downloading inner apk from bridge: {inner_path}")
                req = Request(direct_inner_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urlopen(req) as response:
                    data = response.read(1024)
                    if data.startswith(b'<!DOCTYPE html>') or data.startswith(b'<html>'):
                        print(f"ERROR: [{uid}] bridge returned HTML instead of file", file=stderr)
                        return False, uid
                    with open(tmp.name, 'wb') as f:
                        f.write(data)
                        while True:
                            chunk = response.read(1024*1024)
                            if not chunk:
                                break
                            f.write(chunk)
                import zipfile
                try:
                    with zipfile.ZipFile(tmp.name) as zip:
                        return _processApkZip(uid, zip, basename, img_path, manifest_path, image_only)
                except zipfile.BadZipFile:
                    print(f"ERROR: [{uid}] downloaded file is not a valid zip", file=stderr)
                    return False, uid
        except Exception as e:
            print(f"ERROR: [{uid}] could not download/process inner apk: {e}", file=stderr)
            return False, uid

    try:
        with RemoteZip(url) as outer_zip:
            if inner_path:
                import zipfile
                with tempfile.NamedTemporaryFile(suffix='.apk') as tmp:
                    print(f"  extracting nested apk to temp: {inner_path}")
                    with outer_zip.open(inner_path) as src:
                        while True:
                            buf = src.read(1024*1024)
                            if not buf:
                                break
                            tmp.write(buf)
                    tmp.flush()
                    with zipfile.ZipFile(tmp.name) as zip:
                        return _processApkZip(uid, zip, basename, img_path, manifest_path, image_only)
            else:
                if USE_ZIP_FILESIZE:
                    filesize = outer_zip.fp.tell() if outer_zip.fp else 0
                    with open(basename.with_suffix('.size'), 'w') as fp:
                        fp.write(str(filesize))
                return _processApkZip(uid, outer_zip, basename, img_path, manifest_path, image_only)
    except Exception as e:
        if '404' in str(e):
            print(f"ERROR: [{uid}] File not found (404): {url}", file=stderr)
        elif "File is not a zip file" in str(e):
            print(f"  [ERROR] [{uid}] BadZipFile: {url} is not a valid zip.", file=stderr)
        else:
            print(f"ERROR: [{uid}] connection failed: {e}", file=stderr)
    return False, uid


def _processApkZip(uid: int, zip, basename, img_path, manifest_path, image_only) -> 'tuple[bool, int]':
    artwork = False
    used_image_pk = uid
    zip_listing = zip.infolist()

    # Extract AndroidManifest.xml
    for entry in zip_listing:
        fn = entry.filename.lstrip('/')
        if fn == MANIFEST_NAME:
            extractZipEntry(zip, entry, manifest_path)
            break

    # Icon heuristics: scan res/ for PNGs, prefer ic_launcher/launcher/icon
    if not image_only:
        preferred = []
        for entry in zip_listing:
            fn = entry.filename.lstrip('/')
            if '/res/' in fn and fn.lower().endswith('.png'):
                name = fn.split('/')[-1].lower()
                score = 100
                if 'ic_launcher' in name:
                    score = 1
                elif 'launcher' in name:
                    score = 2
                elif 'icon' in name:
                    score = 3
                preferred.append((score, entry))
        preferred.sort(key=lambda x: x[0])
        for _score, entry in preferred:
            extractZipEntry(zip, entry, img_path)
            if img_path.exists() and img_path.stat().st_size > 8:
                if processImage(img_path):
                    artwork = True
                    break
                else:
                    try:
                        img_path.unlink()
                    except Exception:
                        pass

        if not artwork:
            for entry in zip_listing:
                fn = entry.filename.lstrip('/')
                if fn.lower().endswith('.png') and not fn.startswith('META-INF'):
                    extractZipEntry(zip, entry, img_path)
                    if img_path.exists() and img_path.stat().st_size > 8:
                        if processImage(img_path):
                            artwork = True
                            break
                        else:
                            try:
                                img_path.unlink()
                            except Exception:
                                pass

    if not manifest_path.exists():
        print(f'ERROR: [{uid}] apk has no "{MANIFEST_NAME}"', file=stderr)

    if artwork and not manifest_path.exists():
        # still return true for image-only calls
        return True, used_image_pk

    return manifest_path.exists(), used_image_pk


def extractZipEntry(zip: 'RemoteZip', zipInfo: 'ZipInfo', dest_filename: Path):
    import time
    for attempt in range(3):
        try:
            with zip.open(zipInfo) as src:
                data = src.read()
                if data and (data.startswith(b'<!DOCTYPE html>') or data.startswith(b'<html>')):
                    print(f'  [WARN] detected HTML 404 instead of data for {zipInfo.filename}', file=stderr)
                    return
                if data and data.startswith(b'\x00' * 32):
                    print(f'  [WARN] detected zero-filled data for {zipInfo.filename}', file=stderr)
                    return
                if data and data.startswith(b'PK\x03\x04'):
                    print(f'  [WARN] detected ZIP header instead of data for {zipInfo.filename}', file=stderr)
                    return
                if data:
                    with open(dest_filename, 'wb') as tgt:
                        tgt.write(data)
                    return
        except Exception as e:
            print(f'  [WARN] attempt {attempt+1} failed to extract {zipInfo.filename}: {e}', file=stderr)
            if '500' in str(e) or '503' in str(e):
                time.sleep(1)
                continue
            break


def processImage(png_path: Path) -> bool:
    if not png_path.exists() or png_path.stat().st_size < 8:
        return False

    with open(png_path, 'rb') as f:
        header = f.read(32)
        if header.startswith(b'\x00' * 8):
            return False

    if b'CgBI' in header:
        try:
            subprocess.run([str(PNGDEFRY_BIN), '-s', '-free', str(png_path)],
                           check=True, capture_output=True)
            fixed_path = png_path.with_name(png_path.stem + "-free.png")
            if fixed_path.exists():
                fixed_path.replace(png_path)
        except Exception as e:
            print(f"  [WARN] pngdefry failed: {e}", file=stderr)

    try:
        jpg_path = png_path.with_suffix('.jpg')
        with Image.open(png_path) as img:
            if img.mode != 'RGB':
                img = img.convert('RGB')
            MAX_SIZE = (128, 128)
            if img.width > MAX_SIZE[0] or img.height > MAX_SIZE[1]:
                img.thumbnail(MAX_SIZE, Image.Resampling.LANCZOS)
            img.save(jpg_path, 'JPEG', quality=80, optimize=True)
            os.chmod(jpg_path, 0o644)
        try:
            png_path.unlink()
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"  [WARN] PIL conversion/optimization failed for {png_path}: {e}", file=stderr)
        return False


# Helper icon selection functions (kept/adapted)

RESOLUTION_ORDER = ['xxxhdpi', 'xxhdpi', 'xhdpi', 'hdpi', 'mdpi', 'ldpi', '3x', '2x', '180', '167', '152', '120']

def resolutionIndex(icon_name: str):
    penalty = 0
    if 'small' in icon_name.lower() or icon_name.lower().startswith('default'):
        penalty = 10
    for i, match in enumerate(RESOLUTION_ORDER):
        if match in icon_name:
            return i + penalty
    return 50 + penalty

def sortedByResolution(icons: 'list[str]') -> 'list[str]':
    if not isinstance(icons, list):
        if isinstance(icons, str):
            icons = [icons]
        else:
            return []
    icons = [str(x) for x in icons if x]
    icons.sort(key=resolutionIndex)
    return icons

# Export JSON (applies to site)

def parse_version(v: str) -> list:
    if not v:
        return []
    v = str(v).split(' ')[0].split('-')[0]
    return [int(x) for x in re.findall(r'\d+', v)]

def normalize_title(t: str) -> str:
    if not t:
        return ''
    return re.sub(r'[^a-z0-9]', '', t.lower())

def export_json():
    DB = CacheDB()
    url_map = DB.jsonUrlMap()
    maxUrlId = max(url_map.keys()) if url_map else 0
    maxUrlId += 1
    url_map[maxUrlId] = '---'
    submap = {}
    total = DB.count(done=1)

    entries = []
    print(f'Collecting {total} entries...')
    for i, entry in enumerate(DB.enumJsonApk(done=1)):
        if i % 1000 == 0:
            print(f'\rcollected [{i}/{total}]', end='')
        entry = list(entry)
        path_name = entry[7].replace(NESTED_SEP, '/')
        entry[7] = path_name
        if '/' in entry[7]:
            baseurl = url_map[entry[6]]
            sub_dir, sub_file = entry[7].rsplit('/', 1)
            newurl = baseurl + '/' + sub_dir
            subIdx = submap.get(newurl, None)
            if subIdx is None:
                maxUrlId += 1
                submap[newurl] = maxUrlId
                subIdx = maxUrlId
            entry[6] = subIdx
            entry[7] = sub_file
        entries.append(entry)
    print(f'\rcollected [{total}/{total}] done.')

    print('Sorting entries...')
    entries.sort(key=lambda x: (
        normalize_title(x[3] or ''),
        x[4] or '',
        parse_version(x[5]),
        x[1] or 0
    ))

    print(f'Writing {CACHE_DIR / "apk.json"}...')
    with open(CACHE_DIR / 'apk.json', 'w') as fp:
        fp.write('[')
        for i, entry in enumerate(entries):
            fp.write(json.dumps(entry, separators=(',', ':')))
            if i < len(entries) - 1:
                fp.write(',\n')
        fp.write(']')
    print(f'write apk.json: {len(entries)} entries')

    for newurl, newidx in submap.items():
        url_map[newidx] = newurl
    with open(CACHE_DIR / 'urls.json', 'w') as fp:
        fp.write(json.dumps(url_map, separators=(',\n', ':'), sort_keys=True))
    print(f'write urls.json: {len(url_map)} entries')

def export_filesize():
    ignored = 0
    written = 0
    for i, (uid, fsize) in enumerate(CacheDB().enumFilesize()):
        size_path = diskPath(uid, '.size')
        if not size_path.exists():
            with open(size_path, 'w') as fp:
                fp.write(str(fsize))
            written += 1
        else:
            ignored += 1
        if i % 113 == 0:
            print(f'\r{written} files written. {ignored} ignored', end='')
    print(f'\r{written} files written. {ignored} ignored. done.')


# Helpers

def diskPath(uid: int, ext: str) -> Path:
    return CACHE_DIR / str(uid // 1000) / f'{uid}{ext}'

def printProgress(blocknum, bs, size):
    if size == 0:
        return
    percent = (blocknum * bs) / size
    done = "#" * int(40 * percent)
    print(f'\r[{done:<40}] {percent:.1%}', end='')

if __name__ == '__main__':
    main()
