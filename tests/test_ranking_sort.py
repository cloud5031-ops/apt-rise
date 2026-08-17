# -*- coding: utf-8 -*-
"""헤더 클릭 정렬 회귀 테스트 (개별 아파트 / 시군구).

정렬 로직을 파이썬으로 옮겨 적으면 사본을 검증하게 되므로,
site/index.html의 인라인 스크립트를 Node VM에서 그대로 실행하고
실제 함수를 호출해 결과를 확인한다 (tests/run_index_js.js).

실행:
    python -m unittest discover -s tests -v
"""
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "tests" / "run_index_js.js"
INDEX_HTML = (REPO / "site" / "index.html").read_text(encoding="utf-8")
INDEX_CSS = (REPO / "site" / "index.css").read_text(encoding="utf-8")
NODE = shutil.which("node")


def js(expression):
    """인라인 스크립트를 로드한 뒤 표현식을 평가하고 결과를 파이썬 값으로 돌려준다."""
    proc = subprocess.run(
        [NODE, str(RUNNER), expression],
        capture_output=True, cwd=str(REPO),
    )
    if proc.returncode != 0:
        raise AssertionError(
            "node 실행 실패:\n" + proc.stderr.decode("utf-8", "replace")
        )
    return json.loads(proc.stdout.decode("utf-8"))


def names(items):
    return "[" + ",".join(json.dumps(i, ensure_ascii=False) for i in items) + "]"


# 개별 아파트 fixture: 기준가/최근가/상승률이 서로 다른 순서를 만들도록 구성했다.
APT_FIXTURE = [
    {"apartmentName": "가", "areaGroup": 84.0, "baselineMedian": 300000000,
     "currentMedian": 360000000, "riseRate": 20.0},
    {"apartmentName": "나", "areaGroup": 59.0, "baselineMedian": 900000000,
     "currentMedian": 990000000, "riseRate": 10.0},
    {"apartmentName": "다", "areaGroup": 101.0, "baselineMedian": 100000000,
     "currentMedian": 130000000, "riseRate": 30.0},
]

# 시군구 fixture: 요청받은 A/B/C 그대로.
REGION_FIXTURE = [
    {"regionName": "경기>남부권>에이시", "momRate": 5, "threeMonthRate": 1, "yoyRate": 12},
    {"regionName": "경기>남부권>비시", "momRate": 2, "threeMonthRate": 10, "yoyRate": -3},
    {"regionName": "경기>남부권>씨시", "momRate": -1, "threeMonthRate": 5, "yoyRate": 20},
]


@unittest.skipUnless(NODE, "node가 없어 인라인 스크립트를 실행할 수 없습니다")
class ApartmentSortTest(unittest.TestCase):
    """개별 아파트: 기준가 / 최근가 / 상승률."""

    def sorted_names(self, key, direction, fixture=None):
        return js("sortAptItems(%s, %s, %s).map(x => x.apartmentName)"
                  % (names(fixture if fixture is not None else APT_FIXTURE),
                     json.dumps(key), json.dumps(direction)))

    def test_default_state_is_rise_rate_desc(self):
        self.assertEqual(js("[state.aptSortKey, state.aptSortDir]"), ["riseRate", "desc"])

    def test_rise_rate_desc_and_asc(self):
        self.assertEqual(self.sorted_names("riseRate", "desc"), ["다", "가", "나"])
        self.assertEqual(self.sorted_names("riseRate", "asc"), ["나", "가", "다"])

    def test_baseline_price_desc_and_asc(self):
        self.assertEqual(self.sorted_names("baselineMedian", "desc"), ["나", "가", "다"])
        self.assertEqual(self.sorted_names("baselineMedian", "asc"), ["다", "가", "나"])

    def test_current_price_desc_and_asc(self):
        self.assertEqual(self.sorted_names("currentMedian", "desc"), ["나", "가", "다"])
        self.assertEqual(self.sorted_names("currentMedian", "asc"), ["다", "가", "나"])

    def test_sort_uses_numbers_not_display_strings(self):
        """문자열 비교라면 '9억' < '30억' 같은 역전이 생긴다."""
        fixture = [
            {"apartmentName": "구억", "areaGroup": 84, "baselineMedian": 900000000,
             "currentMedian": 900000000, "riseRate": 1},
            {"apartmentName": "삼십억", "areaGroup": 84, "baselineMedian": 3000000000,
             "currentMedian": 3000000000, "riseRate": 1},
        ]
        self.assertEqual(self.sorted_names("baselineMedian", "desc", fixture),
                         ["삼십억", "구억"])

    def test_nulls_go_last_in_both_directions(self):
        fixture = [
            {"apartmentName": "널", "areaGroup": 84, "baselineMedian": None,
             "currentMedian": 100, "riseRate": 1},
            {"apartmentName": "빈문자", "areaGroup": 84, "baselineMedian": "",
             "currentMedian": 100, "riseRate": 1},
            {"apartmentName": "작음", "areaGroup": 84, "baselineMedian": 10,
             "currentMedian": 100, "riseRate": 1},
            {"apartmentName": "큼", "areaGroup": 84, "baselineMedian": 900,
             "currentMedian": 100, "riseRate": 1},
        ]
        desc = self.sorted_names("baselineMedian", "desc", fixture)
        asc = self.sorted_names("baselineMedian", "asc", fixture)
        self.assertEqual(desc[:2], ["큼", "작음"])
        self.assertEqual(asc[:2], ["작음", "큼"])
        self.assertEqual(sorted(desc[2:]), sorted(["널", "빈문자"]))
        self.assertEqual(sorted(asc[2:]), sorted(["널", "빈문자"]))

    def test_nan_is_treated_as_missing(self):
        self.assertIsNone(js("sortableNumber('abc')"))
        self.assertIsNone(js("sortableNumber(undefined)"))
        self.assertEqual(js("sortableNumber('12.5')"), 12.5)

    def test_tie_break_is_rise_rate_then_name_then_area(self):
        fixture = [
            {"apartmentName": "나단지", "areaGroup": 84, "baselineMedian": 100,
             "currentMedian": 100, "riseRate": 5},
            {"apartmentName": "가단지", "areaGroup": 84, "baselineMedian": 100,
             "currentMedian": 100, "riseRate": 5},
            {"apartmentName": "가단지", "areaGroup": 59, "baselineMedian": 100,
             "currentMedian": 100, "riseRate": 5},
            {"apartmentName": "높은상승", "areaGroup": 84, "baselineMedian": 100,
             "currentMedian": 100, "riseRate": 9},
        ]
        result = js("sortAptItems(%s, 'baselineMedian', 'desc')"
                    ".map(x => x.apartmentName + '/' + x.areaGroup)" % names(fixture))
        self.assertEqual(result, ["높은상승/9".replace("9", "84"),
                                  "가단지/59", "가단지/84", "나단지/84"])

    def test_tie_break_keeps_result_stable_across_repeated_sorts(self):
        expr = ("(() => { const f = %s;"
                " const a = sortAptItems(f.slice(), 'baselineMedian', 'desc')"
                "   .map(x => x.apartmentName + x.areaGroup);"
                " const b = sortAptItems(f.slice().reverse(), 'baselineMedian', 'desc')"
                "   .map(x => x.apartmentName + x.areaGroup);"
                " return [a, b]; })()") % names([
                    {"apartmentName": "가", "areaGroup": 84, "baselineMedian": 100,
                     "currentMedian": 1, "riseRate": 3},
                    {"apartmentName": "나", "areaGroup": 84, "baselineMedian": 100,
                     "currentMedian": 1, "riseRate": 3},
                    {"apartmentName": "다", "areaGroup": 84, "baselineMedian": 100,
                     "currentMedian": 1, "riseRate": 3},
                ])
        a, b = js(expr)
        self.assertEqual(a, b, "입력 순서가 달라도 같은 결과여야 한다")

    def test_unknown_key_falls_back_to_rise_rate(self):
        self.assertEqual(self.sorted_names("buildYear", "desc"), ["다", "가", "나"])

    def test_build_year_is_not_a_sortable_column(self):
        self.assertNotIn("buildYear", js("Object.keys(APT_SORT_FIELDS)"))
        self.assertNotIn('data-apt-sort="buildYear"', INDEX_HTML)

    def test_sort_fields_exist_in_published_json(self):
        """정렬 필드가 실제 배포 JSON에 있는 이름인지 확인."""
        data = json.loads(
            (REPO / "site" / "data" / "apt_rankings_latest.json").read_text(encoding="utf-8")
        )
        item = data["items"][0]
        for field in js("Object.values(APT_SORT_FIELDS)"):
            self.assertIn(field, item, "%s 필드가 배포 JSON에 없습니다" % field)


@unittest.skipUnless(NODE, "node가 없어 인라인 스크립트를 실행할 수 없습니다")
class RegionSortTest(unittest.TestCase):
    """시군구: 전월대비 / 3개월 / 전년동월."""

    def sorted_names(self, key, direction, fixture=None):
        return js("sortRegionItems(%s, %s, %s).map(x => regionLocalName(x))"
                  % (names(fixture if fixture is not None else REGION_FIXTURE),
                     json.dumps(key), json.dumps(direction)))

    def test_default_state_is_mom_rate_desc(self):
        self.assertEqual(js("[state.regionSortKey, state.regionSortDir]"),
                         ["momRate", "desc"])

    def test_mom_rate_order(self):
        self.assertEqual(self.sorted_names("momRate", "desc"), ["에이시", "비시", "씨시"])
        self.assertEqual(self.sorted_names("momRate", "asc"), ["씨시", "비시", "에이시"])

    def test_three_month_rate_order(self):
        self.assertEqual(self.sorted_names("threeMonthRate", "desc"),
                         ["비시", "씨시", "에이시"])
        self.assertEqual(self.sorted_names("threeMonthRate", "asc"),
                         ["에이시", "씨시", "비시"])

    def test_yoy_rate_order(self):
        self.assertEqual(self.sorted_names("yoyRate", "desc"), ["씨시", "에이시", "비시"])
        self.assertEqual(self.sorted_names("yoyRate", "asc"), ["비시", "에이시", "씨시"])

    def test_negative_and_zero_are_ordered_numerically(self):
        fixture = [
            {"regionName": "경기>가", "momRate": 8, "threeMonthRate": 0, "yoyRate": 0},
            {"regionName": "경기>나", "momRate": 3, "threeMonthRate": 0, "yoyRate": 0},
            {"regionName": "경기>다", "momRate": 0, "threeMonthRate": 0, "yoyRate": 0},
            {"regionName": "경기>라", "momRate": -2, "threeMonthRate": 0, "yoyRate": 0},
            {"regionName": "경기>마", "momRate": -10, "threeMonthRate": 0, "yoyRate": 0},
        ]
        self.assertEqual(
            js("sortRegionItems(%s, 'momRate', 'desc').map(x => x.momRate)" % names(fixture)),
            [8, 3, 0, -2, -10])
        self.assertEqual(
            js("sortRegionItems(%s, 'momRate', 'asc').map(x => x.momRate)" % names(fixture)),
            [-10, -2, 0, 3, 8])

    def test_nulls_go_last_in_both_directions(self):
        fixture = [
            {"regionName": "경기>널", "momRate": 1, "threeMonthRate": 1, "yoyRate": None},
            {"regionName": "경기>음수", "momRate": 1, "threeMonthRate": 1, "yoyRate": -5},
            {"regionName": "경기>양수", "momRate": 1, "threeMonthRate": 1, "yoyRate": 5},
        ]
        self.assertEqual(self.sorted_names("yoyRate", "desc", fixture),
                         ["양수", "음수", "널"])
        self.assertEqual(self.sorted_names("yoyRate", "asc", fixture),
                         ["음수", "양수", "널"])

    def test_tie_break_is_mom_then_sido_then_local(self):
        fixture = [
            {"regionName": "서울>나구", "momRate": 1, "threeMonthRate": 7, "yoyRate": 0},
            {"regionName": "경기>다시", "momRate": 1, "threeMonthRate": 7, "yoyRate": 0},
            {"regionName": "경기>가시", "momRate": 1, "threeMonthRate": 7, "yoyRate": 0},
            {"regionName": "경기>높은몸", "momRate": 9, "threeMonthRate": 7, "yoyRate": 0},
        ]
        self.assertEqual(self.sorted_names("threeMonthRate", "desc", fixture),
                         ["높은몸", "가시", "다시", "나구"])

    def test_region_name_is_split_into_sido_and_local(self):
        item = json.dumps({"regionName": "경기>서해안권>화성시>동탄구"}, ensure_ascii=False)
        self.assertEqual(js("regionSidoName(%s)" % item), "경기")
        self.assertEqual(js("regionLocalName(%s)" % item), "화성시 동탄구")

    def test_sort_fields_exist_in_published_json(self):
        data = json.loads(
            (REPO / "site" / "data" / "region_rankings_latest.json").read_text(encoding="utf-8")
        )
        item = data["items"][0]
        for field in js("Object.values(REGION_SORT_FIELDS)"):
            self.assertIn(field, item, "%s 필드가 배포 JSON에 없습니다" % field)


@unittest.skipUnless(NODE, "node가 없어 인라인 스크립트를 실행할 수 없습니다")
class SortDirectionToggleTest(unittest.TestCase):
    """다른 헤더 첫 클릭은 항상 DESC, 같은 헤더 재클릭은 DESC ↔ ASC."""

    def test_new_column_always_starts_descending(self):
        self.assertEqual(js("nextSortDirection('riseRate', 'desc', 'baselineMedian')"), "desc")
        self.assertEqual(js("nextSortDirection('riseRate', 'asc', 'baselineMedian')"), "desc")

    def test_same_column_toggles(self):
        self.assertEqual(js("nextSortDirection('riseRate', 'desc', 'riseRate')"), "asc")
        self.assertEqual(js("nextSortDirection('riseRate', 'asc', 'riseRate')"), "desc")

    def test_toggle_round_trips(self):
        self.assertEqual(
            js("nextSortDirection('momRate', nextSortDirection('momRate', 'desc', 'momRate'),"
               " 'momRate')"),
            "desc")


@unittest.skipUnless(NODE, "node가 없어 인라인 스크립트를 실행할 수 없습니다")
class SortHeaderUiTest(unittest.TestCase):
    """헤더 표시: ↕ / ↓ / ↑ 와 aria-sort."""

    def test_aria_sort_values(self):
        self.assertEqual(js("ariaSortValue('riseRate', 'desc', 'riseRate')"), "descending")
        self.assertEqual(js("ariaSortValue('riseRate', 'asc', 'riseRate')"), "ascending")
        self.assertEqual(js("ariaSortValue('riseRate', 'desc', 'currentMedian')"), "none")

    def test_indicator_characters(self):
        self.assertEqual(js("sortIndicator('riseRate', 'desc', 'riseRate')"), "↓")
        self.assertEqual(js("sortIndicator('riseRate', 'asc', 'riseRate')"), "↑")
        self.assertEqual(js("sortIndicator('riseRate', 'desc', 'baselineMedian')"), "↕")

    def test_header_is_a_real_button_for_keyboard_access(self):
        html = js("sortHeaderButton('riseRate', '상승률', '', 'riseRate', 'desc', 'data-apt-sort')")
        self.assertIn('<button type="button"', html)
        self.assertIn('data-apt-sort="riseRate"', html)
        self.assertIn("↓", html)

    def test_apartment_table_renders_sortable_headers(self):
        html = js("renderApartmentRankingList({referenceMonth: '202606', items: %s})"
                  % names(APT_FIXTURE))
        for key in ("baselineMedian", "currentMedian", "riseRate"):
            self.assertIn('data-apt-sort="%s"' % key, html)
        self.assertIn('aria-sort="descending"', html)
        self.assertEqual(html.count('aria-sort="none"'), 2)

    def test_region_table_renders_sortable_headers(self):
        html = js("renderRegionRankingTable({referenceMonth: '202606', items: %s})"
                  % names(REGION_FIXTURE))
        for key in ("momRate", "threeMonthRate", "yoyRate"):
            self.assertIn('data-region-sort="%s"' % key, html)
        self.assertIn('aria-sort="descending"', html)
        self.assertEqual(html.count('aria-sort="none"'), 2)

    def test_rendered_rows_follow_the_selected_sort(self):
        html = js("(() => { state.aptSortKey = 'baselineMedian'; state.aptSortDir = 'asc';"
                  " return renderApartmentRankingList("
                  "   {referenceMonth: '202606', items: %s}); })()" % names(APT_FIXTURE))
        order = re.findall(r"<b>(.)</b>", html)
        self.assertEqual(order, ["다", "가", "나"])

    def test_header_style_does_not_change_table_layout(self):
        """기존 th 서식을 물려받아 목록 레이아웃을 바꾸지 않는다."""
        self.assertIn("th .sort-header{all:unset;", INDEX_CSS)
        self.assertIn("font:inherit", INDEX_CSS)


@unittest.skipUnless(NODE, "node가 없어 인라인 스크립트를 실행할 수 없습니다")
class SortPaginationTest(unittest.TestCase):
    """정렬과 더보기(100건 단위)의 상호작용."""

    SIG = ("currentAptSignature({referenceMonth: '202606'})")

    def test_signature_includes_sort_key_and_direction(self):
        base = js(self.SIG)
        changed_key = js("(() => { state.aptSortKey = 'currentMedian'; return %s; })()" % self.SIG)
        changed_dir = js("(() => { state.aptSortDir = 'asc'; return %s; })()" % self.SIG)
        self.assertNotEqual(base, changed_key)
        self.assertNotEqual(base, changed_dir)

    def test_signature_includes_region_and_filter_state(self):
        base = js(self.SIG)
        for mutation in ("state.aptMacroRegion = '경기'",
                         "state.aptSubMacroRegion = '전라'",
                         "state.aptMicroRegion = '52130'",
                         "state.apartmentView = 'regional'"):
            changed = js("(() => { %s; return %s; })()" % (mutation, self.SIG))
            self.assertNotEqual(base, changed, "%s 가 signature에 반영되지 않습니다" % mutation)

    def test_signature_includes_reference_month(self):
        self.assertNotEqual(
            js("currentAptSignature({referenceMonth: '202606'})"),
            js("currentAptSignature({referenceMonth: '202607'})"))

    def test_more_button_only_increases_visible_count(self):
        """더보기 핸들러는 정렬 상태를 건드리지 않는다."""
        handler = re.search(
            r"const btn = e\.target\.closest\(\"\[data-apt-more\]\"\);[\s\S]{0,200}?\}\);",
            INDEX_HTML)
        self.assertIsNotNone(handler)
        body = handler.group(0)
        self.assertIn("aptVisibleCount += APT_PAGE_SIZE", body)
        self.assertNotIn("aptSortKey", body)
        self.assertNotIn("aptSortDir", body)
        self.assertNotIn("aptVisibleCount = APT_PAGE_SIZE", body)

    def test_page_size_is_one_hundred(self):
        self.assertEqual(js("APT_PAGE_SIZE"), 100)

    def test_sort_click_does_not_reset_sort_state_of_the_other_table(self):
        """개별 아파트 정렬과 시군구 정렬은 서로 독립적이다."""
        result = js("(() => {"
                    " state.aptSortKey = 'currentMedian'; state.aptSortDir = 'asc';"
                    " state.regionSortDir = nextSortDirection(state.regionSortKey,"
                    "   state.regionSortDir, 'yoyRate');"
                    " state.regionSortKey = 'yoyRate';"
                    " return [state.aptSortKey, state.aptSortDir,"
                    "         state.regionSortKey, state.regionSortDir]; })()")
        self.assertEqual(result, ["currentMedian", "asc", "yoyRate", "desc"])


@unittest.skipUnless(NODE, "node가 없어 인라인 스크립트를 실행할 수 없습니다")
class SortDoesNotBreakExistingFeaturesTest(unittest.TestCase):
    """정렬 추가가 buildYear·52 매핑 구현을 건드리지 않았는지."""

    def test_build_year_label_still_works(self):
        self.assertEqual(js("formatBuildYearLabel({buildYear: 2018})"), "2018년 건축")
        self.assertEqual(js("formatBuildYearLabel({buildYear: 0})"), "건축년도 미상")
        self.assertEqual(js("formatBuildYearLabel({})"), "건축년도 미상")

    def test_jeonbuk_mapping_still_present(self):
        self.assertEqual(js("SIDO_NAMES['52']"), "전북")
        self.assertEqual(js("SGG_CODE_MAP['52130']"), "군산시")
        self.assertEqual(js("LEGACY_SIDO_CODES['45']"), "52")
        self.assertEqual(js("OFFICIAL_REGION_TREE['52'].length"), 2)

    def test_area_label_still_truncates(self):
        self.assertEqual(js("formatAreaLabel({exclusiveArea: 84.97})"), "84㎡ (25평)")


if __name__ == "__main__":
    unittest.main()
