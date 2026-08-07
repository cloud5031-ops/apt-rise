# -*- coding: utf-8 -*-
"""발행(publish) 스텝의 셸 로직 회귀 테스트.

workflow YAML에서 push 스텝의 run 블록을 그대로 꺼내, 임시 git 저장소와
로컬 bare remote 위에서 실제로 실행한다. 셸을 흉내내지 않고 돌리므로
YAML이 바뀌면 이 테스트가 따라간다.

배경: run 30949008365가 마지막 단계에서 실패했다. export_details.py가 갱신하는
tracked 파일 data/details_backfill_state.json이 allowlist에 없어, 커밋 뒤에도
작업트리가 dirty로 남아 `git rebase`가 거부했다.

실행:
    python -m unittest discover -s tests -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
PUSH_STEP = "Push Shards and National JSON"

# 배포 대상 tracked 산출물. 실제 실행에서 스크립트가 건드리는 파일들이다.
PUBLISHED_FILES = [
    "data/shards/202606/stable/seoul.json",
    "data/shards/202606/stable/gyeonggi_incheon.json",
    "data/details_backfill_state.json",
    "data/geocoding/apartment_coordinates.json",
    "site/data/apt_rankings_latest.json",
    "site/data/apt_rankings_manifest.json",
    "site/data/details/11110/11110-1.json",
]
# 커밋하면 안 되는 진단 파일
UNTRACKED_DIAGNOSTIC = "run_meta.json"
IGNORED_DIAGNOSTIC = "data/apt.sqlite"


def bash():
    for candidate in ("/usr/bin/bash", "/bin/bash", shutil.which("bash")):
        if candidate and Path(candidate).exists():
            return candidate
    raise unittest.SkipTest("bash를 찾을 수 없다")


def push_script(workflow_name):
    data = yaml.safe_load((WORKFLOWS / workflow_name).read_text(encoding="utf-8"))
    job = next(iter(data["jobs"].values()))
    for step in job["steps"]:
        if step.get("name") == PUSH_STEP:
            return step["run"]
    raise AssertionError(f"{workflow_name}에 '{PUSH_STEP}' 스텝이 없다")


def drop_allowlist_entry(script, path):
    """allowlist에서 한 경로만 빼낸 스크립트. 사고 상황을 재현하는 데 쓴다."""
    kept = [line for line in script.splitlines()
            if line.strip().rstrip("\\").strip() != path]
    assert len(kept) == len(script.splitlines()) - 1, f"'{path}' 줄을 찾지 못했다"
    return "\n".join(kept) + "\n"


class PublishStepHarness(unittest.TestCase):
    """bare remote + 작업 클론을 만들고 실제 스텝 스크립트를 돌린다."""

    WORKFLOW = "capital-daily-refresh.yml"

    def setUp(self):
        self.bash = bash()
        self.tmp = Path(tempfile.mkdtemp(prefix="publish_step_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.remote = self.tmp / "remote.git"
        self.work = self.tmp / "work"
        self.summary = self.tmp / "step_summary.md"
        self.summary.touch()

        self.git("init", "--bare", "--initial-branch=main", str(self.remote), cwd=self.tmp)

        seed = self.tmp / "seed"
        seed.mkdir()
        self.git("init", "--initial-branch=main", cwd=seed)
        self._config(seed)
        (seed / ".gitignore").write_text("data/*.sqlite\n__pycache__/\n", encoding="utf-8")
        for rel in PUBLISHED_FILES:
            p = seed / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{"seed": true}\n', encoding="utf-8")
        self.git("add", "-A", cwd=seed)
        self.git("commit", "-m", "seed", cwd=seed)
        self.git("remote", "add", "origin", str(self.remote), cwd=seed)
        self.git("push", "-u", "origin", "main", cwd=seed)

        self.git("clone", str(self.remote), str(self.work), cwd=self.tmp)
        self._config(self.work)

    def _config(self, cwd):
        self.git("config", "user.name", "tester", cwd=cwd)
        self.git("config", "user.email", "tester@example.com", cwd=cwd)

    def git(self, *args, cwd=None):
        # 셸과 git의 한글 출력은 UTF-8이다. Windows 기본 코덱(cp949)에 맡기면
        # 디코딩이 깨져 stdout이 None으로 돌아온다.
        return subprocess.run(["git", *args], cwd=str(cwd or self.work),
                              capture_output=True, text=True, check=True,
                              encoding="utf-8", errors="replace")

    # ── 작업트리 조작 ────────────────────────────────────────────
    def modify(self, rel, content='{"changed": true}\n'):
        p = self.work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def advance_remote(self, rel="site/data/apt_rankings_latest.json",
                       content='{"remote": "ahead"}\n'):
        """다른 실행이 main을 한 커밋 전진시킨 상황."""
        other = self.tmp / f"other_{abs(hash(rel)) % 10000}"
        self.git("clone", str(self.remote), str(other), cwd=self.tmp)
        self._config(other)
        p = other / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        self.git("add", "-A", cwd=other)
        self.git("commit", "-m", "remote moves ahead", cwd=other)
        self.git("push", "origin", "main", cwd=other)

    # ── 스텝 실행 ────────────────────────────────────────────────
    def run_step(self, script=None):
        script = script if script is not None else push_script(self.WORKFLOW)
        path = self.tmp / "step.sh"
        path.write_text(script, encoding="utf-8")
        env = dict(os.environ, GITHUB_STEP_SUMMARY=str(self.summary))
        return subprocess.run([self.bash, "-e", str(path)], cwd=str(self.work),
                              capture_output=True, text=True, env=env,
                              encoding="utf-8", errors="replace")

    # ── 관찰 ─────────────────────────────────────────────────────
    def remote_head(self):
        out = subprocess.run(["git", "rev-parse", "main"], cwd=str(self.remote),
                             capture_output=True, text=True, check=True,
                             encoding="utf-8", errors="replace")
        return out.stdout.strip()

    def remote_commit_count(self):
        out = subprocess.run(["git", "rev-list", "--count", "main"], cwd=str(self.remote),
                             capture_output=True, text=True, check=True,
                             encoding="utf-8", errors="replace")
        return int(out.stdout.strip())

    def worktree_dirty(self):
        out = self.git("status", "--porcelain", "--untracked-files=no")
        return out.stdout.strip()


class TestSuccessfulPublish(PublishStepHarness):
    def test_all_artifacts_commit_rebase_and_push(self):
        """사례 1: allowlist 산출물이 전부 바뀌면 커밋·rebase·push까지 간다."""
        before = self.remote_head()
        for rel in PUBLISHED_FILES:
            self.modify(rel)

        result = self.run_step()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Push successful", result.stdout)
        self.assertNotEqual(self.remote_head(), before, "remote main이 전진해야 한다")
        self.assertEqual(self.worktree_dirty(), "", "실행 후 작업트리는 clean이어야 한다")

    def test_ledger_is_actually_published(self):
        """이번 사고의 핵심 파일이 실제로 remote에 올라가는지."""
        self.modify("data/details_backfill_state.json", '{"sggMonths": {"11110": ["202607"]}}\n')
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        shown = subprocess.run(
            ["git", "show", "main:data/details_backfill_state.json"],
            cwd=str(self.remote), capture_output=True, text=True, check=True,
                             encoding="utf-8", errors="replace")
        self.assertIn("202607", shown.stdout)


class TestMissingAllowlistEntry(PublishStepHarness):
    """사례 2: 산출물 하나가 allowlist에서 빠지면 rebase 전에 멈춘다."""

    def test_missing_ledger_fails_before_rebase(self):
        script = drop_allowlist_entry(push_script(self.WORKFLOW),
                                      "data/details_backfill_state.json")
        self.assertNotIn("data/details_backfill_state.json \\", script,
                         "누락 상황을 만들지 못했다")

        before = self.remote_head()
        for rel in PUBLISHED_FILES:
            self.modify(rel)

        result = self.run_step(script)
        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 1, output)
        self.assertIn("data/details_backfill_state.json", output,
                      "남은 파일 이름이 로그에 나와야 한다")
        self.assertIn("allowlist", output)
        self.assertNotIn("Push successful", output, "push하면 안 된다")
        self.assertEqual(self.remote_head(), before, "remote main이 변하면 안 된다")
        self.assertIn("발행 중단", self.summary.read_text(encoding="utf-8"))

    def test_no_rebase_is_attempted_when_dirty(self):
        """rebase가 실행되지 않았는지: rebase 진행 상태가 남지 않아야 한다."""
        script = drop_allowlist_entry(push_script(self.WORKFLOW),
                                      "data/details_backfill_state.json")
        for rel in PUBLISHED_FILES:
            self.modify(rel)
        self.run_step(script)

        for marker in ("rebase-merge", "rebase-apply"):
            self.assertFalse((self.work / ".git" / marker).exists(),
                             f"{marker}가 남아 있다 — rebase가 시도됐다")


class TestDiagnosticFilesAreTolerated(PublishStepHarness):
    def test_untracked_run_meta_does_not_fail(self):
        """사례 3: untracked run_meta.json은 실패 원인이 아니다."""
        for rel in PUBLISHED_FILES:
            self.modify(rel)
        (self.work / UNTRACKED_DIAGNOSTIC).write_text('{"regionGroup": "seoul"}\n',
                                                      encoding="utf-8")
        result = self.run_step()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Push successful", result.stdout)

        listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"],
                                cwd=str(self.remote), capture_output=True, text=True, check=True,
                             encoding="utf-8", errors="replace")
        self.assertNotIn(UNTRACKED_DIAGNOSTIC, listed.stdout.split(),
                         "진단용 run_meta.json이 커밋되면 안 된다")

    def test_ignored_sqlite_does_not_fail(self):
        """사례 4: gitignored apt.sqlite도 실패 원인이 아니다."""
        for rel in PUBLISHED_FILES:
            self.modify(rel)
        p = self.work / IGNORED_DIAGNOSTIC
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)

        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"],
                                cwd=str(self.remote), capture_output=True, text=True, check=True,
                             encoding="utf-8", errors="replace")
        self.assertNotIn(IGNORED_DIAGNOSTIC, listed.stdout.split())


class TestRemoteMovedAhead(PublishStepHarness):
    def test_rebase_onto_advanced_main_then_push(self):
        """사례 5: 실행 중 main이 전진해도 clean 상태면 rebase 후 push된다."""
        self.advance_remote("site/data/details/11110/11110-1.json",
                            '{"remote": "other run"}\n')
        self.modify("data/details_backfill_state.json")
        self.modify("data/shards/202606/stable/seoul.json")

        before_count = self.remote_commit_count()
        result = self.run_step()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Push successful", result.stdout)
        self.assertEqual(self.remote_commit_count(), before_count + 1,
                         "다른 실행의 커밋 위에 하나만 얹혀야 한다")

        shown = subprocess.run(
            ["git", "show", "main:site/data/details/11110/11110-1.json"],
            cwd=str(self.remote), capture_output=True, text=True, check=True,
                             encoding="utf-8", errors="replace")
        self.assertIn("other run", shown.stdout, "remote 변경을 덮어쓰면 안 된다")


class TestRebaseConflict(PublishStepHarness):
    def test_conflict_aborts_and_does_not_push(self):
        """사례 6: 같은 파일이 양쪽에서 바뀌어 충돌하면 abort하고 멈춘다."""
        target = "data/shards/202606/stable/seoul.json"
        self.advance_remote(target, '{"remote": "conflicting"}\n')
        self.modify(target, '{"local": "conflicting"}\n')

        before = self.remote_head()
        result = self.run_step()
        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 1, output)
        self.assertIn("rebase", output.lower())
        self.assertNotIn("Push successful", output)
        self.assertEqual(self.remote_head(), before, "충돌 상태로 push하면 안 된다")
        for marker in ("rebase-merge", "rebase-apply"):
            self.assertFalse((self.work / ".git" / marker).exists(),
                             "rebase 상태가 abort되지 않고 남았다")


class TestNothingToPublish(PublishStepHarness):
    def test_no_changes_exits_zero_without_commit(self):
        """사례 7: 바뀐 산출물이 없으면 빈 커밋 없이 정상 종료한다."""
        before_local = self.git("rev-parse", "HEAD").stdout.strip()
        before_remote = self.remote_head()

        result = self.run_step()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("발행할 변경이 없습니다", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), before_local,
                         "빈 커밋이 생기면 안 된다")
        self.assertEqual(self.remote_head(), before_remote)

    def test_exit_zero_does_not_end_the_job(self):
        """스텝의 exit 0은 그 스텝만 끝낸다 — 후속 스텝은 조건대로 실행된다.

        capital의 후속 3개 스텝은 always()라 어떤 결과에서도 실행되고,
        nationwide에는 후속 스텝 자체가 없다. YAML로 확인한다.
        """
        data = yaml.safe_load((WORKFLOWS / "capital-daily-refresh.yml").read_text(encoding="utf-8"))
        steps = data["jobs"]["capital-daily"]["steps"]
        names = [s["name"] for s in steps]
        push_at = names.index(PUSH_STEP)
        after = steps[push_at + 1:]

        self.assertTrue(after, "push 뒤에 보존·검증 스텝이 있어야 한다")
        for step in after:
            self.assertIn("always()", step.get("if", ""),
                          f"'{step['name']}'가 앞 스텝 결과에 좌우되면 안 된다")

        nation = yaml.safe_load(
            (WORKFLOWS / "nationwide-weekly-refresh.yml").read_text(encoding="utf-8"))
        nsteps = [s["name"] for s in next(iter(nation["jobs"].values()))["steps"]]
        self.assertEqual(nsteps[-1], PUSH_STEP, "nationwide는 push가 마지막 스텝이다")


class TestNationwideWorkflow(PublishStepHarness):
    """사례 9: nationwide도 같은 결함이었고 같은 방식으로 고쳐졌다."""

    WORKFLOW = "nationwide-weekly-refresh.yml"

    def test_publish_succeeds_with_ledger(self):
        for rel in PUBLISHED_FILES:
            self.modify(rel)
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Push successful", result.stdout)
        self.assertEqual(self.worktree_dirty(), "")

    def test_missing_ledger_reproduces_the_failure(self):
        script = drop_allowlist_entry(push_script(self.WORKFLOW),
                                      "data/details_backfill_state.json")
        before = self.remote_head()
        for rel in PUBLISHED_FILES:
            self.modify(rel)

        result = self.run_step(script)
        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 1, output)
        self.assertIn("data/details_backfill_state.json", output)
        self.assertEqual(self.remote_head(), before)


class TestAllowlistCoversScriptOutputs(unittest.TestCase):
    """사례 10: 스크립트가 쓰는 tracked 경로가 두 allowlist에 모두 들어 있는지.

    경로 상수를 모듈에서 직접 읽으므로, 새 산출물 상수가 생기면
    분류되지 않은 채로는 이 테스트가 실패한다.
    """

    # 커밋하지 않는 것들과 그 이유
    NOT_PUBLISHED = {
        "data/apt.sqlite": "gitignored 진단 DB",
        "run_meta.json": "untracked 진단 파일",
        "data/regions.json": "입력 데이터, 수집이 갱신하지 않음",
        "site/data/apt_rankings_manifest.json": "apt_rankings_*.json 글롭에 포함",
    }

    @classmethod
    def setUpClass(cls):
        scripts = REPO / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        cls.allowlists = {}
        for wf in ("capital-daily-refresh.yml", "nationwide-weekly-refresh.yml"):
            cls.allowlists[wf] = push_script(wf)

    def repo_relative_path_constants(self):
        """export_details / details_history의 저장소 내부 경로 상수."""
        import importlib
        found = {}
        for mod_name in ("export_details", "details_history"):
            mod = importlib.import_module(mod_name)
            for attr in dir(mod):
                if not attr.isupper():
                    continue
                value = getattr(mod, attr)
                if not isinstance(value, Path):
                    continue
                try:
                    rel = value.resolve().relative_to(REPO).as_posix()
                except ValueError:
                    continue
                if rel in (".", "data", "site", "scripts", "site/data", "data/shards"):
                    continue
                found[rel] = f"{mod_name}.{attr}"
        return found

    def test_every_written_path_is_published_or_declared(self):
        constants = self.repo_relative_path_constants()
        self.assertTrue(constants, "경로 상수를 하나도 찾지 못했다 — 테스트가 무력하다")

        for rel, origin in sorted(constants.items()):
            if rel in self.NOT_PUBLISHED:
                continue
            for wf, script in self.allowlists.items():
                covered = (rel in script) or (
                    rel.startswith("site/data/details")
                    and "site/data/details/*/*.json" in script)
                self.assertTrue(
                    covered,
                    f"{origin} 이 쓰는 '{rel}' 이 {wf} allowlist에 없다. "
                    f"발행 대상이면 allowlist에, 아니면 NOT_PUBLISHED에 넣어라.")

    def test_ledger_is_in_both_allowlists(self):
        for wf, script in self.allowlists.items():
            self.assertIn("data/details_backfill_state.json", script,
                          f"{wf}: 이번 사고의 원인 파일이 빠졌다")

    def test_no_blanket_add_or_stash(self):
        for wf, script in self.allowlists.items():
            for banned in ("git add -A", "git add .", "git stash", "--autostash"):
                self.assertNotIn(banned, script, f"{wf}: '{banned}'를 쓰면 안 된다")

    def test_both_workflows_share_the_same_guards(self):
        for wf, script in self.allowlists.items():
            for guard in ("git diff --cached --quiet",
                          "git status --porcelain --untracked-files=no",
                          "git rebase --abort",
                          "git fetch origin main"):
                self.assertIn(guard, script, f"{wf}: '{guard}' 안전장치가 없다")


if __name__ == "__main__":
    unittest.main()
