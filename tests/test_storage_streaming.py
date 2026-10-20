"""Large preview requests must not load the complete object into RAM."""
from contextlib import contextmanager
from io import BytesIO
from urllib.parse import quote

import pytest

from test_contest_permissions import context, client
from app.services.storage import object_storage, ObjectStorage
from app.settings import settings


def test_range_reads_only_requested_bytes_and_never_uses_read_bytes(context, monkeypatch):
    c = context
    key = f"contests/{c['cid']}/testcases/large.in"
    size = 100 * 1024 * 1024
    reads, closed = [], []
    monkeypatch.setattr(object_storage, "size_bytes", lambda _: size)
    monkeypatch.setattr(object_storage, "read_bytes", lambda _: pytest.fail("Unbounded object read"))

    @contextmanager
    def reader(_, *, offset=0, length=None):
        class Source:
            def read(self, count):
                reads.append(count)
                return b"x" * count
        assert offset == 0 and length == 262145
        try:
            yield Source()
        finally:
            closed.append(True)
    monkeypatch.setattr(object_storage, "open_reader", reader)
    response = client.get('/api/storage/objects/' + key, headers={**c['tokens']['problem_author'], 'Range': 'bytes=0-262144'})
    assert response.status_code == 206
    assert len(response.content) == 262145
    assert response.headers['content-range'] == f'bytes 0-262144/{size}'
    assert max(reads) <= 65536 and sum(reads) == 262145 and closed


@pytest.mark.parametrize('header,expected', [('bytes=3-6', b'3456'), ('bytes=7-', b'789'), ('bytes=-3', b'789')])
def test_ranges_and_native_download_keep_exact_contents(context, header, expected):
    c = context
    key = f"contests/{c['cid']}/testcases/range.in"
    object_storage.write_bytes(key, b'0123456789')
    response = client.get('/api/storage/objects/' + key, headers={**c['tokens']['problem_author'], 'Range': header})
    assert response.status_code == 206 and response.content == expected
    full = client.get('/api/storage/objects/' + key, headers=c['tokens']['problem_author'])
    assert full.status_code == 200 and full.content == b'0123456789'
    assert full.headers['content-length'] == '10'


@pytest.mark.parametrize('header', ['bytes=100-', 'bytes=4-2', 'bytes=0-1,3-4', 'bytes=-0', 'garbage'])
def test_invalid_ranges_are_rejected_without_reading(context, header, monkeypatch):
    c = context
    key = f"contests/{c['cid']}/testcases/invalid.in"
    object_storage.write_bytes(key, b'123')
    monkeypatch.setattr(object_storage, 'open_reader', lambda *_a, **_k: pytest.fail('Must not read invalid ranges'))
    response = client.get('/api/storage/objects/' + key, headers={**c['tokens']['problem_author'], 'Range': header})
    assert response.status_code == 416 and response.headers['content-range'] == 'bytes */3'


def test_signed_native_download_retains_auth_scope_and_empty_files(context):
    c = context
    key = f"contests/{c['cid']}/testcases/empty.in"
    object_storage.write_bytes(key, b'')
    url = '/api/storage/download-url?storage_key=' + quote(key, safe='')
    assert client.get(url).status_code == 401
    assert client.get(url, headers=c['tokens']['problem_reviewer']).status_code == 403
    result = client.get(url, headers=c['tokens']['problem_author'])
    assert result.status_code == 200
    download = client.get(result.json()['data']['download_url'])
    assert download.status_code == 200 and download.content == b''
    assert download.headers['content-disposition'].startswith('attachment;')
    assert client.get('/api/storage/objects/' + key, headers={**c['tokens']['problem_author'], 'Range':'bytes=0-10'}).status_code == 416


def test_minio_reader_passes_range_and_releases_connection(monkeypatch):
    storage = ObjectStorage()
    storage.backend = 'minio'
    calls, released = [], []
    class Response(BytesIO):
        def release_conn(self): released.append(True)
    class Client:
        def get_object(self, bucket, key, **kwargs):
            calls.append(kwargs)
            return Response(b'456')
    monkeypatch.setattr(storage, '_client', lambda: Client())
    with storage.open_reader('contests/demo/x', offset=4, length=3) as source:
        assert source.read() == b'456'
    assert calls == [{'offset':4,'length':3}] and released == [True]
