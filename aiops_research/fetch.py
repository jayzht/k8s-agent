import sys, subprocess, re, html
def get(url, out):
    r = subprocess.run(["curl","-sSL","--max-time","45","-A","Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",url],capture_output=True)
    t = r.stdout.decode("utf-8","ignore")
    for pat in [r'(?is)<script.*?</script>', r'(?is)<style.*?</style>', r'(?is)<noscript.*?</noscript>', r'(?is)<svg.*?</svg>', r'(?is)<!--.*?-->']:
        t = re.sub(pat,' ',t)
    t = re.sub(r'(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>','\n',t)
    t = re.sub(r'(?s)<[^>]+>',' ',t)
    t = html.unescape(t)
    t = re.sub(r'[ \t\xa0]+',' ',t)
    t = re.sub(r'\n\s*\n+','\n',t)
    open(out,"w").write(t)
    print("OK",len(t),url)
for i in range(1,len(sys.argv),2):
    get(sys.argv[i], sys.argv[i+1])
