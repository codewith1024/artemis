"""回顶路径的离线回归：列表页非空（可能正停在底部）时必须真的回顶。"""

import unittest
from unittest import mock

import qnh_product_scan as scan

CATEGORY = "休闲零食/薯片薯条"


def product(title, barcode):
    return {"title": title, "barcode": barcode, "spec": "100g", "tap": (0, 0)}


class ResetToTopTests(unittest.TestCase):
    """scan.reset_to_top_verified：回顶必须真做，且失败不装作回顶。"""

    def setUp(self):
        self.bottom_nodes = [{"text": "薯片薯条（12）"}]
        self.bottom_products = [product("底部商品", "6900000000001")]
        self.top_nodes = [{"text": "薯片薯条（12）"}]
        self.top_products = [product("顶部商品", "6900000000002")]

    def test_bottom_list_still_triggers_real_reset(self):
        # 列表页非空（正停在底部）时也必须 reset_category_to_top：用回顶后的
        # 顶部重扫，拿底部当"顶部"只会原地打转。
        with mock.patch.object(scan, "ensure_list_page",
                               return_value=(self.bottom_nodes,
                                             self.bottom_products)), \
             mock.patch.object(scan, "reset_category_to_top",
                               return_value=(self.top_nodes,
                                             self.top_products)) as reset, \
             mock.patch.object(scan, "category_selection_valid",
                               return_value=True):
            nodes, products = scan.reset_to_top_verified(CATEGORY)
        reset.assert_called_once_with(CATEGORY)
        self.assertEqual(products, self.top_products)
        self.assertEqual(nodes, self.top_nodes)

    def test_failed_reset_does_not_fall_back_to_bottom_list(self):
        with mock.patch.object(scan, "ensure_list_page",
                               return_value=(self.bottom_nodes,
                                             self.bottom_products)), \
             mock.patch.object(scan, "reset_category_to_top",
                               return_value=([], [])), \
             mock.patch.object(scan, "category_selection_valid",
                               return_value=True):
            nodes, products = scan.reset_to_top_verified(CATEGORY)
        self.assertEqual((nodes, products), ([], []))

    def test_reset_with_lost_category_is_rejected(self):
        with mock.patch.object(scan, "ensure_list_page",
                               return_value=(self.bottom_nodes,
                                             self.bottom_products)), \
             mock.patch.object(scan, "reset_category_to_top",
                               return_value=(self.top_nodes,
                                             self.top_products)), \
             mock.patch.object(scan, "category_selection_valid",
                               side_effect=[True, False]), \
             mock.patch.object(scan, "restore_category_after_navigation",
                               return_value=(self.top_nodes, self.top_products)):
            nodes, products = scan.reset_to_top_verified(CATEGORY)
        self.assertEqual((nodes, products), ([], []))


if __name__ == "__main__":
    unittest.main()
