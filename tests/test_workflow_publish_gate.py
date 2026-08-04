# -*- coding: utf-8 -*-
"""capital-daily-refresh workflow의 발행(publish) 차단 조건 회귀 테스트.

feature 브랜치에서 수동 실행하면 `git push origin HEAD:main`이 데이터뿐 아니라
그 브랜치의 코드 커밋까지 main에 올린다. 그래서 push 스텝에 조건을 걸어 두었고,
이 테스트는 그 조건식을 실제 YAML에서 읽어 다섯 가지 상황에 대해 평가한다.

GitHub의 표현식 엔진을 그대로 쓸 수는 없으므로, 여기서 쓰는 부분집합
(문자열 비교, ==, !=, &&, ||, 괄호, 컨텍스트 조회)을 같은 의미로 구현해 검증한다.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "capital-daily-refresh.yml"

PUSH_STEP = "Push Shards and National JSON"
SKIP_STEP = "Report skipped publish"


# ── GitHub Actions 표현식 (사용하는 부분집합) 평가기 ─────────────

_TOKEN = re.compile(r"""
    \s*(?:
        (?P<lparen>\()
      | (?P<rparen>\))
      | (?P<and>&&)
      | (?P<or>\|\|)
      | (?P<eq>==)
      | (?P<ne>!=)
      | (?P<str>'[^']*')
      | (?P<name>[A-Za-z_][A-Za-z0-9_.\-]*)
    )
""", re.VERBOSE)


def tokenize(expr):
    tokens, pos = [], 0
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m:
            if expr[pos:].strip() == "":
                break
            raise ValueError(f"파싱 불가: {expr[pos:]!r}")
        kind = m.lastgroup
        tokens.append((kind, m.group(kind)))
        pos = m.end()
    return tokens


class Parser:
    """우선순위: 비교(==, !=) > && > ||"""

    def __init__(self, tokens, context):
        self.tokens, self.i, self.ctx = tokens, 0, context

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def parse(self):
        value = self.parse_or()
        if self.i != len(self.tokens):
            raise ValueError(f"남은 토큰: {self.tokens[self.i:]}")
        return value

    def parse_or(self):
        left = self.parse_and()
        while self.peek()[0] == "or":
            self.take()
            right = self.parse_and()
            left = truthy(left) or truthy(right)
        return left

    def parse_and(self):
        left = self.parse_cmp()
        while self.peek()[0] == "and":
            self.take()
            right = self.parse_cmp()
            left = truthy(left) and truthy(right)
        return left

    def parse_cmp(self):
        left = self.parse_atom()
        kind = self.peek()[0]
        if kind in ("eq", "ne"):
            self.take()
            right = self.parse_atom()
            equal = left == right
            return equal if kind == "eq" else not equal
        return left

    def parse_atom(self):
        kind, text = self.take()
        if kind == "lparen":
            value = self.parse_or()
            if self.take()[0] != "rparen":
                raise ValueError("괄호가 닫히지 않음")
            return value
        if kind == "str":
            return text[1:-1]
        if kind == "name":
            if text == "true":
                return True
            if text == "false":
                return False
            if text == "null":
                return None
            return lookup(self.ctx, text)
        raise ValueError(f"예상치 못한 토큰: {kind} {text!r}")


def lookup(context, dotted):
    """없는 컨텍스트/키는 GitHub와 같이 null로 평가한다."""
    node = context
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def truthy(value):
    return bool(value)


def evaluate(expr, context):
    return truthy(Parser(tokenize(expr), context).parse())


def make_context(event_name, ref, publish):
    """publish가 None이면 inputs 컨텍스트 자체가 없는 상황(schedule)."""
    ctx = {"github": {"event_name": event_name, "ref": ref}}
    if publish is not None:
        ctx["inputs"] = {"publish": publish}
    return ctx


# ── 실제 YAML에서 조건식을 읽어온다 ──────────────────────────────

def step_conditions():
    import yaml
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = data["jobs"]["capital-daily"]["steps"]
    return {s["name"]: s.get("if") for s in steps if "name" in s}


class TestExpressionEvaluator(unittest.TestCase):
    """평가기 자체가 GitHub 의미론과 맞는지 먼저 확인한다."""

    def test_basic_semantics(self):
        ctx = make_context("workflow_dispatch", "refs/heads/main", True)
        self.assertTrue(evaluate("github.ref == 'refs/heads/main'", ctx))
        self.assertFalse(evaluate("github.ref != 'refs/heads/main'", ctx))
        self.assertTrue(evaluate("inputs.publish == true", ctx))
        self.assertFalse(evaluate("inputs.publish != true", ctx))
        self.assertTrue(evaluate("false || true", ctx))
        self.assertFalse(evaluate("true && false", ctx))
        self.assertTrue(evaluate("(false || true) && true", ctx))

    def test_missing_context_is_null(self):
        """schedule 실행에는 inputs 컨텍스트가 없다."""
        ctx = make_context("schedule", "refs/heads/main", None)
        self.assertFalse(evaluate("inputs.publish == true", ctx))
        self.assertTrue(evaluate("inputs.publish != true", ctx))


class TestPublishGate(unittest.TestCase):
    """요구된 다섯 가지 상황에서 push 스텝이 실행되는지."""

    @classmethod
    def setUpClass(cls):
        conditions = step_conditions()
        cls.push_if = conditions.get(PUSH_STEP)
        cls.skip_if = conditions.get(SKIP_STEP)

    def assert_case(self, event_name, ref, publish, should_push, label):
        ctx = make_context(event_name, ref, publish)
        pushed = evaluate(self.push_if, ctx)
        skipped_report = evaluate(self.skip_if, ctx)

        self.assertEqual(pushed, should_push, f"{label}: push 판정이 다르다")
        self.assertNotEqual(
            pushed, skipped_report,
            f"{label}: push 스텝과 안내 스텝의 조건이 서로 정확한 부정이어야 한다")

    def test_push_step_has_condition(self):
        self.assertTrue(self.push_if, "push 스텝에 if 조건이 있어야 한다")
        self.assertTrue(self.skip_if, "안내 스텝에 if 조건이 있어야 한다")

    def test_a_schedule_on_main_publishes(self):
        self.assert_case("schedule", "refs/heads/main", None,
                         should_push=True, label="A schedule + main")

    def test_b_dispatch_on_main_with_publish_true(self):
        self.assert_case("workflow_dispatch", "refs/heads/main", True,
                         should_push=True, label="B dispatch + main + publish=true")

    def test_c_dispatch_on_main_with_publish_false(self):
        self.assert_case("workflow_dispatch", "refs/heads/main", False,
                         should_push=False, label="C dispatch + main + publish=false")

    def test_d_dispatch_on_feature_with_publish_false(self):
        self.assert_case("workflow_dispatch", "refs/heads/feature/inline-apartment-details",
                         False, should_push=False, label="D dispatch + feature + publish=false")

    def test_e_dispatch_on_feature_with_publish_true(self):
        self.assert_case("workflow_dispatch", "refs/heads/feature/inline-apartment-details",
                         True, should_push=False, label="E dispatch + feature + publish=true")

    def test_no_feature_branch_ref_ever_publishes(self):
        """main 이외의 어떤 ref에서도, publish 값과 무관하게 발행되지 않는다."""
        for ref in ("refs/heads/feature/x", "refs/heads/rescue/y", "refs/tags/v1"):
            for publish in (True, False, None):
                ctx = make_context("workflow_dispatch", ref, publish)
                self.assertFalse(evaluate(self.push_if, ctx),
                                 f"{ref} / publish={publish} 에서 발행되면 안 된다")


class TestWorkflowInputs(unittest.TestCase):
    """workflow_dispatch 입력 정의와 기존 트리거 보존."""

    @classmethod
    def setUpClass(cls):
        import yaml
        cls.data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        # PyYAML은 YAML 1.1 규칙으로 키 `on`을 True로 읽는다.
        cls.triggers = cls.data.get("on", cls.data.get(True))

    def test_publish_input_defaults_to_false(self):
        publish = self.triggers["workflow_dispatch"]["inputs"]["publish"]
        self.assertEqual(publish["type"], "boolean")
        self.assertIs(publish["default"], False, "기본값은 발행하지 않는 쪽이어야 한다")

    def test_schedule_trigger_is_preserved(self):
        schedule = self.triggers["schedule"]
        self.assertEqual(len(schedule), 1)
        self.assertEqual(schedule[0]["cron"], "17 4 * * 1-6")


if __name__ == "__main__":
    unittest.main()
