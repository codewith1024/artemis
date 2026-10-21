"""Raw-XML card analysis v4 (final).

Independent of scanner/parser code: only the uiautomator XML trees are used.

Card container = direct child ViewGroup of the product-list ScrollView.
Title  = direct-child TextView with product-title text, either in the title
         column (x0 >= 380) OR a zero-bounds (off-screen/prefetched) node.
Spec   = TextView starting with 规格：
"""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = Path('/Users/jalen/artemis/qnh_acceptance/20261021/count_gaps')

PRICE_TAG = re.compile(r'^(￥|¥)')
AMOUNT = re.compile(r'^\d+(\.\d+)?$')
FRAC = re.compile(r'^\.\d+$')
SPEC = re.compile(r'^\s*规格[:：]')
BARCODE = re.compile(r'^\s*条码[:：]')
SALE = re.compile(r'^\s*月售[:：]')
STOCK = re.compile(r'^\s*库存[:：]')
VIEWALL = re.compile(r'^\s*查看全部价格')
EDIT = re.compile(r'^\s*改价')
NODATA = re.compile(r'^\s*无数据')
VIEWN = re.compile(r'^\s*查看\(\d+\)$')
BADGE = ('活动品', '已售罄', '力荐', '新品', '推荐', '爆款', '秒杀', '包邮', '特价')
UNIT = re.compile(r'^[/／]\S{1,4}$|^起$')
TITLE_X = 380
ZERO = (0, 0, 0, 0)


def bb(n):
    m = re.match(r'\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]', n.get('bounds') or '')
    return tuple(int(x) for x in m.groups()) if m else None


def walk(e):
    for c in e:
        yield c
        yield from walk(c)


def classify(t):
    if SPEC.match(t):
        return 'spec'
    if BARCODE.match(t):
        return 'barcode'
    if SALE.match(t):
        return 'sale'
    if STOCK.match(t):
        return 'stock'
    if VIEWALL.match(t):
        return 'viewall'
    if EDIT.match(t):
        return 'edit'
    if NODATA.match(t):
        return 'nodata'
    if VIEWN.match(t):
        return 'viewn'
    if t in BADGE:
        return 'badge'
    if PRICE_TAG.match(t):
        return 'cur'
    if AMOUNT.match(t) or FRAC.match(t):
        return 'amount'
    if UNIT.match(t):
        return 'unit'
    return 'title'


def descendants(node):
    for c in node:
        yield c
        yield from descendants(c)


def parse_card(row):
    c = {'row': bb(row), 'title': None, 'spec': None, 'barcode': None, 'sale': None,
         'stock': None, 'viewall': False, 'edit': False, 'nodata': False, 'viewn': None,
         'badges': [], 'units': [], 'prices': 0, 'zero_price_nodes': 0, 'title_zero': False,
         'has_image': False}
    cand = []
    for ch in row:
        t = (ch.get('text') or '').strip()
        if not t:
            continue
        if (ch.get('class') or '').endswith('ViewGroup'):
            continue
        k = classify(t)
        b = bb(ch)
        if k == 'spec' and c['spec'] is None:
            c['spec'] = t
        elif k == 'barcode' and c['barcode'] is None:
            c['barcode'] = t
        elif k == 'sale' and c['sale'] is None:
            c['sale'] = t
        elif k == 'stock' and c['stock'] is None:
            c['stock'] = t
        elif k == 'viewall':
            c['viewall'] = True
        elif k == 'edit':
            c['edit'] = True
        elif k == 'nodata':
            c['nodata'] = True
        elif k == 'viewn' and c['viewn'] is None:
            c['viewn'] = t
        elif k == 'badge':
            c['badges'].append(t)
        elif k == 'unit':
            c['units'].append(t)
        elif k == 'cur':
            c['prices'] += 1
            if b == ZERO:
                c['zero_price_nodes'] += 1
        elif k == 'title' and len(t) >= 6:
            if b and (b[0] >= TITLE_X or b == ZERO):
                cand.append((len(t), t, b))
    if cand:
        cand.sort(reverse=True)
        c['title'] = cand[0][1]
        c['title_zero'] = cand[0][2] == ZERO
    else:
        for n in descendants(row):
            t = (n.get('text') or '').strip()
            if not t or classify(t) != 'title' or len(t) < 6:
                continue
            b = bb(n)
            if b and (b[0] >= TITLE_X or b == ZERO):
                cand.append((len(t), t, b))
        if cand:
            cand.sort(reverse=True)
            c['title'] = cand[0][1]
            c['title_zero'] = cand[0][2] == ZERO
    # product image = first ViewGroup child spanning x 230..365
    for ch in row:
        b = bb(ch)
        if b and b[0] < 380 and (b[2] - b[0]) > 50:
            c['has_image'] = True
            break
    return c


def load(cat):
    files = sorted((BASE / cat).glob('list_*.xml'))
    out = []
    for f in files:
        root = ET.parse(f).getroot()
        pane = None
        for n in walk(root):
            if n.get('class') == 'android.widget.ScrollView' and (n.get('bounds') or '').startswith('[203,'):
                pane = n
                break
        rows = [c for c in pane if (c.get('class') or '').endswith('ViewGroup')] if pane is not None else []
        cards = [parse_card(r) for r in rows]
        hdr = None
        for n in walk(root):
            t = (n.get('text') or '')
            if re.match(r'^[^\s（）]{2,6}（\d+）$', t):
                hdr = t
                break
        # orphan sweep
        inside = set()
        for r in rows:
            for n in descendants(r):
                inside.add(id(n))
        orphans = []
        for n in walk(root):
            if id(n) in inside:
                continue
            t = (n.get('text') or '').strip()
            if not t or classify(t) != 'title' or len(t) < 8:
                continue
            b = bb(n)
            if b and b[0] >= TITLE_X:
                orphans.append((t, b))
        out.append({'file': f.name, 'cards': cards, 'header': hdr, 'orphans': orphans})
    return out


def merge_v4(screens):
    G = []
    for si, sc in enumerate(screens):
        cards = sc['cards']
        titles = [c['title'] for c in cards]
        best = None
        for k in range(-len(cards), len(G) + 1):
            matched = consec = best_consec = 0
            for j, t in enumerate(titles):
                if t is None:
                    continue
                p = k + j
                if 0 <= p < len(G) and G[p]['title'] == t:
                    matched += 1
                    consec += 1
                    best_consec = max(best_consec, consec)
                else:
                    consec = 0
            if matched == 0:
                continue
            score = (matched, best_consec, -abs(k))
            if best is None or score > best[0]:
                best = (score, k)
        k = best[1] if best else len(G)
        for j, cd in enumerate(cards):
            t = cd['title']
            p = k + j
            if t is None:
                if 0 <= p < len(G):
                    G[p]['inst'].append((si, j, 'partial'))
                    G[p]['cut_top'] = True
                else:
                    G.append({'title': None, 'spec': cd['spec'], 'inst': [(si, j, 'notitle')],
                              'cut_top': True, 'prices': cd['prices']})
                continue
            if 0 <= p < len(G) and G[p]['title'] == t:
                g = G[p]
                g['inst'].append((si, j, 'full'))
                if cd['spec'] and not g['spec']:
                    g['spec'] = cd['spec']
                g['prices'] = max(g.get('prices', 0), cd['prices'])
                g['zero'] = g.get('zero', 0) + cd['zero_price_nodes']
                g['zero_only'] = g.get('zero_only', False) and cd['title_zero']
                continue
            G.insert(max(0, min(p, len(G))), {'title': t, 'spec': cd['spec'],
                                              'inst': [(si, j, 'new')], 'prices': cd['prices'],
                                              'zero': cd['zero_price_nodes'], 'zero_only': cd['title_zero'],
                                              'cut_top': False})
            k += 1
    return G


def main():
    cat = sys.argv[1] if len(sys.argv) > 1 else 'cat_food'
    screens = load(cat)
    G = merge_v4(screens)
    titled = [g for g in G if g['title']]
    print(f'## {cat}: screens={len(screens)} global_cards={len(G)} titled={len(titled)} notitle={len(G)-len(titled)}')
    for i, g in enumerate(G):
        inst = ','.join(f'{s}:{j}' for s, j, kk in g['inst'])
        print(f'  {i:02d} n={len(g["inst"])} prices={g.get("prices")} zero={g.get("zero",0)} '
              f'spec={g["spec"]!r} title={g["title"]!r} inst={inst}')
    print('--- zero-bounds card rows (off-screen/prefetch) per screen:')
    zseen = {}
    for si, sc in enumerate(screens):
        for j, cd in enumerate(sc['cards']):
            if cd['row'] == ZERO or cd['title_zero']:
                print(f'   {sc["file"]} row{j} row_bounds={cd["row"]} title={cd["title"]!r} spec={cd["spec"]!r}')
                zseen.setdefault(cd['title'], []).append((sc['file'], j))
    print('--- titles seen ONLY in zero-bounds rows:')
    real_titles = set()
    for si, sc in enumerate(screens):
        for j, cd in enumerate(sc['cards']):
            if cd['title'] and not cd['title_zero'] and cd['row'] != ZERO:
                real_titles.add(cd['title'])
    for t, occ in zseen.items():
        if t not in real_titles:
            print('   ! ZERO-ONLY', repr(t), occ)
    print('--- within-screen repeated titles:')
    for si, sc in enumerate(screens):
        seen = {}
        for j, cd in enumerate(sc['cards']):
            if cd['title']:
                seen.setdefault(cd['title'], []).append(j)
        for t, js in seen.items():
            if len(js) > 1:
                print(f'   screen{si} rows{js} {t!r}')
    print('--- duplicate (title,spec) global pairs:')
    seen = {}
    for i, g in enumerate(titled):
        seen.setdefault((g['title'], (g['spec'] or '').strip()), []).append(i)
    for kk, v in seen.items():
        if len(v) > 1:
            print('   !', kk, v)
    print('--- cards with no 规格 / empty 规格:')
    for i, g in enumerate(titled):
        s = (g['spec'] or '').replace('规格：', '').strip()
        if s in ('无', ''):
            print('   ?', i, repr(g['title']), repr(g['spec']), g['inst'])
    inv = BASE / cat / 'inventory.json'
    if inv.exists():
        data = json.loads(inv.read_text())
        names = [c['name'] for c in data['cards']]
        xt = list(dict.fromkeys(g['title'] for g in titled))
        print(f'-- inventory title_count={data.get("title_count")} card_count={data.get("card_count")} '
              f'unique={data.get("unique_cards")} len={len(names)}')
        print(f'-- xml distinct titles={len(xt)}  xml_cards={len(titled)}')
        print('-- XML-only:')
        for t in xt:
            if t not in set(names):
                print('   *', t)
        print('-- inventory-only:')
        for n in names:
            if n not in set(xt):
                print('   *', n)


if __name__ == '__main__':
    main()
