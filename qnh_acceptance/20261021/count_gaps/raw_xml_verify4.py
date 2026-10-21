import sys
import xml.etree.ElementTree as ET
sys.path.insert(0, '/tmp/qnh_xml')
from analyze import load, bb, classify, ZERO

for cat in ('cat_food', 'cat_snacks'):
    screens = load(cat)
    print(f'== {cat}: untitled (top-cut) row anatomy ==')
    for sc in screens:
        root = ET.parse(f"/Users/jalen/artemis/qnh_acceptance/20261021/count_gaps/{cat}/{sc['file']}").getroot()
        pane = None
        for n in ([x for x in root.iter()]):
            if n.get('class') == 'android.widget.ScrollView' and (n.get('bounds') or '').startswith('[203,'):
                pane = n
                break
        rows = [c for c in pane if (c.get('class') or '').endswith('ViewGroup')]
        for j, (r, card) in enumerate(zip(rows, sc['cards'])):
            if card['title']:
                continue
            kids = []
            for ch in r:
                b = bb(ch)
                kids.append((b, (ch.get('text') or '').strip()[:12], (ch.get('class') or '')[-9:]))
            imgs = [k for k in kids if k[0] and k[0][0] < 380 and 80 <= (k[0][2] - k[0][0]) <= 200
                    and 80 <= (k[0][3] - k[0][1]) <= 200]
            print(f'  {sc["file"]} r{j} bounds={card["row"]} square_image_children={len(imgs)} '
                  f'first_kids={kids[:3]}')
