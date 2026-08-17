# -*- coding: utf-8 -*-
"""건축년도(buildYear) 파이프라인과 전북특별자치도(52) 매핑 회귀 테스트.

추가 의존성 없이 표준 unittest만 사용한다.
프론트 상수는 site/index.html의 인라인 스크립트에서 그대로 읽어와 검증하므로,
사람이 손으로 옮겨 적은 사본이 아니라 실제 배포되는 값이 대상이다.

실행:
    python -m unittest discover -s tests -v
"""
import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

INDEX_HTML = (REPO / "site" / "index.html").read_text(encoding="utf-8")
INLINE_JS = (REPO / "site" / "assets" / "apartment-inline.js").read_text(encoding="utf-8")
REGIONS = json.loads((REPO / "data" / "regions.json").read_text(encoding="utf-8"))


def js_object_literal(name):
    """index.html에서 `const NAME = {...};` 본문을 잘라 JSON으로 읽는다."""
    start = INDEX_HTML.index("const %s = {" % name)
    brace = INDEX_HTML.index("{", start)
    depth, i = 0, brace
    while True:
        if INDEX_HTML[i] == "{":
            depth += 1
        elif INDEX_HTML[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = INDEX_HTML[brace:i + 1]
    body = re.sub(r"//[^\n]*", "", body)          # 주석 제거
    body = re.sub(r",(\s*[}\]])", r"\1", body)     # 후행 쉼표 제거
    body = re.sub(r"(\w+)\s*:", r'"\1":', body)    # 따옴표 없는 키
    body = body.replace("'", '"')
    return json.loads(body)


class RepresentativeBuildYearTest(unittest.TestCase):
    """대표 건축년도 규칙: 유효값의 최빈값, 동률이면 큰 연도."""

    def setUp(self):
        from compute_apt_rankings import representative_build_year
        self.fn = representative_build_year

    def test_single_value(self):
        self.assertEqual(self.fn([2018, 2018, 2018]), 2018)

    def test_mode_wins_over_frequency_of_outlier(self):
        self.assertEqual(self.fn([2018, 2018, 1999]), 2018)

    def test_tie_picks_the_later_year(self):
        self.assertEqual(self.fn([1999, 2018]), 2018)
        self.assertEqual(self.fn([2018, 1999]), 2018)

    def test_zero_and_none_are_not_valid_values(self):
        self.assertEqual(self.fn([0, None, 2005, 0]), 2005)

    def test_no_valid_value_returns_none(self):
        self.assertIsNone(self.fn([]))
        self.assertIsNone(self.fn(None))
        self.assertIsNone(self.fn([0, None, ""]))

    def test_never_invents_an_unobserved_year(self):
        """평균·중앙값을 쓰면 2010이 나오는 입력. 실제 있었던 연도만 골라야 한다."""
        self.assertIn(self.fn([2000, 2020]), (2000, 2020))
        self.assertNotEqual(self.fn([2000, 2020]), 2010)


class BuildYearPipelineTest(unittest.TestCase):
    """수집 → DB → 순위 JSON → 화면까지 build_year가 끊기지 않는지."""

    def test_db_schema_has_build_year_column(self):
        text = (SCRIPTS / "db.py").read_text(encoding="utf-8")
        self.assertIn("build_year", text)

    def test_collector_parses_build_year(self):
        text = (SCRIPTS / "collect_trades.py").read_text(encoding="utf-8")
        self.assertIn("build_year", text)

    def test_ranking_query_selects_build_year(self):
        text = (SCRIPTS / "compute_apt_rankings.py").read_text(encoding="utf-8")
        self.assertRegex(text, r"SELECT[\s\S]{0,400}build_year")

    def test_ranking_item_carries_build_year_field(self):
        text = (SCRIPTS / "compute_apt_rankings.py").read_text(encoding="utf-8")
        self.assertIn('"buildYear": representative_build_year(', text)

    def test_shard_schema_does_not_reject_extra_field(self):
        """buildYear를 추가해도 shard 검증이 막지 않는지 (화이트리스트 부재 확인)."""
        text = (SCRIPTS / "shard_schema.py").read_text(encoding="utf-8")
        self.assertIsNone(
            re.search(r"allowed_keys|ALLOWED_KEYS|unexpected", text),
            "shard_schema에 필드 화이트리스트가 생기면 buildYear가 거부될 수 있다",
        )


class BuildYearRenderingTest(unittest.TestCase):
    """표시 규칙: 값이 없으면 '미상', 절대 '0년'/'null년'이 나오지 않는다."""

    def test_index_has_build_year_formatter(self):
        self.assertIn("function formatBuildYearLabel(item)", INDEX_HTML)

    def test_formatter_guards_zero_and_missing(self):
        start = INDEX_HTML.index("function formatBuildYearLabel(item)")
        body = INDEX_HTML[start:start + 400]
        self.assertIn("건축년도 미상", body)
        self.assertIn("year <= 0", body)
        self.assertIn("Number.isFinite", body)

    def test_ranking_row_renders_build_year(self):
        self.assertIn("formatBuildYearLabel(i)", INDEX_HTML)
        self.assertIn('data-build-year=', INDEX_HTML)

    def test_inline_panel_reads_build_year_from_row(self):
        self.assertIn('getAttribute("data-build-year")', INLINE_JS)
        self.assertIn("건축년도 미상", INLINE_JS)
        self.assertIn("년 건축", INLINE_JS)

    def test_no_approval_wording(self):
        """'준공일'·'사용승인일'은 build_year의 의미가 아니므로 쓰지 않는다."""
        for text in (INDEX_HTML, INLINE_JS):
            self.assertNotIn("준공일", text)
            self.assertNotIn("사용승인일", text)


class JeonbukMappingTest(unittest.TestCase):
    """전북특별자치도(52) 매핑. 기준은 data/regions.json."""

    @classmethod
    def setUpClass(cls):
        cls.codes_52 = sorted(
            r["sgg_code"] for r in REGIONS if r["sgg_code"].startswith("52")
        )
        cls.names_52 = {
            r["sgg_code"]: r["sigungu_name"]
            for r in REGIONS if r["sgg_code"].startswith("52")
        }
        cls.sgg_map = js_object_literal("SGG_CODE_MAP")
        cls.sido_names = js_object_literal("SIDO_NAMES")
        cls.tree = js_object_literal("OFFICIAL_REGION_TREE")

    def test_regions_json_uses_52_not_45(self):
        self.assertEqual(len(self.codes_52), 16)
        self.assertEqual([r for r in REGIONS if r["sgg_code"].startswith("45")], [])

    def test_sido_names_has_52(self):
        self.assertEqual(self.sido_names.get("52"), "전북")

    def test_every_52_code_has_a_name(self):
        missing = [c for c in self.codes_52 if c not in self.sgg_map]
        self.assertEqual(missing, [], "이름 없는 52 코드: %s" % missing)

    def test_52_names_match_regions_json(self):
        for code, name in self.names_52.items():
            self.assertEqual(self.sgg_map[code], name, "코드 %s 이름 불일치" % code)

    def test_tree_covers_every_selectable_52_code(self):
        nodes = self.tree["52"]
        leaves, parents = [], []
        for node in nodes:
            if node["type"] == "parent":
                parents.append(node["parentCode"])
                leaves.extend(node["children"])
            else:
                leaves.extend(node["codes"])
        self.assertEqual(parents, ["52110"], "전주시만 일반구를 가진다")
        self.assertEqual(sorted(leaves + parents), self.codes_52)

    def test_local_submacro_includes_52(self):
        start = INDEX_HTML.index("const APT_SUBMACRO")
        block = INDEX_HTML[start:start + 1200]
        jeonla = re.search(r"id:\s*'전라',\s*match:\s*\[([^\]]*)\]", block)
        self.assertIsNotNone(jeonla, "전라 그룹을 찾지 못했다")
        codes = re.findall(r"'(\d+)'", jeonla.group(1))
        self.assertIn("52", codes)
        self.assertIn("45", codes, "과거 데이터 호환을 위해 45도 남긴다")

    def test_parent_child_rendering_branch_includes_52(self):
        """일반구가 있는 시도만 타는 분기에 52가 포함되어야 전주시가 펼쳐진다."""
        self.assertRegex(
            INDEX_HTML,
            r"treeKey === '48'\s*\|\|\s*treeKey === '52'",
        )

    def test_old_45_mapping_is_kept_for_historical_data(self):
        self.assertEqual(self.sgg_map.get("45110"), "전주시")
        self.assertIn("45", self.tree)

    def test_legacy_sido_block_is_hidden_when_it_has_no_items(self):
        """45와 52를 모두 매핑하면 '전북' 블록이 두 번 나온다. 항목이 있을 때만 그린다."""
        self.assertIn("const LEGACY_SIDO_CODES = { '45': '52' };", INDEX_HTML)
        self.assertIn(
            "if (LEGACY_SIDO_CODES[sido] && getTotalCountForSido(sido) === 0) return;",
            INDEX_HTML,
        )


if __name__ == "__main__":
    unittest.main()
