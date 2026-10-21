import sys
sys.path.insert(0, '/tmp/qnh_xml')
from analyze import load
import xml.etree.ElementTree as ET
from collections import Counter

for cat in ('cat_food', 'cat_snacks'):
    screens = load(cat)
    sc = screens[-1]
    print('==', cat, 'last screen', sc['file'], 'header', sc['header'], 'rows', len(sc['cards']))
    path = f'/Users/jalen/artemis/qnh_acceptance/20261021/count_gaps/{cat}/{sc["file"]}'
    root = ET.parse(path).getroot()
    cnt = Counter((n.get('text') or '').strip() for n in root.iter() if (n.get('text') or '').strip())
    marks = [t for t in cnt if any(k in t for k in ('没有更多', '到底', '已全部', '无更多', '加载更多', '查看更多', '更多商品'))]
    print('   end-of-list markers:', marks)
    print('   row bounds:', [c['row'] for c in sc['cards']])
    print('   viewport bottom = 4414')
    # last two screens identical?
    prev = screens[-2]
    print('   prev screen titles == last screen titles:',
          [c['title'] for c in prev['cards']] == [c['title'] for c in sc['cards']])
