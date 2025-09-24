"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = require("vscode");
function activate(context) {
    // 기본 키워드/템플릿
    const KEYWORDS = [
        "func", "obj", "var", "const", "if", "while",
        "run", "call", "show", "title", "rand"
    ];
    const keywordItems = KEYWORDS.map(k => {
        const item = new vscode.CompletionItem(k, vscode.CompletionItemKind.Keyword);
        item.insertText = k;
        return item;
    });
    // 스니펫 템플릿들
    const snippetItems = [];
    function snippet(label, detail, body) {
        const it = new vscode.CompletionItem(label, vscode.CompletionItemKind.Snippet);
        it.detail = detail;
        it.insertText = new vscode.SnippetString(body);
        it.filterText = label;
        it.sortText = '000' + label;
        return it;
    }
    snippetItems.push(snippet("func", "함수 스니펫", "func ${1:name}(){\n\t$0\n}"), snippet("obj", "오브젝트 선언", "obj ${1:objective}(${2:dummy});"), snippet("var", "변수 초기화", "var ${1:objective}:${2:name};"), snippet("const", "상수", "const ${1:NAME} = ${2:10};"), snippet("if", "if 블록", "if(${1:a}:${2:x} ${3:==} ${4:10}){\n\t$0\n}"), snippet("if[Q]", "if + 큐", "if(${1:a}:${2:x} ${3:==} ${4:10})[${5:Q}]{\n\t$0\n}"), snippet("while", "while 블록", "while(${1:a}:${2:flag}){\n\t$0\n}"), snippet("while[Q]", "while + 큐", "while(${1:a}:${2:flag})[${3:Q}]{\n\t$0\n}"), snippet("call", "함수 호출", "call ${1:foo}();"), snippet("call[Q]", "호출 + 강한 대기(wait)", "call ${1:foo}()[${2:Q}];"), snippet("run", "명령 실행", "run(\"${1:say hi}\");"), snippet("runv", "보간/JSON tellraw", "run(v\"${1:tellraw @a {text:\\\"[${2:obj}:${3:name}]\\\"}}\"\");"), snippet("show", "간단 출력", "show(v\"${1:hello}\");"), snippet("title", "타이틀", "title(title, \"${1:text}\");"), snippet("rand", "난수", "rand(${1:a}:${2:x}, ${3:0}, ${4:100});"), snippet("assign", "대입/연산", "${1:a}:${2:x} = ${3:b}:${4:y} ${5:+} ${6:c}:${7:z};"));
    // 문서에서 obj/var 스캔 → 제안에 쓰기
    function scanDoc(doc) {
        const text = doc.getText();
        const objSet = new Set();
        const nameMap = new Map(); // objective -> names
        // obj a(dummy), b(air);
        const objRe = /\bobj\s+([A-Za-z_]\w*)(?:\([A-Za-z_]\w*\))?/g;
        let m;
        while ((m = objRe.exec(text))) {
            objSet.add(m[1]);
        }
        // var a:x, b:y;
        const varRe = /\bvar\s+([^;]+);/g;
        while ((m = varRe.exec(text))) {
            const seg = m[1];
            const pairs = seg.matchAll(/([A-Za-z_]\w*):([A-Za-z_]\w*)/g);
            for (const p of pairs) {
                const obj = p[1], name = p[2];
                if (!nameMap.has(obj))
                    nameMap.set(obj, new Set());
                nameMap.get(obj).add(name);
                objSet.add(obj);
            }
        }
        return { objSet, nameMap };
    }
    const provider = vscode.languages.registerCompletionItemProvider({ language: 'mcfn' }, {
        provideCompletionItems(doc, pos) {
            const items = [];
            // 항상 보여줄 스니펫/키워드
            items.push(...snippetItems, ...keywordItems);
            // 콜론(:) 뒤에 있으면 objective에 해당하는 이름 제안
            const line = doc.lineAt(pos).text.substring(0, pos.character);
            const colonMatch = /([A-Za-z_]\w*):([A-Za-z_]\w*)?$/.exec(line);
            if (colonMatch) {
                const obj = colonMatch[1];
                const { objSet, nameMap } = scanDoc(doc);
                // objective 이름도 제안
                for (const o of objSet) {
                    const it = new vscode.CompletionItem(o, vscode.CompletionItemKind.Struct);
                    it.insertText = o;
                    it.sortText = '100' + o;
                    items.push(it);
                }
                // 해당 objective의 name들 제안
                const names = nameMap.get(obj);
                if (names) {
                    for (const n of names) {
                        const it = new vscode.CompletionItem(n, vscode.CompletionItemKind.Variable);
                        it.insertText = n;
                        it.sortText = '090' + n;
                        items.push(it);
                    }
                }
            }
            // v-string에서 스코어 보간 템플릿
            const vscore = new vscode.CompletionItem("[obj:name]", vscode.CompletionItemKind.Snippet);
            vscore.insertText = new vscode.SnippetString("[${1:obj}:${2:name}]");
            vscore.detail = "보간: 스코어 컴포넌트";
            vscore.sortText = '010[obj:name]';
            items.push(vscore);
            return items;
        }
    }, ':', '[', '"' // 트리거 문자(있으면 더 빨리 뜸)
    );
    context.subscriptions.push(provider);
}
function deactivate() { }
