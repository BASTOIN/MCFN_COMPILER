#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
MCFN-DSL -> Minecraft .mcfunction Transpiler (clean, maintainable)

Design goals
- Easy to read & modify: clear sections (Lexer/Parser/AST/Codegen/CLI)
- UTF-8 in/out
- Overwrite existing files (no accidental appends)
- Helpful error messages (VS Code/Visual Studio friendly)

Language features
- Files define one or more functions:   func <name>([params]) { ... }
- Scoreboard setup:                      obj <objective>(<criterion>)[, ...];
  * criterion optional: "obj foo;" == "obj foo(dummy);"
- Declare scoreboard players (init 0):   var <objective:name>[, ...];
- Mutations:
  * <obj:name> += <int> ;   <obj:name> -= <int> ;
  * <obj:name> = <int> ;
  * <obj:dst> = <obj:src> ;
  * <obj:dst> = <obj:l> + <obj:r> ;  (also with '-')
- Const to marker NBT:                  const <name> = <number | "string"> ;
- If/While:
  * if(<obj:name> <op> <int|obj:rhs>) { ... }  where op ∈ {==, !=, <, <=, >, >=}
  * while(<obj:name>) { ... }  (loops while score >= 1)
- Random:                               rand(<obj:name>[, min, max]);
- Run passthrough:                       run("literal command");
- Interpolated tellraw:
  * run(v"say ...[objective:player] ..."); // auto-converts to tellraw
  * show(v"... [objective:player] ...");  // always tellraw
- Title:                                 title(title, "text");   (no newlines)
- Call:                                  call <func>(args...);    (runtime function call)

Quality-of-life
- Statement terminator: ';' OR newline (both accepted)
- Works with CRLF/Windows newlines
- Single-line comments: // ...

라이선스:
    LICENCE.md 참고

Version 0.2.1 (BETA)
"""
"""
MCFN-DSL (Lark 버전) — 폴더 입력
- 입력: .mcfn 들이 들어있는 폴더 (내부 defines/*.define 지원)
- 출력: <ns>/<out>/... (네가 요청한 '뒤집힌' 경로)
- MVP 우선 반영 + 업데이트 예정안 일부 반영
  * #define / $def(name), $(name), defines/*.define 병합
  * stor a=..., b="...", c={...}, d=$def(name)
  * func _ready { }, func _tick { } → load.json, tick.json 자동 등록
  * func foo(a,b) + call foo(*args) + *a 접근, vcall dst,foo()
  * if/while (정수/스코어/스토리지 비교) + [Q] 큐
  * queue after|wait tick|sec <N|scoreRef> [call foo(...)]
  * set_queue(<int|&const|scoreRef>)[Q]
  * rep_call foo(N)  (schedule append)
  * run("..."), run(def"..."), runs{...}, /원라인...\0
  * exec @selector{ runs{...} | data{...} }  (exec 블록 내부에 제어문 금지)
  * 산술: +,-,*,/ 및 +=,-=,*=,/=, 대입 =, 이항(+,-,*,/)
  * 비교: ==,!=,<,<=,>,>=
  * return;  (업데이트안 제시대로 storage flag 기반)
  * using{ initializer; no_queue; } (기본 2개 옵션)
- 요구: Python 3.9+, lark-parser
"""


import os, sys, re, json, argparse
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional, Union, Any

# ------------- 경로/IO 유틸 -------------
def ensure_dir(path: str): os.makedirs(os.path.dirname(path), exist_ok=True)
def clear_file(path: str):
    ensure_dir(path)
    with open(path, "w", encoding="utf-8") as f: f.write("")
def write_line(path: str, s: str):
    ensure_dir(path)
    with open(path, "a", encoding="utf-8") as f: f.write(s.rstrip() + "\n")

def mcpath(out_root: str, ns: str, *parts: str) -> str:
    return os.path.join(out_root, ns, "function", *parts)
def tagpath(out_root: str, ns: str, *parts: str) -> str:
    return os.path.join(out_root, ns, "tags", "functions", *parts)

# ------------- defines 로딩 -------------
def _flat_json_text(s: str) -> str:
    return " ".join(x.strip() for x in s.strip().splitlines())

def load_external_defines(folder: str) -> Dict[str, str]:
    d = {}
    p = os.path.join(folder, "defines")
    if not os.path.isdir(p): return d
    for fn in os.listdir(p):
        if not fn.endswith(".define"): continue
        name = os.path.splitext(fn)[0]
        with open(os.path.join(p, fn), "r", encoding="utf-8") as f:
            d[name] = _flat_json_text(f.read())
    return d

# ===== I/O helpers for codegen =====
import os

def ensure_dir(path: str) -> None:
    """디렉토리 생성(부모까지)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)

def clear_file(path: str) -> None:
    """파일을 '덮어쓰기 모드'로 비우기."""
    ensure_dir(path)
    with open(path, "w", encoding="utf-8") as f:
        pass

def emit_line(path: str, s: str) -> None:
    """한 줄 쓰기(+개행)."""
    ensure_dir(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(s.rstrip("\n") + "\n")

def mc_path(ctx, rel: str) -> str:
    """
    .mcfunction 파일 실제 경로 생성.
    기본: <outdir>/<namespace>/function/<rel>
    ※ 표준 폴더를 쓰려면 아래 'function'을 'functions'로 바꾸세요.
    """
    rel = rel.replace("\\", "/")
    return os.path.join(ctx.outdir, ctx.namespace, "function", rel)

def score2(ref) -> str:
    """ScoreRef -> 'name objective' 포맷."""
    return f"{ref.name} {ref.obj}"



#    ("JSONTEXT", r'\{([^{}]|\{[^{}]*\})*\}'),
# ------------- 토큰화 -------------
TOKEN_SPEC = [
    ("WS",        r"[ \t]+"),
    ("COMMENT",   r"//[^\n]*"),
    ("NEWLINE",   r"\n"),

    # 전처리
    ("HASHDEFINE", r"#define\b"),

    # 키워드
    ("FUNC",    r"\bfunc\b"),
    ("OBJ",     r"\bobj\b"),
    ("VAR",     r"\bvar\b"),
    ("CONST",   r"\bconst\b"),
    ("IF",      r"\bif\b"),
    ("WHILE",   r"\bwhile\b"),
    ("RUN",     r"\brun\b"),
    ("RUNS",    r"\bruns\b"),
    ("SHOW",    r"\bshow\b"),
    ("TITLE",   r"\btitle\b"),
    ("CALL",    r"\bcall\b"),
    ("RETURN",  r"\breturn\b"),
    ("EXEC",    r"\bexec\b"),
    ("DATA",    r"\bdata\b"),
    ("RAND",    r"\brand\b"),

    # 연산자/구분자
    ("LE",       r"<="),
    ("GE",       r">="),
    ("EQ",       r"=="),
    ("NE",       r"!="),
    ("LT",       r"<"),
    ("GT",       r">"),
    ("PLUSEQ",   r"\+="),
    ("MINUSEQ",  r"-="),
    ("LPAREN",   r"\("),
    ("RPAREN",   r"\)"),
    ("LBRACE",   r"\{"),
    ("RBRACE",   r"\}"),
    ("COLON",    r":"),
    ("COMMA",    r","),
    ("SEMI",     r";"),
    ("ASSIGN",   r"="),
    ("PLUS",     r"\+"),
    ("MINUS",    r"-"),

    # 문자열/숫자/셀렉터/식별자
    ("VSTRING",  r'v"([^"\\]|\\.)*"'),
    ("DSTRING",  r'"([^"\\]|\\.)*"'),
    ("NUMBER",   r"-?\d+"),
    ("SELECTOR", r"@[pares]{1,2}(?:\[[^\]]*\])?"),
    ("IDENT",    r"[A-Za-z_][A-Za-z0-9_]*"),

    # RAW (runs/data 내부 라인 흡수자)
    ("RAW",      r"[^{}\n]+"),
]

TOKEN_RE = re.compile("|".join(f"(?P<{n}>{p})" for n,p in TOKEN_SPEC))

@dataclass
class Tok:
    kind: str
    val: str
    line: int
    col: int

def lex(src: str) -> List[Tok]:
    toks: List[Tok] = []
    i=0; line=1; col=1
    while i < len(src):
        m = TOKEN_RE.match(src, i)
        if not m:
            frag = src[i:i+40].replace("\n","\\n")
            raise SyntaxError(f"Lex error at {line}:{col}: {frag}")
        kind = m.lastgroup; val = m.group()
        if kind == "NEWLINE":
            toks.append(Tok(kind, val, line, col))
            line+=1; col=1
        elif kind not in ("WS","COMMENT"):
            toks.append(Tok(kind, val, line, col))
            col += (m.end()-m.start())
        else:
            col += (m.end()-m.start())
        i = m.end()
    toks.append(Tok("EOF","",line,col))
    return toks

# ------------- AST -------------
@dataclass
class ScoreRef: obj: str; name: str

class Stmt: ...
@dataclass
class S_Obj(Stmt):   
    pairs: List[Tuple[str,str]]

@dataclass
class S_Var(Stmt):   
    inits: List[ScoreRef]

@dataclass
class S_Assign(Stmt):
    ref: ScoreRef; 
    expr: "Expr"

@dataclass
class S_Arith(Stmt): 
    ref: ScoreRef; 
    op: str; 
    expr: "Expr"

@dataclass
class S_Run(Stmt):   
    text: str; 
    is_v: bool; 
    def_mode: bool=False

@dataclass
class S_Runs(Stmt):  
    lines: List[str]

@dataclass
class S_If(Stmt):    
    lhs: "Expr"; 
    op: str; 
    rhs: "Expr"; 
    body: List[Stmt]; 
    qslot: Optional[str]

@dataclass
class S_While(Stmt): 
    ref: ScoreRef; 
    body: List[Stmt]; 
    qslot: Optional[str]

@dataclass
class S_Rand(Stmt):
    ref: ScoreRef
    lo: Optional[int] = None
    hi: Optional[int] = None

@dataclass
class S_Call(Stmt):  
    name: str; args: List["Expr"]; 
    qslot: Optional[str]

@dataclass
class S_VCall(Stmt): 
    dst: ScoreRef; 
    name: str; 
    args: List["Expr"]; 
    qslot: Optional[str]

@dataclass
class S_Return(Stmt):
    val: Optional["Expr"]

@dataclass
class S_Stor(Stmt):  
    items: List[Tuple[str, "StorVal"]]

@dataclass
class S_Exec(Stmt):  
    selector: str; 
    runs_lines: List[str]; 
    data_lines: List[str]

@dataclass
class S_Show(Stmt):
    text: str  # v-문자열: [obj:name] 보간 허용

@dataclass
class S_Title(Stmt):
    text: str  # 일반 문자열, 개행 불가

class Expr: ...
@dataclass
class E_Int(Expr): val: int
@dataclass
class E_Str(Expr): val: str
@dataclass
class E_Ref(Expr): ref: ScoreRef
@dataclass
class E_Bin(Expr): op: str; a: Expr; b: Expr

StorVal = Union[int, str, Tuple[str,str], Tuple[str,str]]  # int | str | ("json",txt) | ("def","NAME")

@dataclass
class DefineTable:
    inline: Dict[str,str] = field(default_factory=dict)    # #define
    external: Dict[str,str] = field(default_factory=dict)  # defines/*.define
    def get(self, name:str)->Optional[str]:
        return self.inline.get(name) or self.external.get(name)

@dataclass
class Func:
    name: str
    params: List[str]
    body: List[Stmt]
    special: Optional[str]=None


#--------------------------------

def get_def_raw(defs_raw: dict, name: str):
    if name not in defs_raw:
        raise KeyError(f"$def({name}) 원문을 찾을 수 없습니다. #define/#define: 를 확인해 주세요.")
    return defs_raw[name]

def get_def(defs_inline: dict, name: str) -> str:
    if name not in defs_inline:
        raise KeyError(
            f"$def({name}) 가 정의되지 않았습니다. "
            f"#define 을 확인해 주세요."
        )
    return defs_inline[name]

def substitute_defs(text: str, defs_inline: dict) -> str:
    """문자열 안의 $def(NAME)를 정의값으로 치환."""
    if "$def(" not in text:
        return text
    import re, json
    def repl(m):
        key = m.group(1)
        val = get_def(defs_inline, key)
        if isinstance(val, (dict, list)):
            return json.dumps(val, ensure_ascii=False)
        return str(val)
    return re.sub(r"\$def\(([A-Za-z_][A-Za-z0-9_]*)\)", repl, text)

def interpolate_json(text: str) -> list:
    """
    v-문자열 안에서 [obj:name] 을 tellraw score 컴포넌트로 바꿔주는 간단 유틸.
    예: 'value is [a:x]!' ->
        [{"text":"value is "},{"score":{"name":"x","objective":"a"}},{"text":"!"}]
    """
    import re, json
    out = []
    i = 0
    for m in re.finditer(r"\[([A-Za-z_][A-Za-z0-9_]*)\:([A-Za-z_][A-Za-z0-9_]*)\]", text):
        if m.start() > i:
            out.append({"text": text[i:m.start()]})
        obj, name = m.group(1), m.group(2)
        out.append({"score": {"name": name, "objective": obj}})
        i = m.end()
    if i < len(text):
        out.append({"text": text[i:]})
    return out or [{"text": ""}]

def parse_json_relaxed(text: str):
    """
    표준 json.loads → 실패 시 느슨 파서로 재시도
    - //, # 주석 제거
    - 작은따옴표 → 큰따옴표
    - { key: ... } / , key: ... → {"key": ...}, ,"key": ...
    - 트레일링 콤마 제거
    """
    try:
        return json.loads(text)
    except Exception:
        s = text.strip()
        s = re.sub(r'//.*?$', '', s, flags=re.MULTILINE)
        s = re.sub(r'#.*?$',  '', s, flags=re.MULTILINE)
        s = re.sub(r"'", '"', s)
        def _quote_keys(m): return f'{m.group(1)}"{m.group(2)}":'
        s = re.sub(r'(\{|\s*,)\s*([A-Za-z_][A-Za-z0-9_]*)\s*:', _quote_keys, s)
        s = re.sub(r',\s*([}\]])', r'\1', s)
        return json.loads(s)

# ------------- 파서 -------------
class Parser:
    def __init__(self, toks, base_dir="."):
        self.toks = toks; self.i = 0
        self.base_dir = base_dir
        self.defs_inline: Dict[str, Any] = {}  # 파싱된 값
        self.defs_raw: Dict[str, str] = {}     # 원문 보관


    def cur(self)->Tok: return self.toks[self.i]
    
    def eat(self, kind:str)->Tok:
        t=self.cur()
        if t.kind!=kind: raise SyntaxError(f"Expected {kind} at {t.line}:{t.col}, got {t.kind}")
        self.i+=1; return t
    
    def match(self, kind:str)->bool:
        if self.cur().kind==kind: self.i+=1; return True
        return False
    
    def collect_brace_text(self) -> str:
        depth, buf = 0, []
        while self.i < len(self.toks):
            t = self.cur()
            if t.kind == "LBRACE":
                depth += 1
            elif t.kind == "RBRACE":
                depth -= 1
                if depth < 0:
                    break
            buf.append(t.val)
            self.i += 1
        return "".join(buf)
    
    def _load_define_file(self, fname: str):
        import os, json, re
        candidates = [
            os.path.join(self.base_dir, fname),
            os.path.join(self.base_dir, "defines", fname),
            os.path.join("defines", fname),
            fname,
        ]
        path = next((p for p in candidates if os.path.exists(p)), None)
        if not path:
            raise FileNotFoundError(f"define file not found: {fname}")

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or not s.startswith("#define"):
                    continue
                m = re.match(r"#define\s+([A-Za-z_][A-Za-z0-9_]*)\s+(.+)$", s)
                if not m:
                    continue
                key, val = m.group(1), m.group(2).strip()
                if val.startswith("{") and val.endswith("}"):
                    self.defs_inline[key] = val
                elif val.startswith('"'):
                    self.defs_inline[key] = json.loads(val)
                else:
                    try:
                        self.defs_inline[key] = int(val)
                    except ValueError:
                        self.defs_inline[key] = val

    def _load_external_define(self, key: str):
        import os
        candidates = [
            os.path.join(self.base_dir, "defines", f"{key}.define"),
            os.path.join("defines", f"{key}.define"),
            f"{key}.define",
        ]
        for p in candidates:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8-sig") as f:
                    raw = f.read()
                self.defs_raw[key] = raw  # 원문 저장(개행 포함)
                self.defs_inline[key] = parse_json_relaxed(raw)
                return
        raise FileNotFoundError(f"외부 define 파일을 찾을 수 없습니다: defines/{key}.define")
    
    def parse_all(self):
        funcs = []
        while self.cur().kind != "EOF":
            k = self.cur().kind

            # 1) 공백 줄 스킵
            if k == "NEWLINE":
                self.i += 1
                continue

            # 2) #define
            if k == "HASHDEFINE":
                self.i += 1
                name = self.eat("IDENT").val

                # 외부 참조: '#define Name;' 또는 '#define Name:'
                if self.cur().kind in ("SEMI", "COLON"):
                    self.i += 1
                    # 줄 끝(선택)
                    if self.cur().kind == "NEWLINE":
                        self.i += 1
                    self._load_external_define(name)
                    continue

                # 내부 JSON: '#define Name { ... }'
                if self.cur().kind == "LBRACE":
                    txt = self.collect_brace_text()         # 원문 통째로 보관
                    self.defs_raw[name] = txt
                    self.defs_inline[name] = parse_json_relaxed(txt)
                    # 문장 끝(선택)
                    if self.cur().kind in ("SEMI", "NEWLINE"):
                        self.i += 1
                    continue

                # 문법 오류 안내
                t = self.cur()
                raise SyntaxError(
                    f"#define {name} 구문 오류. 허용 형태:\n"
                    f"  #define {name} {{ ... }}   (파일 내부 JSON)\n"
                    f"  #define {name}; 또는 #define {name}:  (외부 ./defines/{name}.define JSON)\n"
                    f"위치: {t.line}:{t.col}"
                )

            # 3) func
            if k == "FUNC":
                funcs.append(self.parse_func())
                continue

            # 그 외는 모두 오류
            t = self.cur()
            raise SyntaxError(f"Unexpected {t.kind} at {t.line}:{t.col}")

        # 전 파일(폴더)에서 모은 define 들을 함께 반환
        return funcs, dict(self.defs_inline), dict(self.defs_raw)
    
    def parse_define(self):
        self.eat("HASHDEFINE")
        name = self.eat("IDENT").val

        if self.cur().kind == "LBRACE":
            txt = self.collect_brace_text()            # ← JSON 원문 통째로
            self.defs_inline[name] = _flat_json_text(txt)
        else:
            raise SyntaxError(f"define expects {{...}} at {self.cur().line}:{self.cur().col}")

        if self.match("SEMI"):
            return
        if self.cur().kind == "NEWLINE":
            self.i += 1

        # 문장 끝 유연 처리
        if self.match("SEMI"): 
            return
        if self.cur().kind == "NEWLINE":
            self.i += 1

    # ------- functions / blocks -------
    def parse_func(self)->Func:
        self.eat("FUNC")
        name=self.eat("IDENT").val
        params=[]
        self.eat("LPAREN")
        if self.cur().kind=="IDENT":
            params.append(self.eat("IDENT").val)
            while self.match("COMMA"):
                params.append(self.eat("IDENT").val)
        self.eat("RPAREN")
        body=self.parse_block_stmts()
        special = "_ready" if name=="_ready" else "_tick" if name=="_tick" else None
        return Func(name, params, body, special)

    def parse_block_stmts(self)->List[Stmt]:
        self.eat("LBRACE")
        out=[]
        while self.cur().kind not in ("RBRACE","EOF"):
            if self.cur().kind=="NEWLINE": self.i+=1; continue
            out.append(self.parse_stmt())
        self.eat("RBRACE")
        return out

    def need_semi(self):
        if self.match("SEMI"): return
        if self.cur().kind=="NEWLINE": self.i+=1; return
        if self.cur().kind in ("RBRACE","EOF"): return
        raise SyntaxError(f"Expected ';' or newline at {self.cur().line}:{self.cur().col}")

    def parse_stmt(self)->Stmt:
        k=self.cur().kind
        if   k=="OBJ":   return self.parse_obj()
        elif k=="VAR":   return self.parse_var()
        elif k=="STOR":  return self.parse_stor()
        elif k=="IDENT": return self.parse_assign_or_arith()
        elif k=="IF":    return self.parse_if()
        elif k=="WHILE": return self.parse_while()
        elif k=="RUN":   return self.parse_run()
        elif k=="RUNS":  return self.parse_runs()
        elif k=="CALL":  return self.parse_call()
        elif k=="VCALL": return self.parse_vcall()
        elif k=="RETURN":return self.parse_return()
        elif k=="EXEC":  return self.parse_exec()   # ← 이 메서드가 아래에 반드시 있어야 함
        else:
            raise SyntaxError(f"Unexpected token {k} at {self.cur().line}:{self.cur().col}")
    # ------- declarations -------
    def parse_obj(self)->S_Obj:
        self.eat("OBJ")
        pairs=[]
        while True:
            obj=self.eat("IDENT").val
            crit="dummy"
            if self.match("LPAREN"):
                crit=self.eat("IDENT").val
                self.eat("RPAREN")
            pairs.append((obj,crit))
            if not self.match("COMMA"): break
        self.need_semi()
        return S_Obj(pairs)

    def parse_var(self)->S_Var:
        self.eat("VAR")
        inits=[]
        while True:
            obj=self.eat("IDENT").val
            self.eat("COLON")
            name=self.eat("IDENT").val
            inits.append(ScoreRef(obj,name))
            if not self.match("COMMA"): break
        self.need_semi()
        return S_Var(inits)

    def parse_stor(self) -> S_Stor:
        self.eat("STOR")
        items=[]
        while True:
            key = self.eat("IDENT").val
            self.eat("ASSIGN")
            k = self.cur().kind
            if k == "NUMBER":
                items.append((key, int(self.eat("NUMBER").val)))
            elif k == "DSTRING":
                items.append((key, json.loads(self.eat("DSTRING").val)))
            elif k == "LBRACE":
                txt = self.collect_brace_text()        # ← JSON 원문 통째로
                items.append((key, ("json", _flat_json_text(txt))))
            elif k == "DEFREF":
                raw = self.eat("DEFREF").val
                m = re.match(r'\$(?:def)?\(([A-Za-z_][A-Za-z0-9_]*)\)', raw)
                items.append((key, ("def", m.group(1) if m else raw)))
            else:
                raise SyntaxError(f"stor value invalid at {self.cur().line}:{self.cur().col}")
            if not self.match("COMMA"): break
        self.need_semi()
        return S_Stor(items)

    def parse_rand(self) -> S_Rand:
        self.eat("RAND")
        self.eat("LPAREN")
        ref = self.parse_ref()
        lo = hi = None
        if self.match("COMMA"):
            lo = int(self.eat("NUMBER").val)
            self.eat("COMMA")
            hi = int(self.eat("NUMBER").val)
        self.eat("RPAREN")
        self.need_semi()
        return S_Rand(ref, lo, hi)

    # ------- expressions -------
    def parse_ref(self)->ScoreRef:
        obj=self.eat("IDENT").val
        self.eat("COLON")
        name=self.eat("IDENT").val
        return ScoreRef(obj,name)

    def parse_paren_expr(self)->Expr:
        self.eat("LPAREN")
        e=self.parse_expr()
        self.eat("RPAREN")
        return e

    def parse_expr(self)->Expr:
        e=self.parse_term()
        while self.cur().kind in ("PLUS","MINUS"):
            op=self.cur().val; self.i+=1
            e=E_Bin(op,e,self.parse_term())
        return e
    def parse_term(self)->Expr:
        e=self.parse_factor()
        while self.cur().kind in ("MUL","DIV"):
            op=self.cur().val; self.i+=1
            e=E_Bin(op,e,self.parse_factor())
        return e
    def parse_factor(self)->Expr:
        k=self.cur().kind
        if k=="NUMBER":
            v=int(self.eat("NUMBER").val); return E_Int(v)
        if k=="DSTRING":
            s=json.loads(self.eat("DSTRING").val); return E_Str(s)
        if k=="IDENT":
            return E_Ref(self.parse_ref())
        if k=="LPAREN":
            return self.parse_paren_expr()
        raise SyntaxError(f"expr expected at {self.cur().line}:{self.cur().col}")

    # ------- assign/arith -------
    def parse_assign_or_arith(self)->Stmt:
        obj=self.eat("IDENT").val
        self.eat("COLON")
        name=self.eat("IDENT").val
        ref=ScoreRef(obj,name)
        k=self.cur().kind
        if k=="ASSIGN":
            self.i+=1
            e=self.parse_expr()
            self.need_semi()
            return S_Assign(ref,e)
        elif k in ("PLUSEQ","MINUSEQ","MULEQ","DIVEQ"):
            op=self.eat(k).val
            e=self.parse_expr()
            self.need_semi()
            return S_Arith(ref,op,e)
        else:
            raise SyntaxError(f"Expected assignment/op at {self.cur().line}:{self.cur().col}")

    # ------- control -------
    def parse_cmp(self)->Tuple[Expr,str,Expr]:
        a=self.parse_expr()
        op_tok=self.cur()
        if op_tok.kind not in ("EQ","NE","LT","LE","GT","GE"):
            raise SyntaxError(f"cmp op expected at {op_tok.line}:{op_tok.col}")
        self.i+=1
        b=self.parse_expr()
        return (a, op_tok.val, b)

    def parse_qopt(self)->Optional[str]:
        if self.match("LBRACK"):
            slot=self.eat("IDENT").val
            self.eat("RBRACK")
            return slot
        return None

    def parse_if(self)->S_If:
        self.eat("IF"); self.eat("LPAREN")
        a,op,b=self.parse_cmp()
        self.eat("RPAREN")
        q=self.parse_qopt()
        body=self.parse_block_stmts()
        return S_If(a,op,b,body,q)

    def parse_while(self)->S_While:
        self.eat("WHILE"); self.eat("LPAREN")
        ref=self.parse_ref()
        self.eat("RPAREN")
        q=self.parse_qopt()
        body=self.parse_block_stmts()
        return S_While(ref,body,q)

    # ------- run / runs -------
    def parse_run(self)->S_Run:
        self.eat("RUN"); self.eat("LPAREN")
        t=self.cur()
        if t.kind=="VSTRING":
            s=t.val[2:-1]; self.i+=1
            self.eat("RPAREN"); self.need_semi()
            # v-문자열: def 치환을 원할 때 run(def"...") 형태로 사용 → def_mode 플래그
            return S_Run(s,True,False)
        elif t.kind=="DSTRING":
            s=json.loads(t.val); self.i+=1
            self.eat("RPAREN"); self.need_semi()
            # "def..." 인지 검사
            is_def = isinstance(s,str) and s.startswith("def")
            # run("def ...") → def_mode=True 로 처리
            return S_Run(s,False,is_def)
        else:
            raise SyntaxError(f"run(...) expects string at {t.line}:{t.col}")

    def parse_runs(self) -> S_Runs:
        self.eat("RUNS")
        self.eat("LBRACE")
        lines, buf = [], []
        depth = 1

        while self.i < len(self.toks):
            t = self.cur()
            if t.kind == "LBRACE":
                depth += 1
            elif t.kind == "RBRACE":
                depth -= 1
                if depth == 0:
                    break

            if t.kind == "NEWLINE":
                if buf:
                    # --- 공백 복원 ---
                    pieces = [tok.val for tok in buf]
                    raw = " ".join(pieces)
                    raw = re.sub(r"\s+", " ", raw).strip()
                    c = raw.find("//")
                    if c != -1:
                        raw = raw[:c].rstrip()
                    if raw:
                        lines.append(raw)
                    buf = []
            else:
                buf.append(t)
            self.i += 1

        if buf:
            pieces = [tok.val for tok in buf]
            raw = " ".join(pieces)
            raw = re.sub(r"\s+", " ", raw).strip()
            c = raw.find("//")
            if c != -1:
                raw = raw[:c].rstrip()
            if raw:
                lines.append(raw)

        self.eat("RBRACE")
        return S_Runs(lines)
    
    # ------- calls / return / exec -------
    def parse_call(self)->S_Call:
        self.eat("CALL")
        name=self.eat("IDENT").val
        self.eat("LPAREN")
        args=[]
        if self.cur().kind!="RPAREN":
            args.append(self.parse_expr())
            while self.match("COMMA"):
                args.append(self.parse_expr())
        self.eat("RPAREN")
        q=self.parse_qopt()
        self.need_semi()
        return S_Call(name,args,q)

    def parse_vcall(self)->S_VCall:
        self.eat("VCALL")
        dst=self.parse_ref()
        self.eat("COMMA")
        name=self.eat("IDENT").val
        self.eat("LPAREN")
        args=[]
        if self.cur().kind!="RPAREN":
            args.append(self.parse_expr())
            while self.match("COMMA"):
                args.append(self.parse_expr())
        self.eat("RPAREN")
        q=self.parse_qopt()
        self.need_semi()
        return S_VCall(dst,name,args,q)

    def parse_return(self)->S_Return:
        self.eat("RETURN")
        if self.cur().kind in ("NUMBER","DSTRING","IDENT","LPAREN"):
            val=self.parse_expr()
        else:
            val=None
        self.need_semi()
        return S_Return(val)

    def parse_exec(self) -> S_Exec:
        self.eat("EXEC")

        # selector
        if self.cur().kind == "IDENT":
            selector = self.eat("IDENT").val
        elif self.cur().kind == "SELECTOR":
            selector = self.eat("SELECTOR").val
        elif self.cur().kind == "DSTRING":
            selector = json.loads(self.eat("DSTRING").val)
        else:
            raise SyntaxError(f"exec expects selector at {self.cur().line}:{self.cur().col}")

        self.eat("LBRACE")

        runs_lines, data_lines = [], []

        while self.cur().kind != "RBRACE":
            if self.cur().kind == "NEWLINE":
                self.i += 1
                continue

            # runs 블록
            if self.cur().kind == "RUNS":
                self.i += 1
                self.eat("LBRACE")
                tmp, buf, depth = [], [], 1
                while self.i < len(self.toks):
                    t = self.cur()
                    if t.kind == "LBRACE": depth += 1
                    elif t.kind == "RBRACE":
                        depth -= 1
                        if depth == 0: break

                    if t.kind == "NEWLINE":
                        if buf:
                            pieces = [tok.val for tok in buf]
                            raw = " ".join(pieces)
                            raw = re.sub(r"\s+", " ", raw).strip()
                            c = raw.find("//")
                            if c != -1:
                                raw = raw[:c].rstrip()
                            if raw:
                                tmp.append(raw)
                            buf = []
                    else:
                        buf.append(t)
                    self.i += 1

                if buf:
                    pieces = [tok.val for tok in buf]
                    raw = " ".join(pieces)
                    raw = re.sub(r"\s+", " ", raw).strip()
                    c = raw.find("//")
                    if c != -1:
                        raw = raw[:c].rstrip()
                    if raw:
                        tmp.append(raw)

                self.eat("RBRACE")
                runs_lines.extend(tmp)
                continue

            # data 블록
            if self.cur().kind == "DATA":
                self.i += 1
                self.eat("LBRACE")
                tmp, buf, depth = [], [], 1
                while self.i < len(self.toks):
                    t = self.cur()
                    if t.kind == "LBRACE": depth += 1
                    elif t.kind == "RBRACE":
                        depth -= 1
                        if depth == 0: break

                    if t.kind == "NEWLINE":
                        if buf:
                            pieces = [tok.val for tok in buf]
                            raw = " ".join(pieces)
                            raw = re.sub(r"\s+", " ", raw).strip()
                            c = raw.find("//")
                            if c != -1:
                                raw = raw[:c].rstrip()
                            if raw:
                                tmp.append(raw)
                            buf = []
                    else:
                        buf.append(t)
                    self.i += 1

                if buf:
                    pieces = [tok.val for tok in buf]
                    raw = " ".join(pieces)
                    raw = re.sub(r"\s+", " ", raw).strip()
                    c = raw.find("//")
                    if c != -1:
                        raw = raw[:c].rstrip()
                    if raw:
                        tmp.append(raw)

                self.eat("RBRACE")
                data_lines.extend(tmp)
                continue

            self.i += 1

        self.eat("RBRACE")
        return S_Exec(selector, runs_lines, data_lines)
# ------------- 코드 생성 -------------

@dataclass
class CGCtx:
    ns: str
    out_root: str
    filebase: str
    if_counter: int = 0
    while_counter: int = 0
    queue_counter: int = 0
    queue_slots: List[str] = field(default_factory=list)
    queue_entries: List[Tuple[str, str]] = field(default_factory=list)
    called_qmain: bool = False
    current_func: str = ""
    storage_prefix: str = ""
    defines: Any = None                
    defs_inline: Dict[str, Any] = field(default_factory=dict) 
    defs_raw: Dict[str, str] = field(default_factory=dict) 
    param_bind: Dict[str, Any] = field(default_factory=dict)

    def mc(self, *parts) -> str: 
        return mcpath(self.out_root, self.ns, self.filebase, *parts)

    def qpath(self, *parts) -> str: 
        return mcpath(self.out_root, self.ns, "queue", *parts)

def score2(r: ScoreRef)->str: return f"{r.name} {r.obj}"

def _emit_expr_to_score(ctx: CGCtx, path: str, target: ScoreRef, e: Expr):
    if isinstance(e,E_Int):
        write_line(path, f"scoreboard players set {score2(target)} {e.val}")
    elif isinstance(e,E_Ref):
        write_line(path, f"scoreboard players operation {score2(target)} = {score2(e.ref)}")
    elif isinstance(e,E_Bin):
        _emit_expr_to_score(ctx, path, target, e.a)
        # rhs
        if isinstance(e.b,E_Int):
            write_line(path, f"scoreboard players set __tmp {target.obj} {e.b.val}")
            rhs=ScoreRef(target.obj,"__tmp")
        elif isinstance(e.b,E_Ref):
            rhs=e.b.ref
        else:
            return
        opmap={"+":"+=","-":"-=","*":"*=","/":"/="}
        write_line(path, f"scoreboard players operation {score2(target)} {opmap[e.op]} {score2(rhs)}")

def _emit_runs(path: str, lines: List[str]):
    for ln in lines:
        write_line(path, ln)

def _ensure_mcfq(path: str):
    write_line(path, "scoreboard objectives add mcfq dummy")

def _qappend(ctx: CGCtx, slot: str, target_woext: str):
    ctx.queue_slots.append(slot)
    ctx.queue_entries.append((slot, target_woext))

def _write_qdispatcher(ctx: CGCtx):
    if not ctx.queue_entries: return
    # queue_main
    qmain=ctx.qpath("queue_main.mcfunction"); clear_file(qmain); _ensure_mcfq(qmain)
    for slot, fn in ctx.queue_entries:
        write_line(qmain, f"execute if score {slot} mcfq matches 0 run function {ctx.ns}:{fn}")
    # any_open
    anyp=ctx.qpath("any_open.mcfunction"); clear_file(anyp); _ensure_mcfq(anyp)
    write_line(anyp, "scoreboard players set __open mcfq 0")
    for s in sorted(set(ctx.queue_slots)):
        write_line(anyp, f"execute if score {s} mcfq matches 0 run scoreboard players set __open mcfq 1")

def _emit_wait(ctx: CGCtx, caller_path: str, slot: str, wait_name: str):
    wpath = ctx.qpath(f"{wait_name}.mcfunction")
    clear_file(wpath); _ensure_mcfq(wpath)
    write_line(wpath, f"function {ctx.ns}:queue/queue_main")
    write_line(wpath, f"function {ctx.ns}:queue/any_open")
    write_line(wpath, f"execute if score __open mcfq matches 1 run schedule function {ctx.ns}:queue/{wait_name} 1t")
    write_line(wpath, f"execute unless score __open mcfq matches 1 run scoreboard players set {slot} mcfq 0")
    write_line(wpath, f"execute unless score __open mcfq matches 1 run function {ctx.ns}:queue/queue_main")
    write_line(caller_path, f"schedule function {ctx.ns}:queue/{wait_name} 1t")
    ctx.called_qmain=True

def _emit_cmp_call(ctx: CGCtx, path: str, lhs: Expr, op: str, rhs: Expr, goto_woext: str):
    def _score_score(op,A:ScoreRef,B:ScoreRef):
        m={"==":"=","!=":"!=", "<":"<", "<=":"<=", ">":">", ">=":">="}[op]
        if op=="!=":
            write_line(path, f"execute unless score {score2(A)} {m} {score2(B)} run function {ctx.ns}:{goto_woext}")
        else:
            write_line(path, f"execute if score {score2(A)} {m} {score2(B)} run function {ctx.ns}:{goto_woext}")

    if isinstance(lhs,E_Ref) and isinstance(rhs,E_Ref):
        _score_score(op,lhs.ref,rhs.ref)
    elif isinstance(lhs,E_Ref) and isinstance(rhs,E_Int):
        if op=="!=":
            write_line(path, f"execute unless score {score2(lhs.ref)} matches {rhs.val}..{rhs.val} run function {ctx.ns}:{goto_woext}")
        else:
            rng={"==":f"{rhs.val}..{rhs.val}","<=":f"..{rhs.val}",">=":f"{rhs.val}..","<":f"..{rhs.val-1}",">":f"{rhs.val+1}.."}.get(op)
            if rng: write_line(path, f"execute if score {score2(lhs.ref)} matches {rng} run function {ctx.ns}:{goto_woext}")

def _def_replace(defs: DefineTable, s: str)->str:
    # $def(NAME) / $(NAME) 치환
    def repl(m):
        name=m.group(1)
        body=defs.get(name) or ""
        return body
    return re.sub(r'\$\(?def\)?\(([A-Za-z_][A-Za-z0-9_]*)\)', repl, s)

def emit_block(ctx: CGCtx, path: str, body: List[Stmt]):
    import json

    for s in body:
        # ---------- 선언/초기화 ----------
        if isinstance(s, S_Obj):
            for obj, crit in s.pairs:
                emit_line(path, f"scoreboard objectives add {obj} {crit}")

        elif isinstance(s, S_Var):
            for r in s.inits:
                emit_line(path, f"scoreboard players set {r.name} {r.obj} 0")

        # ---------- 대입/산술 ----------
        elif isinstance(s, S_Assign):
            _emit_expr_to_score(ctx, path, s.ref, s.expr)

        elif isinstance(s, S_Arith):
            if isinstance(s.expr, E_Int):
                if s.op == "+=":
                    emit_line(path, f"scoreboard players add {s.ref.name} {s.ref.obj} {s.expr.val}")
                elif s.op == "-=":
                    emit_line(path, f"scoreboard players remove {s.ref.name} {s.ref.obj} {abs(s.expr.val)}")
                else:
                    # *=, /= 의 즉석 임시값
                    emit_line(path, f"scoreboard players set __tmp {s.ref.obj} {s.expr.val}")
                    emit_line(path, f"scoreboard players operation {s.ref.name} {s.ref.obj} {s.op} __tmp {s.ref.obj}")
            elif isinstance(s.expr, E_Ref):
                emit_line(
                    path,
                    f"scoreboard players operation {s.ref.name} {s.ref.obj} {s.op} {s.expr.ref.name} {s.expr.ref.obj}"
                )

        # ---------- rand (1.21+) ----------
        elif isinstance(s, S_Rand):
            lo = 0 if s.lo is None else s.lo
            hi = 100 if s.hi is None else s.hi
            emit_line(path, f"execute store result score {s.ref.name} {s.ref.obj} run random value {lo}..{hi}")

        # ---------- run / runs ----------
        elif isinstance(s, S_Run):
            """
            규칙:
            - v-string 이고, 전체가 '$def(NAME)' 하나만일 때:
                defs_raw[NAME]의 '원문'을 줄 단위 strip 후 빈칸 없이 바로 이어붙여
                한 줄로 출력 (tellraw 금지)
            예)
                { \n  Tags:["asdf"]\n}\n → {Tags:["asdf"]}
            - 그 외:
                $def 치환 후 (v-string이면) tellraw, 아니면 평문
            """
            m = re.fullmatch(r'\s*\$def\(([A-Za-z_][A-Za-z0-9_]*)\)\s*', s.text)
            if s.is_v and m:
                key = m.group(1)
                # 1) 원문이 있으면 원문 기반 "엔터 없는 한 줄" 출력
                if key in ctx.defs_raw:
                    raw = get_def_raw(ctx.defs_raw, key)
                    one_line = ''.join(line.strip() for line in raw.splitlines())
                    if one_line:
                        emit_line(path, one_line)
                    return
                # 2) 원문은 없지만 값이 문자열인 경우 문자열을 1줄로
                if key in ctx.defs_inline and isinstance(ctx.defs_inline[key], str):
                    txt = ctx.defs_inline[key]
                    one_line = re.sub(r'[\r\n]+', ' ', txt).strip()
                    emit_line(path, one_line)
                    return
                # 3) 나머지는 일반 v-string 처리로 진행 (tellraw)

            # 일반 케이스: $def 치환
            text = substitute_defs(s.text, ctx.defs_inline)

            if s.is_v:
                msg = text.strip()
                if msg.startswith("say "):
                    comps = interpolate_json(msg[4:])
                else:
                    comps = interpolate_json(msg)
                emit_line(path, f"tellraw @a {json.dumps(comps, ensure_ascii=False)}")
            else:
                emit_line(path, text)
        elif isinstance(s, S_Runs):
            for ln in s.lines:
                emit_line(path, ln)

        # ---------- show / title ----------
        elif isinstance(s, S_Show):
            text = substitute_defs(s.text, ctx.defs_inline)
            comps = interpolate_json(text)
            emit_line(path, f"tellraw @a {json.dumps(comps, ensure_ascii=False)}")

        elif isinstance(s, S_Title):
            text = substitute_defs(s.text, ctx.defs_inline)
            if "\n" in text:
                raise ValueError("title(...) 문자열에 개행(\\n) 불가")
            emit_line(path, f'title @a title {json.dumps({"text": text}, ensure_ascii=False)}')

        # ---------- storage ----------
        elif isinstance(s, S_Stor):
            for key, val in s.items:
                if isinstance(val, tuple) and len(val) == 2 and val[0] == "json":
                    raw = substitute_defs(val[1], ctx.defs_inline)
                    emit_line(path, f"data modify storage {ctx.storage_prefix} {key} set value {raw}")
                elif isinstance(val, tuple) and len(val) == 2 and val[0] == "def":
                    resolved = get_def(ctx.defs_inline, val[1])
                    if isinstance(resolved, (dict, list)):
                        emit_line(path, f"data modify storage {ctx.storage_prefix} {key} set value {json.dumps(resolved, ensure_ascii=False)}")
                    elif isinstance(resolved, (int, float)):
                        emit_line(path, f"data modify storage {ctx.storage_prefix} {key} set value {resolved}")
                    else:
                        emit_line(path, f"data modify storage {ctx.storage_prefix} {key} set value {json.dumps(str(resolved), ensure_ascii=False)}")
                elif isinstance(val, str):
                    text = substitute_defs(val, ctx.defs_inline)
                    emit_line(path, f"data modify storage {ctx.storage_prefix} {key} set value {json.dumps(text, ensure_ascii=False)}")
                else:
                    emit_line(path, f"data modify storage {ctx.storage_prefix} {key} set value {val}")

        # ---------- 제어 ----------
        elif isinstance(s, S_If):
            ctx.if_counter += 1
            sub_woext = f"ifs/if_{ctx.current_func}_{ctx.if_counter}"
            subpath = ctx.mc("functions", sub_woext + ".mcfunction")
            clear_file(subpath)
            emit_block(ctx, subpath, s.body)
            _emit_cmp_call(ctx, path, s.lhs, s.op, s.rhs, f"{ctx.filebase}/functions/{sub_woext}")

        elif isinstance(s, S_While):
            ctx.while_counter += 1
            sub_woext = f"whiles/while_{ctx.current_func}_{ctx.while_counter}"
            subpath = ctx.mc("functions", sub_woext + ".mcfunction")
            clear_file(subpath)
            emit_block(ctx, subpath, s.body)
            emit_line(subpath, f"execute if score {s.ref.name} {s.ref.obj} matches 1.. run function {ctx.ns}:{ctx.filebase}/functions/{sub_woext}")
            emit_line(path,     f"function {ctx.ns}:{ctx.filebase}/functions/{sub_woext}")

        # ---------- 호출/반환 ----------
        elif isinstance(s, S_Call):
            emit_line(path, f"function {ctx.ns}:{ctx.filebase}/functions/{s.name}")

        elif isinstance(s, S_VCall):
            emit_line(path, f"data modify storage {ctx.storage_prefix} return set value 0")
            emit_line(path, f"function {ctx.ns}:{ctx.filebase}/functions/{s.name}")
            emit_line(path, "execute if data storage {sp} {{return:1}} run "
                             "execute store result score {dn} {do} run "
                             "data get storage {sp} retval"
                             .format(sp=ctx.storage_prefix, dn=s.dst.name, do=s.dst.obj))

        elif isinstance(s, S_Return):
            emit_line(path, f"data modify storage {ctx.storage_prefix} return set value 1")
            if s.val is None:
                emit_line(path, f"data remove storage {ctx.storage_prefix} retval")
            elif isinstance(s.val, E_Int):
                emit_line(path, f"data modify storage {ctx.storage_prefix} retval set value {s.val.val}")
            elif isinstance(s.val, E_Str):
                emit_line(path, f"data modify storage {ctx.storage_prefix} retval set value {json.dumps(s.val.val, ensure_ascii=False)}")
            elif isinstance(s.val, E_Ref):
                pass

        # ---------- exec ----------
        elif isinstance(s, S_Exec):
            for ln in s.runs_lines:
                emit_line(path, f"execute as {s.selector} at @s run {ln}")
            for ln in s.data_lines:
                emit_line(path, f"execute as {s.selector} at @s run data {ln}")

        else:
            raise RuntimeError(f"Unhandled stmt: {type(s).__name__}")



def generate_for_file(ns: str, out_root: str, filebase: str, funcs: List[Func], defs: DefineTable):
    ctx = CGCtx(ns=ns, out_root=out_root, filebase=filebase, defines=defs)
    load_vals: List[str]=[]
    tick_vals: List[str]=[]

    for fn in funcs:
        ctx.current_func = fn.name
        ctx.storage_prefix = f"mcfn_{ns}:{fn.name}"

        fpath = ctx.mc("functions", f"{fn.name}.mcfunction")
        clear_file(fpath)
        # return flag 초기화(안전)
        write_line(fpath, f"data modify storage {ctx.storage_prefix} return set value 0")
        emit_block(ctx, fpath, fn.body)

        if fn.special=="_ready": load_vals.append(f"{ns}:{filebase}/functions/{fn.name}")
        if fn.special=="_tick":  tick_vals.append(f"{ns}:{filebase}/functions/{fn.name}")

    _write_qdispatcher(ctx)

    if load_vals:
        clear_file(tagpath(out_root, ns, "load.json"))
        with open(tagpath(out_root, ns, "load.json"), "w", encoding="utf-8") as f:
            json.dump({"values": load_vals}, f, ensure_ascii=False, indent=2)
    if tick_vals:
        clear_file(tagpath(out_root, ns, "tick.json"))
        with open(tagpath(out_root, ns, "tick.json"), "w", encoding="utf-8") as f:
            json.dump({"values": tick_vals}, f, ensure_ascii=False, indent=2)

def compile_folder(folder: str, ns: str, out_root: str):
    import os, glob

    global_defs: Dict[str, Any] = {}
    global_defs_raw: Dict[str, str] = {}

    units = []
    for path in glob.glob(os.path.join(folder, "**", "*.mcfn"), recursive=True):
        with open(path, "r", encoding="utf-8-sig") as f:
            src = f.read()
        toks = lex(src)
        p = Parser(toks, base_dir=folder)
        funcs, defs_inline, defs_raw = p.parse_all()

        global_defs.update(defs_inline)
        global_defs_raw.update(defs_raw)

        filebase = os.path.splitext(os.path.relpath(path, folder))[0].replace("\\", "/")
        units.append((filebase, funcs))

    for filebase, funcs in units:
        ctx = CGCtx(ns=ns, out_root=out_root, filebase=filebase)
        ctx.defs_inline = global_defs
        ctx.defs_raw = global_defs_raw                   # ★ 원문 전달
        ctx.storage_prefix = f"mcfn_{ns}:{filebase}"

        for f in funcs:
            ctx.current_func = f.name
            out_path = ctx.mc("functions", f"{f.name}.mcfunction")
            clear_file(out_path)
            emit_block(ctx, out_path, f.body)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("folder", help=".mcfn 파일이 들어있는 폴더")
    ap.add_argument("--ns", default="namespace", help="데이터팩 네임스페이스 (data/<ns>/...)")
    ap.add_argument("--out", required=True, help="출력 루트 (예: <월드>/datapacks/<팩>/data)")
    args=ap.parse_args()

    if not os.path.isdir(args.folder):
        print("Error: 폴더 경로를 지정하세요."); sys.exit(1)

    compile_folder(args.folder, args.ns, args.out)

if __name__=="__main__":
    main()