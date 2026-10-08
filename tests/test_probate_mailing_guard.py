"""_write_mailing_address must never overwrite a probate mailing address.

2026-10-08: two probate records whose PR/heir lives in the decedent's house
(mailing == property, from the filing or the user) were treated as holding a
placeholder and overwritten with skip-trace vendor addresses. Offline: the
DataSift write is stubbed.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import skip_trace_agent as sta  # noqa: E402


def _subject(**extra):
    s = {
        "owner_uuid": "owner-1",
        "name": "Anita Lewis",
        "property_address": "2264 N Xanthus Ave",
        "current_mail_street": "2264 N Xanthus Ave",
        "people": [{"is_primary": True, "mailing_street": "243 E Mohawk Blvd",
                    "mailing_city": "Tulsa", "mailing_state": "OK", "mailing_zip": "74106"}],
    }
    s.update(extra)
    return s


class ProbateMailingGuard(unittest.TestCase):
    def _run(self, subject):
        result = {"skipped": []}
        with mock.patch.object(sta._api, "update_owner_address",
                               return_value={"ok": True}) as upd:
            sta._write_mailing_address(subject, result)
        return upd, result

    def test_probate_mailing_equal_to_property_is_never_overwritten(self):
        upd, result = self._run(_subject(decedent_name="Smiley Anderson"))
        upd.assert_not_called()
        self.assertNotIn("mailing_updated", result)

    def test_probate_detected_by_pr_alone(self):
        upd, _ = self._run(_subject(personal_representative="Anita K. Lewis"))
        upd.assert_not_called()

    def test_foreclosure_placeholder_still_replaced(self):
        upd, result = self._run(_subject())
        upd.assert_called_once()
        self.assertEqual(upd.call_args[0][1]["street"], "243 E Mohawk Blvd")
        self.assertEqual(result["mailing_updated"], 1)

    def test_foreclosure_real_mailing_still_kept(self):
        upd, _ = self._run(_subject(current_mail_street="PO Box 195"))
        upd.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
