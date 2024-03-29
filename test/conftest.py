import pytest
import logging
import docker
import os

def pull_image(client, name):
    try:
        client.images.get(name)
        logging.info(f'Docker image {name} already exists')
    except docker.errors.ImageNotFound:
        logging.info(f'Pulling image {name}')
        client.images.pull(name)

import re
import io
import tarfile
import requests
from ftplib import FTP
class FTPClient:
    """
    ftp://user_name:password@hostname
    """
    def __init__(self, url):
        prog = re.compile('ftp://(.+)@')
        pat = prog.search(url)
        if pat:
            self.user, self.passwd = pat.group(1).split(':')
        else:
            self.user, self.passwd = None, None
        self.host = prog.sub('', url)

        self.session = FTP(self.host, user=self.user, passwd=self.passwd)

    def download_and_untar(self, fn):
        logging.info(f'Download & extract {fn}')
        buf = io.BytesIO()
        self.session.retrbinary(
            f'RETR {fn}',
            buf.write)
        buf.seek(0)
        tar = tarfile.open(fileobj=buf)
        tar.extractall()

    def get_nntc(self):
        path = '/sophon-sdk/tpu-nntc/daily_build/latest_release'
        self.session.cwd(path)
        fn = next(filter(lambda x: x.startswith('tpu-nntc_'), self.session.nlst()))
        logging.info(f'Latest nntc package is {fn}')
        out_dir = fn.replace('.tar.gz', '')
        if os.path.exists(out_dir):
            logging.info(f'{out_dir} already exists')
            return fn
        self.download_and_untar(os.path.join(path, fn))
        return fn

from html.parser import HTMLParser

class ReleasePageParser(HTMLParser):
    def __init__(self, *args, **kwargs):
        super(ReleasePageParser, self).__init__(*args, **kwargs)
        self.results = []

    def handle_starttag(self, tag, attrs):
        if tag == 'include-fragment':
            attrs = dict(attrs)
            m = re.match('^.+(\\d+\\.)+\\d+$', attrs.get('src', ''))
            if not m:
                return
            self.results.append(m.group(0))

class ExpandParser(HTMLParser):
    def __init__(self, *args, **kwargs):
        super(ExpandParser, self).__init__(*args, **kwargs)
        self.results = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            attrs = dict(attrs)
            self.results.append(attrs.get('href'))

def get_latest_tpu_perf():
    resp = requests.get('https://github.com/sophgo/tpu-perf/releases')
    resp.raise_for_status()
    parser = ReleasePageParser()
    parser.feed(resp.text)

    page = parser.results[0]
    resp = requests.get(page)
    resp.raise_for_status()
    parser = ExpandParser()
    parser.feed(resp.text)

    return parser.results

tpu_perf_whl = None
@pytest.fixture(scope='session')
def latest_tpu_perf_whl():
    global tpu_perf_whl
    if not tpu_perf_whl:
        tpu_perf_whl = next(filter(lambda x: 'x86' in x, get_latest_tpu_perf()))
    return f'https://github.com/{tpu_perf_whl}'

import shutil
def remove_tree(path):
    if os.path.exists(path):
        logging.info(f'Removing {path}')
        shutil.rmtree(path)

@pytest.fixture(scope='session')
def nntc_docker(latest_tpu_perf_whl):
    # Env assertion
    assert os.path.exists('/run/docker.sock')

    root = os.path.dirname(os.path.dirname(__file__))
    logging.info(f'Working dir {root}')
    os.chdir(root)
    remove_tree('./build')

    # Download
    ftp_server = os.environ.get('FTP_SERVER')
    assert ftp_server
    f = FTPClient(ftp_server)
    nntc_fn = f.get_nntc()
    nntc_dir = nntc_fn.replace('.tar.gz', '')

    # Docker init
    client = docker.from_env()
    image = 'sophgo/tpuc_dev:latest'
    pull_image(client, image)

    # NNTC container
    nntc_container = client.containers.run(
        image, 'bash',
        volumes=[f'{root}:/workspace'],
        restart_policy={'Name': 'always'},
        environment=[
            f'PATH=/workspace/{nntc_dir}/bin:/usr/local/bin:/usr/bin:/bin',
            f'NNTC_TOP=/workspace/{nntc_dir}'],
        tty=True, detach=True)
    logging.info(f'Setting up NNTC')
    ret, _ = nntc_container.exec_run(
        'bash -c "source /workspace/tpu-nntc*/scripts/envsetup.sh"',
        tty=True)
    assert ret == 0

    logging.info(f'NNTC container {nntc_container.name}')

    yield dict(docker=client, nntc_container=nntc_container)

    # Docker cleanup
    logging.info(f'Removing NNTC container {nntc_container.name}')
    nntc_container.remove(v=True, force=True)

    remove_tree('./build')
    remove_tree('./data')
    remove_tree(nntc_dir)

import subprocess

def git_commit_parents(rev='HEAD'):
    p = subprocess.run(
        f'git rev-parse {rev}^@',
        shell=True, check=True,
        capture_output=True)
    return p.stdout.decode().strip(' \n').split()

def get_relevant_commits():
    head_parents = git_commit_parents()
    if len(head_parents) == 1:
        return ['HEAD']
    assert len(head_parents) == 2
    branch_a_is_good = branch_b_is_good = True
    ap, bp = a, b = head_parents
    al, bl = [], []
    while True:
        if branch_a_is_good:
            al.append(ap)
            parents = git_commit_parents(ap)
            if len(parents) > 1:
                branch_a_is_good = False
                al = None
            else:
                if b == parents[0]:
                    bl = None
                    break
                ap = parents[0]

        if branch_b_is_good:
            bl.append(bp)
            parents = git_commit_parents(bp)
            if len(parents) > 1:
                branch_b_is_good = False
                bl = None
            else:
                if a == parents[0]:
                    al = None
                    break
                bp = parents[0]
    assert al or bl, 'PR commits are diverged'
    return al if al is not None else bl

def git_changed_files(rev):
    p = subprocess.run(
        f'git show --pretty="" --name-only {rev}',
        shell=True, check=True,
        capture_output=True)
    return p.stdout.decode().strip(' \n').split()

from functools import reduce
@pytest.fixture(scope='session')
def case_list():
    if os.environ.get('FULL_TEST'):
        return '--full'

    files = reduce(
        lambda acc, x: acc + x,
        [git_changed_files(c) for c in get_relevant_commits()], [])

    # Skip certain files
    files = [
        f for f in files
        if not f.endswith('.md')
        and not f.endswith('.txt')
        and not os.path.basename(f).startswith('.')]

    is_model = lambda x: x.startswith('vision') or x.startswith('language')
    files = [f for f in files if is_model(f)]

    dirs = set([os.path.dirname(f) for f in files])
    def has_config(d):
        try:
            next(filter(lambda x: x.endswith('config.yaml'), os.listdir(d)))
        except StopIteration:
            return False
        else:
            return True
    s = ' '.join(d for d in dirs if has_config(d))
    return s

@pytest.fixture(scope='session')
def nntc_env(nntc_docker, latest_tpu_perf_whl, case_list):
    logging.info(f'Installing tpu_perf {latest_tpu_perf_whl}')

    subprocess.run(
        f'pip3 install {latest_tpu_perf_whl}',
        shell=True, check=True)

    ret, _ = nntc_docker['nntc_container'].exec_run(
        f'bash -c "pip3 install {latest_tpu_perf_whl}"',
        tty=True)
    assert ret == 0

    logging.info(f'Running cases "{case_list}"')

    yield dict(**nntc_docker, case_list=case_list)

@pytest.fixture(scope='session')
def get_cifar100():
    data_server = os.environ.get('DATA_SERVER')
    assert data_server
    fn = 'cifar-100-python.tar.gz'
    url = os.path.join(data_server, fn)
    logging.info(f'Downloading {fn}')
    ret = os.system(
        f'curl -s {url} | tar -zx --strip-components=1 '
         '-C dataset/CIFAR100/cifar-100-python/')
    assert ret == 0

@pytest.fixture(scope='session')
def get_imagenet_val():
    data_server = os.environ.get('DATA_SERVER')
    assert data_server
    fn = 'ILSVRC2012_img_val.tar'
    url = os.path.join(data_server, fn)
    logging.info(f'Downloading {fn}')
    ret = os.system(
        f'curl -s {url} | tar -x -C dataset/ILSVRC2012/ILSVRC2012_img_val/')
    assert ret == 0

def main():
    logging.basicConfig(level=logging.INFO)

    files = reduce(
        lambda acc, x: acc + x,
        [git_changed_files(c) for c in get_relevant_commits()], [])
    print(files)

if __name__ == '__main__':
    main()
