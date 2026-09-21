import sys, re, html
from html.parser import HTMLParser

class P(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out=[]
        self.skip=0
        self.block={'p','div','br','li','h1','h2','h3','h4','h5','h6','tr','table','section','article','header','footer','ul','ol','blockquote','pre'}
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript','svg'):
            self.skip+=1
        if tag in self.block:
            self.out.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript','svg') and self.skip>0:
            self.skip-=1
        if tag in self.block:
            self.out.append('\n')
    def handle_data(self,d):
        if self.skip==0:
            self.out.append(d)

data=sys.stdin.read()
data=re.sub(r'(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>',' ',data)
p=P(); p.feed(data)
t=''.join(p.out)
t=html.unescape(t)
t=re.sub(r'[ \t\xa0]+',' ',t)
t=re.sub(r'\n\s*\n+','\n',t)
print(t.strip())
