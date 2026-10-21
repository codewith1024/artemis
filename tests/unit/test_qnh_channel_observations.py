"""Real channel-row absence is unknown, not a made-up listing status."""
import unittest
from pathlib import Path

import qnh_capture_common as C
import qnh_sku_capture as K
from qnh_observation import parse_nodes


class RealChannelObservationTests(unittest.TestCase):
    def rows(self, name):
        path = Path(__file__).parents[1] / 'fixtures' / name
        return K.parse_sku_details(parse_nodes(path.read_text()))

    def test_wowo_keeps_both_skus_but_does_not_guess_jd_40g(self):
        rows = self.rows('qnh_wowo_channel_absent.xml')
        self.assertEqual(len(rows), 2)
        a, b = rows
        self.assertEqual(a['规格'], '鳕鱼虾味1kg')
        self.assertEqual(a['京东到家库存'], '4')
        self.assertEqual(b['SKU'], '2098339639743791201')
        self.assertEqual(b['京东到家状态'], '未知')
        self.assertEqual(b['京东到家零售价'], '')
        self.assertEqual(b['京东到家库存'], '')
        self.assertEqual(b['美团闪购零售价'], '2.8')
        self.assertEqual(b['美团闪购库存'], '售罄')
        self.assertEqual(set(b['_channel_observations']), {'京东到家'})
        self.assertIn('京东到家详情', C.row_detail_errors(b))

    def test_yanxuan_reports_two_absent_channels_without_losing_eleme(self):
        rows = self.rows('qnh_yanxuan_channel_absent.xml')
        self.assertEqual(len(rows), 2)
        b = next(x for x in rows if x['规格'] == '3包')
        self.assertEqual(b['饿了么零售价'], '159')
        self.assertEqual(b['饿了么库存'], '2')
        self.assertEqual(set(b['_channel_observations']), {'美团闪购', '京东到家'})
        for channel in b['_channel_observations']:
            self.assertEqual(b[channel + '状态'], '未知')
            self.assertEqual(b[channel + '零售价'], '')
            self.assertEqual(b[channel + '库存'], '')
            self.assertEqual(b['_channel_observations'][channel]['section_status'], '已上架')

    def test_no_footer_cannot_turn_unrendered_channel_into_absent_observation(self):
        path = Path(__file__).parents[1] / 'fixtures/qnh_wowo_channel_absent.xml'
        nodes = [n for n in parse_nodes(path.read_text()) if n['text'] != '其他信息']
        with self.assertRaises(C.CaptureContractError):
            K.parse_sku_details(nodes)


if __name__ == '__main__':
    unittest.main()
