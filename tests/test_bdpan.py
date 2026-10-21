"""bdpan-duration 的离线单元测试，不联网、不需要 BDUSS。"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location('bdpan', ROOT / 'bdpan-duration.py')
bdpan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bdpan)


class FakeClient:
    """按预设内容返回响应，避免真实请求。"""

    def __init__(self, body):
        self.body = body

    def get(self, url, timeout=45):
        return self.body.encode('utf-8')


def make_rows():
    return bdpan.enrich([
        {'path': '/学习/数学/a.mp4', 'size': 900 * 1048576, 'fs_id': '1',
         'secs': 6556.0, 'complete': True, 'code': None},
        {'path': '/学习/数学/b.mp4', 'size': 10 * 1048576, 'fs_id': '2',
         'secs': 493.0, 'complete': True, 'code': None},
        {'path': '/学习/英语/c.mp4', 'size': 5 * 1048576, 'fs_id': '3',
         'secs': None, 'complete': False, 'code': 31341},
    ])


def read(path, encoding='utf-8'):
    with open(path, encoding=encoding) as f:
        return f.read()


class TestFormat(unittest.TestCase):

    def test_with_hours(self):
        self.assertEqual(bdpan.fmt_hms(6556), '1:49:16')

    def test_under_one_hour(self):
        self.assertEqual(bdpan.fmt_hms(493), '8:13')

    def test_empty(self):
        self.assertEqual(bdpan.fmt_hms(None), '—')
        self.assertEqual(bdpan.fmt_hms(0), '—')

    def test_rounds_to_nearest_second(self):
        self.assertEqual(bdpan.fmt_hms(59.6), '1:00')


class TestBetter(unittest.TestCase):

    def test_complete_beats_longer_incomplete(self):
        a = {'secs': 100.0, 'complete': False}
        b = {'secs': 80.0, 'complete': True}
        self.assertIs(bdpan._better(a, b), b)

    def test_longer_wins_when_both_complete(self):
        a = {'secs': 100.0, 'complete': True}
        b = {'secs': 120.0, 'complete': True}
        self.assertIs(bdpan._better(a, b), b)

    def test_handles_missing_side(self):
        a = {'secs': 1.0, 'complete': True}
        self.assertIs(bdpan._better(None, a), a)
        self.assertIs(bdpan._better(a, None), a)


class TestErrCode(unittest.TestCase):

    def test_error_code_field(self):
        self.assertEqual(bdpan._err_code('{"error_code":31341}'), 31341)

    def test_errno_field(self):
        self.assertEqual(bdpan._err_code('{"errno":-6}'), -6)

    def test_no_code(self):
        self.assertIsNone(bdpan._err_code('#EXTM3U'))


class TestProbe(unittest.TestCase):

    def setUp(self):
        self._waits = bdpan.RETRY_WAITS
        bdpan.RETRY_WAITS = (0,)

    def tearDown(self):
        bdpan.RETRY_WAITS = self._waits

    def test_complete_playlist(self):
        body = '#EXTM3U\n#EXTINF:6.0,\n#EXTINF:5.5,\n#EXT-X-ENDLIST\n'
        r = bdpan.probe(FakeClient(body), '/a.mp4')
        self.assertAlmostEqual(r['secs'], 11.5)
        self.assertTrue(r['complete'])
        self.assertEqual(r['segs'], 2)

    def test_missing_endlist_is_marked_incomplete(self):
        r = bdpan.probe(FakeClient('#EXTM3U\n#EXTINF:6.0,\n'), '/a.mp4')
        self.assertAlmostEqual(r['secs'], 6.0)
        self.assertFalse(r['complete'])

    def test_transcode_error_is_reported(self):
        r = bdpan.probe(FakeClient('{"error_code":31341}'), '/a.mp4')
        self.assertIsNone(r['secs'])
        self.assertEqual(r['code'], 31341)

    def test_garbage_response_does_not_crash(self):
        r = bdpan.probe(FakeClient('<html>error</html>'), '/a.mp4')
        self.assertIsNone(r['secs'])


class TestCache(unittest.TestCase):

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, '.cache.json')
            c = bdpan.Cache(p, '/学习')
            c.put('1', {'secs': 10.0, 'complete': True})
            c.save()
            self.assertEqual(bdpan.Cache(p, '/学习').get('1')['secs'], 10.0)

    def test_cache_for_other_root_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, '.cache.json')
            bdpan.Cache(p, '/学习').save()
            self.assertEqual(bdpan.Cache(p, '/别的').items, {})

    def test_failed_probe_is_not_cached(self):
        with tempfile.TemporaryDirectory() as d:
            c = bdpan.Cache(os.path.join(d, '.cache.json'), '/x')
            c.put('2', {'secs': None, 'complete': False})
            self.assertIsNone(c.get('2'))

    def test_broken_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, '.cache.json')
            with open(p, 'w', encoding='utf-8') as f:
                f.write('{not json')
            self.assertEqual(bdpan.Cache(p, '/x').items, {})


class TestEnrichAndOutput(unittest.TestCase):

    def test_enrich_splits_dir_and_name(self):
        rows = make_rows()
        self.assertEqual(rows[0]['dir'], '/学习/数学')
        self.assertEqual(rows[0]['name'], 'a.mp4')
        self.assertEqual(rows[2]['hms'], '—')

    def test_bitrate(self):
        rows = make_rows()
        # 900MB / 6556s ≈ 0.14 MB/s
        self.assertAlmostEqual(rows[0]['mbps'], 0.14, places=2)
        self.assertEqual(rows[2]['mbps'], 0)

    def test_csv_content(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'i.csv')
            bdpan.write_csv(make_rows(), p)
            text = read(p, 'utf-8-sig')
            self.assertIn('a.mp4', text)
            self.assertIn('1:49:16', text)
            self.assertIn('否', text)

    def test_html_is_self_contained(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'i.html')
            bdpan.write_html(make_rows(), p, '/学习')
            text = read(p)
            self.assertIn('a.mp4', text)
            self.assertIn('1:49:16', text)
            self.assertIn('搜索文件名', text)
            self.assertNotIn('__BODY__', text)
            self.assertNotIn('__NOW__', text)

    def test_md_groups_by_dir(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'i.md')
            bdpan.write_md(make_rows(), p, '/学习')
            text = read(p)
            self.assertIn('## /学习/数学', text)
            self.assertIn('## /学习/英语', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
