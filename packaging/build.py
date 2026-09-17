"""Build the thing you send somebody.

    python packaging/build.py            one build, for the machine it runs on
    python packaging/build.py --check    build, then start it and prove it runs

macOS gets `dist/LUMEN.app`, zipped as `dist/LUMEN-mac.zip` with `ditto`, which
is the one archiver that keeps a bundle's symlinks and its signature intact.
Windows gets `dist/LUMEN.exe`, one file, nothing to install.

A build has to happen on the platform it is for: the interpreter, SDL, and the
four libraries underneath the game are all native code, and there is no
cross-compiler for that. `packaging/build_windows.sh` is how a Windows build is
made from a Mac - in a container, with Wine - and the GitHub Actions workflow
in `.github/workflows/` is how both are made from a tag.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, 'build')
DIST = os.path.join(ROOT, 'dist')


def run(command, **kwargs):
    print('$', ' '.join(str(c) for c in command), flush=True)
    return subprocess.run(command, check=True, **kwargs)


def make_icon():
    # By path, not by name: `packaging` is also a library PyInstaller itself
    # imports, and this directory would shadow it.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'lumen_icon', os.path.join(ROOT, 'packaging', 'icon.py'))
    icon_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(icon_mod)
    png, ico, icns = icon_mod.write(os.path.join(BUILD, 'icon'))
    chosen = icns if sys.platform == 'darwin' else ico
    print(f'icon: {chosen}')
    return chosen


def build(clean=True):
    os.makedirs(BUILD, exist_ok=True)
    if clean:
        shutil.rmtree(os.path.join(BUILD, 'lumen'), ignore_errors=True)
        # This platform's leavings only. The Windows build is made on a Mac
        # (see `build_windows.sh`) and lands in the same folder, and a mac
        # build that cleaned indiscriminately deleted it.
        stale = {'darwin': ('LUMEN.app', 'LUMEN'),
                 'win32': ('LUMEN.exe',)}.get(sys.platform, ('LUMEN',))
        for name in stale:
            path = os.path.join(DIST, name)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)
    env = dict(os.environ, LUMEN_ICON=make_icon())
    started = time.perf_counter()
    run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
         '--distpath', DIST, '--workpath', os.path.join(BUILD, 'lumen'),
         os.path.join(ROOT, 'packaging', 'lumen.spec')],
        cwd=ROOT, env=env)
    print(f'built in {time.perf_counter() - started:.0f}s')


def signing_identity():
    """The Developer ID to sign with, or None to sign ad-hoc.

    Ad-hoc is enough to *run* on Apple silicon and not enough for anything
    else: an ad-hoc app cannot be notarised, and an app that is not notarised
    is one macOS refuses until the player goes into System Settings and
    presses Open Anyway. `LUMEN_SIGN_IDENTITY` names one explicitly;
    otherwise the first Developer ID Application certificate in the keychain
    is it.
    """
    named = os.environ.get('LUMEN_SIGN_IDENTITY')
    if named:
        return named
    try:
        found = subprocess.run(['security', 'find-identity', '-v',
                                '-p', 'codesigning'],
                               capture_output=True, text=True, check=True)
    except Exception:
        return None
    for line in found.stdout.splitlines():
        if 'Developer ID Application' in line:
            return line.split('"')[1]
    return None


def sign(app, identity):
    """Sign every Mach-O in the bundle, and then the bundle.

    Inside out, because a signature covers what is beneath it: signing the
    bundle first and its libraries afterwards invalidates the bundle's own
    seal. `--options runtime` is the hardened runtime, which notarisation
    requires; `--timestamp` gets Apple's, so the signature outlives the
    certificate.
    """
    entitlements = os.path.join(ROOT, 'packaging', 'entitlements.plist')
    base = ['codesign', '--force', '--options', 'runtime', '--timestamp',
            '--sign', identity]
    inner = []
    for folder, _dirs, files in os.walk(app):
        for name in files:
            path = os.path.join(folder, name)
            if os.path.islink(path):
                continue
            if name.endswith(('.so', '.dylib')) or os.access(path, os.X_OK):
                with open(path, 'rb') as handle:
                    magic = handle.read(4)
                # Mach-O, thin or fat, either byte order.
                if magic in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe',
                             b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'):
                    inner.append(path)
    print(f'signing {len(inner)} binaries, then the bundle')
    for path in inner:
        subprocess.run(base + [path], check=True, capture_output=True)
    run(base + ['--entitlements', entitlements, app])
    run(['codesign', '--verify', '--strict', '--deep', app])


def notarize(app, zipped):
    """Send it to Apple, wait for the answer, and staple it to the app.

    Notarisation is what removes the warning entirely: macOS checks the
    stapled ticket offline, and the app opens on the first double-click like
    anything bought from anywhere. Credentials come from a keychain profile
    made once with `xcrun notarytool store-credentials`, or from an App Store
    Connect key named in the environment. Nothing secret is read here or
    written anywhere.
    """
    profile = os.environ.get('LUMEN_NOTARY_PROFILE')
    key = os.environ.get('LUMEN_NOTARY_KEY')
    key_id = os.environ.get('LUMEN_NOTARY_KEY_ID')
    issuer = os.environ.get('LUMEN_NOTARY_ISSUER')
    if profile:
        credentials = ['--keychain-profile', profile]
    elif key and key_id and issuer:
        credentials = ['--key', key, '--key-id', key_id, '--issuer', issuer]
    else:
        raise SystemExit(
            'notarising needs credentials: either LUMEN_NOTARY_PROFILE, or\n'
            'LUMEN_NOTARY_KEY, LUMEN_NOTARY_KEY_ID and LUMEN_NOTARY_ISSUER.')
    run(['xcrun', 'notarytool', 'submit', zipped, '--wait',
         '--timeout', '30m'] + credentials)
    run(['xcrun', 'stapler', 'staple', app])
    run(['spctl', '--assess', '--type', 'execute', '-vv', app])


def package():
    """Whatever the platform's one-file answer is, and how big it came out."""
    if sys.platform == 'darwin':
        app = os.path.join(DIST, 'LUMEN.app')
        # Signed last: every change to a bundle after its signature
        # invalidates it.
        identity = signing_identity()
        if identity:
            print(f'signing as {identity}')
            sign(app, identity)
        else:
            # Ad-hoc: enough for an Apple silicon Mac to run it at all, and
            # not enough to be notarised.
            run(['codesign', '--force', '--deep', '--sign', '-', app])
            run(['codesign', '--verify', '--deep', '--strict', app])
        out = os.path.join(DIST, 'LUMEN-mac.zip')
        if os.path.exists(out):
            os.remove(out)
        run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', app, out])
    elif sys.platform == 'win32':
        out = os.path.join(DIST, 'LUMEN.exe')
    else:
        out = os.path.join(DIST, 'LUMEN')
    size = os.path.getsize(out) / (1024 * 1024)
    print(f'\n{out}  ({size:.0f} MB)')
    return out


def check():
    """Start the built game, let it draw, and make sure it drew the game.

    Runs it the way a player would - the built binary, not the source - with
    the self-test hook set, so it opens a window, plays a few hundred frames
    and says what state it ended in.
    """
    if sys.platform == 'darwin':
        binary = os.path.join(DIST, 'LUMEN.app', 'Contents', 'MacOS', 'LUMEN')
    elif sys.platform == 'win32':
        binary = os.path.join(DIST, 'LUMEN.exe')
    else:
        binary = os.path.join(DIST, 'LUMEN')
    shot = os.path.join(BUILD, 'check.png')
    env = dict(os.environ, LUMEN_SELFTEST='260', LUMEN_SELFTEST_SHOT=shot,
               LUMEN_WINDOW='1280x720')
    print('\nstarting the built game...', flush=True)
    done = subprocess.run([binary], env=env, capture_output=True, text=True,
                          timeout=180)
    print(done.stdout.strip())
    print(done.stderr.strip())
    if done.returncode != 0:
        raise SystemExit(f'the built game exited {done.returncode}')
    if 'selftest ok' not in done.stderr:
        raise SystemExit('the built game did not report a good self-test')
    print(f'screenshot: {shot}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true',
                        help='run the built game once it is built')
    parser.add_argument('--notarize', action='store_true',
                        help='send the signed app to Apple and staple the '
                             'ticket it sends back')
    parser.add_argument('--no-clean', action='store_true')
    args = parser.parse_args()
    build(clean=not args.no_clean)
    out = package()
    if args.notarize:
        if sys.platform != 'darwin':
            raise SystemExit('nothing to notarise off macOS')
        notarize(os.path.join(DIST, 'LUMEN.app'), out)
        # The stapled ticket lives in the bundle, so the zip has to be made
        # again after it is attached.
        os.remove(out)
        run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
             os.path.join(DIST, 'LUMEN.app'), out])
        print(f'\nnotarised: {out}')
    if args.check:
        check()


if __name__ == '__main__':
    main()
