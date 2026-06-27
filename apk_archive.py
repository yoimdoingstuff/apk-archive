#!/usr/bin/env python3
from typing import TYPE_CHECKING, Iterable
from multiprocessing import Pool
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen, urlretrieve
from argparse import ArgumentParser
from sys import stderr
import zipfile
import sqlite3
import json
import gzip
import os
import re

# try to import optional libs
try:
    from axmlparserpy.axmlprinter import AXMLPrinter
except Exception:
    AXMLPrinter = None

try:
    from PIL import Image
except Exception:
    Image = None

import xml.etree.ElementTree as ET

import warnings
with warnings.catch_warnings():  # hide macOS LibreSSL warning
    warnings.filterwarnings('ignore')
    from remotezip import RemoteZip  # pip install remotezip

if TYPE_CHECKING:
    from zipfile import ZipInfo

USE_ZIP_FILESIZE = False
re_manifest = re.compile(r'AndroidManifest.xml')
re_archive_url = re.compile(
    r'https?://archive.org/(?:metadata|details|download)/([^/]+)(?:/.*)?')
CACHE_DIR = Path(__file__).parent / 'data'
CACHE_DIR.mkdir(exist_ok=True)

ANDROID_NS = 'http://schemas.android.com/apk/res/android'
A_ANDROID = '{' + ANDROID_NS + '}'


def main():
    CacheDB().init()
    parser = ArgumentParser()
    cli = parser.add_subparsers(metavar='command', dest='cmd', required=True)

    cmd = cli.add_parser('add', help='Add urls to cache')
    cmd.add_argument('urls', metavar='URL', nargs='+',
                     help='Search URLs for .apk links')

    cmd = cli.add_parser('update', help='Update all urls')
    cmd.add_argument('urls', metavar='URL', nargs='*', help='URLs or index')

    cmd = cli.add_parser('run', help='Download and process pending urls')
    cmd.add_argument('-force', '-f', action='store_true',
                     help='Reindex local data / populate DB.'
                     'Make sure to export fsize before!')
    cmd.add_argument('pk', metavar='PK', type=int,
                     nargs='*', help='Primary key')

    cmd = cli.add_parser('export', help='Export data')
    cmd.add_argument('export_type', choices=['json', 'fsize'],
                     help='Export to json or temporary-filesize file')

    cmd = cli.add_parser('err', help='Handle problematic entries')
    cmd.add_argument('err_type', choices=['reset'], help='Set done=0 to retry')

    cmd = cli.add_parser('get', help='Lookup value')
    cmd.add_argument('get_type', choices=['url', 'img', 'apk'],
                     help='Get data field or download image.')
    cmd.add_argument('pk', metavar='PK', type=int,
                     nargs='+', help='Primary key')

    cmd = cli.add_parser('set', help='(Re)set value')
    cmd.add_argument('set_type', choices=['err'], help='Data field/column')
    cmd.add_argument('pk', metavar='PK', type=int,
                     nargs='+', help='Primary key')

    args = parser.parse_args()

    if args.cmd == 'add':
        for url in args.urls:
            addNewUrl(url)
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
                loadApk(pk, url, overwrite=True)
        else:
            if args.force:
                print('Resetting done state ...')
                DB.setAllUndone(whereDone=1)
            processPending()

    elif args.cmd == 'err':
        if args.err_type == 'reset':
            print('Resetting error state ...')
            CacheDB().setAllUndone(whereDone=3)

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


###############################################
# Database
###############################################

class Cache

