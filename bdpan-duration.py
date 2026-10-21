#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bdpan-duration — 批量读取百度网盘视频时长，生成可搜索的资产清单。

纯标准库实现，无需 pip install。

用法：
    python3 bdpan-duration.py /学习 --limit 5
    python3 bdpan-duration.py /学习 --out ./report

凭证只在本进程内存中使用，不写入任何文件、不发送到任何第三方。
"""

import argparse
import csv
import html
import http.cookiejar
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter

__version__ = '1.2.0'

API_LIST = 'https://pan.baidu.com/rest/2.0/xpan/file'
API_STREAM = 'https://d.pcs.baidu.com/rest/2.0/pcs/file'
APP_ID = '250528'
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

VIDEO_EXT = ('.mp4', '.m4v', '.mov', '.mkv', '.avi', '.flv', '.wmv', '.webm', '.ts')

# 转码未完成时等多久再问一次（秒）。列表越长说明已经转得越多，不必久等。
RETRY_WAITS = (0, 12, 25, 40)

ERR_HINT = {
    -6: 'BDUSS 无效或已过期，请重新获取',
    2: '路径参数有误',
    31066: '文件不存在或已删除',
    31341: '转码尚未完成（稍后单独重试通常能好）',
    31363: '文件正在转码中',
    111: '登录态失效',
    120: '触发访问频率限制，请降低 --workers',
}


# ---------------------------------------------------------------- 彩色输出

def _tty():
    return sys.stdout.isatty() and os.environ.get('NO_COLOR') is None


def paint(s, code):
    return f'\033[{code}m{s}\033[0m' if _tty() else s


def dim(s):
    return paint(s, '2')


def green(s):
    return paint(s, '32')


def yellow(s):
    return paint(s, '33')


def red(s):
    return paint(s, '31')


def bold(s):
    return paint(s, '1')


def progress(msg):
    """只在真终端里原地刷新，重定向到文件时不刷屏。"""
    if _tty():
        print(dim(msg), end='\r', flush=True)


def progress_done():
    if _tty():
        print(' ' * 70, end='\r', flush=True)


# ---------------------------------------------------------------- 凭证

def get_bduss(args):
    """按优先级取 BDUSS，全程只存在内存里。"""
    if args.bduss:
        return args.bduss.strip()
    if os.environ.get('BAIDU_BDUSS'):
        return os.environ['BAIDU_BDUSS'].strip()
    if args.from_browser:
        return _from_browser()

    print(dim('未检测到 BDUSS。获取方式：浏览器登录 pan.baidu.com → F12 →'))
    print(dim('Application → Cookies → .baidu.com → 复制 BDUSS 那一行的 Value'))
    print()
    try:
        value = input('粘贴 BDUSS（输入不回显、不落盘）：').strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit('\n已取消。')
    if not value:
        sys.exit('BDUSS 为空，退出。')
    return value


def _from_browser():
    """可选路径：装了 browser_cookie3 才能用。macOS 会弹钥匙串授权。"""
    try:
        import browser_cookie3
    except ImportError:
        sys.exit('未安装 browser_cookie3。请改用粘贴方式，或 pip install browser_cookie3')
    print(dim('正在从浏览器读取 Cookie（macOS / Windows 可能弹出系统授权框）…'))
    for getter, name in ((browser_cookie3.chrome, 'Chrome'),
                         (browser_cookie3.edge, 'Edge'),
                         (browser_cookie3.firefox, 'Firefox')):
        try:
            jar = getter(domain_name='baidu.com')
        except Exception:
            continue
        for c in jar:
            if c.name == 'BDUSS' and c.value:
                print(green(f'已从 {name} 读取到 BDUSS'))
                return c.value
    sys.exit('浏览器里没找到 BDUSS。请先登录 pan.baidu.com，或改用粘贴方式。')


# ---------------------------------------------------------------- HTTP

class Client:
    """每线程一个实例，避免共用 opener 带来的竞争。"""

    def __init__(self, bduss):
        cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        self.opener.addheaders = [
            ('User-Agent', UA),
            ('Referer', 'https://pan.baidu.com/disk/home'),
        ]
        cj.set_cookie(http.cookiejar.Cookie(
            0, 'BDUSS', bduss, None, False, '.baidu.com', True, False,
            '/', True, True, None, False, None, None, {}))

    def get(self, url, timeout=45):
        with self.opener.open(urllib.request.Request(url), timeout=timeout) as r:
            return r.read()


_tls = threading.local()
_bduss_holder = {}


def client():
    if not hasattr(_tls, 'c'):
        _tls.c = Client(_bduss_holder['v'])
    return _tls.c


def _err_code(body):
    m = re.search(r'"error_code"\s*:\s*(-?\d+)', body[:400])
    if m:
        return int(m.group(1))
    m = re.search(r'"errno"\s*:\s*(-?\d+)', body[:400])
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- 枚举

def list_dir(cl, path, page, tries=3):
    url = (f'{API_LIST}?method=list&dir={urllib.parse.quote(path)}'
           f'&order=time&desc=1&showempty=0&page={page}&num=100'
           f'&clienttype=0&app_id={APP_ID}')
    for i in range(tries):
        try:
            j = json.loads(cl.get(url).decode('utf-8', 'replace'))
            if j.get('errno') == 0:
                return j.get('list', [])
            if j.get('errno') in (-6, 111):
                sys.exit(red('\nBDUSS 已失效，请重新获取后重试。'))
            return []
        except Exception:
            if i == tries - 1:
                return []
            time.sleep(1.5 * (i + 1))


def walk(cl, root, exts):
    """广度优先递归，返回 [{'path','name','size','fs_id'}]。"""
    found, seen, ids, todo = [], set(), set(), [root]
    dirs = 0
    while todo:
        d = todo.pop(0)
        if d in seen:
            continue
        seen.add(d)
        dirs += 1
        page = 1
        while True:
            items = list_dir(cl, d, page)
            if not items:
                break
            for it in items:
                p = it.get('path', '')
                if it.get('isdir'):
                    if p not in seen:
                        todo.append(p)
                elif p.lower().endswith(exts):
                    fid = str(it.get('fs_id', ''))
                    if fid in ids:
                        continue
                    ids.add(fid)
                    found.append({
                        'path': p,
                        'name': it.get('server_filename', p.rsplit('/', 1)[-1]),
                        'size': int(it.get('size', 0)),
                        'fs_id': fid,
                    })
            if len(items) < 100:
                break
            page += 1
        progress(f'  已扫描 {dirs} 个目录，累计 {len(found)} 个视频')
    progress_done()
    print(green(f'枚举完成：{dirs} 个目录，{len(found)} 个视频'))
    return found


# ---------------------------------------------------------------- 探测时长

def probe(cl, path, log=None):
    """请求播放器同款 HLS 列表，累加 #EXTINF 得到时长。"""
    url = (f'{API_STREAM}?method=streaming&type=M3U8_AUTO_480'
           f'&app_id={APP_ID}&path={urllib.parse.quote(path)}')
    best = {'secs': None, 'segs': 0, 'complete': False, 'code': None}

    for i, wait in enumerate(RETRY_WAITS):
        if wait:
            time.sleep(wait)
        try:
            body = cl.get(url).decode('utf-8', 'replace')
        except Exception:
            best['code'] = best['code'] or -1
            continue

        if not body.startswith('#EXTM3U'):
            code = _err_code(body)
            if code is not None:
                best['code'] = code
                if code in (31341, 31363):
                    continue
                break
            continue

        times = re.findall(r'#EXTINF:([\d.]+)', body)
        if not times:
            continue
        secs = sum(float(t) for t in times)
        # 没拿到 ENDLIST 说明列表被截断，只保留目前最大的那份
        if best['secs'] is None or secs > best['secs']:
            best = {'secs': secs, 'segs': len(times),
                    'complete': '#EXT-X-ENDLIST' in body, 'code': None}
        if best['complete']:
            return best
    return best


def fmt_hms(secs):
    if not secs:
        return '—'
    s = int(round(secs))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


# ---------------------------------------------------------------- 断点缓存

class Cache:
    """把探测结果落到 JSON，中断后重跑能跳过已完成的文件。

    只记录 fs_id、时长和完整标记，不含任何账号信息。
    """

    VERSION = 1

    def __init__(self, path, root):
        self.path = path
        self.root = root
        self.items = {}
        self.lock = threading.Lock()
        self.savelock = threading.Lock()
        self.pending = 0
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                j = json.load(f)
        except (OSError, ValueError):
            return
        # 换目录扫描时旧缓存不适用，直接忽略
        if j.get('version') == self.VERSION and j.get('root') == self.root:
            self.items = j.get('items', {})

    def get(self, fsid):
        with self.lock:
            return self.items.get(fsid)

    def put(self, fsid, probe):
        if not probe.get('secs'):
            return
        with self.lock:
            self.items[fsid] = probe
            self.pending += 1
            flush = self.pending >= 20
            if flush:
                self.pending = 0
        if flush:
            self.save()

    def save(self):
        with self.savelock:
            with self.lock:
                data = {'version': self.VERSION, 'root': self.root,
                        'items': dict(self.items)}
            tmp = self.path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.path)


# ---------------------------------------------------------------- 并发跑批

def _better(a, b):
    """两遍结果取优：完整的优先，其次时长更大的。"""
    if not a:
        return b
    if not b:
        return a
    if b.get('complete') != a.get('complete'):
        return b if b.get('complete') else a
    return b if (b.get('secs') or 0) > (a.get('secs') or 0) else a


def run_batch(rows, workers, label, cache=None):
    q = queue.Queue()
    for r in rows:
        q.put(r)
    total, done, lock = len(rows), [0], threading.Lock()
    t0 = time.time()

    def worker():
        cl = client()
        while True:
            try:
                r = q.get_nowait()
            except queue.Empty:
                return
            r['probe'] = _better(r.get('probe'), probe(cl, r['path']))
            if cache:
                cache.put(r['fs_id'], r['probe'])
            with lock:
                done[0] += 1
                n = done[0]
            if n % 10 == 0 or n == total:
                el = max(time.time() - t0, 0.01)
                eta = (total - n) / (n / el)
                progress(f'  {label} {n}/{total}  {n/el:.1f}/s  剩余 {eta/60:.1f} 分钟')
            time.sleep(0.05)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    progress_done()


def scan(rows, workers, cache):
    todo = []
    for r in rows:
        hit = cache.get(r['fs_id']) if cache else None
        if hit and hit.get('complete'):
            r['probe'] = hit
        else:
            todo.append(r)

    if len(todo) < len(rows):
        print(dim(f'缓存命中 {len(rows) - len(todo)} 个，跳过探测'))

    if todo:
        run_batch(todo, workers, '第一遍', cache)

        # 只重跑列表不完整的：转码没跑完时第一次只能拿到前几段
        suspect = [r for r in todo if not (r.get('probe') or {}).get('complete')]
        if suspect:
            print(yellow(f'{len(suspect)} 个列表不完整，开始第二遍校验'))
            run_batch(suspect, max(2, workers // 2), '校验中', cache)

    for r in rows:
        p = r.get('probe') or {}
        r['secs'] = p.get('secs')
        r['complete'] = bool(p.get('complete'))
        r['code'] = p.get('code')
        r['segs'] = p.get('segs', 0)


# ---------------------------------------------------------------- 输出

def enrich(rows):
    for r in rows:
        r['dir'] = r['path'].rsplit('/', 1)[0] or '/'
        r['name'] = r['path'].rsplit('/', 1)[-1]
        r['gb'] = round(r['size'] / 1073741824, 3)
        r['mbps'] = round(r['size'] / r['secs'] / 1048576, 2) if r['secs'] else 0
        r['hms'] = fmt_hms(r['secs'])
    return rows


def write_csv(rows, path):
    cols = ['dir', 'name', 'hms', 'secs', 'gb', 'mbps', 'complete', 'code', 'path']
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['目录', '文件名', '时长', '秒', '大小GB', '码率MB/s', '列表完整', '错误码', '完整路径'])
        for r in rows:
            w.writerow([r['dir'], r['name'], r['hms'], r['secs'] or '',
                        r['gb'], r['mbps'], '是' if r['complete'] else '否',
                        r['code'] or '', r['path']])


def write_md(rows, path, root):
    total = sum(r['secs'] or 0 for r in rows)
    size = sum(r['size'] for r in rows)
    groups = {}
    for r in rows:
        groups.setdefault(r['dir'], []).append(r)

    lines = [f'# 网盘视频清单 · {root}', '',
             f'- 视频 **{len(rows)}** 个，合计 **{fmt_hms(total)}**（{total/3600:.1f} 小时）',
             f'- 体积 **{size/1073741824:.1f} GB**，目录 **{len(groups)}** 个', '']
    for d in sorted(groups):
        items = sorted(groups[d], key=lambda x: x['path'])
        sub = sum(i['secs'] or 0 for i in items)
        lines += [f'## {d}  ', f'{len(items)} 个 · {fmt_hms(sub)} · '
                  f'{sum(i["size"] for i in items)/1073741824:.1f} GB', '',
                  '| 文件名 | 时长 | 大小 | 码率 |', '|---|---|---|---|']
        for i in items:
            flag = '' if i['complete'] else ' ⚠️'
            lines.append(f'| {i["name"]}{flag} | {i["hms"]} | {i["gb"]:.2f} GB | '
                         f'{i["mbps"]:.2f} |')
        lines.append('')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


def write_html(rows, path, root):
    total = sum(r['secs'] or 0 for r in rows)
    size = sum(r['size'] for r in rows)
    groups = {}
    for r in rows:
        groups.setdefault(r['dir'], []).append(r)

    body = []
    for d in sorted(groups):
        items = sorted(groups[d], key=lambda x: -(x['secs'] or 0))
        sub = sum(i['secs'] or 0 for i in items)
        body.append('<details>'
                    f'<summary><span class="dn">{html.escape(d)}</span>'
                    f'<span class="dm">{len(items)} 个 · {fmt_hms(sub)} · '
                    f'{sum(i["size"] for i in items)/1073741824:.1f} GB</span></summary>'
                    '<table><tbody>')
        for i in items:
            warn = '' if i['complete'] else '<span class="w" title="列表不完整，时长可能偏短">!</span>'
            body.append(f'<tr><td class="nm">{html.escape(i["name"])}{warn}</td>'
                        f'<td class="du">{i["hms"]}</td>'
                        f'<td class="sz">{i["gb"]:.2f} GB</td></tr>')
        body.append('</tbody></table></details>')

    doc = HTML_TMPL.replace('__TITLE__', html.escape(root)) \
                   .replace('__COUNT__', str(len(rows))) \
                   .replace('__TOTAL__', fmt_hms(total)) \
                   .replace('__SIZE__', f'{size/1073741824:.1f}') \
                   .replace('__DIRS__', str(len(groups))) \
                   .replace('__NOW__', time.strftime('%Y-%m-%d %H:%M')) \
                   .replace('__BODY__', '\n'.join(body))
    with open(path, 'w', encoding='utf-8') as f:
        f.write(doc)


HTML_TMPL = '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>网盘视频清单 · __TITLE__</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f6f6f4;color:#1a1a18;
 font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:880px;margin:0 auto;padding:40px 20px 80px}
h1{font-size:22px;margin:0 0 4px;font-weight:600}
.sub{color:#77756e;font-size:13px;margin-bottom:24px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:24px}
.stat{background:#fff;border:1px solid #e6e4dd;border-radius:10px;padding:14px}
.stat b{display:block;font-size:20px;font-weight:600;letter-spacing:-.5px}
.stat span{color:#77756e;font-size:12px}
input{width:100%;padding:11px 14px;border:1px solid #ddd9d0;border-radius:10px;
 font-size:14px;background:#fff;margin-bottom:18px;outline:none}
input:focus{border-color:#999}
details{background:#fff;border:1px solid #e6e4dd;border-radius:10px;margin-bottom:10px;
 overflow:hidden}
summary{padding:13px 16px;cursor:pointer;display:flex;justify-content:space-between;
 gap:12px;align-items:baseline;list-style:none}
summary::-webkit-details-marker{display:none}
summary:hover{background:#faf9f6}
.dn{font-weight:500;word-break:break-all}
.dm{color:#8a8880;font-size:12px;white-space:nowrap;flex-shrink:0}
table{width:100%;border-collapse:collapse;border-top:1px solid #efede6}
td{padding:8px 16px;border-bottom:1px solid #f4f2ee;font-size:13px}
tr:last-child td{border-bottom:none}
.nm{width:auto;word-break:break-all}
.du{width:78px;text-align:right;font-variant-numeric:tabular-nums;color:#444}
.sz{width:88px;text-align:right;color:#8a8880;font-variant-numeric:tabular-nums}
.w{display:inline-block;width:15px;height:15px;line-height:15px;text-align:center;
 background:#fdf0d8;color:#96702a;border-radius:50%;font-size:10px;margin-left:6px}
mark{background:#fff2a8;padding:0 1px}
.hide{display:none}
</style>
<style>
.gh-ribbon{position:fixed;top:16px;right:20px;z-index:99;
 font:500 12.5px/1 -apple-system,"PingFang SC",sans-serif;color:#6b6a63;
 text-decoration:none;background:#fff;border:1px solid #e6e4dd;
 border-radius:999px;padding:8px 14px;box-shadow:0 1px 3px rgba(0,0,0,.05);
 transition:color .15s,border-color .15s,box-shadow .15s}
.gh-ribbon:hover{color:#1a1a18;border-color:#cfccc1;box-shadow:0 2px 8px rgba(0,0,0,.08)}
</style></head><body>
<a class="gh-ribbon" href="https://github.com/xwenhui02-rgb/bdpan-duration"
   title="由 bdpan-duration 生成">← bdpan-duration</a>
<div class="wrap">
<h1>网盘视频清单</h1>
<div class="sub">__TITLE__ · 生成于 __NOW__</div>
<div class="stats">
<div class="stat"><b>__COUNT__</b><span>视频数</span></div>
<div class="stat"><b>__TOTAL__</b><span>总时长</span></div>
<div class="stat"><b>__SIZE__</b><span>GB 体积</span></div>
<div class="stat"><b>__DIRS__</b><span>目录数</span></div>
</div>
<input id="q" placeholder="搜索文件名或目录…" autocomplete="off">
<div id="list">__BODY__</div>
</div>
<script>
const q=document.getElementById('q');
q.addEventListener('input',()=>{
  const k=q.value.trim().toLowerCase();
  document.querySelectorAll('details').forEach(d=>{
    let hit=0;
    const dn=d.querySelector('.dn').textContent.toLowerCase();
    d.querySelectorAll('tbody tr').forEach(tr=>{
      const nm=tr.querySelector('.nm');
      const raw=nm.dataset.raw||(nm.dataset.raw=nm.textContent.replace('!',''));
      const ok=!k||raw.toLowerCase().includes(k)||dn.includes(k);
      tr.classList.toggle('hide',!ok);
      if(ok)hit++;
    });
    d.classList.toggle('hide',hit===0);
    if(k)d.open=true;
  });
});
</script></body></html>'''


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(
        description='批量读取百度网盘视频时长，生成 CSV / Markdown / HTML 清单。',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='示例：\n'
               '  %(prog)s /学习 --limit 5\n'
               '  %(prog)s /学习 --out ./report --workers 4\n')
    ap.add_argument('dir', help='要扫描的网盘目录，如 /学习')
    ap.add_argument('--out', default='./bdpan-report', help='输出目录（默认 ./bdpan-report）')
    ap.add_argument('--ext', default=None, help='只扫这些扩展名，逗号分隔（默认常见视频格式）')
    ap.add_argument('--limit', type=int, default=0, help='最多处理多少个文件，用于试跑')
    ap.add_argument('--workers', type=int, default=4, help='并发数，默认 4；被限流就降到 2')
    ap.add_argument('--bduss', default=None, help='直接传入 BDUSS（不建议，会留在 shell 历史里）')
    ap.add_argument('--from-browser', action='store_true', help='尝试从本机浏览器读 Cookie')
    ap.add_argument('--refresh', action='store_true', help='忽略断点缓存，全部重新探测')
    ap.add_argument('--version', action='version', version=f'bdpan-duration {__version__}')
    args = ap.parse_args()

    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass

    _bduss_holder['v'] = get_bduss(args)

    print()
    print(bold(f'扫描目录：{args.dir}'))
    exts = tuple(e.strip().lower() if e.startswith('.') else '.' + e.strip().lower()
                 for e in args.ext.split(',')) if args.ext else VIDEO_EXT

    cl = client()
    rows = walk(cl, args.dir, exts)
    if not rows:
        sys.exit(yellow('没有找到视频文件。检查目录是否存在、扩展名是否匹配。'))
    if args.limit:
        rows = rows[:args.limit]
        print(dim(f'已按 --limit 限制为前 {len(rows)} 个'))

    os.makedirs(args.out, exist_ok=True)
    cache = None if args.refresh else Cache(
        os.path.join(args.out, '.cache.json'), args.dir)

    print(f'开始探测时长（{args.workers} 并发）…')
    try:
        scan(rows, args.workers, cache)
    finally:
        if cache:
            cache.save()
    enrich(rows)

    ok = sum(1 for r in rows if r['secs'])
    full = sum(1 for r in rows if r['complete'])
    total = sum(r['secs'] or 0 for r in rows)

    csv_p = os.path.join(args.out, 'inventory.csv')
    md_p = os.path.join(args.out, 'inventory.md')
    html_p = os.path.join(args.out, 'inventory.html')
    write_csv(rows, csv_p)
    write_md(rows, md_p, args.dir)
    write_html(rows, html_p, args.dir)

    print()
    print(bold('完成'))
    print(f'  视频 {green(str(ok))}/{len(rows)} 个拿到时长，其中 {full} 个列表完整')
    print(f'  合计 {bold(fmt_hms(total))}（{total/3600:.1f} 小时），'
          f'{sum(r["size"] for r in rows)/1073741824:.1f} GB')

    bad = [r for r in rows if not r['secs']]
    if bad:
        print(yellow(f'  {len(bad)} 个失败：'))
        for code, n in Counter(r['code'] for r in bad).most_common():
            print(dim(f'    {n:>4} 个 · {ERR_HINT.get(code, "未知原因")}'))
        print(dim('  已完成的都存进缓存了，重跑同一条命令会自动跳过它们。'))

    print()
    for p in (html_p, csv_p, md_p):
        print('  ' + p)
    print()
    print(dim('提示：inventory.html 直接用浏览器打开，支持按文件名搜索。'))

    if bad:
        sys.exit(1)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n已中断。')
        sys.exit(130)
