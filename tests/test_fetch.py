"""Characterization tests: the pagination helpers against the mock's quirks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from frontegg_data_export import fetch, logs
from frontegg_data_export.client import ApiError, FronteggClient
from tests.mock_frontegg import CLIENT_ID, CLIENT_SECRET, MockFrontegg, make_dataset


class FetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = make_dataset()
        cls.mock = MockFrontegg(cls.ds).start()

    @classmethod
    def tearDownClass(cls):
        cls.mock.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patches = [
            mock.patch.object(logs, "LOG_PATH", Path(self.tmp.name) / "export.log"),
            mock.patch.object(logs, "_log_fp", None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._close_log)
        self.mock.requests.clear()
        self.client = FronteggClient(self.mock.url, CLIENT_ID, CLIENT_SECRET, sleep=lambda s: None)
        self.client.authenticate()

    def _close_log(self):
        if logs._log_fp:
            logs._log_fp.close()
        self.tmp.cleanup()

    def test_users_page_index_offset(self):
        users = fetch.pull_pages_by_pageindex(self.client, "/identity/resources/users/v3", 200, "users")
        self.assertEqual(len(users), len(self.ds.users))
        self.assertGreater(len(self.ds.users), 200, "dataset must span more than one page")
        offsets = [r["query"]["_offset"][0] for r in self.mock.calls_to("/identity/resources/users/v3")]
        self.assertEqual(offsets[:2], ["0", "1"])

    def test_tenants_page_index_offset(self):
        tenants = fetch.pull_pages_by_pageindex(self.client, "/tenants/resources/tenants/v2", 200, "tenants")
        self.assertEqual({t["tenantId"] for t in tenants}, {t["tenantId"] for t in self.ds.tenants})

    def test_enriched_plans_return_more_than_ten(self):
        plans = fetch.pull_paginated_limit_offset(
            self.client, "/entitlements/resources/plans/v1/enriched", limit=200, label="plans")
        self.assertEqual(len(plans), 12)
        self.assertIn("assignedTenantsCount", plans[0])

    def test_features_limit_100(self):
        features = fetch.pull_paginated_limit_offset(
            self.client, "/entitlements/resources/features/v1", limit=100, label="features")
        self.assertEqual(len(features), len(self.ds.features))

    def test_entitlements_paged_by_ten(self):
        ents = fetch.pull_entitlements(self.client)
        self.assertEqual(len(ents), len(self.ds.entitlements))
        self.assertGreater(len(ents), 10)
        limits = {r["query"]["limit"][0] for r in self.mock.calls_to("/entitlements/resources/entitlements/v2")}
        self.assertEqual(limits, {"10"})

    def test_hierarchy_one_call_per_reseller_with_tenant_header(self):
        tenants = list(self.ds.tenants)
        trees, failed = fetch.pull_hierarchy(self.client, tenants, [])
        self.assertEqual(failed, [])
        resellers = [t["tenantId"] for t in tenants if t["isReseller"]]
        self.assertEqual([t["tenantId"] for t in trees], resellers)
        calls = self.mock.calls_to("/tenants/resources/hierarchy/v1/tree")
        self.assertEqual([c["tenant"] for c in calls], resellers)
        walked = {n["tenantId"] for tree in trees for n in fetch._walk_tree(tree)}
        self.assertEqual(len(walked), 7)   # 2 roots + 5 descendants

    # ---- the mock itself reproduces the quirks --------------------------
    def test_mock_plain_limit_on_users_returns_stale_page(self):
        resp = self.client.get("/identity/resources/users/v3", {"limit": 200, "offset": 0})
        self.assertEqual(len(resp["items"]), 10)

    def test_mock_item_offset_on_users_returns_empty(self):
        resp = self.client.get("/identity/resources/users/v3", {"_limit": 200, "_offset": 200})
        self.assertEqual(resp["items"], [])

    def test_mock_entitlements_limit_above_ten_returns_nothing(self):
        resp = self.client.get("/entitlements/resources/entitlements/v2", {"limit": 11, "offset": 0})
        self.assertEqual(resp["items"], [])

    def test_mock_non_enriched_plans_capped_at_ten(self):
        resp = self.client.get("/entitlements/resources/plans/v1", {"limit": 200, "offset": 0})
        self.assertEqual(len(resp["items"]), 10)

    def test_mock_hierarchy_without_header_is_403(self):
        with self.assertRaises(ApiError) as cm:
            self.client.get("/tenants/resources/hierarchy/v1/tree")
        self.assertEqual(cm.exception.status, 403)

    def test_mock_long_url_is_414(self):
        ids = ",".join(u["id"] for u in self.ds.users[:250])
        with self.assertRaises(ApiError) as cm:
            self.client.get("/identity/resources/users/v3/roles", {"ids": ids},
                            tenant_id=self.ds.tenants[-1]["tenantId"])
        self.assertEqual(cm.exception.status, 414)

    def test_role_lookups_batched_per_tenant_in_chunks_of_100(self):
        users = list(self.ds.users)
        assignments, failed = fetch.pull_user_role_assignments(self.client, users, [])
        self.assertEqual(failed, [])
        self.assertEqual(len(assignments), len(self.ds.role_assignments))
        big = next(t["tenantId"] for t in self.ds.tenants if t["name"] == "Acme Big")
        big_calls = [c for c in self.mock.calls_to("/identity/resources/users/v3/roles") if c["tenant"] == big]
        sizes = [len(c["query"]["ids"][0].split(",")) for c in big_calls]
        self.assertEqual(sizes, [100, 100, 30])


if __name__ == "__main__":
    unittest.main()
