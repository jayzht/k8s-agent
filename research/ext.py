#!/usr/bin/env python3
import sys, re, html

def strip(fn, width=200):
    raw = open(fn, encoding='utf-8', errors='ignore').read()
    raw = re.sub(r'(?is)<(script|style|noscript|svg)\b[^>]*>.*?</\1>', ' ', raw)
    raw = re.sub(r'(?is)<header\b[^>]*>.*?</header>', ' ', raw)
    raw = re.sub(r'(?is)<footer\b[^>]*>.*?</footer>', ' ', raw)
    raw = re.sub(r'(?is)<nav\b[^>]*>.*?</nav>', ' ', raw)
    # anchor on the real content container when present
    for anchor in [r'<div class=ft-official-body', r'<div[^>]*class="[^"]*ft-official-body',
                   r'<main\b', r'<article\b', r'<div[^>]*id="content"']:
        m = re.search(anchor, raw)
        if m:
            raw = raw[m.start():]
            break
    else:
        m = re.search(r'<h1', raw)
        if m:
            raw = raw[m.start():]
    raw = re.sub(r'(?is)<br\s*/?>', '\n', raw)
    raw = re.sub(r'(?is)</(p|div|li|h1|h2|h3|h4|h5|h6|tr|section|td|th|dd|dt|figcaption)\s*>', '\n', raw)
    raw = re.sub(r'(?s)<[^>]+>', ' ', raw)
    raw = html.unescape(raw)
    raw = re.sub(r'[ \t\xa0]+', ' ', raw)
    res = []
    for l in [x.strip() for x in raw.split('\n')]:
        if len(l) < 2:
            continue
        if not res or res[-1] != l:
            res.append(l)
    return '\n'.join(res)

for f in sys.argv[1:]:
    print("=" * 15, f, "=" * 15)
    print(strip(f))
