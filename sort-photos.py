#!/usr/bin/env python3

"""
photosorter - https://github.com/dbader/photosorter
---------------------------------------------------

A little Python daemon to keep my photos organized on Dropbox.

It watches a *source directory* for modifications and moves new image
files to a *target directory* depending on when the photo was taken,
using EXIF data and creation date as a fallback.

Inspired by
    - http://simplicitybliss.com/exporting-your-iphoto-library-to-dropbox/
    - https://github.com/wting/exifrenamer
    - http://chambersdaily.com/learning-to-love-photo-management/

"""
import collections
import datetime
import hashlib
import os
import re
import shutil
import sys
import time

import exifread
import glob

import socket # gethostname
import logging

logging.basicConfig(format='[%(asctime)s] %(levelname)s %(message)s', datefmt='%Y%m%d %H:%M:%S')

class HashCache(object):
    """
    Gives a quick answer to the question if there's an identical file
    in the given target folder.

    """
    def __init__(self):
        # folder -> (hashes, filename -> hash)
        self.hashes = collections.defaultdict(lambda: (set(), dict()))

    def has_file(self, target_folder, path):
        # Strip trailing slashes etc.
        target_folder = os.path.normpath(target_folder)

        # Update the cache by ensuring that we have the hashes of all
        # files in the target folder. `_add_file` is smart enough to
        # skip any files we already hashed.
        for f in self._files_in_folder(target_folder):
            self._add_file(f)

        # Hash the new file at `path`.
        file_hash = self._hash(path)

        # Check if we already have an identical file in the target folder.
        return file_hash in self.hashes[target_folder][0]

    def _add_file(self, path):
        # Bail out if we already have a hash for the file at `path`.
        folder = self._target_folder(path)
        if path in self.hashes[folder][1]:
            return

        file_hash = self._hash(path)

        basename = os.path.basename(path)
        self.hashes[folder][0].add(file_hash)
        self.hashes[folder][1][basename] = file_hash

    @staticmethod
    def _hash(path):
        hasher = hashlib.sha1()
        with open(path, 'rb') as f:
            data = f.read()
            hasher.update(data)
        return hasher.hexdigest()

    @staticmethod
    def _target_folder(path):
        return os.path.dirname(path)

    @staticmethod
    def _files_in_folder(folder_path):
        """
        Iterable with full paths to all files in `folder_path`.

        """
        try:
            names = (
                os.path.join(folder_path, f) for f in os.listdir(folder_path)
            )
            return [f for f in names if os.path.isfile(f)]
        except OSError:
            return []


hash_cache = HashCache()


def move_file(root_folder, path):
    if not os.path.exists(path):
        print("Invalid path:", path)
        return

    if not is_valid_filename(path):
        print("Invalid path:", path)
        return

    dst = dest_path(root_folder, path)
    dirs = os.path.dirname(dst)

    #if hash_cache.has_file(dirs, path):
    #    print('%s is a duplicate, skipping' % path)
    #    return

    try:
        os.makedirs(dirs)
        print('Created folder %s' % dirs)
    except OSError as e:
        # Catch "File exists"
        if e.errno != 17:
            raise e

    if dst.find("2012-02-28") > 0:
        print("Skip %s %s (shoot on 2012-02-28)" % (path, dst))
    else:
        print('Moving %s to %s' % (path, dst))
        shutil.move(path, dst)
        aaefile = path[:-3] + "AAE"
        if os.path.isfile(aaefile):
            shutil.move(aaefile, dst + ".AAE")


def resolve_duplicate(path):
    if not os.path.exists(path):
        return path

    basename = os.path.basename(path)
    filename, ext = os.path.splitext(basename)
    dirname = os.path.dirname(path)
    dedup_index = 1

    while True:
        new_fname = '%s-%i%s' % (filename, dedup_index, ext)
        new_path = os.path.join(dirname, new_fname)
        if not os.path.exists(new_path):
            # print('Deduplicating %s to %s' % (path, new_path))
            break
        dedup_index += 1

    return new_path


def is_valid_filename(path):
    ext = os.path.splitext(path)[1].lower()
    return ext in ['.jpg', '.jpeg', '.png', '.dng', '.crw', '.pef', ".tif", ".heic", ".mov", ".mp4"]


def dest_path(root_folder, path):
    cdate = creation_date(path)
    path = path_from_datetime(root_folder, cdate, path)
    if path.endswith(".jpeg"): path = path[:-4] + "jpg"
    return resolve_duplicate(path)


def path_from_datetime(root_folder, dt, path):
    folder = folder_from_datetime(dt)
    filename = filename_from_datetime(dt, path)
    return os.path.join(root_folder, folder, filename)


def folder_from_datetime(dt):
    return dt.strftime('%Y' + os.sep + '%Y-%m')


def filename_has_14digit(basename):
    bb = re.sub("[^A-Za-z0-9]", "", basename)
    bb0 = re.sub("[^0-9]", "", basename)
    if bb0.startswith("20") and re.search(r"20[0-9]{12}", bb):
        return bb
    # logging.warning(f"{bb} no 14 digits")
    return None

def filename_from_datetime(dt, path):
    """
    Returns basename + original path. e.g. 2017-11-11_11.11.11_file.jpg

    """
    basename = os.path.basename(path)
    filename, ext = os.path.splitext(basename)
    alfdigit_name = filename_has_14digit(filename)

    base = basename_from_datetime(dt)
    #ext = os.path.splitext(path)[1]
    if filename.find(base) == 0:
        return filename + ext.lower()
    elif alfdigit_name:
        return base + "__" + alfdigit_name + ext.lower()

    return base + "_" + filename + ext.lower()


def basename_from_datetime(dt):
    """
    Returns a string formatted like this '2004-05-07_20.16.31'.

    """
    return dt.strftime('%Y-%m-%d_%H.%M.%S')


def creation_date(path):
    if path.lower().endswith(".mov"):
        return mov_creation_date(path)

    try:
        exif_date = exif_creation_date(path)
        if exif_date:
            return exif_date
    except ValueError as e:
        print(e)
    return file_creation_date(path)


def file_creation_date(path):
    """
    Use mtime as creation date because ctime returns the
    the time when the file's inode was last modified; which is
    wrong and almost always later.

    """
    mtime = os.path.getmtime(path)
    return datetime.datetime.fromtimestamp(mtime)

def mov_creation_date(path):
    import ffmpeg

    dtstr = None
    try:
        for info in ffmpeg.probe(path)["streams"]:
            dtstr = info["tags"].get("creation_time", None)
            if dtstr: break
    except:
        # return None
        logging.error(f"invalid date {path}")
        raise

    from dateutil import parser, tz
    ts = parser.parse(dtstr).astimezone(tz.tzlocal())
    if int(ts.strftime("%Y%m%d")) <= 2001:
        logging.warning(f"{ts} is too old, invalid. {path}")
        return None
    return ts

def mp4_creation_date(path):
    import ffmpeg
    info = ffmpeg.probe(path)["streams"][0]
    dtstr = info["tags"]["creation_time"]
    from dateutil import parser, tz
    ts = parser.parse(dtstr).astimezone(tz.tzlocal())
    if int(ts.strftime("%Y%m%d")) <= 2001:
        logging.warning(f"{ts} is too old, invalid. {path}")
        return None
    return ts

def exif_creation_date(path):
    try:
        ts = exif_creation_timestamp(path)
    except MissingExifTimestampError as e:
        print(e)
        return None

    try:
        return exif_timestamp_to_datetime(ts)
    except BadExifTimestampError:
        print(e)
        return None


class BadExifTimestampError(Exception):
    pass


class MissingExifTimestampError(Exception):
    pass


def exif_creation_timestamp(path):
    with open(path, 'rb') as f:
        tags = exifread.process_file(f, details=False)

    if 'EXIF DateTimeOriginal' in tags:
        return str(tags['EXIF DateTimeOriginal'])
    elif 'EXIF DateTimeDigitized' in tags:
        return str(tags['EXIF DateTimeDigitized'])

    raise MissingExifTimestampError()


def exif_timestamp_to_datetime(ts):
    elements = [int(_) for _ in re.split(':| ', ts)]

    if len(elements) != 6:
        raise BadExifTimestampError

    return datetime.datetime(*elements)


def _files_in_folder(folder_path):
    """
    Iterable with full paths to all files in `folder_path`.
    """
    try:
        names = (
            os.path.join(folder_path, f) for f in os.listdir(folder_path)
        )
        return [f for f in names if os.path.isfile(f)]
    except OSError:
        return []

def run_dirs(dest_folder, src_folder):
    for root, dirs, files in os.walk(src_folder):
        for f in files:
            if root.find("@eaDir") >= 0: continue
            #print(f)
            move_file(dest_folder, os.path.join(root, f))
    
if __name__ == '__main__':
    #for root, dirs, files in os.walk("2011-09"):
    #src_folder = "2013-hean-iphone"
    #for src_folder in glob.glob("hz*"):
    #    run(".", src_folder)
    #for p in _files_in_folder(src_folder):
    #    move_file(".", p)
    #run(".", "Z:/lyyang/Mac Air/Disney")
    #run(".", "_not_sorted")
    #run(".", "D:/photo")
    #run(".", "/media/lyyang/Data2/transfer/Camera/")
    if socket.gethostname() != "lagrange":
        raise RuntimeError("must run on 'lagrange'")

    dest_dir = "/volume3/photo" # "/mnt/photo"
    if not os.path.isdir(dest_dir):
        raise RuntimeError(f"{dest_dir} must be a dir")

    if len(sys.argv) == 2 and os.path.isdir(sys.argv[1]):
        print("running {} -> {}".format(sys.argv[1], dest_dir))
        run_dirs(dest_dir, sys.argv[1])

    elif os.path.isfile(sys.argv[1]):
        for fname in sys.argv[1:]:
            # print("moving file {}".format(fname))
            move_file(dest_dir, fname)
    else:
        print("Invalid options {} {}".format(len(sys.argv), sys.argv[1]))
            
            

