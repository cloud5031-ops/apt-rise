// site/index.html의 인라인 스크립트를 그대로 Node VM에서 실행한 뒤,
// 인자로 받은 표현식을 평가해 JSON으로 출력한다.
// 정렬 로직을 테스트용으로 복사하지 않고 실제 배포되는 코드를 그대로 검증하기 위한 도구다.
//
//   node tests/run_index_js.js "<expression>"
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const REPO = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(REPO, "site", "index.html"), "utf8");

let src = "";
for (const block of html.match(/<script>([\s\S]*?)<\/script>/g) || []) {
  src += block.replace(/^<script>/, "").replace(/<\/script>$/, "") + "\n";
}

// const/let 바인딩은 sandbox 전역에 붙지 않으므로 명시적으로 꺼낸다.
const EXPORTS = [
  "state", "sortableNumber", "compareNumericWithNullsLast", "nextSortDirection",
  "ariaSortValue", "sortIndicator", "sortHeaderButton",
  "APT_SORT_FIELDS", "sortAptItems", "APT_PAGE_SIZE", "currentAptSignature",
  "renderApartmentRankingList", "normalizeAptRankingItem",
  "REGION_SORT_FIELDS", "sortRegionItems", "regionSidoName", "regionLocalName",
  "renderRegionRankingTable", "formatBuildYearLabel", "formatAreaLabel",
  "SGG_CODE_MAP", "SIDO_NAMES", "OFFICIAL_REGION_TREE", "LEGACY_SIDO_CODES",
];
src += "\nglobalThis.__api = { " + EXPORTS.join(", ") + " };\n";

// 최소 DOM 스텁. 렌더 함수가 읽는 것은 라디오 입력의 value 정도다.
function makeElement() {
  return new Proxy({}, {
    get(target, prop) {
      switch (prop) {
        case "style": return {};
        case "classList": return { add() {}, remove() {}, toggle() {} };
        case "dataset": return {};
        case "value": return globalThis.__radioValue || "stable";
        case "addEventListener":
        case "removeEventListener":
        case "setAttribute":
        case "removeAttribute":
        case "appendChild":
        case "remove":
        case "click":
        case "scrollIntoView":
          return () => {};
        case "querySelectorAll": return () => [];
        case "querySelector": return () => null;
        case "innerHTML":
        case "innerText":
        case "textContent": return "";
        default: return undefined;
      }
    },
    set() { return true; },
  });
}

const el = makeElement();
const document = {
  querySelector: () => el,
  querySelectorAll: () => [],
  getElementById: () => el,
  createElement: () => el,
  addEventListener: () => {},
  body: el,
  head: el,
};

const sandbox = {
  document,
  console,
  setTimeout,
  clearTimeout,
  fetch: () => Promise.resolve({ ok: false, json: async () => ({}) }),
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  location: { href: "", search: "" },
  addEventListener: () => {},
  MutationObserver: function () { this.observe = () => {}; this.disconnect = () => {}; },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.runInNewContext(src, sandbox, { filename: "index.html-inline.js" });

const expression = process.argv[2];
// 큰 fixture는 명령줄 인자 길이 제한에 걸리므로 JSON 파일로 넘겨 __input으로 받는다.
sandbox.__input = process.argv[3]
  ? JSON.parse(fs.readFileSync(process.argv[3], "utf8"))
  : null;

const fn = vm.runInNewContext(
  "(function(api, __input){ with (api) { return (" + expression + "); } })",
  sandbox
);
const result = fn(sandbox.__api, sandbox.__input);
process.stdout.write(JSON.stringify(result === undefined ? null : result));
