# test_zen_server.py
"""Тесты для чистой логики фильтрации/пагинации в zen_server.py -- то, что
раньше делал JS над ПОЛНЫМ allData в браузере (и уронило вкладку по памяти
на 13k+ строках). Сама HTTP-обвязка проверяется вручную через curl, как и
раньше в этом проекте."""
import unittest

import zen_server as zs


def make_item(**over):
    item = {
        "id": 1, "uid": "narrative::x", "comp": "narrative",
        "comp_name": "Сюжет", "key": "x", "ru": "текст", "en": "text",
        "status": "pending", "score": 0, "up": 0, "down": 0, "user_vote": 0,
        "suggestions": [], "history": [], "qa_warnings": [],
        "truncated": False, "clause": False, "color_tagged": False,
        "event_id": "", "event_role": "",
    }
    item.update(over)
    return item


class TestParseQuery(unittest.TestCase):
    def test_plain_query_targets_all_fields(self):
        parsed = zs.parse_query("hello")
        self.assertEqual(parsed.target_field, "all")
        self.assertEqual(parsed.clean_q, "hello")

    def test_ru_prefix(self):
        parsed = zs.parse_query("!ru комитет")
        self.assertEqual(parsed.target_field, "ru")
        self.assertEqual(parsed.clean_q, "комитет")

    def test_en_prefix(self):
        parsed = zs.parse_query("!en committee")
        self.assertEqual(parsed.target_field, "en")
        self.assertEqual(parsed.clean_q, "committee")

    def test_key_prefix(self):
        parsed = zs.parse_query("!key chaplain")
        self.assertEqual(parsed.target_field, "key")
        self.assertEqual(parsed.clean_q, "chaplain")

    def test_has_var_extracted(self):
        parsed = zs.parse_query("has:var")
        self.assertTrue(parsed.has_var)
        self.assertEqual(parsed.clean_q, "")

    def test_has_color_sugg_diff_extracted(self):
        parsed = zs.parse_query("has:color has:sugg has:diff")
        self.assertTrue(parsed.has_color)
        self.assertTrue(parsed.has_sugg)
        self.assertTrue(parsed.has_diff)

    def test_lowercased(self):
        parsed = zs.parse_query("HELLO")
        self.assertEqual(parsed.clean_q, "hello")


class TestItemMatches(unittest.TestCase):
    def test_status_all_matches_everything(self):
        item = make_item(status="pending")
        parsed = zs.parse_query("")
        self.assertTrue(zs.item_matches(item, "all", "all", parsed))

    def test_admin_approved_status_filter(self):
        approved = make_item(status="admin_approved")
        pending = make_item(status="pending")
        parsed = zs.parse_query("")
        self.assertTrue(zs.item_matches(approved, "admin_approved", "all", parsed))
        self.assertFalse(zs.item_matches(pending, "admin_approved", "all", parsed))

    def test_has_suggestions_status_filter(self):
        with_sugg = make_item(suggestions=[{"id": 1, "score": 0}])
        without = make_item()
        parsed = zs.parse_query("")
        self.assertTrue(zs.item_matches(with_sugg, "has_suggestions", "all", parsed))
        self.assertFalse(zs.item_matches(without, "has_suggestions", "all", parsed))

    def test_unvoted_status_filter(self):
        untouched = make_item(status="pending", up=0, down=0, suggestions=[])
        voted = make_item(status="pending", up=1)
        parsed = zs.parse_query("")
        self.assertTrue(zs.item_matches(untouched, "unvoted", "all", parsed))
        self.assertFalse(zs.item_matches(voted, "unvoted", "all", parsed))

    def test_admin_queue_requires_activity_and_not_approved(self):
        active = make_item(status="pending", up=1)
        inactive = make_item(status="pending")
        approved_active = make_item(status="admin_approved", up=1)
        parsed = zs.parse_query("")
        self.assertTrue(zs.item_matches(active, "admin_queue", "all", parsed))
        self.assertFalse(zs.item_matches(inactive, "admin_queue", "all", parsed))
        self.assertFalse(zs.item_matches(approved_active, "admin_queue", "all", parsed))

    def test_component_filter(self):
        item = make_item(comp="labels")
        parsed = zs.parse_query("")
        self.assertTrue(zs.item_matches(item, "all", "labels", parsed))
        self.assertFalse(zs.item_matches(item, "all", "narrative", parsed))

    def test_has_var_filter(self):
        with_var = make_item(ru="привет %s")
        without = make_item(ru="привет")
        parsed = zs.parse_query("has:var")
        self.assertTrue(zs.item_matches(with_var, "all", "all", parsed))
        self.assertFalse(zs.item_matches(without, "all", "all", parsed))

    def test_has_color_filter(self):
        colored = make_item(color_tagged=True)
        plain = make_item(color_tagged=False)
        parsed = zs.parse_query("has:color")
        self.assertTrue(zs.item_matches(colored, "all", "all", parsed))
        self.assertFalse(zs.item_matches(plain, "all", "all", parsed))

    def test_free_text_search_matches_ru_en_key(self):
        item = make_item(ru="капеллан", en="chaplain", key="chaplain_key")
        self.assertTrue(zs.item_matches(item, "all", "all", zs.parse_query("капеллан")))
        self.assertTrue(zs.item_matches(item, "all", "all", zs.parse_query("chaplain")))
        self.assertFalse(zs.item_matches(item, "all", "all", zs.parse_query("nomatch")))

    def test_targeted_search_ru_only(self):
        item = make_item(ru="капеллан", en="chaplain")
        parsed = zs.parse_query("!ru chaplain")  # englisn text, ru field only
        self.assertFalse(zs.item_matches(item, "all", "all", parsed))
        parsed2 = zs.parse_query("!ru капеллан")
        self.assertTrue(zs.item_matches(item, "all", "all", parsed2))


class TestAdminPriorityScore(unittest.TestCase):
    def test_active_unapproved_sorts_before_inactive_unapproved(self):
        active = make_item(up=1)
        inactive = make_item()
        cat_a, _ = zs.admin_priority_score(active)
        cat_i, _ = zs.admin_priority_score(inactive)
        self.assertLess(cat_a, cat_i)

    def test_unapproved_sorts_before_approved(self):
        unapproved = make_item()
        approved = make_item(status="admin_approved")
        cat_u, _ = zs.admin_priority_score(unapproved)
        cat_a, _ = zs.admin_priority_score(approved)
        self.assertLess(cat_u, cat_a)

    def test_higher_activity_sorts_first_within_category(self):
        low = make_item(up=1)
        high = make_item(up=10)
        _, neg_activity_low = zs.admin_priority_score(low)
        _, neg_activity_high = zs.admin_priority_score(high)
        self.assertLess(neg_activity_high, neg_activity_low)


class TestQueryItems(unittest.TestCase):
    def test_pagination_slices_and_reports_total(self):
        items = [make_item(id=i, uid=f"narrative::{i}") for i in range(120)]
        page, total, _ = zs.query_items(items, comp="all", status="all", q="",
                                         offset=0, limit=50)
        self.assertEqual(len(page), 50)
        self.assertEqual(total, 120)
        self.assertEqual(page[0]["id"], 0)
        self.assertEqual(page[-1]["id"], 49)

    def test_second_page_continues_from_offset(self):
        items = [make_item(id=i, uid=f"narrative::{i}") for i in range(120)]
        page, total, _ = zs.query_items(items, comp="all", status="all", q="",
                                         offset=50, limit=50)
        self.assertEqual(page[0]["id"], 50)
        self.assertEqual(len(page), 50)

    def test_filters_then_paginates(self):
        items = ([make_item(id=i, uid=f"a::{i}", comp="narrative") for i in range(5)]
                  + [make_item(id=100 + i, uid=f"b::{i}", comp="labels") for i in range(5)])
        page, total, _ = zs.query_items(items, comp="labels", status="all", q="",
                                         offset=0, limit=50)
        self.assertEqual(total, 5)
        self.assertTrue(all(it["comp"] == "labels" for it in page))

    def test_global_stats_ignore_current_filters(self):
        items = ([make_item(id=i, uid=f"a::{i}", comp="narrative") for i in range(3)]
                  + [make_item(id=100 + i, uid=f"b::{i}", comp="labels",
                                status="admin_approved") for i in range(2)])
        _, _, stats = zs.query_items(items, comp="narrative", status="all", q="",
                                      offset=0, limit=50)
        self.assertEqual(stats["all"], 5)
        self.assertEqual(stats["admin_approved"], 2)

    def test_by_component_breakdown(self):
        items = ([make_item(id=i, uid=f"a::{i}", comp="narrative") for i in range(3)]
                  + [make_item(id=100 + i, uid=f"b::{i}", comp="labels",
                                status="admin_approved") for i in range(2)])
        _, _, stats = zs.query_items(items, comp="all", status="all", q="",
                                      offset=0, limit=50)
        self.assertEqual(stats["by_component"]["narrative"], {"total": 3, "admin_approved": 0})
        self.assertEqual(stats["by_component"]["labels"], {"total": 2, "admin_approved": 2})

    def test_admin_queue_is_sorted_by_priority(self):
        items = [
            make_item(id=1, uid="a::1", up=1),
            make_item(id=2, uid="a::2", up=10),
            make_item(id=3, uid="a::3"),
        ]
        page, total, _ = zs.query_items(items, comp="all", status="admin_queue", q="",
                                         offset=0, limit=50)
        self.assertEqual([it["id"] for it in page], [2, 1])
        self.assertEqual(total, 2)


if __name__ == "__main__":
    unittest.main()
