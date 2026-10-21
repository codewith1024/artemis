import sys
sys.path.insert(0, '/tmp/qnh_xml')
from analyze import load, merge_v4

for cat in ('cat_food', 'cat_snacks'):
    screens = load(cat)
    print('==', cat)
    for i in range(len(screens) - 1):
        a = [c['title'] for c in screens[i]['cards'] if c['title']]
        b = [c['title'] for c in screens[i + 1]['cards'] if c['title']]
        dup = (a and b and a[-1] == b[0])
        # a card appearing twice in the SAME screen
        within = len(a) != len(set(a))
        if dup or within:
            print(f'   screens {i}->{i+1} boundary_dup={dup} within_dup={within}')
            print('      prev last:', (a[-1] or '')[:40])
            print('      next first:', (b[0] or '')[:40])
    G = merge_v4(screens)
    # any card instance recorded twice from the same screen?
    for idx, g in enumerate(G):
        scs = [s for s, j, k in g['inst']]
        if len(scs) != len(set(scs)):
            print(f'   !! card {idx} {g["title"][:40]!r} instances from same screen: {g["inst"]}')
    print(f'   cards={len(G)} total_instances={sum(len(g["inst"]) for g in G)}')
