"""Offline tests for datasift_uploader._ensure_boards_after_enrich (2026-09-30).

All API calls are stubbed -- nothing touches the CRM.
Run: python tests/test_board_recheck.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import datasift_uploader as up  # noqa: E402


class FakeAPI:
    DataSiftAPIError = Exception

    def __init__(self, boards, owners, dry=False):
        self.boards, self.owners, self.dry, self.posts = boards, owners, dry, []

    def is_dry_run(self):
        return self.dry

    def get_property(self, uuid):
        return {"owner": {"uuid": self.owners[uuid]}}

    def get_message_board(self, owner_uuid):
        return [{"message": m} for m in self.boards.get(owner_uuid, [])]

    def post_message_board(self, owner_uuid, message):
        self.posts.append((owner_uuid, message))
        self.boards.setdefault(owner_uuid, []).insert(0, message)


PET = "FORECLOSURE PETITION\nCASE\n  Case Number: CJ-2026-1"
failures = 0


def check(name, cond):
    global failures
    print(("PASS " if cond else "FAIL ") + name)
    failures += not cond


# 1. present -> nothing posted (whitespace differences ignored)
api = FakeAPI({"o1": ["Tracerfy summary", PET.replace("\n", "\n ")]}, {"p1": "o1"})
up._api = api
r = {}
up._ensure_boards_after_enrich({"p1": PET}, r)
check("present post is not duplicated", api.posts == [] and "board_reposted" not in r)

# 2. missing (owner replaced) -> re-posted once to the CURRENT owner, verified
api = FakeAPI({"old": [PET], "new": []}, {"p1": "new"})
up._api = api
r = {}
up._ensure_boards_after_enrich({"p1": PET}, r)
check("missing post re-posted to current owner",
      api.posts == [("new", PET)] and r["board_reposted"] == [{"uuid": "p1", "verified": True}])

# 3. dry run -> no reads, no writes
api = FakeAPI({"new": []}, {"p1": "new"}, dry=True)
up._api = api
up._ensure_boards_after_enrich({"p1": PET}, {})
check("dry run writes nothing", api.posts == [])

# 4. mixed batch -> only the missing one is touched
api = FakeAPI({"a": [PET + "A"], "b": []}, {"pa": "a", "pb": "b"})
up._api = api
up._ensure_boards_after_enrich({"pa": PET + "A", "pb": PET + "B"}, {})
check("only the missing record is re-posted", api.posts == [("b", PET + "B")])

print("FAILURES:", failures)
sys.exit(1 if failures else 0)
