#!/usr/bin/env python3
"""Build a small personal recommendation index from IMDb's official public TSV datasets."""
import datetime
import fcntl
import gzip
import io
import os
from pathlib import Path
import sqlite3
import sys
import time
import urllib.request


def records(name):
    url = 'https://datasets.imdbws.com/'+name+'.tsv.gz'
    with urllib.request.urlopen(url,timeout=90) as response:
        with gzip.GzipFile(fileobj=response) as stream:
            with io.TextIOWrapper(stream,encoding='utf-8',errors='replace') as text:
                header = next(text).rstrip('\n').split('\t')
                for line in text:
                    fields = line.rstrip('\n').split('\t')
                    if len(fields)==len(header):
                        yield dict(zip(header,fields))


def build(directory, ratings=None, basics=None):
    directory.mkdir(parents=True,exist_ok=True)
    path = directory/'catalog.sqlite3'
    if ratings is None and path.exists() and time.time()-path.stat().st_mtime<20*3600:
        print('IMDb recommendation catalogue already available.',flush=True)
        return
    print('Building IMDb recommendation catalogue. The current bot can keep running.',flush=True)
    scores = {}
    for row in (records('title.ratings') if ratings is None else ratings):
        try:
            score,votes = float(row['averageRating']),int(row['numVotes'])
            if 6.5<=score<=10 and votes>=10000:
                scores[row['tconst']] = (score,votes)
        except (ValueError,KeyError):
            continue
    print('Ratings loaded. Reading movie titles and genres…',flush=True)
    temporary = directory/f'catalog.{os.getpid()}.tmp'
    try:
        db = sqlite3.connect(temporary)
        db.execute('CREATE TABLE movies (imdb TEXT PRIMARY KEY, title TEXT, original TEXT, year INTEGER, '
                   'runtime INTEGER, genres TEXT, kind TEXT, score REAL, votes INTEGER)')
        db.execute('CREATE TABLE meta (name TEXT PRIMARY KEY, value TEXT)')
        count = 0
        for row in (records('title.basics') if basics is None else basics):
            iid = row.get('tconst')
            if iid not in scores or row.get('titleType') not in ('movie','tvMovie') or row.get('isAdult')!='0':
                continue
            try:
                year = int(row['startYear'])
                if year>datetime.date.today().year:
                    continue
                runtime = int(row['runtimeMinutes']) if row.get('runtimeMinutes','').isdigit() else 0
                genres = ','+','.join(g.strip() for g in row.get('genres','').split(',') if g and g!='\\N')+','
                db.execute('INSERT OR REPLACE INTO movies VALUES (?,?,?,?,?,?,?,?,?)',
                           (iid,row['primaryTitle'],row['originalTitle'],year,runtime,genres,row['titleType'],*scores[iid]))
                count += 1
            except (ValueError,KeyError):
                continue
        if count<1:
            raise ValueError('No qualifying movies in dataset')
        db.execute('INSERT INTO meta VALUES (?,?)',('built',datetime.datetime.now(datetime.timezone.utc).isoformat()))
        db.execute('INSERT INTO meta VALUES (?,?)',('source','https://data.imdb.com/non-commercial-datasets/'))
        db.commit()
        db.close()
        temporary.chmod(0o600)
        temporary.replace(path)
        print(f'IMDb catalogue ready: {count:,} rated movies. No movie files were changed.',flush=True)
    finally:
        if temporary.exists():
            temporary.unlink()


if __name__=='__main__':
    os.umask(0o077)
    directory = Path(sys.argv[1]) if len(sys.argv)>1 else Path.home()/'.local/state/media-request-bot'
    directory.mkdir(parents=True,exist_ok=True)
    with open(directory/'catalog.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:
            build(directory)
        except Exception as error:
            raise SystemExit('Catalogue build failed ('+type(error).__name__+'). Existing bot and files were left in place.') from None
