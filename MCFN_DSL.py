#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCFN-DSL -> Minecraft .mcfunction Transpiler
- 파일이 이미 있으면 '수정(덮어쓰기)' 방식으로 동작
- Python 3.9+ 권장
"""
from __future__ import annotations
import os, re, sys, json
from dataclasses import dataclass, field
from typing import List, Dict, Tuple

# ---------------------- Lexer ----------------------
TOKEN_SPEC = [
    ("WS",       r"[ \t]+"),
    ("COMMENT",  r"//[^\n]*"),
    ("NEWLINE",  r"\n"),
    ("FUNC",     r"\bfunc\b"),
    ("OBJ",      r"\bobj\b"),
    ("VAR",      r"\bvar\b"),
    ("CONST",    r"\bconst\b"),
    ("IF",       r"\bif\b"),
    ("WHILE",    r"\bwhile\b"),
    ("RUN",      r"\brun\b"),
    ("CALL",     r"\bcall\b"),
    ("SHOW",     r"\bshow\b"),
    ("TITLE",    r"\btitle\b"),
    ("LE",       r"<="),
    ("EQ",       r"=="),
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
    ("VSTRING",  r'v"([^"\\]|\\.)*"'),
    ("STRING",   r'"([^"\\]|\\.)*"'),
    ("NUMBER",   r"\d+"),
    ("IDENT",    r"[A-Za-z_][A-Za-z0-9_]*"),
]
TOKEN_RE = re.compile("|".join(f"(?P<{n}>{p})" for n,p in TOKEN_SPEC))

@dataclass
class Tok:
    kind: str
    val: str
    pos: int
    line: int
    col: int

def lex(src: str) -> List[Tok]:
    toks = []
    line, col = 1, 1
    i = 0
    while i < len(src):
        m = TOKEN_RE.match(src, i)
        if not m:
            raise SyntaxError(f"Lex error at line {line}, col {col}: {src[i:i+20]!r}")
        kind = m.lastgroup
        val = m.group()
        if kind == "NEWLINE":
            line += 1; col = 1
        elif kind not in ("WS", "COMMENT"):
            toks.append(Tok(kind, val, i, line, col))
        i = m.end()
        if kind != "NEWLINE":
            col += (i - m.start())
    toks.append(Tok("EOF","",i,line,col))
    return toks

# ---------------------- AST ----------------------
@dataclass
class ScoreRef:
    obj: str
    name: str

@dataclass 

class Stmt: ...
@dataclass 
class S_Obj(Stmt):   
    pairs: List[Tuple[str,str]]

@dataclass 
class S_Var(Stmt):   
    inits: List[ScoreRef]

@dataclass 
class S_Add(Stmt):   
    ref: ScoreRef; 
    amount: int

@dataclass 
class S_Const(Stmt): 
    name: str; 
    value: int

@dataclass 
class S_Run(Stmt):   
    text: str; 
    is_v: bool

@dataclass 
class S_Show(Stmt):  
    text: str

@dataclass 
class S_Title(Stmt): 
    mode: str; 
    text: str

@dataclass 
class S_If(Stmt):    
    ref: ScoreRef
    op: str
    num: int
    body: List[Stmt]

@dataclass 
class S_While(Stmt): 
    ref: ScoreRef; 
    body: List[Stmt]

@dataclass 
class S_Call(Stmt):  
    target: str; 
    args: Dict[str,ScoreRef]

@dataclass
class Func:
    name: str
    params: List[str]
    body: List[Stmt]

# ---------------------- Parser ----------------------
class Parser:
    def __init__(self, toks: List[Tok]):
        self.toks = toks; self.i=0

    def cur(self) -> Tok: return self.toks[self.i]
    def eat(self, kind: str) -> Tok:
        t = self.cur()
        if t.kind != kind: raise SyntaxError(f"Expected {kind} at line {t.line}, col {t.col}, got {t.kind}")
        self.i += 1; return t
    def match(self, kind:str) -> bool:
        if self.cur().kind==kind:
            self.i+=1; return True
        return False

    def parse(self) -> List[Func]:
        funcs=[]
        while self.cur().kind!="EOF":
            funcs.append(self.parse_func())
        return funcs

    def parse_func(self) -> Func:
        self.eat("FUNC")
        name = self.eat("IDENT").val
        self.eat("LPAREN")
        params=[]
        if self.cur().kind=="IDENT":
            params.append(self.eat("IDENT").val)
            while self.match("COMMA"):
                params.append(self.eat("IDENT").val)
        self.eat("RPAREN")
        self.eat("LBRACE")
        body=self.parse_stmts()
        self.eat("RBRACE")
        return Func(name, params, body)

    def parse_stmts(self) -> List[Stmt]:
        out=[]
        while self.cur().kind not in ("RBRACE","EOF"):
            k=self.cur().kind
            if k=="OBJ": out.append(self.parse_obj())
            elif k=="VAR": out.append(self.parse_var())
            elif k=="CONST": out.append(self.parse_const())
            elif k=="IF": out.append(self.parse_if())
            elif k=="WHILE": out.append(self.parse_while())
            elif k=="RUN": out.append(self.parse_run())
            elif k=="SHOW": out.append(self.parse_show())
            elif k=="TITLE": out.append(self.parse_title())
            elif k=="CALL": out.append(self.parse_call())
            elif k=="IDENT": out.append(self.parse_add())
            else:
                raise SyntaxError(f"Unexpected token {self.cur().kind} at line {self.cur().line}")
        return out

    def parse_obj(self)->S_Obj:
        self.eat("OBJ")
        pairs=[]
        while True:
            obj = self.eat("IDENT").val
            self.eat("LPAREN"); crit = self.eat("IDENT").val; self.eat("RPAREN")
            pairs.append((obj,crit))
            if not self.match("COMMA"): break
        self.eat("SEMI")
        return S_Obj(pairs)

    def parse_var(self)->S_Var:
        self.eat("VAR")
        inits=[]
        while True:
            obj = self.eat("IDENT").val
            self.eat("COLON")
            name = self.eat("IDENT").val
            inits.append(ScoreRef(obj,name))
            if not self.match("COMMA"): break
        self.eat("SEMI")
        return S_Var(inits)

    def parse_add(self)->S_Add:
        obj = self.eat("IDENT").val
        self.eat("COLON")
        name = self.eat("IDENT").val

        if   self.match("PLUSEQ"):
            sign = 1
        elif self.match("MINUSEQ"):
            sign = -1
        else:
            raise SyntaxError("Expected += or -=")

        num = int(self.eat("NUMBER").val)
        self.eat("SEMI")
        return S_Add(ScoreRef(obj, name), sign * num)

    def parse_const(self)->S_Const:
        self.eat("CONST")
        name = self.eat("IDENT").val
        self.eat("ASSIGN")
        num = int(self.eat("NUMBER").val)
        self.eat("SEMI")
        return S_Const(name, num)

    def parse_comp(self)->Tuple[ScoreRef,str,int]:
        obj = self.eat("IDENT").val
        self.eat("COLON")
        name = self.eat("IDENT").val
        if   self.match("EQ"): op="=="
        elif self.match("LE"): op="<="
        else: raise SyntaxError("Expected == or <=")
        num = int(self.eat("NUMBER").val)
        return ScoreRef(obj,name), op, num

    def parse_if(self)->S_If:
        self.eat("IF"); self.eat("LPAREN")
        ref, op, num = self.parse_comp()
        self.eat("RPAREN")
        self.eat("LBRACE")
        body=self.parse_stmts()
        self.eat("RBRACE")
        return S_If(ref, op, num, body)

    def parse_while(self)->S_While:
        self.eat("WHILE"); self.eat("LPAREN")
        obj = self.eat("IDENT").val
        self.eat("COLON")
        name = self.eat("IDENT").val
        self.eat("RPAREN")
        self.eat("LBRACE")
        body=self.parse_stmts()
        self.eat("RBRACE")
        return S_While(ScoreRef(obj,name), body)

    def _parse_string_like(self)->Tuple[str,bool]:
        t=self.cur()
        if t.kind=="VSTRING":
            self.i+=1
            return t.val[2:-1], True  # drop prefix v"
        elif t.kind=="STRING":
            self.i+=1
            return t.val[1:-1], False
        else:
            raise SyntaxError(f"Expected string at line {t.line}")

    def parse_run(self)->S_Run:
        self.eat("RUN"); self.eat("LPAREN")
        text,is_v=self._parse_string_like()
        self.eat("RPAREN"); self.eat("SEMI")
        return S_Run(text,is_v)

    def parse_show(self)->S_Show:
        self.eat("SHOW"); self.eat("LPAREN")
        text,is_v=self._parse_string_like()
        if not is_v: raise SyntaxError("show(...) must use v\"...\" for interpolation")
        self.eat("RPAREN"); self.eat("SEMI")
        return S_Show(text)

    def parse_title(self)->S_Title:
        self.eat("TITLE"); self.eat("LPAREN")
        mode = self.eat("IDENT").val   # expecting "title"
        self.eat("COMMA")
        text,is_v=self._parse_string_like()
        if is_v: raise SyntaxError("title(...) does not support v-strings")
        self.eat("RPAREN"); self.eat("SEMI")
        return S_Title(mode, text)

    def parse_call(self)->S_Call:
        self.eat("CALL")
        target = self.eat("IDENT").val
        self.eat("LPAREN")
        args={}
        if self.cur().kind!="RPAREN":
            while True:
                p = self.eat("IDENT").val
                self.eat("ASSIGN")
                obj = self.eat("IDENT").val
                self.eat("COLON")
                name = self.eat("IDENT").val
                args[p]=ScoreRef(obj,name)
                if not self.match("COMMA"): break
        self.eat("RPAREN")
        self.match("COMMA")  # optional stray comma
        self.eat("SEMI")
        return S_Call(target,args)
# ---------------------- Codegen ----------------------
@dataclass
class Ctx:
    namespace: str
    outdir: str
    if_counter: int = 0
    while_counter: int = 0
    funcs: Dict[str,Func] = field(default_factory=dict)
    param_bind: Dict[str,ScoreRef] = field(default_factory=dict)
    current_func: str = ""

def ensure_dir(p:str): os.makedirs(os.path.dirname(p), exist_ok=True)
def clear_file(path:str): ensure_dir(path); open(path,"w",encoding="utf-8").close()
def emit_line(path:str, s:str): 
    ensure_dir(path)
    with open(path,"a",encoding="utf-8") as f: f.write(s.rstrip()+"\n")

def matches_expr(op:str, num:int)->str:
    return f"{num}..{num}" if op=="==" else f"..{num}"

def interpolate_json(text: str, binding: Dict[str, ScoreRef]) -> List[dict]:
    """
    v-문자열 치환 규칙:
      - [ParamName]           -> binding에 있으면 그 ScoreRef로 치환
      - [objective:player]    -> {"score":{"name":player, "objective":objective}}
      - 위 두 경우에 모두 해당하지 않으면 원문 그대로 출력
    """
    parts = []
    i = 0
    pattern = re.compile(r"\[([A-Za-z_][A-Za-z0-9_]*)(?::([A-Za-z_][A-Za-z0-9_]*))?\]")
    for m in pattern.finditer(text):
        if m.start() > i:
            parts.append({"text": text[i:m.start()]})
        key1 = m.group(1)
        key2 = m.group(2)
        if key2 is not None:
            # [objective:player] 직접 지정
            parts.append({"score": {"name": key2, "objective": key1}})
        elif key1 in binding:
            # [ParamName] -> 호출자 바인딩 사용
            ref = binding[key1]
            parts.append({"score": {"name": ref.name, "objective": ref.obj}})
        else:
            # 매칭 실패: 원문 유지
            parts.append({"text": m.group(0)})
        i = m.end()
    if i < len(text):
        parts.append({"text": text[i:]})
    return parts or [{"text": ""}]

def emit_block(ctx:Ctx, path:str, body:List[Stmt]):
    for s in body:
        if isinstance(s,S_Obj):
            for obj,crit in s.pairs:
                emit_line(path, f"scoreboard objectives add {obj} {crit}")
        elif isinstance(s,S_Var):
            for r in s.inits:
                emit_line(path, f"scoreboard players set {r.name} {r.obj} 0")
        elif isinstance(s, S_Add):
            if s.amount >= 0:
                emit_line(path, f"scoreboard players add {s.ref.name} {s.ref.obj} {s.amount}")
            else:
                emit_line(path, f"scoreboard players remove {s.ref.name} {s.ref.obj} {abs(s.amount)}")
        elif isinstance(s,S_Const):
            tag=f"MCFN_{ctx.current_func}"
            emit_line(path, f"execute unless entity @e[type=minecraft:marker,tag={tag},limit=1] run summon minecraft:marker ~ ~ ~ {{Tags:[\"{tag}\"]}}")
            emit_line(path, f"data merge entity @e[type=minecraft:marker,tag={tag},limit=1] {{MCFN:{{{s.name}:{s.value}}}}}")
        elif isinstance(s,S_Run):
            if s.is_v:
                if s.text.strip().startswith("say "):
                    msg=s.text.strip()[4:]
                    comps=interpolate_json(msg,ctx.param_bind)
                    emit_line(path, f"tellraw @a {json.dumps(comps,ensure_ascii=False)}")
                else:
                    comps=interpolate_json(s.text,ctx.param_bind)
                    emit_line(path, f"tellraw @a {json.dumps(comps,ensure_ascii=False)}")
            else:
                emit_line(path, s.text)
        elif isinstance(s,S_Show):
            comps=interpolate_json(s.text,ctx.param_bind)
            emit_line(path, f"tellraw @a {json.dumps(comps,ensure_ascii=False)}")
        elif isinstance(s,S_Title):
            if "\n" in s.text: raise ValueError("title(...) 문자열에 개행(\\n) 불가")
            emit_line(path, f'title @a title {json.dumps({"text":s.text},ensure_ascii=False)}')
        elif isinstance(s,S_If):
            ctx.if_counter+=1; fname=f"ifs/if_{ctx.if_counter}.mcfunction"
            subpath=mc_path(ctx,fname); clear_file(subpath)   # 덮어쓰기
            emit_block(ctx, subpath, s.body)
            cond=matches_expr(s.op,s.num)
            emit_line(path, f"execute if score {s.ref.name} {s.ref.obj} matches {cond} run function {ctx.namespace}:{fname[:-11]}")
        elif isinstance(s,S_While):
            ctx.while_counter+=1; fname=f"whiles/while_{ctx.while_counter}.mcfunction"
            subpath=mc_path(ctx,fname); clear_file(subpath)   # 덮어쓰기
            emit_block(ctx, subpath, s.body)
            emit_line(subpath, f"execute if score {s.ref.name} {s.ref.obj} matches 1.. run function {ctx.namespace}:{fname[:-11]}")
            emit_line(path, f"function {ctx.namespace}:{fname[:-11]}")
        elif isinstance(s,S_Call):
            emit_line(path, f"function {ctx.namespace}:{s.target}")
        else: raise RuntimeError("Unhandled stmt")

def mc_path(ctx:Ctx,*parts:str)->str: return os.path.join(ctx.outdir,ctx.namespace,*parts)

def compile_funcs(funcs:List[Func], namespace="namespace", outdir="out"):
    ctx=Ctx(namespace,outdir)
    for f in funcs: ctx.funcs[f.name]=f
    for f in funcs:
        ctx.current_func=f.name
        path=mc_path(ctx,f"{f.name}.mcfunction")
        clear_file(path)   # 덮어쓰기
        ctx.param_bind={}
        emit_block(ctx,path,f.body)

def transpile(src:str,namespace="namespace",outdir="out"):
    toks=lex(src); funcs=Parser(toks).parse()
    compile_funcs(funcs,namespace,outdir)

# ---------------------- CLI ----------------------
def main():
    if len(sys.argv) < 2:
        print("Usage: python mcfndsl.py <input.mcfn> [--ns <namespace>] [--out <outdir>]")
        return
    inp = sys.argv[1]

    # 확장자 검사
    if not inp.endswith(".mcfn"):
        print("Error: 확장자는 .mcfn 이어야 합니다.")
        return

    ns = "namespace"
    out = "out"
    if "--ns" in sys.argv:
        ns = sys.argv[sys.argv.index("--ns")+1]
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out")+1]

    with open(inp,"r",encoding="utf-8") as f:
        src=f.read()
    transpile(src, ns, out)
    print(f"DONE! : wrote .mcfunction files under {out}/{ns}/")

if __name__=="__main__": main()
