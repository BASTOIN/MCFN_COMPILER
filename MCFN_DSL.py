#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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

MIT License (c) 2025 프로젝트-MCFN22

Version 0.2.1 (BETA)
"""
from __future__ import annotations
import os, re, sys, json
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional, Union, Set

# ==========================
# Lexer
# ==========================
TOKEN_SPEC = [
    ("WS",       r"[ \t]+"),
    ("COMMENT",  r"//[^\n]*"),
    ("NEWLINE",  r"\r?\n"),
    ("FUNC",     r"\bfunc\b"),
    ("OBJ",      r"\bobj\b"),
    ("VAR",      r"\bvar\b"),
    ("CONST",    r"\bconst\b"),
    ("IF",       r"\bif\b"),
    ("WHILE",    r"\bwhile\b"),
    ("RUN",      r"\brun\b"),
    ("RUNS",     r"\bruns\b"),     # ★ NEW
    ("CALL",     r"\bcall\b"),
    ("SHOW",     r"\bshow\b"),
    ("TITLE",    r"\btitle\b"),
    ("RAND",     r"\brand\b"),
    # ops
    ("LE", r"<="), ("GE", r">="), ("EQ", r"=="), ("NE", r"!="),
    ("LT", r"<"),  ("GT", r">"),
    ("PLUSEQ", r"\+="), ("MINUSEQ", r"-="),
    ("PLUS", r"\+"), ("MINUS", r"-"),
    # delims
    ("LPAREN", r"\("), ("RPAREN", r"\)"),
    ("LBRACE", r"\{"), ("RBRACE", r"\}"),
    ("LBRACK", r"\["), ("RBRACK", r"\]"),
    ("COLON", r":"), ("COMMA", r","), ("SEMI", r";"),
    ("ASSIGN", r"="),
    # strings
    ("VSTRING", r'v"([^"\\]|\\.)*"'),
    ("STRING",  r'"([^"\\]|\\.)*"'),
    # ids & numbers
    ("NUMBER",  r"\d+"),
    ("IDENT",   r"[A-Za-z_][A-Za-z0-9_]*"),
]
TOKEN_RE = re.compile("|".join(f"(?P<{n}>{p})" for n,p in TOKEN_SPEC))

@dataclass
class Tok:
    kind: str; val: str; pos: int; line: int; col: int

def lex(src: str) -> List[Tok]:
    toks: List[Tok] = []
    line=1; col=1; i=0
    while i < len(src):
        m = TOKEN_RE.match(src, i)
        if not m: raise SyntaxError(f"Lex error at line {line}, col {col}: {src[i:i+30]!r}")
        kind = m.lastgroup; val = m.group()
        if kind=="NEWLINE":
            line += 1; col = 1
        elif kind not in ("WS","COMMENT"):
            toks.append(Tok(kind,val,i,line,col))
        i = m.end()
        if kind!="NEWLINE":
            col += (i - m.start())
    toks.append(Tok("EOF","",i,line,col))
    return toks

# ==========================
# AST
# ==========================
@dataclass
class ScoreRef: obj: str; name: str
@dataclass
class ScoreRef:
    obj: str
    name: str

@dataclass
class Stmt:
    pass

@dataclass
class S_Obj(Stmt):
    pairs: List[Tuple[str, str]]  # (objective, criterion)

@dataclass
class S_Var(Stmt):
    inits: List[ScoreRef]

@dataclass
class S_Add(Stmt):  # += (positive or negative value); negative -> remove
    ref: ScoreRef
    amount: int

@dataclass
class S_Set(Stmt):  # = number
    ref: ScoreRef
    value: int

@dataclass
class S_SetCopy(Stmt):  # dst = src
    target: ScoreRef
    src: ScoreRef

@dataclass
class S_SetBinOp(Stmt):  # dst = left (+|-) right
    target: ScoreRef
    left: ScoreRef
    op: str  # '+' or '-'
    right: ScoreRef

@dataclass
class S_Const(Stmt):  # const name = number | "string"
    name: str
    value: Union[int, str]

@dataclass
class S_Run(Stmt):  # run("...") | run(v"...")
    text: str
    is_v: bool

@dataclass
class S_Runs(Stmt):  # runs{ ... }  (원문 그대로 라인 단위 추출)
    lines: List[str]

@dataclass
class S_Show(Stmt):  # show(v"...")
    text: str

@dataclass
class S_Title(Stmt):  # title(title, "...")
    mode: str
    text: str

@dataclass
class S_If(Stmt):  # if(scoreRef op (number|scoreRef)) { body }
    ref: ScoreRef
    op: str  # '==','!=','<','<=','>','>='
    rhs_num: Optional[int] = None
    rhs_ref: Optional[ScoreRef] = None
    body: List[Stmt] = field(default_factory=list)

@dataclass
class S_While(Stmt):
    ref: ScoreRef  # while(scoreRef) { ... } — loop while score >= 1
    body: List[Stmt]

@dataclass
class S_Rand(Stmt):  # rand(scoreRef[, min, max])
    ref: ScoreRef
    min_val: Optional[int] = None
    max_val: Optional[int] = None

@dataclass
class S_Call(Stmt):
    target: str
    args: Dict[str, ScoreRef]  # currently unused in codegen (runtime call only)

@dataclass
class Func:
    name: str
    params: List[str]
    body: List[Stmt]

# ==========================
# Parser
# ==========================
class Parser:
    def __init__(self, toks: List[Tok], src: str):
        self.toks=toks; self.i=0; self.src=src
    def cur(self)->Tok: return self.toks[self.i]
    def eat(self,k)->Tok:
        t=self.cur()
        if t.kind!=k: raise SyntaxError(f"Expected {k} at line {t.line}, col {t.col}, got {t.kind}")
        self.i+=1; return t
    def match(self,k)->bool:
        if self.cur().kind==k: self.i+=1; return True
        return False

    def eat_stmt_end(self):
        if self.match("SEMI"):
            while self.cur().kind=="NEWLINE": self.i+=1
            return
        if self.cur().kind in ("NEWLINE","RBRACE","EOF"):
            while self.cur().kind=="NEWLINE": self.i+=1
            return
        t=self.cur(); raise SyntaxError(f"Expected ';' or newline at line {t.line}, col {t.col}, got {t.kind}")

    def parse_int(self)->int:
        sign=-1 if self.match("MINUS") else 1
        n=int(self.eat("NUMBER").val); return sign*n
    def parse_score_ref(self)->ScoreRef:
        obj=self.eat("IDENT").val; self.eat("COLON"); name=self.eat("IDENT").val
        return ScoreRef(obj,name)

    def parse(self)->List[Func]:
        funcs=[]
        while self.cur().kind!="EOF":
            while self.cur().kind=="NEWLINE": self.i+=1
            if self.cur().kind=="EOF": break
            funcs.append(self.parse_func())
        return funcs

    def parse_func(self)->Func:
        self.eat("FUNC"); name=self.eat("IDENT").val
        self.eat("LPAREN"); params=[]
        if self.cur().kind=="IDENT":
            params.append(self.eat("IDENT").val)
            while self.match("COMMA"): params.append(self.eat("IDENT").val)
        self.eat("RPAREN")
        self.eat("LBRACE"); body=self.parse_stmts(); self.eat("RBRACE")
        return Func(name,params,body)

    def parse_stmts(self)->List[Stmt]:
        out=[]
        while self.cur().kind=="NEWLINE": self.i+=1
        while self.cur().kind not in ("RBRACE","EOF"):
            while self.cur().kind=="NEWLINE": self.i+=1
            k=self.cur().kind
            if   k=="OBJ":   out.append(self.parse_obj())
            elif k=="VAR":   out.append(self.parse_var())
            elif k=="CONST": out.append(self.parse_const())
            elif k=="IF":    out.append(self.parse_if())
            elif k=="WHILE": out.append(self.parse_while())
            elif k=="RUN":   out.append(self.parse_run())
            elif k=="RUNS":  out.append(self.parse_runs())   # ★ NEW
            elif k=="SHOW":  out.append(self.parse_show())
            elif k=="TITLE": out.append(self.parse_title())
            elif k=="RAND":  out.append(self.parse_rand())
            elif k=="CALL":  out.append(self.parse_call())
            elif k=="IDENT": out.append(self.parse_score_stmt())
            else:
                t=self.cur(); raise SyntaxError(f"Unexpected token {t.kind} at line {t.line}")
            while self.cur().kind=="NEWLINE": self.i+=1
        return out

    def parse_obj(self)->S_Obj:
        self.eat("OBJ"); pairs=[]
        while True:
            obj=self.eat("IDENT").val
            if self.match("LPAREN"): crit=self.eat("IDENT").val; self.eat("RPAREN")
            else: crit="dummy"
            pairs.append((obj,crit))
            if not self.match("COMMA"): break
        self.eat_stmt_end(); return S_Obj(pairs)

    def parse_var(self)->S_Var:
        self.eat("VAR"); inits=[]
        while True:
            inits.append(self.parse_score_ref())
            if not self.match("COMMA"): break
        self.eat_stmt_end(); return S_Var(inits)

    def _parse_string_like(self)->Tuple[str,bool]:
        t=self.cur()
        if t.kind=="VSTRING": self.i+=1; return t.val[2:-1], True
        if t.kind=="STRING":  self.i+=1; return t.val[1:-1], False
        raise SyntaxError(f"Expected string at line {t.line}")

    def parse_const(self)->S_Const:
        self.eat("CONST"); name=self.eat("IDENT").val; self.eat("ASSIGN")
        t=self.cur()
        if t.kind=="STRING":
            s,is_v=self._parse_string_like()
            if is_v: raise SyntaxError("const는 v\"...\" 불가")
            self.eat_stmt_end(); return S_Const(name,s)
        elif t.kind in ("NUMBER","MINUS"):
            n=self.parse_int(); self.eat_stmt_end(); return S_Const(name,n)
        else: raise SyntaxError("const 값은 숫자 또는 \"문자열\"")

    def parse_score_stmt(self)->Stmt:
        lhs=self.parse_score_ref()
        if self.match("PLUSEQ"):
            n=self.parse_int(); self.eat_stmt_end(); return S_Add(lhs,+n)
        if self.match("MINUSEQ"):
            n=self.parse_int(); self.eat_stmt_end(); return S_Add(lhs,-n)
        self.eat("ASSIGN")
        if self.cur().kind in ("NUMBER","MINUS"):
            n=self.parse_int(); self.eat_stmt_end(); return S_Set(lhs,n)
        r1=self.parse_score_ref()
        if self.match("PLUS"):
            r2=self.parse_score_ref(); self.eat_stmt_end(); return S_SetBinOp(lhs,r1,"+",r2)
        if self.match("MINUS"):
            r2=self.parse_score_ref(); self.eat_stmt_end(); return S_SetBinOp(lhs,r1,"-",r2)
        self.eat_stmt_end(); return S_SetCopy(lhs,r1)

    def parse_rand(self)->S_Rand:
        self.eat("RAND"); self.eat("LPAREN")
        ref=self.parse_score_ref(); mn=mx=None
        if self.match("COMMA"):
            mn=self.parse_int(); self.eat("COMMA"); mx=self.parse_int()
        self.eat("RPAREN"); self.eat_stmt_end()
        return S_Rand(ref,mn,mx)

    def parse_comp(self):
        lhs=self.parse_score_ref()
        if   self.match("EQ"): op="=="
        elif self.match("LE"): op="<="
        elif self.match("GE"): op=">="
        elif self.match("LT"): op="<"
        elif self.match("GT"): op=">"
        elif self.match("NE"): op="!="
        else: raise SyntaxError("Expected one of ==,<=,>=,<,>,!=")
        if self.cur().kind in ("NUMBER","MINUS"):
            n=self.parse_int(); return lhs,op,n,None
        rhs=self.parse_score_ref(); return lhs,op,None,rhs

    def parse_optional_slot(self)->Optional[str]:
        if self.match("LBRACK"):
            slot=self.eat("IDENT").val; self.eat("RBRACK"); return slot
        return None

    def parse_if(self)->S_If:
        self.eat("IF"); self.eat("LPAREN")
        ref,op,num,rf=self.parse_comp()
        self.eat("RPAREN")
        slot=self.parse_optional_slot()
        self.eat("LBRACE"); body=self.parse_stmts(); self.eat("RBRACE")
        return S_If(ref,op,num,rf,body,queue_slot=slot)

    def parse_while(self)->S_While:
        self.eat("WHILE"); self.eat("LPAREN")
        ref=self.parse_score_ref(); self.eat("RPAREN")
        slot=self.parse_optional_slot()
        self.eat("LBRACE"); body=self.parse_stmts(); self.eat("RBRACE")
        return S_While(ref,body,queue_slot=slot)

    def parse_run(self)->S_Run:
        self.eat("RUN"); self.eat("LPAREN")
        txt,is_v=self._parse_string_like()
        self.eat("RPAREN"); self.eat_stmt_end(); return S_Run(txt,is_v)

    def parse_runs(self)->S_Runs:
        # runs{ ... } : 원문 그대로 라인 단위 추출 (주석/빈 줄은 제거)
        self.eat("RUNS")
        l = self.eat("LBRACE")
        depth = 1
        j = self.i
        while j < len(self.toks):
            tk = self.toks[j]
            if tk.kind == "LBRACE": depth += 1
            elif tk.kind == "RBRACE":
                depth -= 1
                if depth == 0: break
            j += 1
        if depth != 0:
            raise SyntaxError(f"runs{{...}} not closed at line {l.line}")
        r = self.toks[j]  # RBRACE
        # 원문 추출: '{' 바로 뒤 ~ '}' 바로 앞
        raw = self.src[l.pos+1 : r.pos]
        lines = []
        for ln in raw.splitlines():
            # 한 줄 주석 제거(// ...), 단순 처리
            p = ln.find("//")
            if p != -1:
                ln = ln[:p]
            ln = ln.strip()
            if ln:
                lines.append(ln)
        # 토큰 포인터 이동: RBRACE 소비
        self.i = j+1
        # 문장 경계: runs는 블록형이라 세미콜론 없음
        return S_Runs(lines)

    def parse_show(self)->S_Show:
        self.eat("SHOW"); self.eat("LPAREN")
        txt,is_v=self._parse_string_like()
        if not is_v: raise SyntaxError("show는 v\"...\"만 허용")
        self.eat("RPAREN"); self.eat_stmt_end(); return S_Show(txt)

    def parse_title(self)->S_Title:
        self.eat("TITLE"); self.eat("LPAREN")
        mode=self.eat("IDENT").val; self.eat("COMMA")
        txt,is_v=self._parse_string_like()
        if is_v: raise SyntaxError("title은 v-string 불가")
        self.eat("RPAREN"); self.eat_stmt_end(); return S_Title(mode,txt)

    def parse_call(self)->S_Call:
        self.eat("CALL"); tgt=self.eat("IDENT").val
        self.eat("LPAREN"); args={}
        if self.cur().kind!="RPAREN":
            while True:
                p=self.eat("IDENT").val; self.eat("ASSIGN")
                args[p]=self.parse_score_ref()
                if not self.match("COMMA"): break
        self.eat("RPAREN")
        slot=self.parse_optional_slot()
        self.match("COMMA"); self.eat_stmt_end()
        return S_Call(tgt,args,queue_slot=slot)

# ==========================
# Codegen
# ==========================
INT_MIN, INT_MAX = -2147483648, 2147483647

@dataclass
class Ctx:
    namespace: str; outdir: str
    if_counter:int=0; while_counter:int=0
    funcs: Dict[str,Func]=field(default_factory=dict)
    current_func: str=""
    queue_entries: List[Tuple[str,str]] = field(default_factory=list)  # (slot, func_wo_ext)
    used_queue: bool=False
    called_queue_main_in_func: bool=False
    all_slots: Set[str] = field(default_factory=set)

def ensure_dir(p:str): os.makedirs(os.path.dirname(p), exist_ok=True)
def clear_file(path:str): ensure_dir(path); open(path,"w",encoding="utf-8").close()
def emit_line(path:str,s:str):
    ensure_dir(path)
    with open(path,"a",encoding="utf-8") as f: f.write(s.rstrip()+"\n")
def mc_path(ctx:Ctx,*parts:str)->str: return os.path.join(ctx.outdir,ctx.namespace,*parts)

def matches_expr(op:str, num:int)->Optional[str]:
    if op=="==": return f"{num}..{num}"
    if op=="<=": return f"..{num}"
    if op==">=": return f"{num}.."
    if op=="<":
        if num<=INT_MIN: return None
        return f"..{num-1}"
    if op==">":
        if num>=INT_MAX: return None
        return f"{num+1}.."
    raise ValueError(op)

JSON_TELLRAW_RE = re.compile(r'^\s*tellraw\s+@a\s+(\{.*\})\s*$', re.DOTALL)
SCORE_TOKEN_RE   = re.compile(r"\[([A-Za-z_][A-Za-z0-9_]*):([A-Za-z_][A-Za-z0-9_]*)\]")

def _interpolate_plain(s:str)->List[dict]:
    parts=[]; i=0
    for m in SCORE_TOKEN_RE.finditer(s):
        if m.start()>i: parts.append({"text": s[i:m.start()]})
        parts.append({"score":{"name":m.group(2),"objective":m.group(1)}})
        i=m.end()
    if i<len(s): parts.append({"text": s[i:]})
    return parts or [{"text":""}]

def interpolate_json(text:str)->List[dict]:
    m = JSON_TELLRAW_RE.match(text)
    if m:
        try:
            obj=json.loads(m.group(1))
            if isinstance(obj,dict) and "text" in obj and isinstance(obj["text"],str):
                return _interpolate_plain(obj["text"])
        except Exception:
            pass
    return _interpolate_plain(text)

def emit_stmt(ctx:Ctx, out_path:str, s:Stmt):
    """한 문장 출력 (세그먼트 전환/트리거는 emit_block_segmented에서 처리)"""
    if   isinstance(s,S_Obj):
        for obj,crit in s.pairs: emit_line(out_path, f"scoreboard objectives add {obj} {crit}")

    elif isinstance(s,S_Var):
        for r in s.inits: emit_line(out_path, f"scoreboard players set {r.name} {r.obj} 0")

    elif isinstance(s,S_Add):
        if s.amount>=0: emit_line(out_path, f"scoreboard players add {s.ref.name} {s.ref.obj} {s.amount}")
        else:           emit_line(out_path, f"scoreboard players remove {s.ref.name} {s.ref.obj} {abs(s.amount)}")

    elif isinstance(s,S_Set):
        emit_line(out_path, f"scoreboard players set {s.ref.name} {s.ref.obj} {s.value}")

    elif isinstance(s,S_SetCopy):
        emit_line(out_path, f"scoreboard players operation {s.target.name} {s.target.obj} = {s.src.name} {s.src.obj}")

    elif isinstance(s,S_SetBinOp):
        emit_line(out_path, f"scoreboard players operation {s.target.name} {s.target.obj} = {s.left.name} {s.left.obj}")
        op = "+=" if s.op=="+" else "-="
        emit_line(out_path, f"scoreboard players operation {s.target.name} {s.target.obj} {op} {s.right.name} {s.right.obj}")

    elif isinstance(s,S_Const):
        tag=f"MCFN_{ctx.current_func}"
        emit_line(out_path, f"execute unless entity @e[type=minecraft:marker,tag={tag},limit=1] run summon minecraft:marker ~ ~ ~ {{Tags:[\"{tag}\"]}}")
        vtxt = str(s.value) if isinstance(s.value,int) else json.dumps(s.value,ensure_ascii=False)
        emit_line(out_path, f"data merge entity @e[type=minecraft:marker,tag={tag},limit=1] {{MCFN:{{{s.name}:{vtxt}}}}}")

    elif isinstance(s,S_Run):
        if s.is_v:
            comps = interpolate_json(s.text.strip())
            emit_line(out_path, f"tellraw @a {json.dumps(comps,ensure_ascii=False)}")
        else:
            emit_line(out_path, s.text)

    elif isinstance(s,S_Runs):
        for line in s.lines:
            emit_line(out_path, line)

    elif isinstance(s,S_Show):
        comps = _interpolate_plain(s.text)
        emit_line(out_path, f"tellraw @a {json.dumps(comps,ensure_ascii=False)}")

    elif isinstance(s,S_Title):
        if "\n" in s.text: raise ValueError("title(...) 문자열 개행 불가")
        emit_line(out_path, f"title @a title {json.dumps({'text':s.text},ensure_ascii=False)}")

    elif isinstance(s,S_Rand):
        lo = 0 if s.min_val is None else s.min_val
        hi = 100 if s.max_val is None else s.max_val
        emit_line(out_path, f"scoreboard players random {s.ref.name} {s.ref.obj} {lo} {hi}")

    elif isinstance(s,S_If):
        ctx.if_counter += 1
        fname=f"ifs/if_{ctx.if_counter}.mcfunction"
        sub = mc_path(ctx, fname); clear_file(sub)
        emit_block_segmented(ctx, sub, s.body)
        # condition jump
        if s.rhs_ref is not None:
            if s.op=="!=":
                emit_line(out_path, f"execute unless score {s.ref.name} {s.ref.obj} = {s.rhs_ref.name} {s.rhs_ref.obj} run function {ctx.namespace}:{fname[:-11]}")
            else:
                mcop={"==":"=","<":"<","<=":"<=",">":">",">=":">="}[s.op]
                emit_line(out_path, f"execute if score {s.ref.name} {s.ref.obj} {mcop} {s.rhs_ref.name} {s.rhs_ref.obj} run function {ctx.namespace}:{fname[:-11]}")
        else:
            if s.op=="!=":
                rng=f"{s.rhs_num}..{s.rhs_num}"
                emit_line(out_path, f"execute unless score {s.ref.name} {s.ref.obj} matches {rng} run function {ctx.namespace}:{fname[:-11]}")
            else:
                rng = matches_expr(s.op, s.rhs_num)
                if rng is not None:
                    emit_line(out_path, f"execute if score {s.ref.name} {s.ref.obj} matches {rng} run function {ctx.namespace}:{fname[:-11]}")

    elif isinstance(s,S_While):
        ctx.while_counter += 1
        fname=f"whiles/while_{ctx.while_counter}.mcfunction"
        sub = mc_path(ctx, fname); clear_file(sub)
        emit_block_segmented(ctx, sub, s.body)
        emit_line(sub, f"execute if score {s.ref.name} {s.ref.obj} matches 1.. run function {ctx.namespace}:{fname[:-11]}")
        emit_line(out_path, f"function {ctx.namespace}:{fname[:-11]}")

    elif isinstance(s,S_Call):
        if s.queue_slot:
            # call[Q]: callee 실행 → wait 스케줄 → Q 오픈은 wait가 담당
            emit_line(out_path, f"function {ctx.namespace}:{s.target}")
            qidx = len(ctx.queue_entries) + 1
            wait_name = f"queue/wait_{ctx.current_func}_{qidx}.mcfunction"
            wait_path = mc_path(ctx, wait_name)
            clear_file(wait_path)
            emit_line(wait_path, "scoreboard objectives add mcfq dummy")
            emit_line(wait_path, f"function {ctx.namespace}:queue/queue_main")
            emit_line(wait_path, f"function {ctx.namespace}:queue/any_open")
            emit_line(wait_path, f"execute if score __open mcfq matches 1 run schedule function {ctx.namespace}:{wait_name[:-11]} 1t")
            slot = s.queue_slot
            emit_line(wait_path, f"execute unless score __open mcfq matches 1 run scoreboard players set {slot} mcfq 0")
            emit_line(wait_path, f"execute unless score __open mcfq matches 1 run function {ctx.namespace}:queue/queue_main")
            emit_line(out_path, f"schedule function {ctx.namespace}:{wait_name[:-11]} 1t")
            ctx.called_queue_main_in_func = True
        else:
            emit_line(out_path, f"function {ctx.namespace}:{s.target}")

    else:
        raise RuntimeError("Unhandled stmt in emit_stmt")

def emit_block_segmented(ctx:Ctx, main_path:str, body:List[Stmt]):
    """
    세그먼트 전환:
    - [slot] 문장 이후부터 새 큐파일로 전환
    - Call/If/While 모두 wait 방식: 여기서 '다음 세그먼트 파일 생성/등록' + 'wait 스케줄'까지 처리
    """
    out_path = main_path
    for s in body:
        emit_stmt(ctx, out_path, s)

        slot = None
        if isinstance(s, (S_If, S_While, S_Call)):
            slot = s.queue_slot
        if not slot:
            continue

        # 다음 세그먼트 파일 준비
        qidx = len(ctx.queue_entries) + 1
        qname = f"queue/{ctx.current_func}_queue{qidx}.mcfunction"
        qpath = mc_path(ctx, qname)
        clear_file(qpath)
        emit_line(qpath, "scoreboard objectives add mcfq dummy")
        emit_line(qpath, f"scoreboard players set {slot} mcfq 1")  # 소비

        # 등록
        ctx.queue_entries.append((slot, qname[:-11]))
        ctx.all_slots.add(slot)

        if isinstance(s, S_Call):
            # call[Q]: 트리거는 emit_stmt의 wait가 담당
            out_path = qpath
            continue

        # if/while[Q]: 여기서 wait 함수 생성+스케줄
        ctx.used_queue = True
        wait_name = f"queue/wait_{ctx.current_func}_{qidx}.mcfunction"
        wait_path = mc_path(ctx, wait_name)
        clear_file(wait_path)
        emit_line(wait_path, "scoreboard objectives add mcfq dummy")
        emit_line(wait_path, f"function {ctx.namespace}:queue/queue_main")
        emit_line(wait_path, f"function {ctx.namespace}:queue/any_open")
        emit_line(wait_path, f"execute if score __open mcfq matches 1 run schedule function {ctx.namespace}:{wait_name[:-11]} 1t")
        emit_line(wait_path, f"execute unless score __open mcfq matches 1 run scoreboard players set {slot} mcfq 0")
        emit_line(wait_path, f"execute unless score __open mcfq matches 1 run function {ctx.namespace}:queue/queue_main")
        emit_line(out_path, f"schedule function {ctx.namespace}:{wait_name[:-11]} 1t")
        ctx.called_queue_main_in_func = True

        out_path = qpath

def has_queue_slot(stmt) -> bool:
    if isinstance(stmt, (S_If, S_While, S_Call)) and stmt.queue_slot:
        return True
    if isinstance(stmt, S_If):
        return any(has_queue_slot(s) for s in stmt.body)
    if isinstance(stmt, S_While):
        return any(has_queue_slot(s) for s in stmt.body)
    return False

def func_needs_queue(body: List[Stmt]) -> bool:
    return any(has_queue_slot(s) for s in body)

def write_queue_main(ctx:Ctx):
    if not ctx.queue_entries: return
    # dispatcher
    qmain = mc_path(ctx, "queue/queue_main.mcfunction")
    clear_file(qmain)
    emit_line(qmain, "scoreboard objectives add mcfq dummy")
    for slot, qfunc in ctx.queue_entries:
        emit_line(qmain, f"execute if score {slot} mcfq matches 0 run function {ctx.namespace}:{qfunc}")
    # any_open: 열린 슬롯 존재 여부 계산
    anyopen = mc_path(ctx, "queue/any_open.mcfunction")
    clear_file(anyopen)
    emit_line(anyopen, "scoreboard objectives add mcfq dummy")
    emit_line(anyopen, "scoreboard players set __open mcfq 0")
    for s in sorted(ctx.all_slots):
        emit_line(anyopen, f"execute if score {s} mcfq matches 0 run scoreboard players set __open mcfq 1")

def compile_funcs(funcs:List[Func], namespace="namespace", outdir="out"):
    ctx = Ctx(namespace,outdir)
    for f in funcs: ctx.funcs[f.name]=f
    ctx.queue_entries.clear(); ctx.all_slots.clear()
    for f in funcs:
        ctx.current_func = f.name
        ctx.called_queue_main_in_func = False
        mpath = mc_path(ctx, f"{f.name}.mcfunction")
        clear_file(mpath)
        # 이 함수가 큐를 쓰면 선행 선언
        if func_needs_queue(f.body):
            emit_line(mpath, "scoreboard objectives add mcfq dummy")
        # 본문 출력
        emit_block_segmented(ctx, mpath, f.body)
        # inline 호출이 한 번도 없었으면 말미에 한 번 호출(안전용)
        if ctx.queue_entries and not ctx.called_queue_main_in_func:
            emit_line(mpath, f"function {ctx.namespace}:queue/queue_main")
    write_queue_main(ctx)

def transpile(src:str, namespace="namespace", outdir="out"):
    toks=lex(src); funcs=Parser(toks, src).parse()
    compile_funcs(funcs, namespace, outdir)

# ==========================
# CLI
# ==========================
HELP = """Usage:
  python mcfndsl.py <input.mcfn> [--ns <namespace>] [--out <outdir>]
"""

def main():
    if len(sys.argv)<2 or sys.argv[1] in ("-h","--help"): print(HELP); return
    inp=sys.argv[1]
    if not inp.endswith(".mcfn"):
        print("Error: 확장자는 .mcfn 이어야 합니다."); sys.exit(1)
    ns="namespace"; out="out"
    if "--ns" in sys.argv:  ns  = sys.argv[sys.argv.index("--ns")+1]
    if "--out" in sys.argv: out = sys.argv[sys.argv.index("--out")+1]
    try:
        with open(inp,"r",encoding="utf-8") as f: src=f.read()
        transpile(src, ns, out)
        print(f"OK: wrote .mcfunction files under {out}/{ns}/")
    except Exception as e:
        msg=str(e)
        m=re.search(r'line (\d+), col (\d+)', msg)
        if m: print(f"{inp}:{m.group(1)}:{m.group(2)}: error: {msg}")
        else:
            m2=re.search(r'Lex error at line (\d+), col (\d+)', msg)
            if m2: print(f"{inp}:{m2.group(1)}:{m2.group(2)}: error: {msg}")
            else:  print(f"{inp}:0:0: error: {msg}")
        sys.exit(1)

if __name__=="__main__": main()