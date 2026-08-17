# -*- coding: utf-8 -*-
"""지역코드 매핑 회귀 테스트.

data/regions.json을 source of truth로 삼아, 프론트가 실제 데이터의 모든
시군구를 이름과 지역 트리 양쪽에서 다룰 수 있는지 검증한다.
상수는 site/index.html의 인라인 스크립트를 Node VM에서 그대로 실행해 읽는다.

실행:
    python -m unittest discover -s tests -v
"""
import glob
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

RUNNER = REPO / "tests" / "run_index_js.js"
NODE = shutil.which("node")

REGIONS = json.loads((REPO / "data" / "regions.json").read_text(encoding="utf-8"))
REGION_BY_CODE = {r["sgg_code"]: r for r in REGIONS}


def js(expression):
    proc = subprocess.run([NODE, str(RUNNER), expression], capture_output=True, cwd=str(REPO))
    if proc.returncode != 0:
        raise AssertionError("node 실행 실패:\n" + proc.stderr.decode("utf-8", "replace"))
    return json.loads(proc.stdout.decode("utf-8"))


def collected_prefixes():
    """수집 파이프라인이 실제로 도는 시도 프리픽스."""
    import config
    return {p for group in config.REGION_GROUPS.values() for p in group}


def tree_codes(tree):
    """지역 트리에서 사용자가 고를 수 있는 모든 코드."""
    codes = set()
    for nodes in tree.values():
        if nodes and isinstance(nodes[0], str):
            codes.update(nodes)
            continue
        for node in nodes:
            if node["type"] == "parent":
                codes.add(node["parentCode"])
                codes.update(node["children"])
            else:
                codes.update(node["codes"])
    return codes


def shard_sgg_codes():
    """저장소에 커밋된 모든 shard가 실제로 담고 있는 시군구코드."""
    codes = set()
    for path in glob.glob(str(REPO / "data" / "shards" / "*" / "*" / "*.json")):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for item in data.get("items", []):
            if item.get("sggCode"):
                codes.add(str(item["sggCode"]))
    return codes


@unittest.skipUnless(NODE, "node가 없어 인라인 스크립트를 실행할 수 없습니다")
class ActiveCodeCoverageTest(unittest.TestCase):
    """수집 대상 시도의 모든 코드가 이름과 트리를 갖는지."""

    @classmethod
    def setUpClass(cls):
        cls.sgg_map = js("SGG_CODE_MAP")
        cls.tree = js("OFFICIAL_REGION_TREE")
        cls.sido_names = js("SIDO_NAMES")
        cls.tree_codes = tree_codes(cls.tree)
        cls.prefixes = collected_prefixes()
        cls.active = [r for r in REGIONS if r["sgg_code"][:2] in cls.prefixes]

    def test_every_active_code_has_a_display_name(self):
        missing = [r["sgg_code"] + " " + r["full_name"]
                   for r in self.active if r["sgg_code"] not in self.sgg_map]
        self.assertEqual(missing, [], "이름 없는 코드: %s" % missing)

    # 화면에서 의도적으로 줄여 쓰는 이름. 새로운 불일치가 생기면 테스트가 잡는다.
    SHORT_LABELS = {"36110": "세종시"}

    def test_active_names_match_regions_json(self):
        wrong = []
        for r in self.active:
            code = r["sgg_code"]
            shown = self.sgg_map.get(code, "")
            if self.SHORT_LABELS.get(code) == shown:
                continue
            # 일반구는 "부천시 소사구"처럼 모시 이름을 앞에 붙여 표기한다.
            if not shown.endswith(r["sigungu_name"]):
                wrong.append("%s: regions.json=%s / 화면=%s"
                             % (code, r["sigungu_name"], shown))
        self.assertEqual(wrong, [], "이름 불일치: %s" % wrong)

    def test_every_active_code_is_reachable_in_the_region_tree(self):
        # 인천은 개편 전/후 트리를 따로 두므로 현행 트리(28_new)만 본다.
        missing = [r["sgg_code"] + " " + r["full_name"]
                   for r in self.active if r["sgg_code"] not in self.tree_codes]
        self.assertEqual(missing, [], "트리에서 접근 불가: %s" % missing)

    def test_every_active_sido_has_a_name(self):
        missing = sorted({r["sgg_code"][:2] for r in self.active
                          if r["sgg_code"][:2] not in self.sido_names})
        self.assertEqual(missing, [], "시도명 없는 프리픽스: %s" % missing)

    def test_no_phantom_code_in_the_current_tree(self):
        """regions.json에도 없고 실제 데이터에도 없는 코드는 트리에 두지 않는다."""
        real = shard_sgg_codes()
        current = set()
        for key, nodes in self.tree.items():
            if key.endswith("_old"):
                continue  # 옛 데이터 호환용 트리는 예외
            codes = tree_codes({key: nodes})
            # 45(구 전북)·29(구 광주)·46(구 전남)처럼 코드가 통째로 교체된
            # 시도의 옛 트리는 과거 데이터 호환을 위해 남겨둔 것이라 제외한다.
            if not any(c in REGION_BY_CODE for c in codes):
                continue
            current.update(codes)
        phantom = sorted(c for c in current
                         if c not in REGION_BY_CODE and c not in real)
        self.assertEqual(phantom, [], "실체 없는 코드: %s" % phantom)


@unittest.skipUnless(NODE, "node가 없어 인라인 스크립트를 실행할 수 없습니다")
class RealDataReachabilityTest(unittest.TestCase):
    """실제 랭킹 데이터의 모든 시군구가 화면에서 이름을 갖고 도달 가능한지."""

    @classmethod
    def setUpClass(cls):
        cls.codes = shard_sgg_codes()
        cls.sgg_map = js("SGG_CODE_MAP")
        cls.tree_codes = tree_codes(js("OFFICIAL_REGION_TREE"))

    def test_shards_are_not_empty(self):
        self.assertGreater(len(self.codes), 100)

    def test_every_ranked_code_has_a_name(self):
        missing = sorted(c for c in self.codes if c not in self.sgg_map)
        self.assertEqual(missing, [], "raw 코드로 표시될 시군구: %s" % missing)

    def test_every_ranked_code_is_reachable(self):
        missing = sorted(c for c in self.codes if c not in self.tree_codes)
        self.assertEqual(missing, [], "지역 필터로 도달 불가: %s" % missing)

    def test_every_ranked_code_exists_in_regions_json(self):
        unknown = sorted(c for c in self.codes if c not in REGION_BY_CODE)
        self.assertEqual(unknown, [], "regions.json에 없는 코드가 수집됨: %s" % unknown)


@unittest.skipUnless(NODE, "node가 없어 인라인 스크립트를 실행할 수 없습니다")
class IncheonReorganizationTest(unittest.TestCase):
    """인천 구 개편: 개편 후 코드가 현행, 개편 전 코드는 호환용."""

    @classmethod
    def setUpClass(cls):
        cls.tree = js("OFFICIAL_REGION_TREE")
        cls.sgg_map = js("SGG_CODE_MAP")
        cls.regions_28 = sorted(r["sgg_code"] for r in REGIONS
                                if r["sgg_code"].startswith("28"))

    def test_current_tree_matches_regions_json_exactly(self):
        self.assertEqual(sorted(self.tree["28_new"]), self.regions_28)

    def test_new_districts_have_names_from_regions_json(self):
        for code in ("28125", "28155", "28275", "28290"):
            self.assertEqual(self.sgg_map.get(code),
                             REGION_BY_CODE[code]["sigungu_name"])

    def test_legacy_tree_is_kept_for_old_data(self):
        self.assertIn("28_old", self.tree)
        for code in ("28110", "28140", "28260"):
            self.assertIn(code, self.sgg_map)

    def test_tree_choice_follows_the_data_not_the_month(self):
        """개편 전 달에도 신설 코드 거래가 섞여 들어오므로 데이터로 판단한다."""
        self.assertEqual(js("incheonTreeKey([{sggCode: '28275'}])"), "28_new")
        self.assertEqual(js("incheonTreeKey([])"), "28_new")
        self.assertEqual(js("incheonTreeKey([{sggCode: '28110'}])"), "28_old")

    def test_no_duplicate_district_name_in_the_current_tree(self):
        shown = [self.sgg_map[c] for c in self.tree["28_new"]]
        self.assertEqual(len(shown), len(set(shown)), "중복 구 이름: %s" % shown)


@unittest.skipUnless(NODE, "node가 없어 인라인 스크립트를 실행할 수 없습니다")
class BucheonDistrictTest(unittest.TestCase):
    """부천시 일반구 코드-이름 대응."""

    @classmethod
    def setUpClass(cls):
        cls.sgg_map = js("SGG_CODE_MAP")
        cls.children = js("OFFICIAL_REGION_TREE['41']"
                          ".find(n => n.parentCode === '41190').children")

    def test_names_match_regions_json(self):
        for code in ("41192", "41194", "41196"):
            self.assertEqual(self.sgg_map[code],
                             "부천시 " + REGION_BY_CODE[code]["sigungu_name"].split()[-1])

    def test_sosa_is_41194_not_41193(self):
        self.assertEqual(self.sgg_map["41194"], "부천시 소사구")
        self.assertNotIn("41193", self.sgg_map)

    def test_tree_children_match_regions_json(self):
        self.assertEqual(sorted(self.children), ["41192", "41194", "41196"])


@unittest.skipUnless(NODE, "node가 없어 인라인 스크립트를 실행할 수 없습니다")
class MacroGroupCoverageTest(unittest.TestCase):
    """시군구 지수 탭: R-ONE 지역명 토큰이 모두 권역에 속하는지."""

    AGGREGATE_ROWS = {"5대광역시", "6대광역시"}

    @classmethod
    def setUpClass(cls):
        cls.items = json.loads(
            (REPO / "site" / "data" / "region_rankings_latest.json").read_text(encoding="utf-8")
        )["items"]
        cls.tokens = {i["regionName"].split(">")[0] for i in cls.items}

    def macro_match(self):
        html = (REPO / "site" / "index.html").read_text(encoding="utf-8")
        start = html.index("const MACRO_GROUPS")
        block = html[start:html.index("\n];", start)]
        import re
        return {m for m in re.findall(r'"([^"]+)"', block)}

    def test_every_real_region_token_belongs_to_a_macro_group(self):
        covered = self.macro_match()
        orphan = sorted(t for t in self.tokens
                        if t not in covered and t not in self.AGGREGATE_ROWS)
        self.assertEqual(orphan, [], "권역에 속하지 않는 지역: %s" % orphan)

    def test_jeonnam_gwangju_token_is_covered(self):
        self.assertIn("전남광주", self.tokens, "데이터에 전남광주 토큰이 없습니다")
        self.assertIn("전남광주", self.macro_match())

    def test_aggregate_rows_are_left_out_on_purpose(self):
        """5대/6대광역시는 시군구가 아니라 집계행이라 권역에 넣지 않는다."""
        for name in self.AGGREGATE_ROWS:
            rows = [i for i in self.items if i["regionName"] == name]
            self.assertEqual(len(rows), 1)
            self.assertNotIn(">", rows[0]["regionName"])
            self.assertNotIn(name, self.macro_match())

    def test_macro_groups_cover_every_item_exactly_once(self):
        covered = self.macro_match()
        counted = sum(1 for i in self.items
                      if i["regionName"].split(">")[0] in covered)
        self.assertEqual(counted + len(self.AGGREGATE_ROWS), len(self.items))


class UncollectedSidoTest(unittest.TestCase):
    """수집 범위 밖 시도는 프론트 매핑 문제가 아니다 (별도 후속 과제)."""

    def test_sido_12_is_outside_the_collection_scope(self):
        prefixes = collected_prefixes()
        self.assertNotIn("12", prefixes,
                         "12가 수집 범위에 들어오면 프론트 매핑도 함께 추가해야 합니다")

    def test_sido_12_has_no_ranking_data(self):
        codes = shard_sgg_codes()
        self.assertEqual([c for c in codes if c.startswith("12")], [])

    def test_regions_json_still_lists_sido_12(self):
        """regions.json에는 있으므로 수집 범위가 넓어지면 바로 드러난다."""
        self.assertEqual(len([r for r in REGIONS if r["sgg_code"].startswith("12")]), 27)


if __name__ == "__main__":
    unittest.main()
