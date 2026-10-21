"""Cross-screen collector termination uses observed bottom, not a page cap."""
import unittest
from pathlib import Path
from unittest import mock

import qnh_capture_common as C
import qnh_product_scan as S


class SkuPageCollectionTests(unittest.TestCase):
    def fixture(self, name):
        return S.parse_nodes((Path(__file__).parents[1] / 'fixtures' / name).read_text())

    def test_collect_parses_only_after_footer_beyond_all_channel_tables(self):
        top = self.fixture('qnh_multispec_detail.xml')
        bottom = [dict(n) for n in top]
        rows = [{'SKU': 'one'}, {'SKU': 'two'}]
        with mock.patch.object(S, 'human_swipe') as swipe, \
             mock.patch.object(S, 'dump_nodes_undisturbed', return_value=bottom), \
             mock.patch.object(S.time, 'sleep'), \
             mock.patch.object(S.SKU_CAPTURE, 'parse_sku_pages', return_value=rows, create=True) as parse, \
             mock.patch.object(C, 'row_detail_errors', return_value=[]):
            self.assertEqual(S.collect_sku_detail({}, top, 1080, 4560), rows)
        self.assertEqual(swipe.call_count, 0)
        self.assertEqual(len(parse.call_args.args[0]), 1)

    def test_real_ten_sku_collector_joins_all_three_screens(self):
        pages = [self.fixture(f'qnh_longsku_page_{i:02d}.xml') for i in range(3)]
        product = {'title': '【10种面值可选】筹码币 德州扑克筹码棋牌室麻将卡片筹码代币炸金花打牌专用娱乐筹码卡1张',
                   'barcode': '', 'spec': '20，2，500，100，1，1000，10，5，200，50'}
        with mock.patch.object(S, 'human_swipe') as swipe, \
             mock.patch.object(S, 'dump_nodes_undisturbed', side_effect=pages[1:]), \
             mock.patch.object(S.time, 'sleep'):
            rows = S.collect_sku_detail(product, pages[0], 1080, 4560)
        self.assertEqual(swipe.call_count, 2)
        self.assertEqual(len(rows), 10)
        self.assertEqual(len({row['SKU'] for row in rows}), 10)
        self.assertEqual({row['规格'] for row in rows},
                         {'20', '2', '500', '100', '1', '1000', '10', '5', '200', '50'})
        self.assertTrue(all(not C.row_detail_errors(row) for row in rows))

    def test_collector_refuses_missing_start_identity(self):
        with self.assertRaisesRegex(C.CaptureContractError, '首屏身份区'):
            S.collect_sku_detail({}, [], 1080, 4560)

    def test_collector_never_claims_complete_when_bottom_is_unproved(self):
        top = self.fixture('qnh_multispec_detail.xml')
        without_bottom = [n for n in top if n['text'] != '店内码/货号']
        with mock.patch.object(S, 'human_swipe'), \
             mock.patch.object(S, 'dump_nodes_undisturbed', return_value=without_bottom), \
             mock.patch.object(S.time, 'sleep'), \
             mock.patch.object(S.SKU_CAPTURE, 'parse_sku_pages', create=True) as parse:
            with self.assertRaisesRegex(C.CaptureContractError, '证实到底'):
                S.collect_sku_detail({}, without_bottom, 1080, 4560)
            parse.assert_not_called()


if __name__ == '__main__':
    unittest.main()
