# This file is part of Cockpit.
#
# Copyright (C) 2022 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


import collections
import hashlib
import json
import fnmatch
import logging
import mimetypes
import os
import zipfile

from pathlib import Path
from systemd_ctypes import bus

from . import config

VERSION = '300'
logger = logging.getLogger('cockpit.packages')


# Sorting is important because the checksums are dependent on the order we
# visit these.  This is not the same as sorting on the full pathname, since
# each directory component is considered separately.  We could split the path
# and sort on that, but it ends up not being particularly elegant.
# Changing the approach here will change the checksum.
# We're allowed to change it as we wish, but let's try to match behaviour with
# cockpitpackages.c for the time being, since the unit tests depend on it.
def directory_items(path):
    return sorted(path.iterdir(), key=lambda item: item.name)


class Package:
    def __init__(self, path):
        self.path = path

        with (self.path / 'manifest.json').open(encoding='utf-8') as manifest_file:
            self.manifest = json.load(manifest_file)

        self.try_override(self.path / 'override.json')
        self.try_override(config.ETC_COCKPIT / f'{path.name}.override.json')
        self.try_override(config.DOT_CONFIG_COCKPIT / f'{path.name}.override.json')

        if 'name' in self.manifest:
            self.name = self.manifest['name']
        else:
            self.name = path.name

        self.content_security_policy = None
        self.priority = self.manifest.get('priority', 1)
        self.bridges = self.manifest.get('bridges', [])

        self.files = set()
        for file in self.path.rglob('*'):
            self.files.add(file.relative_to(self.path))

        self.version = Package.sortify_version(VERSION)

    def try_override(self, path: Path) -> None:
        try:
            with path.open(encoding='utf-8') as override_file:
                override = json.load(override_file)
            self.manifest = self.merge_patch(self.manifest, override)
        except FileNotFoundError:
            # This is the expected usual case
            pass
        except json.JSONDecodeError as exc:
            # User input error: report a warning
            logger.warning('%s: %s', path, exc)

    # https://www.rfc-editor.org/rfc/rfc7386
    @staticmethod
    def merge_patch(target: object, patch: object) -> object:
        # Loosely based on example code from the RFC
        if not isinstance(patch, dict):
            return patch

        # Always take a copy ('result') — we never modify the input ('target')
        result = dict(target if isinstance(target, dict) else {})
        for name, value in patch.items():
            if value is not None:
                result[name] = Package.merge_patch(result[name], value)
            else:
                result.pop(name)
        return result

    @staticmethod
    def sortify_version(version: str) -> str:
        '''Convert a version string to a form that can be compared'''
        # 0-pad each numeric component.  Only supports numeric versions like 1.2.3.
        return '.'.join(part.zfill(8) for part in version.split('.'))

    def walk(self, checksums, path=None):
        if not path:
            path = self.path

        for item in directory_items(path):
            if item.is_dir():
                self.walk(checksums, item)
            elif item.is_file():
                rel = item.relative_to(path)
                with item.open('rb') as file:
                    data = file.read()
                sha = hashlib.sha256(data).hexdigest()
                for context in checksums:
                    context.update(f'{rel}\0{sha}\0'.encode('ascii'))

    def check(self, at_least_prio):
        if 'requires' in self.manifest:
            requires = self.manifest['requires']
            if any(package != 'cockpit' for package in requires):
                return False

            if 'cockpit' in requires and self.version < Package.sortify_version(requires['cockpit']):
                return False

        if at_least_prio is not None:
            if 'priority' not in self.manifest:
                return False

            if self.manifest['priority'] <= at_least_prio:
                return False

        return True

    @staticmethod
    def filename_variants(filename, locale):
        base, _, ext = filename.rpartition('.')

        while base:
            if locale:
                yield f'{base}.{locale}.{ext}'
                yield f'{base}.{locale}.{ext}.gz'

                if '_' in locale:
                    language, _, _ = locale.partition(' ')
                    yield f'{base}.{language}.{ext}'
                    yield f'{base}.{language}.{ext}.gz'

            yield f'{base}.{ext}'
            yield f'{base}.min.{ext}'
            yield f'{base}.{ext}'
            yield f'{base}.{ext}.gz'
            yield f'{base}.min.{ext}.gz'

            base, _, _stripped_ext = base.rpartition('.')

    def negotiate_file(self, path, headers):
        dirname, sep, filename = path.rpartition('/')
        locale = headers.get('Accept-Language', '').split(',')[0].strip()  # obviously fix this

        for variant in self.filename_variants(filename, locale):
            logger.debug('consider variant %s', variant)
            if Path(f'{dirname}{sep}{variant}') in self.files:
                return f'{dirname}{sep}{variant}'

        return None

    def get_content_security_policy(self, origin):
        assert origin.startswith('http')
        origin_ws = origin.replace('http', 'ws', 1)

        # fix me: unit tests depend on the specific order
        policy = collections.OrderedDict({
            "default-src": f"'self' {origin}",
            "connect-src": f"'self' {origin} {origin_ws}",
            "form-action": f"'self' {origin}",
            "base-uri": f"'self' {origin}",
            "object-src": "'none'",
            "font-src": f"'self' {origin} data:",
            "img-src": f"'self' {origin} data:",
        })

        manifest_policy = self.manifest.get('content-security-policy', '')
        for item in manifest_policy.split(';'):
            item = item.strip()
            if item:
                key, _, value = item.strip().partition(' ')
                policy[key] = value

        return ' '.join(f'{k} {v};' for k, v in policy.items()) + ' block-all-mixed-content'

    def serve_file(self, path, channel):
        filename = self.negotiate_file(path, channel.headers)

        if filename is None:
            logger.debug('path %s not in %s', path, self.files)
            channel.http_error(404, 'Not found')
            return

        content_type, encoding = mimetypes.guess_type(filename)

        with (self.path / filename).open('rb') as file:
            headers = {
                "Access-Control-Allow-Origin": channel.origin,
                "Content-Encoding": encoding,
            }
            if content_type is not None and content_type.startswith('text/html'):
                headers['Content-Security-Policy'] = self.get_content_security_policy(channel.origin)
            channel.http_ok(content_type, headers)
            channel.send_data(file.read())


class ZipPathPolyfill(zipfile.Path):
    def all_the_things(self, glob):
        if self.is_file() and fnmatch.fnmatch(self.name, glob):
            yield self
        elif self.is_dir():
            for item in self.iterdir():
                yield from item.all_the_things(glob)

    def rglob(self, glob):
        assert '/' not in glob
        yield from self.all_the_things(glob)

    def relative_to(self, path):
        x = str(self)
        y = str(path)
        assert x.startswith(y)
        return Path(x[len(y):])


# TODO: This doesn't yet implement the slighty complicated checksum
# scheme of the C bridge (see cockpitpackages.c) to support caching
# and reloading.

class Packages(bus.Object, interface='cockpit.Packages'):
    manifests = bus.Interface.Property('s', value="{}")

    def __init__(self):
        super().__init__()
        self.packages = {}
        self.load_packages()

        # Reloading the Shell in the browser should reload the
        # packages.  This is implemented by having the Shell call
        # reload_hint whenever it starts.  The first call of this
        # method in each session is ignored so that packages are not
        # loaded twice right after logging in.
        #
        self.saw_first_reload_hint = False

    def show(self):
        for name in sorted(self.packages):
            package = self.packages[name]
            menuitems = ''
            print(f'{name:20} {menuitems:40} {package.path}')
        if self.checksum:
            print(f'checksum = {self.checksum}')

    def try_packages_dir(self, path, checksums):
        try:
            items = directory_items(path)
        except FileNotFoundError:
            return

        for item in items:
            if item.is_dir():
                try:
                    package = Package(item)
                except (FileNotFoundError, json.JSONDecodeError):
                    # ignore packages with broken/empty manifests
                    continue

                if package.name in self.packages:
                    at_least_prio = self.packages[package.name].priority
                else:
                    at_least_prio = None

                if package.check(at_least_prio):
                    self.packages[package.name] = package
                    package.walk(checksums)

    def load_packages(self):
        checksums = []

        xdg_data_home = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
        self.try_packages_dir(Path(xdg_data_home) / 'cockpit', checksums)

        # we only checksum the system content if there's no user content
        if not self.packages:
            checksums.append(hashlib.sha256())

        if hasattr(__spec__.loader, 'archive'):
            root = ZipPathPolyfill(zipfile.ZipFile(__spec__.loader.archive, 'r'))
            self.try_packages_dir(root / 'dist', checksums)

        xdg_data_dirs = os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share')
        for xdg_dir in xdg_data_dirs.split(':'):
            self.try_packages_dir(Path(xdg_dir) / 'cockpit', checksums)

        if checksums:
            self.checksum = checksums[0].hexdigest()
        else:
            self.checksum = None

        self.manifests = json.dumps({name: package.manifest for name, package in self.packages.items()})

    def serve_manifests_js(self, channel):
        channel.http_ok('text/javascript')
        channel.send_data(("""
            (function (root, data) {
                if (typeof define === 'function' && define.amd) {
                    define(data);
                }

                if (typeof cockpit === 'object') {
                    cockpit.manifests = data;
                } else {
                    root.manifests = data;
                }
            }(this, """ + self.manifests + """))""").encode('ascii'))

    def serve_package_file(self, path, channel):
        package, _, package_path = path[1:].partition('/')
        self.packages[package].serve_file(package_path, channel)

    def serve_checksum(self, channel):
        channel.http_ok('text/plain')
        channel.send_data(self.checksum.encode('ascii'))

    def serve_file(self, path, channel):
        assert path[0] == '/'

        if self.checksum is not None:
            channel.push_header('X-Cockpit-Pkg-Checksum', self.checksum)
        else:
            channel.push_header('Cache-Control', 'no-cache, no-store')

        if path == '/manifests.js':
            self.serve_manifests_js(channel)
        elif path == '/checksum':
            self.serve_checksum(channel)
        elif '*' in path:
            channel.http_error(404, "Not Found")
        else:
            self.serve_package_file(path, channel)

    def get_bridges(self):
        bridges = []
        for package in sorted(self.packages.values(), key=lambda package: -package.priority):
            bridges.extend(package.bridges)
        return bridges

    @bus.Interface.Method()
    def reload(self):
        self.packages = {}
        self.load_packages()

    @bus.Interface.Method()
    def reload_hint(self):
        if self.saw_first_reload_hint:
            self.reload()
        self.saw_first_reload_hint = True
